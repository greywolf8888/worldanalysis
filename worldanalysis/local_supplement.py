"""Reuse exact, already-published local evidence; never acquire or parse anything.

Only supplement delivery bookkeeping is added to the Store's existing SQLite.
Prepared/unknown attempts reconcile the deterministic compose journal; they never
call compose again. Fulfilled means delivery, NOT semantic reading or gap repair.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from . import bundle_merge as _published
from .analysis import Analysis
from .contracts import (ContractError, asset_key, canonical, check_id, digest,
                        hash_file, now, reject_links, safe_path, validate_evidence,
                        validate_schema)

_KIND = "reused_existing_evidence"
_SUPPORTED = {"original_text", "original_image", "page_render"}
_TABLE = "local_supplement_deliveries"


def _schema(store):
    store.connection.execute("""CREATE TABLE IF NOT EXISTS local_supplement_deliveries(
        request_id TEXT PRIMARY KEY REFERENCES supplement_requests,
        project_id TEXT NOT NULL REFERENCES projects,
        task_id TEXT NOT NULL REFERENCES analysis_tasks,
        source_publish_id TEXT NOT NULL REFERENCES publications,
        source_bundle_id TEXT NOT NULL REFERENCES publications(bundle_id),
        request_sha256 TEXT NOT NULL, source_identity_json TEXT NOT NULL,
        compose_key TEXT NOT NULL, state TEXT NOT NULL
          CHECK(state IN ('queued','prepared','published','fulfilled')),
        selection_json TEXT, bundle_id TEXT REFERENCES publications(bundle_id),
        publish_id TEXT REFERENCES publications, reason TEXT,
        revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")


def _row(store, request_id):
    return store.connection.execute("SELECT * FROM " + _TABLE + " WHERE request_id=?", (request_id,)).fetchone()


def _cas(store, row, state, *, reason=None, selection=None, publication=None):
    values = {"state": state, "reason": reason, "updated_at": now(), "revision": row["revision"] + 1}
    if selection is not None:
        values["selection_json"] = canonical(selection).decode()
    if publication is not None:
        values.update(bundle_id=publication["bundle_id"], publish_id=publication["publish_id"])
    changed = store.connection.execute("UPDATE " + _TABLE + " SET " + ",".join(k + "=?" for k in values)
        + " WHERE request_id=? AND state=? AND revision=?", (*values.values(), row["request_id"], row["state"], row["revision"])).rowcount
    if changed != 1:
        raise ContractError("supplement delivery changed concurrently; reconcile, do not replay")
    if state != row["state"]:
        store.event("local_supplement_" + state, row["request_id"], row["project_id"],
                    {"delivery_kind": _KIND, "source_bundle_id": row["source_bundle_id"],
                     "bundle_id": publication["bundle_id"] if publication else row["bundle_id"]})
    return _row(store, row["request_id"])


def _request(store, request_id):
    row = store.connection.execute("SELECT * FROM supplement_requests WHERE request_id=?", (request_id,)).fetchone()
    if not row or len(row["request_json"].encode("utf-8")) > 128 * 1024:
        raise ContractError("unknown or unbounded Analysis supplement request")
    request = _published._decode(row["request_json"])
    if type(request) is not dict or request.get("request_id") != request_id:
        raise ContractError("supplement request identity mismatch")
    task = store.connection.execute("SELECT * FROM analysis_tasks WHERE task_id=?", (row["task_id"],)).fetchone()
    if not task or any(row[k] != task[k] for k in ("project_id", "publish_id")):
        raise ContractError("supplement task/project/publication mismatch")
    publication = store.connection.execute("SELECT * FROM publications WHERE publish_id=? AND project_id=? AND state='published'",
                                         (row["publish_id"], row["project_id"])).fetchone()
    if not publication:
        raise ContractError("supplement source is not database-published")
    budget = _published._Budget()
    source = _published._source(store, row["project_id"],
        _published._admit(store, row["project_id"], [publication["bundle_id"]], budget)[0], budget)
    bundle = source["bundle"]
    result = Analysis(store).get_result(task["task_id"])
    accepted = store.connection.execute("SELECT * FROM analyses WHERE task_id=?", (task["task_id"],)).fetchone()
    journal = store.connection.execute("SELECT * FROM analysis_publications WHERE task_id=?", (task["task_id"],)).fetchone()
    if (not accepted or not journal or journal["state"] != "committed"
            or task["state"] not in {"completed", "partial", "blocked"}
            or task["state"] != result["execution_status"]
            or result["program_validation"]["status"] != "passed"
            or result["agent"] != json.loads(task["agent_json"])
            or any(result[k] != v for k, v in (("project_id", row["project_id"]),
                ("based_publish_id", row["publish_id"]), ("based_bundle_id", bundle["bundle_id"]),
                ("based_bundle_revision", bundle["bundle_revision"])))
            or any(accepted[k] != task[k] for k in ("project_id", "publish_id", "result_sha256", "result_relative_path"))
            or accepted["result_json"] != journal["result_json"]
            or accepted["result_sha256"] != journal["result_sha256"]):
        raise ContractError("supplement requires a genuine accepted Analysis task/result")
    completion = store.connection.execute("SELECT * FROM events WHERE kind='analysis_completed' AND record_id=?", (task["task_id"],)).fetchall()
    if len(completion) != 1:
        raise ContractError("supplement Analysis completion provenance missing")
    event = completion[0]
    payload = _published._decode(event["payload_json"])
    if (event["project_id"] != row["project_id"] or payload.get("analysis_id") != result["analysis_id"]
            or payload.get("result_sha256") != accepted["result_sha256"]
            or payload.get("submitted_sha256") != journal["submitted_sha256"]):
        raise ContractError("supplement Analysis completion provenance mismatch")
    # Validate against the actual original schema using its offline registry.
    check = deepcopy(result)
    check["supplement_requests"] = [request]
    validate_schema("analysis", check)
    if request["status"] == "fulfilled":
        raise ContractError("an Analysis proposal cannot self-fulfil")
    if len(request["minimal_targets"]) > 32:
        raise ContractError("local supplement target bound exceeded")
    if not set(request["gap_ids"]) <= {g["gap_id"] for g in result["gaps"] + bundle["gaps"]}:
        raise ContractError("supplement references nonexistent accepted Analysis gaps")
    for ref in request["minimal_targets"]:
        validate_evidence(ref, bundle)
    original = [r for r in result["supplement_requests"] if r["request_id"] == request_id]
    if original != [request]:
        events = store.connection.execute("SELECT * FROM events WHERE kind='supplement_requested' AND record_id=?", (request_id,)).fetchall()
        if len(events) != 1:
            raise ContractError("supplement lacks Analysis request API provenance")
        e, p = events[0], _published._decode(events[0]["payload_json"])
        if (e["project_id"] != row["project_id"] or p.get("task_id") != task["task_id"]
                or p.get("publish_id") != row["publish_id"] or p.get("request_sha256") != digest(request)
                or p.get("source") != "Analysis.request_supplement"):
            raise ContractError("supplement Analysis request API provenance mismatch")
    return row, request, source


def _source_identity(store, source):
    """Pin local path, version ledger and current identity; never open media bytes."""
    bundle = source["bundle"]
    identities = []
    from .media_store import MediaRegistration, identity
    registration = MediaRegistration(store)
    for asset in bundle["assets"]:
        source_row = store.connection.execute("SELECT * FROM sources WHERE source_id=? AND project_id=?", (asset["source_ref"], bundle["project_id"])).fetchone()
        bound = store.connection.execute("SELECT * FROM asset_revisions WHERE asset_id=? AND asset_revision=?", asset_key(asset)).fetchone()
        versions = store.connection.execute("""SELECT r.* FROM source_revisions r JOIN source_assets sa
            ON sa.source_id=r.source_id AND sa.source_revision=r.revision
            WHERE sa.source_id=? AND sa.asset_id=? AND sa.asset_revision=? ORDER BY r.revision""",
            (asset["source_ref"], *asset_key(asset))).fetchall()
        if not source_row or not bound or not versions or any(r["sha256"] != bound["sha256"] or r["size_bytes"] != bound["size_bytes"] for r in versions):
            raise ContractError("supplement source revision ledger mismatch")
        path = Path(source_row["local_path"])
        reject_links(path)
        if (not path.is_absolute() or not path.resolve(strict=True).is_relative_to(store.root.resolve(strict=True))
                or asset["source_binding"] != {"kind": "local", "local_input_ref": asset["source_ref"]}):
            raise ContractError("supplement source moved or local authorization scope changed")
        current = identity(path)
        if source_row["kind"] == "local_media_stat":
            registration.validate(bundle["project_id"], asset, bound, require_current=True)
        elif source_row["kind"] == "local":
            if current["size_bytes"] > store.max_file_bytes or hash_file(path) != (bound["sha256"], bound["size_bytes"]):
                raise ContractError("supplement original source changed or exceeds local bound")
        else:
            raise ContractError("supplement supports local published sources only")
        identities.append({"source": dict(source_row), "asset": dict(bound),
                           "versions": [dict(r) for r in versions], "current": current})
    return {"manifest_sha256": source["manifest_sha256"], "sources": identities}


def _full_geometry(locator, artifact, root):
    g = locator.get("geometry")
    if g is None:
        return True
    if (g["reference_object_ref"] != artifact["artifact_id"] or g["coordinate_space"] != "original"
            or g["unit"] != "pixel" or g["original_size"]["unit"] != "pixel"
            or g["origin"] != "top_left" or g["rotation_degrees"] != 0
            or g["transform_to_original"] != {"method": "identity", "operations": []}):
        return False
    from PIL import Image
    with Image.open(safe_path(root, artifact["relative_path"])) as image:
        width, height = image.size
    return (g["frame_size"] == {"width": width, "height": height}
            and g["original_size"] == {"width": width, "height": height, "unit": "pixel"}
            and g["bbox_xywh"] == [0, 0, width, height])


def _select(store, bundle, request):
    if not set(request["requested_outputs"]) <= _SUPPORTED:
        return [], "unsupported", "requested_output_not_supported; no media/table/transcript generation"
    if not request["minimal_targets"]:
        return [], "unsupported", "no_matching_target_or_locator; nothing guessed"
    artifacts = {a["artifact_id"]: a for a in bundle["artifacts"]}
    root, selected = store.project_root(bundle["project_id"]), []
    for target in request["minimal_targets"]:
        locator = target["locator"]
        if "geometry" in locator and "original_text" in request["requested_outputs"]:
            return [], "unsupported", "geometry_text_coverage_unproven; no_crop_or_reparse"
        if locator["precision"] not in {"exact", "page_only"} or locator["kind"] == "time":
            return [], "unsupported", "target_locator_not_supported"
        blocks = [b for b in bundle["blocks"] if asset_key(b["asset_ref"]) == asset_key(target) and b["locator"] == locator
                  and ("block_id" not in target or target["block_id"] == b["block_id"])]
        if "block_id" in target and "artifact_id" in target and target["artifact_id"] not in blocks[0]["artifact_ids"]:
            raise ContractError("supplement artifact does not belong to its target block")
        whole = locator == {"kind": "asset", "precision": "exact"} and "block_id" not in target
        for output in request["requested_outputs"]:
            candidates = {}
            for artifact in bundle["artifacts"]:
                if (asset_key(artifact["asset_ref"]) != asset_key(target)
                        or artifact["access_class"] == "restricted_local"
                        or "artifact_id" in target and target["artifact_id"] != artifact["artifact_id"]):
                    continue
                owners = [b for b in blocks if artifact["artifact_id"] in b["artifact_ids"]]
                native = [b for b in owners if b["origin"] in {"native_extract", "author_original"}]
                eligible = False
                if output == "original_text":
                    restricted_original = any(a["kind"] == "original_small_file" and a["access_class"] == "restricted_local"
                                              and asset_key(a["asset_ref"]) == asset_key(target) for a in artifacts.values())
                    eligible = not restricted_original and artifact["media_type"] == "text/plain" and (
                        artifact["kind"] == "native_text" and any(Path(artifact["relative_path"]).name == b["block_id"] + ".txt" for b in native)
                        or whole and artifact["kind"] == "original_small_file")
                elif output == "original_image":
                    eligible = artifact["media_type"].startswith("image/") and (
                        artifact["kind"] == "image" and bool(native)
                        or artifact["kind"] == "original_small_file" and (whole or bool(native)))
                elif output == "page_render":
                    eligible = artifact["kind"] == "page_render" and artifact["media_type"].startswith("image/") and bool(owners)
                if not eligible:
                    continue
                if output == "original_text":
                    text = safe_path(root, artifact["relative_path"]).read_bytes().decode("utf-8")
                    if not text or "\u0000" in text:
                        return [], "unsupported", "original_text_is_not_complete_readable_UTF8"
                elif artifact["kind"] == "original_small_file":
                    from PIL import Image
                    with Image.open(safe_path(root, artifact["relative_path"])) as image:
                        if Image.MIME.get(image.format) != artifact["media_type"]:
                            raise ContractError("original image actual media type mismatch")
                        image.verify()
                if not _full_geometry(locator, artifact, root):
                    return [], "unsupported", "geometry_or_crop_full_coverage_unproven; no_crop_or_reparse"
                candidates[artifact["artifact_id"]] = artifact
            if len(candidates) > 1:
                return [], "unsupported", "ambiguous_matching_artifacts; nothing guessed"
            if not candidates:
                if "geometry" in locator:
                    return [], "unsupported", "geometry_or_crop_full_coverage_unproven; no_crop_or_reparse"
                if not blocks and not whole:
                    return [], "unsupported", "no_matching_target_or_locator; nothing guessed"
                if output == "original_text" and blocks and not any(b["origin"] in {"native_extract", "author_original"} for b in blocks):
                    return [], "unsupported", "derived_OCR_ASR_or_model_text_is_not_original_text"
                return [], "pending", "missing_existing_" + output
            artifact = next(iter(candidates.values()))
            selected.append({"requested_output": output, "target": deepcopy(target), "artifact": deepcopy(artifact)})
    return selected, None, None


def _existing(store, row):
    publication = store.connection.execute("SELECT * FROM publications WHERE project_id=? AND idempotency_key=?",
                                         (row["project_id"], row["compose_key"])).fetchone()
    if not publication:
        return None
    events = store.connection.execute("SELECT * FROM events WHERE kind='publish_registered' AND record_id=?", (publication["publish_id"],)).fetchall()
    if len(events) != 1 or events[0]["project_id"] != row["project_id"]:
        raise ContractError("supplement wrapper publication provenance missing")
    payload = _published._decode(events[0]["payload_json"])
    if payload.get("operation") != "compose_published_evidence" or payload.get("source_bundle_ids") != [row["source_bundle_id"]]:
        raise ContractError("supplement wrapper has wrong publication source scope")
    if (publication["bundle_id"] == row["source_bundle_id"] or publication["publish_id"] == row["source_publish_id"]
            or row["bundle_id"] is not None and (row["bundle_id"], row["publish_id"]) != (publication["bundle_id"], publication["publish_id"])):
        raise ContractError("supplement delivery wrapper identity mismatch")
    if publication["state"] == "quarantined":
        raise ContractError("supplement existing publication quarantined; explicit reconciliation required")
    return publication


def _verify_delivery(store, row, source, request, selection, publication):
    # Public API re-verifies every actual file SHA, original ledger and Plan.
    bundle = store.get_bundle(row["project_id"], publication["bundle_id"])
    original = source["bundle"]
    def rebase(value):
        return _published._rebase(value, bundle["bundle_id"], bundle["bundle_revision"])
    def indexed(items, field):
        return {asset_key(i[field]) if field == "asset_ref" else asset_key(i) if field == "asset" else i[field]: i for i in items}
    coverage = deepcopy(bundle["coverage"])
    before = deepcopy(original["coverage"])
    coverage["per_asset"] = indexed(coverage["per_asset"], "asset_ref")
    before["per_asset"] = indexed(before["per_asset"], "asset_ref")
    if (bundle["publish_id"] != publication["publish_id"]
            or indexed(bundle["assets"], "asset") != indexed(original["assets"], "asset")
            or indexed(bundle["blocks"], "block_id") != indexed(rebase(original["blocks"]), "block_id")
            or indexed(bundle["gaps"], "gap_id") != indexed(rebase(original["gaps"]), "gap_id")
            or coverage != before):
        raise ContractError("supplement wrapper changed source scope/blocks/gaps/coverage")
    actual = {a["artifact_id"]: a for a in bundle["artifacts"]}
    if any(actual.get(a["artifact_id"]) != a for a in original["artifacts"]):
        raise ContractError("supplement wrapper does not retain exact source artifacts")
    report = _published._decode(safe_path(store.project_root(row["project_id"]), bundle["program_validation"]["report_relative_path"]).read_bytes())
    if (report.get("operation") != "compose_published_evidence" or report.get("new_extraction_executed") is not False
            or report.get("source_publications") != [{"bundle_id": original["bundle_id"], "publish_id": original["publish_id"],
                "manifest_relative_path": original["publication"]["manifest_relative_path"], "manifest_sha256": source["manifest_sha256"]}]):
        raise ContractError("supplement wrapper source receipt mismatch")
    rebased = deepcopy(request)
    rebased["minimal_targets"] = rebase(request["minimal_targets"])
    for ref in rebased["minimal_targets"]:
        validate_evidence(ref, bundle)
    delivered, status, _ = _select(store, bundle, rebased)
    if status or delivered != rebase(selection):
        raise ContractError("supplement new Bundle does not fulfil every exact target/output")
    for item in delivered:
        artifact = item["artifact"]
        if hash_file(safe_path(store.project_root(row["project_id"]), artifact["relative_path"])) != (artifact["sha256"], artifact["byte_length"]):
            raise ContractError("supplement delivered artifact changed before fulfilment")
    return bundle, delivered


def _response(row, status, reason=None, *, delivered=(), idempotent=False):
    return {"status": status, "request_id": row["request_id"], "project_id": row["project_id"],
            "delivery_state": row["state"], "delivery_kind": _KIND,
            "source_bundle_id": row["source_bundle_id"], "bundle_id": row["bundle_id"],
            "existing_bundle_id": row["bundle_id"], "publish_id": row["publish_id"],
            "artifacts": [{"requested_output": i["requested_output"], "target": i["target"],
                           "artifact_id": i["artifact"]["artifact_id"], "sha256": i["artifact"]["sha256"],
                           "relative_path": i["artifact"]["relative_path"], "byte_length": i["artifact"]["byte_length"]} for i in delivered],
            "reason": reason, "idempotent": idempotent, "new_extraction_executed": False,
            "semantic_read_performed": False, "gaps_resolved": False}


def fulfil_available(store, request_id):
    """Fulfil only genuine queued Analysis requests with exact existing artifacts.

    Missing/unsupported output leaves the request queued; scope expansion and
    collection pause are not changed. Corruption/missing sources raise
    ContractError, never become an ordinary artifact miss. A prepared attempt
    without a published compose journal remains pending for reconciliation.
    """
    try:
        check_id(request_id)
        with store.transaction():
            reqrow, request, source = _request(store, request_id)
            if reqrow["state"] == "blocked_scope_expansion" or request["requires_scope_expansion"]:
                return {"status": "blocked", "request_id": request_id, "reason": "scope_expansion_blocked", "executed": False}
            if reqrow["state"] not in {"queued", "fulfilled"}:
                return {"status": "blocked", "request_id": request_id, "reason": "request_not_queued", "executed": False}
            if store.pipeline_state("collection") != "running":
                return {"status": "blocked", "request_id": request_id, "reason": "collection paused", "executed": False}
            identity = _source_identity(store, source)
            _schema(store)
            row = _row(store, request_id)
            key = digest([_published._POLICY, reqrow["project_id"], [[source["bundle_id"], source["publish_id"], source["manifest_sha256"]]]])
            if not row:
                if reqrow["state"] != "queued":
                    raise ContractError("fulfilled supplement has no durable delivery provenance")
                stamp = now()
                store.connection.execute("INSERT INTO " + _TABLE + "(request_id,project_id,task_id,source_publish_id,source_bundle_id,request_sha256,source_identity_json,compose_key,state,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?, 'queued',0,?,?)",
                    (request_id, reqrow["project_id"], reqrow["task_id"], source["publish_id"], source["bundle_id"], digest(request), canonical(identity).decode(), key, stamp, stamp))
                row = _row(store, request_id)
                store.event("local_supplement_queued", request_id, row["project_id"], {"delivery_kind": _KIND})
            if (any(row[k] != reqrow[k] for k in ("project_id", "task_id")) or row["source_publish_id"] != source["publish_id"]
                    or row["source_bundle_id"] != source["bundle_id"] or row["request_sha256"] != digest(request)
                    or row["source_identity_json"] != canonical(identity).decode() or row["compose_key"] != key):
                raise ContractError("supplement request/source identity changed after queueing")
            selection, status, reason = _select(store, source["bundle"], request)
            if status:
                if row["state"] != "queued":
                    raise ContractError("previously prepared supplement evidence is no longer available")
                row = _cas(store, row, "queued", reason=reason)
                return _response(row, status, reason)
            if row["selection_json"] is not None and row["selection_json"] != canonical(selection).decode():
                raise ContractError("supplement prepared artifact selection changed")
            fresh = row["state"] == "queued"
            if fresh:
                row = _cas(store, row, "prepared", selection=selection)
            elif reqrow["state"] != ("fulfilled" if row["state"] == "fulfilled" else "queued"):
                raise ContractError("supplement request/delivery state mismatch")
        existing = _existing(store, row)
        # Only the owner of this newly prepared attempt may call compose once.
        if existing is None and fresh:
            produced = store.compose(row["project_id"], [row["source_bundle_id"]])
            existing = _existing(store, row)
            if existing is not None and produced.get("bundle_id") != existing["bundle_id"]:
                raise ContractError("compose API returned a different wrapper")
        if existing is None or existing["state"] != "published":
            with store.transaction():
                row = _cas(store, row, row["state"],
                    reason="prepared_unknown; reconcile_existing_bundle_only; no_republish", publication=existing)
            return _response(row, "pending", row["reason"], idempotent=not fresh)
        with store.transaction():
            # Recheck API provenance and current local source after publication.
            latest, request, source = _request(store, request_id)
            row = _row(store, request_id)
            if row["source_identity_json"] != canonical(_source_identity(store, source)).decode() or row["request_sha256"] != digest(request):
                raise ContractError("supplement source/request changed during publication")
            bundle, delivered = _verify_delivery(store, row, source, request, selection, existing)
            if row["state"] == "fulfilled":
                if latest["state"] != "fulfilled":
                    raise ContractError("fulfilled delivery request state changed")
                return _response(row, "fulfilled", delivered=delivered, idempotent=True)
            if latest["state"] != "queued":
                raise ContractError("supplement request changed before fulfilment")
            if row["state"] == "prepared":
                row = _cas(store, row, "published", publication=existing)
        with store.transaction():
            # Persist published as its own durable checkpoint, then fulfil by CAS.
            reqrow, request, source = _request(store, request_id)
            row = _row(store, request_id)
            if row["source_identity_json"] != canonical(_source_identity(store, source)).decode() or row["state"] != "published":
                raise ContractError("supplement changed before fulfilled CAS; reconcile")
            _, delivered = _verify_delivery(store, row, source, request, selection, existing)
            changed = store.connection.execute("UPDATE supplement_requests SET state='fulfilled' WHERE request_id=? AND state='queued' AND project_id=? AND task_id=? AND publish_id=? AND request_json=?",
                (request_id, row["project_id"], row["task_id"], row["source_publish_id"], reqrow["request_json"])).rowcount
            if changed != 1:
                raise ContractError("supplement request fulfilment CAS failed")
            row = _cas(store, row, "fulfilled", publication=existing)
            return _response(row, "fulfilled", delivered=delivered, idempotent=not fresh)
    except ContractError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, sqlite3.DatabaseError) as exc:
        raise ContractError("invalid/missing local supplement source or publication evidence") from exc
