"""Conservative, bounded reuse of *accepted historical* same-agent analysis.

Only exact inherited block IDs/evidence are supported (not semantic matching or
new extraction). No physical text/image receipt is copied or manufactured. The
existing read_receipts table is the only persistence; task/result state is never
advanced here. Call prepare/reuse explicitly. Integration in Analysis._has_read,
after collecting/identity-checking receipts and before physical-read checking::

    if any(r["kind"] == "reused" for r in receipts):
        from .incremental_analysis import validate_reused
        if len(receipts) != 1:
            raise ContractError("historical reuse receipt must stand alone")
        return validate_reused(self, task, block, bundle, receipts[0])

The original _has_read implementation is still used to prove original physical
reads; multi-hop reuse is deliberately ineligible. Unknown usage remains null.
All verification is local; no provider, acquisition or cloud API is invoked.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

from . import bundle_merge as _published
from .contracts import (ContractError, asset_key, canonical, check_id, digest,
                        safe_path, validate_evidence)

_POLICY = "same-agent-accepted-physical-read-v1"
_MAX_JSON = 8 * 1024**2
_MAX_OUTPUTS = 16 * 1024**2
_MAX_RECEIPTS = 8192
_MAX_RECEIPT_BYTES = 16 * 1024**2
_MAX_ASSERTIONS = 2048
_MAX_ACCEPTED_FILES = 64
_MAX_NODES = 150000
_MAX_DEPTH = 48
_MAX_TEXT = 4 * 1024**2
_MAX_TEXT_TOTAL = 16 * 1024**2
_TERMINAL = {"completed", "partial", "blocked", "failed"}
_UNKNOWN_USAGE = {"status": "not_available", "input_tokens": None,
                  "output_tokens": None, "settled_cost_minor": None}


@contextmanager
def _fail_closed():
    try:
        yield
    except ContractError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError,
            OverflowError, sqlite3.DatabaseError) as exc:
        raise ContractError("invalid incremental analysis evidence/interface") from exc


def _loads(raw, maximum=_MAX_JSON):
    """Reject size/depth/token complexity before JSON object allocation."""
    if type(raw) is not str or not 0 < len(raw.encode("utf-8")) <= maximum:
        raise ContractError("incremental JSON byte bound exceeded")
    depth = tokens = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            tokens += 1
        elif char in "[{":
            depth += 1
            tokens += 1
        elif char in "]}":
            depth -= 1
        elif char in ",:":
            tokens += 1
        if depth > _MAX_DEPTH or tokens > _MAX_NODES or depth < 0:
            raise ContractError("incremental JSON complexity bound exceeded")
    value = _published._decode(raw)
    pending, nodes = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes + len(pending) > _MAX_NODES or depth > _MAX_DEPTH:
            raise ContractError("incremental JSON complexity bound exceeded")
        if type(item) is dict:
            pending.extend((v, depth + 1) for pair in item.items() for v in pair)
        elif type(item) is list:
            pending.extend((v, depth + 1) for v in item)
    return value


def _snapshot(value):
    """Only validated evidence snapshot pins may change during compose."""
    if type(value) is list:
        return [_snapshot(v) for v in value]
    if type(value) is dict:
        evidence = {"project_id", "asset_id", "asset_revision", "locator"} <= value.keys()
        return {k: _snapshot(v) for k, v in value.items()
                if not evidence or k not in {"bundle_id", "bundle_revision"}}
    return value


def _rebase(ref, bundle):
    ref = deepcopy(ref)
    if "bundle_id" in ref:
        ref["bundle_id"] = bundle["bundle_id"]
    if "bundle_revision" in ref:
        ref["bundle_revision"] = bundle["bundle_revision"]
    validate_evidence(ref, bundle, allow_source_only=False)
    return ref


def _unique(items, key):
    result = {}
    for item in items:
        ident = item[key]
        if ident in result:
            raise ContractError("duplicate incremental evidence identity")
        result[ident] = item
    return result


def _closure_fingerprints(direct, dependencies):
    """Iterative dependency hashes: no recursive walk or unbounded reuse chain."""
    remaining = {k: len(v) for k, v in dependencies.items()}
    children = {k: [] for k in direct}
    for ident, deps in dependencies.items():
        for dep in deps:
            if dep not in children:
                raise ContractError("missing incremental block dependency")
            children[dep].append(ident)
    ready, result = deque(k for k, n in remaining.items() if not n), {}
    while ready:
        ident = ready.popleft()
        result[ident] = digest([direct[ident], [[d, result[d]] for d in sorted(dependencies[ident])]])
        for child in children[ident]:
            remaining[child] -= 1
            if not remaining[child]:
                ready.append(child)
    if len(result) != len(direct):
        raise ContractError("cyclic incremental block dependency")
    return result


class _Run:
    def __init__(self, analysis):
        self.analysis, self.store = analysis, analysis.store
        self.conn = analysis.connection
        # Reuse compose's existing coordinator admission, including source/job
        # SQL lengths, entity/file/byte bounds, original hashes and producer scope.
        self.budget = _published._Budget()
        self.sources, self.texts, self.reads = {}, {}, {}
        self.text_bytes = 0

    def task(self, ident, *, running=False):
        check_id(ident)
        row = self.conn.execute("""SELECT length(CAST(agent_json AS BLOB)),
            length(CAST(output_relative_path AS BLOB)),length(CAST(reason AS BLOB))
            FROM analysis_tasks WHERE task_id=?""", (ident,)).fetchone()
        if not row or any(n is not None and not 0 <= n <= 4096 for n in row):
            raise ContractError("incremental task missing/metadata byte bound exceeded")
        task = self.analysis._task(ident, require_running=running)
        agent = _loads(task["agent_json"], 4096)
        if not isinstance(agent, dict):
            raise ContractError("incremental task agent is not explicit")
        return task

    def source(self, task):
        ident = task["publish_id"]
        if ident not in self.sources:
            row = self.conn.execute("""SELECT project_id,bundle_id,state,
                length(CAST(bundle_json AS BLOB)) AS manifest_bytes,
                length(CAST(expected_files_json AS BLOB)) AS expected_bytes
                FROM publications WHERE publish_id=?""", (ident,)).fetchone()
            if not row or row["project_id"] != task["project_id"] or row["state"] != "published":
                raise ContractError("incremental task publication project/version mismatch")
            if any(type(row[k]) is not int or not 0 < row[k] <= _MAX_JSON
                   for k in ("manifest_bytes", "expected_bytes")):
                raise ContractError("incremental publication JSON byte bound exceeded")
            # The compose admission decodes expected_files_json internally. Bound
            # its tree first too, before that existing verifier can allocate it.
            expected_raw = self.conn.execute("SELECT expected_files_json FROM publications WHERE publish_id=?", (ident,)).fetchone()[0]
            _loads(expected_raw)
            admitted = _published._admit(self.store, task["project_id"], [row["bundle_id"]], self.budget)[0]
            # get_bundle's full validators only run after bounded JSON complexity
            # and Plan admission, rather than allocating an adversarial tree first.
            raw = self.conn.execute("SELECT bundle_json FROM publications WHERE publish_id=?", (ident,)).fetchone()[0]
            manifest = _loads(raw)
            for field, limit in (("blocks", 2048), ("artifacts", 4096), ("assets", 128)):
                if not isinstance(manifest[field], list) or len(manifest[field]) > limit:
                    raise ContractError("incremental bundle entity count bound exceeded")
            root = self.store.project_root(task["project_id"])
            plan = safe_path(root, admitted["final_relative_path"] + "/plan.json")
            _loads(plan.read_bytes().decode("utf-8"), 2 * 1024**2)
            source = _published._source(self.store, task["project_id"], admitted, self.budget)
            source["blocks"] = _unique(source["bundle"]["blocks"], "block_id")
            source["artifacts"] = _unique(source["bundle"]["artifacts"], "artifact_id")
            self.sources[ident] = source
        source = self.sources[ident]
        if source["bundle"]["project_id"] != task["project_id"]:
            raise ContractError("incremental source project mismatch")
        return source

    def receipts(self, task):
        row = self.conn.execute("""SELECT COUNT(*),MAX(length(CAST(receipt_json AS BLOB))),
            COALESCE(SUM(length(CAST(receipt_json AS BLOB))+COALESCE(length(CAST(content AS BLOB)),0)),0)
            FROM read_receipts WHERE task_id=?""", (task["task_id"],)).fetchone()
        if row[0] > _MAX_RECEIPTS or (row[1] or 0) > _MAX_JSON or row[2] > _MAX_RECEIPT_BYTES:
            raise ContractError("incremental read receipt count/byte bound exceeded")

    def stored_receipt(self, task, block_id, ident, bundle):
        row = self.conn.execute("SELECT * FROM read_receipts WHERE receipt_id=?", (check_id(ident),)).fetchone()
        if not row or row["task_id"] != task["task_id"] or row["block_id"] != block_id:
            raise ContractError("incremental receipt task/block scope mismatch")
        parsed = _loads(row["receipt_json"])
        receipt = self.analysis.receipt(ident, _bundle=bundle)
        if receipt != {**parsed, "content": row["content"]}:
            raise ContractError("incremental receipt API identity mismatch")
        return receipt, digest({"receipt_json": row["receipt_json"], "content": row["content"]})

    def text(self, source, block):
        key = source["publish_id"], block["block_id"]
        if key not in self.texts:
            for ident in block["artifact_ids"]:
                artifact = source["artifacts"][ident]
                if (artifact["kind"] == "native_text" and Path(artifact["relative_path"]).name == block["block_id"] + ".txt"
                        and artifact["byte_length"] > _MAX_TEXT):
                    raise ContractError("incremental full text byte bound exceeded")
            text, hashes = self.analysis._text(block, source["bundle"])
            self.text_bytes += len(text.encode("utf-8"))
            if self.text_bytes > _MAX_TEXT_TOTAL:
                raise ContractError("incremental cumulative text byte bound exceeded")
            self.texts[key] = text, hashes
        return self.texts[key]

    def fingerprints(self, source):
        if "fingerprints" in source:
            return source["fingerprints"]
        bundle, artifacts = source["bundle"], source["artifacts"]
        assets = {asset_key(a): a for a in bundle["assets"]}
        direct, dependencies, unproven = {}, {}, set()
        # A long-media stat descriptor is not a content hash. Do not claim
        # strict source-byte identity from an unchanged descriptor/mtime alone.
        byte_proven_assets = {asset_key(a["asset_ref"]) for a in artifacts.values()
                              if a["kind"] == "original_small_file"}
        for ident, block in source["blocks"].items():
            refs = block["derived_from"] + block["processing"]["evidence_refs"]
            for ref in refs:
                validate_evidence(ref, bundle)
            dependencies[ident] = {r["block_id"] for r in refs if "block_id" in r}
            keys = {asset_key(block["asset_ref"])} | {asset_key(r) for r in refs}
            named = set(block["artifact_ids"]) | {r["artifact_id"] for r in refs if "artifact_id" in r}
            geometry = block["locator"].get("geometry")
            if geometry:
                named.add(geometry["reference_object_ref"])
                mapping = geometry["transform_to_original"].get("mapping_artifact_id")
                if mapping:
                    named.add(mapping)
            keys.update(asset_key(artifacts[a]["asset_ref"]) for a in named)
            if not keys <= byte_proven_assets:
                unproven.add(ident)
            # All source-asset artifacts, not just a text preview or image path.
            # New snapshot indexes are not new source evidence; explicitly cited
            # indexes are still compared and therefore conservatively invalidate.
            named.update(a["artifact_id"] for a in artifacts.values()
                         if asset_key(a["asset_ref"]) in keys and not _published._ENVELOPE.fullmatch(a["relative_path"]))
            text, _ = self.text(source, block)
            direct[ident] = digest([_snapshot(block),
                [assets[k] for k in sorted(keys)],
                [_snapshot(source["plans"][k]) for k in sorted(keys)],
                [artifacts[a] for a in sorted(named)],
                {"full_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                 "full_text_bytes": len(text.encode("utf-8"))}])
        source["fingerprints"] = _closure_fingerprints(direct, dependencies)
        children = {k: [] for k in direct}
        for child, deps in dependencies.items():
            for parent in deps:
                children[parent].append(child)
        queue = deque(unproven)
        while queue:
            for child in children[queue.popleft()]:
                if child not in unproven:
                    unproven.add(child)
                    queue.append(child)
        source["byte_proven_block_ids"] = direct.keys() - unproven
        return source["fingerprints"]

    def accepted(self, task):
        source = self.source(task)
        ident = task["task_id"]
        # Lengths/counts are SQL preflight, before SELECT * or get_result's JSON,
        # reports, receipt content, directory walk and validation provenance.
        row = self.conn.execute("""SELECT length(CAST(result_json AS BLOB)) FROM analyses WHERE task_id=?""", (ident,)).fetchone()
        if not row or not 0 < row[0] <= _MAX_JSON:
            raise ContractError("incremental source has no bounded accepted analysis")
        sizes = self.conn.execute("""SELECT state,length(CAST(result_json AS BLOB)),
            length(CAST(expected_files_json AS BLOB)),length(CAST(outputs_json AS BLOB))
            FROM analysis_publications WHERE task_id=?""", (ident,)).fetchone()
        if (not sizes or sizes[0] != "committed" or any(n is None or not 0 < n <= limit
                for n, limit in zip(sizes[1:], (_MAX_JSON, _MAX_JSON, _MAX_OUTPUTS)))):
            raise ContractError("incremental source must be accepted and journal-committed")
        counts = self.conn.execute("""SELECT json_array_length(result_json,'$.block_processing'),
            json_array_length(result_json,'$.assertions'),json_array_length(result_json,'$.reports'),
            json_array_length(result_json,'$.gaps') FROM analyses WHERE task_id=?""", (ident,)).fetchone()
        for count, maximum in zip(counts, (2048, _MAX_ASSERTIONS, _MAX_ACCEPTED_FILES, 2048)):
            if type(count) is not int or not 0 <= count <= maximum:
                raise ContractError("incremental accepted entity count bound exceeded")
        journal = self.conn.execute("SELECT * FROM analysis_publications WHERE task_id=?", (ident,)).fetchone()
        accepted = self.conn.execute("SELECT * FROM analyses WHERE task_id=?", (ident,)).fetchone()
        result = _loads(accepted["result_json"])
        _loads(journal["result_json"])
        expected = _loads(journal["expected_files_json"])
        outputs = _loads(journal["outputs_json"], _MAX_OUTPUTS)
        if type(expected) is not dict or type(outputs) is not dict or len(expected) > _MAX_ACCEPTED_FILES:
            raise ContractError("incremental accepted file count bound exceeded")
        final = task["output_relative_path"] + "/accepted"
        if (journal["project_id"] != task["project_id"] or journal["publish_id"] != task["publish_id"]
                or journal["final_relative_path"] != final
                or journal["staging_relative_path"] != task["output_relative_path"] + "/.accepted-staging-" + ident
                or journal["result_relative_path"] != final + "/analysis.json"
                or expected.keys() != outputs.keys()
                or any(not relative.startswith(final + "/") for relative in expected)
                or result["program_validation"]["report_relative_path"] not in expected
                or any(r["relative_path"] not in expected for r in result["reports"])):
            raise ContractError("incremental accepted journal file/path scope mismatch")
        root = self.store.project_root(task["project_id"])
        for relative, item in expected.items():
            self.budget.file(root, relative, item)
        _published._walk_bounded(root / final)
        events = self.conn.execute("""SELECT COUNT(*),MAX(length(CAST(payload_json AS BLOB)))
            FROM events WHERE kind='analysis_completed' AND record_id=?""", (ident,)).fetchone()
        if events[0] != 1 or not events[1] or events[1] > 4096:
            raise ContractError("incremental acceptance provenance missing/unbounded")
        event = self.conn.execute("SELECT * FROM events WHERE kind='analysis_completed' AND record_id=?", (ident,)).fetchone()
        payload = _loads(event["payload_json"], 4096)
        self.receipts(task)
        verified = self.analysis.get_result(ident)
        if verified != result:
            raise ContractError("incremental accepted result API mismatch")
        bundle = source["bundle"]
        if (task["state"] not in _TERMINAL or task["state"] != result["execution_status"]
                or result["program_validation"]["status"] != "passed"
                or result["agent"] != _loads(task["agent_json"], 4096)
                or any(result[k] != value for k, value in (
                    ("project_id", task["project_id"]), ("based_publish_id", task["publish_id"]),
                    ("based_bundle_id", bundle["bundle_id"]), ("based_bundle_revision", bundle["bundle_revision"])))
                or any(accepted[k] != task[k] for k in ("project_id", "publish_id", "result_sha256", "result_relative_path"))
                or accepted["result_json"] != journal["result_json"]
                or accepted["result_sha256"] != journal["result_sha256"]
                or event["project_id"] != task["project_id"]
                or payload.get("analysis_id") != accepted["analysis_id"]
                or payload.get("result_sha256") != accepted["result_sha256"]
                or payload.get("submitted_sha256") != journal["submitted_sha256"]
                or not payload.get("validation_sha256")):
            raise ContractError("incremental accepted task/result/version provenance mismatch")
        entries = _unique(result["block_processing"], "block_id")
        if entries.keys() != source["blocks"].keys():
            raise ContractError("incremental accepted block scope mismatch")
        for block_id, entry in entries.items():
            if entry["asset_ref"] != source["blocks"][block_id]["asset_ref"]:
                raise ContractError("incremental accepted block asset mismatch")
            # Validate *all* accepted read entries, even if configuration differs;
            # damaged accepted evidence is an error, never a cache miss.
            if entry["status"] == "read":
                self.original_read(task, source, entry)
        return {"task": task, "source": source, "result": result, "entries": entries,
                "result_sha256": accepted["result_sha256"]}

    def original_read(self, task, source, entry):
        key = task["task_id"], entry["block_id"]
        if key in self.reads:
            return self.reads[key]
        refs = entry["read_receipt_refs"]
        if not refs or len(refs) != len(set(refs)):
            raise ContractError("incremental accepted read lacks unique original receipts")
        block, bundle = source["blocks"][entry["block_id"]], source["bundle"]
        receipts = [self.stored_receipt(task, block["block_id"], r, bundle) for r in refs]
        if any(r[0]["kind"] == "reused" for r in receipts):
            self.reads[key] = None  # No recursion/multi-hop; fresh physical read is required.
            return None
        text, hashes = self.text(source, block)
        source_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        for receipt, _ in receipts:
            common = {"receipt_id", "task_id", "block_id", "kind", "publish_id", "created_at", "content"}
            if receipt["kind"] == "text":
                fields = {"char_start", "char_end", "total_chars", "source_text_sha256", "artifact_hashes", "content_sha256"}
                start, end = receipt["char_start"], receipt["char_end"]
                if (set(receipt) != common | fields or type(start) is not int or type(end) is not int
                        or not 0 <= start < end <= len(text) or receipt["total_chars"] != len(text)
                        or receipt["source_text_sha256"] != source_sha or receipt["artifact_hashes"] != hashes
                        or receipt["content"] != text[start:end]):
                    raise ContractError("accepted original text receipt no longer proves actual source read")
            elif receipt["kind"] == "image":
                fields = {"artifact_hashes", "width", "height", "viewer_id", "observation", "byte_length"}
                if set(receipt) != common | fields or receipt["content"] is not None or len(receipt["artifact_hashes"]) != 1:
                    raise ContractError("accepted original image receipt invalid")
                artifact_id = next(iter(receipt["artifact_hashes"]))
                artifact = source["artifacts"].get(artifact_id)
                if (artifact_id not in block["artifact_ids"] or not artifact
                        or artifact["kind"] not in {"image", "page_render", "keyframe"}
                        or receipt["artifact_hashes"][artifact_id] != artifact["sha256"]
                        or receipt["byte_length"] != artifact["byte_length"]
                        or not isinstance(receipt["viewer_id"], str) or not receipt["viewer_id"].strip()
                        or not isinstance(receipt["observation"], str) or not receipt["observation"].strip()):
                    raise ContractError("accepted original image receipt source/acknowledgment mismatch")
                from PIL import Image
                with Image.open(safe_path(self.store.project_root(task["project_id"]), artifact["relative_path"])) as image:
                    if (receipt["width"], receipt["height"]) != image.size:
                        raise ContractError("accepted original image receipt geometry mismatch")
            else:
                raise ContractError("accepted block receipt is not a physical text/image read")
        if not self.analysis._has_read(task, block, bundle, refs):
            raise ContractError("accepted original receipts no longer pass _has_read")
        self.reads[key] = {r[0]["receipt_id"]: r[1] for r in receipts}
        return self.reads[key]


def _assertions(prior, current, reusable):
    assertions = _unique(prior["result"]["assertions"], "assertion_id")
    eligible, invalid = {}, set()
    for ident, assertion in assertions.items():
        refs = assertion["evidence_refs"]
        for ref in refs:
            validate_evidence(ref, prior["source"]["bundle"], allow_source_only=False)
        deps = assertion["depends_on_assertion_ids"]
        if len(deps) != len(set(deps)) or not set(deps) <= assertions.keys():
            raise ContractError("accepted assertion dependency broken")
        if (assertion["external_source_ids"] or any(r.get("block_id") not in reusable for r in refs)
                or not refs and not deps):
            invalid.add(ident)
        else:
            copied = deepcopy(assertion)
            copied["evidence_refs"] = [_rebase(r, current) for r in refs]
            eligible[ident] = copied
    # Also validates cycles, iteratively, without invoking old reused receipts.
    _closure_fingerprints({k: digest(v) for k, v in assertions.items()},
                          {k: set(v["depends_on_assertion_ids"]) for k, v in assertions.items()})
    dependents = {k: [] for k in assertions}
    for ident, assertion in assertions.items():
        for dep in assertion["depends_on_assertion_ids"]:
            dependents[dep].append(ident)
    queue = deque(invalid)
    while queue:
        for child in dependents[queue.popleft()]:
            if child not in invalid:
                invalid.add(child)
                queue.append(child)
    return [eligible[k] for k in sorted(eligible.keys() - invalid)], sorted(invalid)


def _plan(run, task):
    current = run.source(task)
    row = run.conn.execute("""SELECT t.task_id FROM analyses a
        JOIN analysis_tasks t ON t.task_id=a.task_id
        JOIN publications p ON p.publish_id=t.publish_id
        WHERE t.project_id=? AND p.revision<? ORDER BY p.revision DESC,t.completed_at DESC,t.task_id LIMIT 1""",
        (task["project_id"], current["revision"])).fetchone()
    prior = run.accepted(run.task(row[0])) if row else None
    now_blocks = current["blocks"]
    old_blocks = prior["source"]["blocks"] if prior else {}
    shared = now_blocks.keys() & old_blocks.keys()
    same_agent = bool(prior and _loads(task["agent_json"], 4096) == prior["result"]["agent"])
    current_fps = run.fingerprints(current)
    old_fps = run.fingerprints(prior["source"]) if prior else {}
    changed = {k for k in shared if current_fps[k] != old_fps[k]}
    reusable, proofs = set(), {}
    if same_agent:
        for ident in shared - changed:
            if (ident not in current["byte_proven_block_ids"]
                    or ident not in prior["source"]["byte_proven_block_ids"]):
                continue
            entry = prior["entries"][ident]
            if entry["status"] == "read":
                proof = run.original_read(prior["task"], prior["source"], entry)
                if proof is not None:
                    reusable.add(ident)
                    proofs[ident] = proof
    assertions, invalid = _assertions(prior, current["bundle"], reusable) if prior else ([], [])
    limitations = list(prior["result"]["coverage"]["limitations"]) if prior else []
    if prior and not same_agent:
        limitations.append("Prior agent/provider/model/prompt differs; no historical reading or assertion is reused.")
    if now_blocks.keys() - current["byte_proven_block_ids"]:
        limitations.append("Stat-only media registration cannot prove full source-byte identity; affected blocks/dependencies require new reading, not historical reuse.")
    if reusable:
        limitations.append("Historical same-agent reuse is not a new physical text delivery/image open or a new semantic verification.")
    old_gaps = prior["result"]["gaps"] if prior else []
    # Preserve provenance separately: removed targets need not validate against
    # the new Bundle, and an old gap must not silently disappear/become resolved.
    gaps = [{"source_task_id": prior["task"]["task_id"], "gap": deepcopy(g)}
            for g in old_gaps if g["status"] in {"open", "requested"}]
    processing = [{"block_id": k, "asset_ref": deepcopy(b["asset_ref"]), "status": "pending",
                   "priority": "normal", "reason": "Pending new reading or explicit verified historical reuse.",
                   "read_receipt_refs": [], "exclusion": None} for k, b in now_blocks.items()]
    public = {"status": "prepared", "task_id": task["task_id"],
        "source_task_id": prior["task"]["task_id"] if prior else None,
        "same_agent": same_agent, "new_block_ids": sorted(now_blocks.keys() - old_blocks.keys()),
        "changed_block_ids": sorted(changed), "removed_block_ids": sorted(old_blocks.keys() - now_blocks.keys()),
        "reusable_block_ids": sorted(reusable), "pending_block_ids": sorted(now_blocks.keys() - reusable),
        "reusable_assertions": assertions, "invalidated_assertion_ids": invalid,
        "block_processing": processing,
        "context": {"limitations": limitations, "open_gaps": gaps,
                    "current_bundle_gaps": deepcopy(current["bundle"]["gaps"]),
                    "prior_block_processing": deepcopy(prior["result"]["block_processing"]) if prior else [],
                    "prior_gaps": deepcopy(old_gaps),
                    "prior_usage": deepcopy(prior["result"].get("usage")) if prior else None,
                    "usage": deepcopy(_UNKNOWN_USAGE)}}
    return public, prior, current, proofs


def prepare(analysis, task_id):
    """Read-only delta/context; reusable candidates are not automatically read."""
    with _fail_closed(), analysis.store.transaction():
        run = _Run(analysis)
        task = run.task(task_id, running=True)
        if analysis.store.pipeline_state("analysis") != "running":
            return {"status": "blocked", "reason": "analysis paused", "task_id": task_id}
        return _plan(run, task)[0]


def _payload(task, prior, current, block_id, proof):
    return {"reuse_policy": _POLICY, "historical_reuse": True, "new_physical_read": False,
        "source_task_id": prior["task"]["task_id"], "source_publish_id": prior["task"]["publish_id"],
        "source_bundle_id": prior["source"]["bundle"]["bundle_id"],
        "source_bundle_revision": prior["source"]["bundle"]["bundle_revision"],
        "source_result_sha256": prior["result_sha256"],
        "source_report_hashes": {r["relative_path"]: r["sha256"] for r in prior["result"]["reports"]},
        "source_read_receipt_sha256": proof,
        "evidence_fingerprint": current["fingerprints"][block_id],
        "agent_sha256": digest(_loads(task["agent_json"], 4096))}


def reuse(analysis, task_id, block_ids):
    """Atomically issue idempotent kind=reused receipts, never physical receipts."""
    with _fail_closed(), analysis.store.transaction():
        if (not isinstance(block_ids, list) or not block_ids or len(block_ids) > 2048
                or any(type(i) is not str for i in block_ids) or len(block_ids) != len(set(block_ids))):
            raise ContractError("reuse block_ids must be a nonempty bounded unique list")
        for ident in block_ids:
            check_id(ident)
        run = _Run(analysis)
        task = run.task(task_id, running=True)
        if analysis.store.pipeline_state("analysis") != "running":
            return {"status": "blocked", "reason": "analysis paused", "task_id": task_id, "items": []}
        delta, prior, current, proofs = _plan(run, task)
        if not set(block_ids) <= set(delta["reusable_block_ids"]):
            raise ContractError("block is new/changed/unread/ambiguous or agent differs; new physical reading required")
        run.receipts(task)
        items, processing = [], []
        for ident in block_ids:
            payload = _payload(task, prior, current, ident, proofs[ident])
            existing = run.conn.execute("SELECT receipt_id FROM read_receipts WHERE task_id=? AND block_id=? AND kind='reused'",
                                        (task_id, ident)).fetchall()
            if len(existing) > 1:
                raise ContractError("duplicate historical reuse receipts require review")
            if existing:
                receipt, _ = run.stored_receipt(task, ident, existing[0][0], current["bundle"])
                actual = {k: v for k, v in receipt.items() if k not in {"receipt_id", "task_id", "block_id", "kind", "publish_id", "created_at", "content"}}
                if actual != payload or receipt["content"] is not None:
                    raise ContractError("historical reuse receipt proof changed/tampered")
                receipt.pop("content")
            else:
                receipt = analysis._save_receipt(task, ident, "reused", payload)
            items.append({"block_id": ident, "receipt": receipt, "historical_reuse": True, "new_physical_read": False})
            processing.append({"block_id": ident, "asset_ref": deepcopy(current["blocks"][ident]["asset_ref"]),
                "status": "read", "priority": "normal", "reason": "Historical same-agent accepted evidence reuse; not a new physical read.",
                "read_receipt_refs": [receipt["receipt_id"]], "exclusion": None})
        return {"status": "reused", "task_id": task_id, "items": items, "block_processing": processing,
                "usage": deepcopy(_UNKNOWN_USAGE)}


def validate_reused(analysis, task, currentBlock, currentBundle, receipt):
    """Verify the exact stored one-hop proof; corruption/scope errors always raise.

    Returns False only when passed a non-reused receipt. No original reused
    receipt is recursively traversed and no model/image opener is invoked.
    """
    if receipt.get("kind") != "reused":
        return False
    with _fail_closed(), analysis.store.transaction():
        run = _Run(analysis)
        stored_task = run.task(task["task_id"])
        if dict(task) != dict(stored_task):
            raise ContractError("historical reuse task scope/version mismatch")
        current = run.source(stored_task)
        ident = currentBlock["block_id"]
        if currentBundle != current["bundle"] or current["blocks"].get(ident) != currentBlock:
            raise ContractError("historical reuse current block/bundle version mismatch")
        run.receipts(stored_task)
        actual, _ = run.stored_receipt(stored_task, ident, receipt["receipt_id"], currentBundle)
        if actual != {**receipt, "content": receipt.get("content")} or actual["content"] is not None:
            raise ContractError("historical reuse supplied/stored receipt mismatch")
        source_task = run.task(actual["source_task_id"])
        if source_task["project_id"] != stored_task["project_id"] or source_task["task_id"] == stored_task["task_id"]:
            raise ContractError("historical reuse source task project/scope mismatch")
        prior = run.accepted(source_task)
        if prior["source"]["revision"] >= current["revision"]:
            raise ContractError("historical reuse source version is not prior")
        if _loads(stored_task["agent_json"], 4096) != prior["result"]["agent"]:
            raise ContractError("historical reuse agent/provider/model/prompt mismatch")
        if ident not in prior["entries"] or prior["entries"][ident]["status"] != "read":
            raise ContractError("historical reuse source block was not accepted read")
        proof = run.original_read(source_task, prior["source"], prior["entries"][ident])
        if proof is None:
            raise ContractError("multi-hop historical reuse is not supported")
        old_fps, new_fps = run.fingerprints(prior["source"]), run.fingerprints(current)
        if (old_fps[ident] != new_fps[ident] or ident not in current["byte_proven_block_ids"]
                or ident not in prior["source"]["byte_proven_block_ids"]):
            raise ContractError("historical reuse source identity/evidence/dependency changed or unproven")
        expected = _payload(stored_task, prior, current, ident, proof)
        base = {"receipt_id", "task_id", "block_id", "kind", "publish_id", "created_at", "content"}
        if {k: v for k, v in actual.items() if k not in base} != expected:
            raise ContractError("historical reuse accepted result/read receipt/fingerprint tampered")
        return True
