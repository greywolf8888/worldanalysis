"""Fulfil genuine audio_window proposals only after exact published-WAV reconciliation.

No decoder, publication, ASR/model/listening, gap repair or file writes. Delivery
receipt/event + request CAS share Store's one SQLite transaction. A second genuine
same-window request can use the same publication, after its own proposal checks.
"""
from __future__ import annotations

from . import audio_publication as publication
from . import bundle_merge as published
from .contracts import ContractError, asset_key, canonical, check_id, digest, now
from .local_supplement import _request

_TABLE = "audio_window_deliveries"
_KIND = "published_audio_window"


def _schema(store):
    store.connection.execute("""CREATE TABLE IF NOT EXISTS audio_window_deliveries(
        request_id TEXT PRIMARY KEY REFERENCES supplement_requests,
        project_id TEXT NOT NULL REFERENCES projects,
        task_id TEXT NOT NULL REFERENCES analysis_tasks,
        source_publish_id TEXT NOT NULL REFERENCES publications,
        source_bundle_id TEXT NOT NULL REFERENCES publications(bundle_id),
        bundle_id TEXT NOT NULL REFERENCES publications(bundle_id),
        publish_id TEXT NOT NULL REFERENCES publications,
        request_sha256 TEXT NOT NULL, verification_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state='fulfilled'), created_at TEXT NOT NULL)""")


def reconcile_audio_window(store, request_id, bundle_id):
    """Return fulfilled only after actual WAV/proof/Plan/job/intent/source checks.

    Source path/stat must still match registration, including on fulfilled replay;
    this is NOT strict cryptographic media-history reuse. Historical get_bundle
    remains possible if the original media later changes. Fulfilled means delivery,
    never that this window was heard or any old/new gap resolved.
    """
    with published._fail_closed(), store.transaction():
        check_id(request_id)
        check_id(bundle_id)
        reqrow, request, source = _request(store, request_id)
        if reqrow["state"] not in {"queued", "fulfilled"} or request["requires_scope_expansion"]:
            return {"status": "blocked", "request_id": request_id, "reason": "request_not_queued_or_scope_expansion", "executed": False}
        if store.pipeline_state("collection") != "running" and reqrow["state"] != "fulfilled":
            return {"status": "blocked", "request_id": request_id, "reason": "collection paused", "executed": False}
        if request["requested_outputs"] != ["audio_window"] or len(request["minimal_targets"]) != 1:
            raise ContractError("single audio_window proposal required")
        target = request["minimal_targets"][0]
        locator = target["locator"]
        exact = publication._window(locator.get("start_ms"), locator.get("end_ms"))
        if locator != exact:
            raise ContractError("exact half-open audio time locator required")
        native = publication._native(store, source, *asset_key(target), exact)
        publication._request(store, request_id, reqrow["project_id"], source, native, exact, states=("queued", "fulfilled"))
        row = store.connection.execute("SELECT * FROM publications WHERE project_id=? AND bundle_id=? AND state='published'", (reqrow["project_id"], bundle_id)).fetchone()
        if not row or bundle_id == source["bundle_id"]:
            raise ContractError("different database-published audio window Bundle required")
        # Bounded admission comes before get_bundle's materialization, followed by
        # the publisher's independent immutable-byte and durable-policy verifier.
        published._admit(store, reqrow["project_id"], [bundle_id], published._Budget())
        bundle = store.get_bundle(reqrow["project_id"], bundle_id)
        proof = publication.verify_audio_window_publication(store, row, bundle)
        if (proof["source_bundle_id"] != source["bundle_id"] or proof["source_publish_id"] != source["publish_id"]
                or proof["source_manifest_sha256"] != source["manifest_sha256"] or proof["locator"] != exact
                or proof["source_identity"] != native["source_identity"]):
            raise ContractError("published audio source/window does not match this exact proposal")
        block = publication._one([b for b in bundle["blocks"] if b["block_id"] == proof["block_id"]], "published audio asset")
        if asset_key(block["asset_ref"]) != asset_key(target):
            raise ContractError("published audio window wrong media asset revision")
        _, published_event = publication._event(store, "bundle_published", row["publish_id"], reqrow["project_id"])
        if published_event != {"bundle_id": bundle_id}:
            raise ContractError("audio publication completion event mismatch")
        proof["request_sha256"] = digest(request)
        encoded = canonical(proof).decode()
        _schema(store)
        old = store.connection.execute("SELECT * FROM " + _TABLE + " WHERE request_id=?", (request_id,)).fetchone()
        payload = {"delivery_kind": _KIND, "task_id": reqrow["task_id"], "source_publish_id": source["publish_id"],
            "source_bundle_id": source["bundle_id"], "bundle_id": bundle_id, "publish_id": row["publish_id"],
            "request_sha256": digest(request), "verification_sha256": digest(proof), "artifact_id": proof["artifact_id"],
            "artifact_sha256": proof["artifact_sha256"], "locator": exact, "source_content_sha256": None,
            "semantic_read_performed": False, "gaps_resolved": False}
        if old:
            if (reqrow["state"] != "fulfilled" or old["state"] != "fulfilled" or old["verification_json"] != encoded
                    or any(old[k] != reqrow[k] for k in ("project_id", "task_id"))
                    or old["source_publish_id"] != source["publish_id"] or old["source_bundle_id"] != source["bundle_id"]
                    or old["bundle_id"] != bundle_id or old["publish_id"] != row["publish_id"] or old["request_sha256"] != digest(request)):
                raise ContractError("fulfilled audio delivery immutable provenance changed")
            _, event = publication._event(store, "audio_window_delivered", request_id, reqrow["project_id"])
            if event != payload:
                raise ContractError("fulfilled audio delivery event provenance changed")
        else:
            if reqrow["state"] != "queued":
                raise ContractError("fulfilled request lacks audio delivery provenance")
            changed = store.connection.execute("UPDATE supplement_requests SET state='fulfilled' WHERE request_id=? AND state='queued' AND project_id=? AND task_id=? AND publish_id=? AND request_json=?",
                (request_id, reqrow["project_id"], reqrow["task_id"], source["publish_id"], reqrow["request_json"])).rowcount
            if changed != 1:
                raise ContractError("audio request fulfilment CAS failed")
            store.connection.execute("INSERT INTO " + _TABLE + " VALUES(?,?,?,?,?,?,?,?,?,'fulfilled',?)",
                (request_id, reqrow["project_id"], reqrow["task_id"], source["publish_id"], source["bundle_id"], bundle_id,
                 row["publish_id"], digest(request), encoded, now()))
            store.event("audio_window_delivered", request_id, reqrow["project_id"], payload)
        return {"status": "fulfilled", "request_id": request_id, "project_id": reqrow["project_id"], "delivery_kind": _KIND,
            "delivery_state": "fulfilled", "source_bundle_id": source["bundle_id"], "bundle_id": bundle_id, "publish_id": row["publish_id"],
            "artifact_id": proof["artifact_id"], "block_id": proof["block_id"], "relative_path": proof["relative_path"],
            "sha256": proof["artifact_sha256"], "byte_length": proof["byte_length"], "locator": exact, "received_bytes": proof["received_bytes"],
            "idempotent": old is not None, "new_extraction_executed": False, "new_publication_created": False,
            "source_content_sha256": None, "semantic_read_performed": False, "gaps_resolved": False}
