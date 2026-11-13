"""Compose verified immutable evidence; never reconstruct raw or run extractors.

Only the Store's existing SQLite publication journal is written. Historical
jobs, scopes, permissions, capabilities, limitations and producer refs are not
new executions/authorizations. Inherited files stay where they were published;
only the new manifest, Plan, block index and validation report are written.
Admission limits cover the coordinator, including get_bundle's JSON/text reads,
not just a downstream media worker. Oversized/ambiguous evidence is rejected.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sqlite3

from .contracts import (ContractError, VALIDATOR_VERSION, asset_key, canonical,
                        check_id, digest, hash_file, now, reject_links, safe_path,
                        validate_bundle, validate_evidence, validate_plan, validate_relative_path)
from .store import _write, new_id

_MAX_BUNDLES = 32
_MAX_SOURCE_JSON = 16 * 1024**2
_MAX_MANIFEST_BYTES = 8 * 1024**2
_MAX_PLAN_BYTES = 2 * 1024**2
_MAX_FILE_BYTES = 64 * 1024**2
_MAX_REFERENCED_BYTES = 256 * 1024**2
_MAX_ASSETS = 128
_MAX_BLOCKS = 2048
_MAX_FILES = 4096
_MAX_NODES = 150000
_MAX_DEPTH = 48
_POLICY = "published-evidence-reuse-v1"
_ENVELOPE = re.compile(r"^证据/v[0-9]{4,}/(?:plan\.json|blocks\.jsonl|validation\.json)$")


@contextmanager
def _fail_closed():
    try:
        yield
    except ContractError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError,
            OverflowError, sqlite3.DatabaseError) as exc:
        raise ContractError("invalid bundle composition evidence/interface") from exc


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate composition JSON key")
        result[key] = value
    return result


def _decode(data):
    return json.loads(data, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))


class _Budget:
    def __init__(self):
        self.json_bytes = self.manifest_bytes = self.nodes = 0
        self.files = {}
        self.counts = [0] * 5

    def json(self, size):
        if type(size) is not int or size < 1:
            raise ContractError("missing composition JSON")
        self.json_bytes += size
        if self.json_bytes > _MAX_SOURCE_JSON:
            raise ContractError("composition cumulative JSON byte bound exceeded")

    def tree(self, value):
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            self.nodes += 1
            if self.nodes + len(pending) > _MAX_NODES or depth > _MAX_DEPTH:
                raise ContractError("composition JSON complexity bound exceeded")
            if type(item) is dict:
                if self.nodes + len(pending) + len(item) * 2 > _MAX_NODES:
                    raise ContractError("composition JSON complexity bound exceeded")
                pending.extend((v, depth + 1) for pair in item.items() for v in pair)
            elif type(item) is list:
                if self.nodes + len(pending) + len(item) > _MAX_NODES:
                    raise ContractError("composition JSON complexity bound exceeded")
                pending.extend((v, depth + 1) for v in item)

    def file(self, root, relative, expected):
        validate_relative_path(relative)
        if (type(expected) is not dict or set(expected) != {"sha256", "byte_length"}
                or not re.fullmatch(r"[a-f0-9]{64}", str(expected.get("sha256", "")))
                or type(expected.get("byte_length")) is not int
                or not 0 <= expected["byte_length"] <= _MAX_FILE_BYTES):
            raise ContractError("composition referenced file byte bound/identity invalid")
        folded = relative.casefold()
        if folded in self.files:
            if self.files[folded] != (relative, expected):
                raise ContractError("conflicting composition file identity")
        else:
            self.files[folded] = (relative, expected)
            if (len(self.files) > _MAX_FILES
                    or sum(e[1]["byte_length"] for e in self.files.values()) > _MAX_REFERENCED_BYTES):
                raise ContractError("composition referenced file count/byte bound exceeded")
        path = safe_path(root, relative)
        if path.stat().st_size != expected["byte_length"]:
            raise ContractError("composition file size mismatch")
        return path


def _walk_bounded(base):
    """Bound directories too, before Store's rglob verification."""
    reject_links(base)
    if not base.is_dir():
        raise ContractError("composition publication directory missing")
    pending, count = [(base, 0)], 0
    while pending:
        directory, depth = pending.pop()
        if depth > _MAX_DEPTH:
            raise ContractError("composition directory depth bound exceeded")
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_FILES * 2:
                    raise ContractError("composition directory count bound exceeded")
                path = Path(entry.path)
                reject_links(path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append((path, depth + 1))
                elif not entry.is_file(follow_symlinks=False):
                    raise ContractError("composition requires regular evidence files")


def _metadata_bound(store, sql, publish_id, budget):
    for sizes in store.connection.execute(sql, (publish_id,)):
        if any(size is not None and not 0 <= size <= 4096 for size in sizes):
            raise ContractError("composition producer/source metadata byte bound exceeded")
        budget.json(max(1, sum(size or 0 for size in sizes)))


def _admit(store, project_id, ids, budget):
    """SQL lengths/counts precede fetching any large source JSON into Python."""
    root, rows = store.project_root(project_id), []
    for ident in ids:
        row = store.connection.execute("""SELECT publish_id,bundle_id,revision,
            final_relative_path,manifest_sha256,
            length(CAST(bundle_json AS BLOB)) AS manifest_bytes,
            length(CAST(expected_files_json AS BLOB)) AS expected_bytes
            FROM publications WHERE project_id=? AND bundle_id=? AND state='published'""",
            (project_id, ident)).fetchone()
        if not row:
            raise ContractError("composition requires same-project database-published bundles")
        row = dict(row)
        if row["final_relative_path"] != f"证据/v{row['revision']:04d}":
            raise ContractError("composition publication directory identity mismatch")
        check_id(row["publish_id"])
        budget.json(row["manifest_bytes"])
        budget.json(row["expected_bytes"])
        budget.manifest_bytes += row["manifest_bytes"]
        if budget.manifest_bytes > _MAX_MANIFEST_BYTES:
            raise ContractError("composition cumulative manifest byte bound exceeded")
        rows.append(row)
    for row in rows:
        counts = store.connection.execute("""SELECT
            json_array_length(bundle_json,'$.assets'),json_array_length(bundle_json,'$.blocks'),
            json_array_length(bundle_json,'$.artifacts'),json_array_length(bundle_json,'$.gaps'),
            json_array_length(bundle_json,'$.contacts') FROM publications WHERE publish_id=?""",
            (row["publish_id"],)).fetchone()
        for index, (count, maximum) in enumerate(zip(counts, (_MAX_ASSETS, _MAX_BLOCKS, _MAX_FILES, _MAX_BLOCKS, _MAX_BLOCKS))):
            if type(count) is not int or count < 0:
                raise ContractError("invalid composition source collections")
            budget.counts[index] += count
            if budget.counts[index] > maximum:
                raise ContractError("composition cumulative entity count bound exceeded")
        # get_bundle reads complete job/source ledger rows, not just the
        # manifest. Bound those strings before it can materialize them too.
        job_fields = ("project_id", "idempotency_key", "operation", "provider_json",
                      "input_revision", "state", "request_id", "remote_task_id",
                      "reason", "receipt_json", "created_at", "updated_at")
        _metadata_bound(store, "SELECT DISTINCT " + ",".join("length(CAST(j." + field + " AS BLOB))" for field in job_fields)
            + " FROM publications p JOIN json_each(p.bundle_json,'$.artifacts') a JOIN jobs j ON j.job_id=json_extract(a.value,'$.job_id') WHERE p.publish_id=?", row["publish_id"], budget)
        _metadata_bound(store, """SELECT length(CAST(s.local_path AS BLOB)),length(CAST(s.kind AS BLOB)),
            length(CAST(r.sha256 AS BLOB)),length(CAST(r.media_type AS BLOB)),
            length(CAST(r.original_relative_path AS BLOB)),length(CAST(r.created_at AS BLOB))
            FROM publications p JOIN json_each(p.bundle_json,'$.assets') a
            JOIN sources s ON s.source_id=json_extract(a.value,'$.source_ref')
            JOIN asset_revisions r ON r.asset_id=json_extract(a.value,'$.asset_id')
                AND r.asset_revision=json_extract(a.value,'$.asset_revision') WHERE p.publish_id=?""", row["publish_id"], budget)
        _metadata_bound(store, """SELECT length(CAST(m.identity_json AS BLOB)),
            length(CAST(m.descriptor_relative_path AS BLOB)),length(CAST(m.descriptor_sha256 AS BLOB)),
            length(CAST(m.source_id AS BLOB)),length(CAST(m.source_revision AS BLOB)),length(CAST(m.observed_at AS BLOB))
            FROM publications p JOIN json_each(p.bundle_json,'$.assets') a
            JOIN media_sources m ON m.asset_id=json_extract(a.value,'$.asset_id')
                AND m.asset_revision=json_extract(a.value,'$.asset_revision') WHERE p.publish_id=?""", row["publish_id"], budget)
        encoded = store.connection.execute("SELECT expected_files_json FROM publications WHERE publish_id=?",
                                           (row["publish_id"],)).fetchone()[0]
        expected = _decode(encoded)
        budget.tree(expected)
        if type(expected) is not dict or len(expected) > _MAX_FILES:
            raise ContractError("composition expected file count bound exceeded")
        for relative, evidence in expected.items():
            budget.file(root, relative, evidence)
        manifest = row["final_relative_path"] + "/manifest.json"
        if (manifest not in expected or expected[manifest]["sha256"] != row["manifest_sha256"]
                or expected[manifest]["byte_length"] != row["manifest_bytes"]):
            raise ContractError("composition manifest/journal identity mismatch")
        plan_rel = row["final_relative_path"] + "/plan.json"
        if plan_rel not in expected or expected[plan_rel]["byte_length"] > _MAX_PLAN_BYTES:
            raise ContractError("composition source Plan byte bound exceeded")
        budget.json(expected[plan_rel]["byte_length"])
        _walk_bounded(root / row["final_relative_path"])
        row["expected"] = expected
    return rows


def _provider(store, provider):
    keys = (provider["provider_id"], provider["implementation_version"], provider["config_version"])
    row = store.connection.execute("""SELECT length(CAST(provider_json AS BLOB))
        FROM provider_configurations WHERE provider_id=? AND implementation_version=? AND config_version=?""", keys).fetchone()
    if not row or not 0 < row[0] <= 4096:
        raise ContractError("composition producer configuration not registered/bounded")
    registered = store.connection.execute("""SELECT provider_json FROM provider_configurations
        WHERE provider_id=? AND implementation_version=? AND config_version=?""", keys).fetchone()[0]
    if _decode(registered) != provider:
        raise ContractError("composition producer configuration mismatch")


def _published_receipt(store, project_id, asset, job):
    receipt = _decode(job["receipt_json"] or "{}")
    publish_id = receipt.get("publish_id") if type(receipt) is dict else None
    check_id(publish_id)
    row = store.connection.execute("""SELECT length(CAST(bundle_json AS BLOB)) FROM publications
        WHERE publish_id=? AND project_id=? AND state='published'""", (publish_id, project_id)).fetchone()
    if not row or not row[0] or row[0] > _MAX_MANIFEST_BYTES:
        raise ContractError("composition producer receipt publication scope/state/bound mismatch")
    bound = store.connection.execute("""SELECT 1 FROM publications p JOIN json_each(p.bundle_json,'$.artifacts') a
        WHERE p.publish_id=? AND json_extract(a.value,'$.job_id')=?
            AND json_extract(a.value,'$.asset_ref.asset_id')=?
            AND json_extract(a.value,'$.asset_ref.asset_revision')=?
            AND json_extract(a.value,'$.provider')=? LIMIT 1""",
        (publish_id, job["job_id"], *asset_key(asset), canonical(_decode(job["provider_json"])).decode())).fetchone()
    if not bound:
        raise ContractError("composition producer receipt lacks matching asset/job/provider evidence")


def _decision_refs(plan):
    for observation in plan["capability_observations"]:
        yield from observation["evidence_refs"]
    for asset in plan["asset_plans"]:
        yield from asset["format_evidence_refs"]
        for job in asset["jobs"]:
            yield from job["basis_refs"]
            yield from job["estimate"]["basis_refs"]


def _source(store, project_id, row, budget):
    bundle = store.get_bundle(project_id, row["bundle_id"])
    budget.tree(bundle)
    root = store.project_root(project_id)
    plan_bytes = safe_path(root, row["final_relative_path"] + "/plan.json").read_bytes()
    plan = _decode(plan_bytes)
    budget.tree(plan)
    if (plan["project_id"] != project_id or plan["program_validation"]["status"] != "passed"
            or plan["program_validation"]["report_relative_path"] != row["final_relative_path"] + "/validation.json"):
        raise ContractError("composition sealed Plan scope/validation mismatch")
    sealed = store.connection.execute("SELECT project_id,length(CAST(plan_json AS BLOB)) FROM plans WHERE plan_id=?",
                                      (plan["plan_id"],)).fetchone()
    if not sealed or tuple(sealed) != (project_id, len(plan_bytes)):
        raise ContractError("composition source Plan ledger mismatch")
    if store.connection.execute("SELECT plan_json FROM plans WHERE plan_id=?", (plan["plan_id"],)).fetchone()[0].encode("utf-8") != plan_bytes:
        raise ContractError("composition source Plan differs from sealed ledger")
    registered = set()
    for observation in plan["capability_observations"]:
        _provider(store, observation["provider"])
        registered.add((observation["provider"]["provider_id"], observation["provider"]["implementation_version"]))
    validate_plan(plan, registered_providers=registered)
    for evidence in _decision_refs(plan):
        if "source_evidence" in evidence:
            validate_evidence(evidence["source_evidence"], bundle)
    observations = {o["observation_id"]: o for o in plan["capability_observations"]}
    permissions = {p["check_id"]: p for p in plan["permission_checks"]}
    plans = {asset_key(p["asset_ref"]): p for p in plan["asset_plans"]}
    if set(plans) != {asset_key(a) for a in bundle["assets"]}:
        raise ContractError("composition Plan asset scope mismatch")
    for asset in bundle["assets"]:
        binding = asset["source_binding"]
        source = store.connection.execute("SELECT kind,local_path FROM sources WHERE source_id=? AND project_id=?",
                                          (asset["source_ref"], project_id)).fetchone()
        if (binding.get("kind") != "local" or binding.get("local_input_ref") != asset["source_ref"]
                or not source or source["kind"] not in {"local", "local_media_stat"} or not source["local_path"]):
            raise ContractError("composition local source binding scope mismatch")
        # Authorization scope only: never stat/read/hash the original media.
        path = Path(source["local_path"])
        reject_links(path)
        if not path.is_absolute() or not path.resolve(strict=False).is_relative_to(store.root.resolve(strict=True)):
            raise ContractError("composition original source outside authorized workspace")
        ap = plans[asset_key(asset)]
        own = [a for a in bundle["artifacts"] if asset_key(a["asset_ref"]) == asset_key(asset)]
        if source["kind"] == "local_media_stat":
            registered = store.connection.execute("SELECT media_type FROM asset_revisions WHERE asset_id=? AND asset_revision=?", asset_key(asset)).fetchone()
            if not registered or registered[0] != asset["media_type"]:
                raise ContractError("composition registered media type mismatch")
            actual_type = registered[0]  # Never the inventory descriptor's JSON type.
        else:
            originals = [a for a in own if a["kind"] == "original_small_file"]
            if len(originals) != 1:
                raise ContractError("composition native original format evidence missing")
            actual_type = originals[0]["media_type"]  # Magic-probed bytes, not filename guess.
        if ap["source_ref"] != asset["source_ref"] or ap["media_type"] != actual_type:
            raise ContractError("composition Plan source/media scope mismatch")
        jobs = {j["job_id"]: j for j in ap["jobs"]}
        if set(jobs) != {a["job_id"] for a in own}:
            raise ContractError("composition Plan/producer job set mismatch")
        for job in jobs.values():
            _provider(store, job["provider"])
            execution = store._check_extraction_job(project_id, asset, job["job_id"], job["provider"], published=True)
            _published_receipt(store, project_id, asset, execution)
            if job["operation"] != "native_extract":
                raise ContractError("composition only reuses published native/derived extraction evidence")
            for ident in job["capability_observation_ids"]:
                scope = observations[ident]["scope"]
                if scope != {"source_ref": asset["source_ref"], "asset_ref": ap["asset_ref"]}:
                    raise ContractError("composition capability scope mismatch")
            for ident in job["permission_check_ids"]:
                permission = permissions[ident]
                if permission["scope_ref"] != asset["asset_id"] or permission["state"] != "granted":
                    raise ContractError("composition permission scope/state mismatch")
        for artifact in own:
            if jobs[artifact["job_id"]]["provider"] != artifact["provider"]:
                raise ContractError("composition artifact provider/job mismatch")
        for block in bundle["blocks"]:
            if asset_key(block["asset_ref"]) == asset_key(asset):
                if block["processing"]["recovery_ref"] not in jobs:
                    raise ContractError("composition block recovery job mismatch")
                if "producer" in block:
                    _provider(store, block["producer"])
    return {**row, "bundle": bundle, "plan": plan, "plans": plans}


def _signature(source, asset):
    """Compare same-job evidence modulo republishing IDs/paths/timestamps."""
    bundle, key = source["bundle"], asset_key(asset)
    aliases = {}
    for artifact in bundle["artifacts"]:
        aliases[artifact["artifact_id"]] = "artifact-" + digest({k: v for k, v in artifact.items()
            if k not in {"artifact_id", "artifact_revision", "relative_path", "created_at"}})
    indexes = {}
    for block in bundle["blocks"]:
        ref = asset_key(block["asset_ref"])
        index = indexes.get(ref, 0)
        indexes[ref] = index + 1
        aliases[block["block_id"]] = "block-" + digest([ref, index])
    def normalize(value):
        if type(value) is str:
            return aliases.get(value, value)
        if type(value) is list:
            return [normalize(v) for v in value]
        if type(value) is dict:
            return {k: normalize(v) for k, v in value.items()
                    if k not in {"gap_id", "contact_id", "bundle_id", "bundle_revision"}}
        return value
    coverage = next(c for c in bundle["coverage"]["per_asset"] if asset_key(c["asset_ref"]) == key)
    gaps = [g for g in bundle["gaps"] if g["gap_id"] in coverage["gap_ids"]]
    own = [a for a in bundle["artifacts"] if asset_key(a["asset_ref"]) == key and not _ENVELOPE.fullmatch(a["relative_path"])]
    coverage = {k: v for k, v in coverage.items() if k != "gap_ids"}
    return digest([asset, sorted(aliases[a["artifact_id"]] for a in own),
        normalize([b for b in bundle["blocks"] if asset_key(b["asset_ref"]) == key]),
        normalize(coverage), normalize(gaps),
        normalize([c for c in bundle["contacts"] if c["value_artifact_id"] in {a["artifact_id"] for a in own}])])


def _union(items, key, label):
    result = {}
    for item in items:
        ident = key(item)
        if ident in result and result[ident] != item:
            raise ContractError("conflicting composition " + label)
        result[ident] = item
    return [deepcopy(result[k]) for k in sorted(result)]


def _select(sources):
    selected, signatures, executions = {}, {}, {}
    for source in sources:
        for asset in source["bundle"]["assets"]:
            ident, key = asset["asset_id"], asset_key(asset)
            jobs = source["plans"][key]["jobs"]
            execution = sorted([(j["job_id"], j["provider"]) for j in jobs], key=lambda j: j[0])
            signature = _signature(source, asset)
            if ident in selected:
                _, old_asset = selected[ident]
                if asset_key(old_asset) != key or executions[ident] != execution:
                    raise ContractError("conflicting asset revision/config/producer; explicitly select one source bundle")
                if signatures[ident] != signature:
                    raise ContractError("conflicting same-job asset evidence; explicitly select one source bundle")
            else:
                selected[ident] = (source, asset)
                signatures[ident], executions[ident] = signature, execution
    if not selected:
        raise ContractError("cannot compose empty fake evidence")
    return [selected[k] for k in sorted(selected)]


def _rebase(value, bundle_id, revision):
    """Only snapshot pins change; original IDs/locators survive in both copies."""
    if type(value) is list:
        return [_rebase(v, bundle_id, revision) for v in value]
    if type(value) is not dict:
        return value
    value = {k: _rebase(v, bundle_id, revision) for k, v in value.items()}
    if {"project_id", "asset_id", "asset_revision", "locator"} <= value.keys():
        if "bundle_id" in value:
            value["bundle_id"] = bundle_id
        if "bundle_revision" in value:
            value["bundle_revision"] = revision
    return value


def _build(store, project_id, sources, selected, publish_id, bundle_id, revision, final_rel, stage):
    created = now()
    def copied(value):
        return _rebase(value, bundle_id, f"v{revision:04d}")
    receipt = {"status": "passed", "validator_version": VALIDATOR_VERSION,
               "checked_at": created, "report_relative_path": final_rel + "/validation.json"}
    def own(field, ref_field):
        return [item for source, asset in selected for item in source["bundle"][field]
                if asset_key(item[ref_field]) == asset_key(asset)]
    artifacts = own("artifacts", "asset_ref")
    artifacts += [a for s in sources for a in s["bundle"]["artifacts"] if _ENVELOPE.fullmatch(a["relative_path"])]
    per_asset = [next(c for c in s["bundle"]["coverage"]["per_asset"] if asset_key(c["asset_ref"]) == asset_key(a)) for s, a in selected]
    gap_ids = {g for c in per_asset for g in c["gap_ids"]}
    gaps = _union([copied(g) for s in sources for g in s["bundle"]["gaps"] if g["gap_id"] in gap_ids], lambda g: g["gap_id"], "gap")
    artifact_ids = {a["artifact_id"] for a in artifacts}
    contacts = _union([copied(c) for s in sources for c in s["bundle"]["contacts"] if c["value_artifact_id"] in artifact_ids], lambda c: c["contact_id"], "contact")
    bundle = {"schema_version": "1.0-draft", "bundle_id": bundle_id, "project_id": project_id,
        "bundle_revision": f"v{revision:04d}", "source_catalog_revision": store._catalog_revision(project_id),
        "publish_id": publish_id, "created_at": created,
        "publication": {"state": "published", "journal_ref": publish_id,
                        "manifest_relative_path": final_rel + "/manifest.json", "published_at": created},
        "assets": [deepcopy(a) for _, a in selected],
        "artifacts": _union(artifacts, lambda a: a["artifact_id"], "artifact"),
        "blocks": copied(own("blocks", "asset_ref")), "gaps": gaps, "contacts": contacts,
        "program_validation": receipt}
    if len(bundle["artifacts"]) + 3 > _MAX_FILES:
        raise ContractError("composition generated artifact count bound exceeded")
    states = [c["enumeration_state"] for c in per_asset]
    inventory = "complete" if all(s == "complete" for s in states) else "unknown" if all(s == "unknown" for s in states) else "partial"
    claims = [s["bundle"]["coverage"]["claimed_status"] for s in sources]
    complete = (all(c == "verified_complete" for c in claims) and inventory == "complete"
                and all(c["processing_status"] == "verified_complete" for c in per_asset)
                and not any(g["status"] in {"open", "requested"} for g in gaps))
    bundle["coverage"] = {"scope": "listed_assets", "inventory_state": inventory,
        "claimed_status": "verified_complete" if complete else "unknown" if all(c == "unknown" for c in claims) else "partial",
        "discovered_counts": {"assets": len(selected), "blocks": len(bundle["blocks"]),
                              "visual_occurrences": sum(b["kind"] in {"image_occurrence", "chart"} for b in bundle["blocks"])},
        "per_asset": deepcopy(per_asset)}
    asset_plans = [copied(s["plans"][asset_key(a)]) for s, a in selected]
    observation_ids = {i for a in asset_plans for j in a["jobs"] for i in j["capability_observation_ids"]}
    permission_ids = {i for a in asset_plans for j in a["jobs"] for i in j["permission_check_ids"]}
    currencies = {s["plan"]["hard_budget"]["currency"] for s in sources}
    if len(currencies) != 1:
        raise ContractError("composition historical budget currency conflict")
    historical_budget = deepcopy(sources[0]["plan"]["hard_budget"])
    for key in historical_budget:
        if key.startswith("max_"):
            historical_budget[key] = max(s["plan"]["hard_budget"][key] for s in sources)
    historical_budget["policy_ref"] = _POLICY
    plan = {"schema_version": "1.0-draft", "plan_id": new_id("plan"), "project_id": project_id,
        "created_at": created, "policy_version": _POLICY,
        "provider_registry_revision": sources[0]["plan"]["provider_registry_revision"],
        "capability_observations": _union([copied(o) for s in sources for o in s["plan"]["capability_observations"] if o["observation_id"] in observation_ids], lambda o: o["observation_id"], "capability"),
        "permission_checks": _union([p for s in sources for p in s["plan"]["permission_checks"] if p["check_id"] in permission_ids], lambda p: p["check_id"], "permission"),
        "hard_budget": historical_budget, "asset_plans": asset_plans, "program_validation": receipt}
    # As in Store._build_bundle, envelope refs anchor the historical execution.
    # The report distinguishes new indexes from inherited extraction bytes.
    anchor = min(bundle["artifacts"], key=lambda a: (asset_key(a["asset_ref"]), a["artifact_id"]))
    report = {"validator_version": VALIDATOR_VERSION, "publish_id": publish_id,
        "checked_at": created, "status": "passed", "operation": "compose_published_evidence",
        "new_extraction_executed": False, "historical_plan_is_new_authorization": False,
        "evidence_version_policy": "Snapshot pins rebind to this snapshot; original IDs/locators and original refs in inherited sealed manifests/Plans remain unchanged.",
        "checks": ["schema", "files", "identity", "sets", "references", "geometry", "coverage", "secrets", "producer_scope", "composition_bounds"],
        "source_publications": [{"bundle_id": s["bundle_id"], "publish_id": s["publish_id"],
            "manifest_relative_path": s["final_relative_path"] + "/manifest.json", "manifest_sha256": s["manifest_sha256"]} for s in sources],
        "selected_assets": [{"asset_ref": {k: a[k] for k in ("asset_id", "asset_revision")}, "bundle_id": s["bundle_id"]} for s, a in selected],
        "coordinator_limits": {"source_json_bytes": _MAX_SOURCE_JSON, "manifest_bytes": _MAX_MANIFEST_BYTES,
            "blocks": _MAX_BLOCKS, "files": _MAX_FILES, "referenced_bytes": _MAX_REFERENCED_BYTES}}
    for filename, data, kind, media in (("plan.json", canonical(plan), "structured_data", "application/json"),
            ("blocks.jsonl", b"".join(canonical(b) + b"\n" for b in bundle["blocks"]), "structured_data", "application/x-ndjson"),
            ("validation.json", canonical(report), "validation_report", "application/json")):
        if len(data) > (_MAX_PLAN_BYTES if filename == "plan.json" else _MAX_MANIFEST_BYTES):
            raise ContractError("composition generated index byte bound exceeded")
        path = stage / filename
        _write(path, data)
        sha, size = hash_file(path)
        bundle["artifacts"].append({"artifact_id": new_id("artifact"), "artifact_revision": "sha256-" + sha,
            "asset_ref": deepcopy(anchor["asset_ref"]), "job_id": anchor["job_id"], "provider": deepcopy(anchor["provider"]),
            "kind": kind, "relative_path": final_rel + "/" + filename, "media_type": media,
            "byte_length": size, "sha256": sha, "created_at": created, "access_class": "project_local"})
    validate_plan(plan, registered_providers={(j["provider"]["provider_id"], j["provider"]["implementation_version"]) for a in asset_plans for j in a["jobs"]})
    def resolver(relative):
        return safe_path(stage, relative[len(final_rel) + 1:]) if relative.startswith(final_rel + "/") else safe_path(store.project_root(project_id), relative)
    validate_bundle(bundle, store.project_root(project_id), path_resolver=resolver)
    return bundle, plan


def compose(store, project_id, bundle_ids):
    """Return a new immutable snapshot of a bounded set of published Bundles.

    Reversed/duplicate IDs are idempotent. Same asset revision/config conflicts
    require callers to select one source; nothing is silently overwritten.
    Interrupted prepared publications use normal Store restart recovery;
    incomplete staging stays fail-closed for explicit reconciliation.
    """
    with _fail_closed():
        check_id(project_id)
        if isinstance(bundle_ids, (str, bytes, dict)):
            raise ContractError("composition requires opaque bundle IDs, not paths/bundles")
        ids = set()
        for count, ident in enumerate(bundle_ids, 1):
            if count > _MAX_BUNDLES:
                raise ContractError("composition source bundle count bound exceeded")
            ids.add(check_id(ident))
        if not ids:
            raise ContractError("composition requires at least one published bundle")
        ids = sorted(ids)
        if store.pipeline_state("collection") != "running":
            return {"status": "blocked", "reason": "collection paused", "project_id": project_id}
        budget = _Budget()
        with store.transaction():
            rows = _admit(store, project_id, ids, budget)
            sources = [_source(store, project_id, r, budget) for r in rows]
            selected = _select(sources)
            key = digest([_POLICY, project_id, [[s["bundle_id"], s["publish_id"], s["manifest_sha256"]] for s in sources]])
            previous = store.connection.execute("SELECT bundle_id,state,publish_id FROM publications WHERE project_id=? AND idempotency_key=?", (project_id, key)).fetchone()
            if previous:
                if previous["state"] != "published":
                    raise ContractError("composition already registered; recover/reconcile its publish_id")
                _admit(store, project_id, [previous["bundle_id"]], _Budget())
                return store.get_bundle(project_id, previous["bundle_id"])
            revision = store.connection.execute("SELECT COALESCE(MAX(revision),0)+1 FROM publications WHERE project_id=?", (project_id,)).fetchone()[0]
            publish_id, bundle_id = new_id("publish"), new_id("bundle")
            final_rel, staging_rel = f"证据/v{revision:04d}", "证据/.staging-" + publish_id
            store.connection.execute("""INSERT INTO publications(publish_id,project_id,bundle_id,revision,idempotency_key,
                state,staging_relative_path,final_relative_path,created_at) VALUES(?,?,?,?,?,'staging',?,?,?)""",
                (publish_id, project_id, bundle_id, revision, key, staging_rel, final_rel, now()))
            store.event("publish_registered", publish_id, project_id, {"operation": "compose_published_evidence", "source_bundle_ids": ids})
        root = store.project_root(project_id)
        stage = root / staging_rel
        reject_links(stage)
        stage.mkdir(parents=True, exist_ok=False)
        bundle, plan = _build(store, project_id, sources, selected, publish_id, bundle_id, revision, final_rel, stage)
        manifest_data = canonical(bundle)
        if len(manifest_data) > _MAX_MANIFEST_BYTES:
            raise ContractError("composition generated manifest byte bound exceeded")
        _write(stage / "manifest.json", manifest_data)
        # Hash inherited manifests on later get_bundle calls too.
        expected = {rel: evidence for s in sources for rel, evidence in s["expected"].items()
                    if rel.endswith("/manifest.json")}
        expected.update({a["relative_path"]: {"sha256": a["sha256"], "byte_length": a["byte_length"]} for a in bundle["artifacts"]})
        expected[final_rel + "/manifest.json"] = dict(zip(("sha256", "byte_length"), hash_file(stage / "manifest.json")))
        if len(expected) > _MAX_FILES:
            raise ContractError("composition generated file count bound exceeded")
        with store.transaction():
            for source in sources:
                current = store.connection.execute("SELECT state,manifest_sha256 FROM publications WHERE publish_id=? AND project_id=?", (source["publish_id"], project_id)).fetchone()
                if not current or tuple(current) != ("published", source["manifest_sha256"]) or hash_file(safe_path(root, source["final_relative_path"] + "/manifest.json"))[0] != source["manifest_sha256"]:
                    raise ContractError("composition source publication changed before seal")
            store.connection.execute("INSERT INTO plans VALUES(?,?,?,?)", (plan["plan_id"], project_id, canonical(plan).decode(), now()))
            store.connection.execute("UPDATE publications SET state='validated',manifest_sha256=?,bundle_json=?,expected_files_json=? WHERE publish_id=? AND state='staging'", (hash_file(stage / "manifest.json")[0], manifest_data.decode(), canonical(expected).decode(), publish_id))
        store._finish_publication(publish_id)
        return store.get_bundle(project_id, bundle_id)
