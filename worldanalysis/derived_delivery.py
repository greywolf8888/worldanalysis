"""Reconcile an EXISTING image-region publication, then fulfil its exact request.

No crop, publication, materialization, semantic receipt, gap repair, or file write.
Only one delivery row + event and a request-state CAS in Store's existing SQLite
are written, in ONE transaction.  The original Analysis proposal remains intact.
An already fulfilled repeat still verifies all source/publication/intention pins.
The first route deliberately supports only a single original_image request with
native identity pixel geometry and an unresized crop from the frozen publisher.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io

from . import bundle_merge as _published
from . import derived_publication as _publication
from . import image_region
from .local_supplement import _request, _source_identity
from .contracts import (ContractError, asset_key, canonical, check_id, digest,
                        hash_file, now, safe_path, validate_plan)

_TABLE = "derived_image_region_deliveries"
_KIND = "published_image_region"


def _schema(store):
    store.connection.execute("""CREATE TABLE IF NOT EXISTS derived_image_region_deliveries(
        request_id TEXT PRIMARY KEY REFERENCES supplement_requests,
        project_id TEXT NOT NULL REFERENCES projects,
        task_id TEXT NOT NULL REFERENCES analysis_tasks,
        source_publish_id TEXT NOT NULL REFERENCES publications,
        source_bundle_id TEXT NOT NULL REFERENCES publications(bundle_id),
        bundle_id TEXT NOT NULL REFERENCES publications(bundle_id),
        publish_id TEXT NOT NULL REFERENCES publications,
        request_sha256 TEXT NOT NULL, verification_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state='fulfilled'), created_at TEXT NOT NULL)""")


def _json(store, project_id, artifact, limit):
    data, _ = image_region._read(safe_path(store.project_root(project_id), artifact["relative_path"]), limit)
    if (hashlib.sha256(data).hexdigest(), len(data)) != (artifact["sha256"], artifact["byte_length"]):
        raise ContractError("sealed derived metadata hash changed")
    return _published._decode(data)


def _one(items, label):
    if len(items) != 1:
        raise ContractError("unique " + label + " required")
    return items[0]


def _event(store, kind, record_id, project_id):
    row = _one(store.connection.execute("SELECT * FROM events WHERE kind=? AND record_id=?", (kind, record_id)).fetchall(), kind + " provenance event")
    if row["project_id"] != project_id:
        raise ContractError("derivation event project mismatch")
    return row, _published._decode(row["payload_json"])


def _target(store, project_id, source, request, metadata):
    if (request["requires_scope_expansion"] or request["requested_outputs"] != ["original_image"]
            or len(request["minimal_targets"]) != 1 or metadata.get("resize") is not None):
        raise ContractError("UNSUPPORTED: single original_image identity ROI without resize required")
    target = request["minimal_targets"][0]
    block, artifact, asset = _publication._select(source, metadata["source_block"]["block_id"], metadata["source_artifact"]["artifact_id"])
    if (metadata["source_block"] != block or metadata["source_artifact"] != artifact
            or target.get("artifact_id") != artifact["artifact_id"] or asset_key(target) != asset_key(asset)
            or target.get("block_id", block["block_id"]) != block["block_id"]):
        raise ContractError("request source block/artifact/asset mismatch")
    data, identity = image_region._read(safe_path(store.project_root(project_id), artifact["relative_path"]), image_region.MAX_SOURCE_BYTES)
    if (hashlib.sha256(data).hexdigest(), len(data)) != (artifact["sha256"], artifact["byte_length"]):
        raise ContractError("source image SHA changed")
    from PIL import Image
    with Image.open(io.BytesIO(data)) as decoded:
        dimensions = image_region._dimensions(*decoded.size)
        if (decoded.format not in {"PNG", "JPEG"} or Image.MIME.get(decoded.format) != artifact["media_type"]
                or getattr(decoded, "n_frames", 1) != 1 or decoded.getexif().get(274, 1) != 1):
            raise ContractError("unsupported source image format/orientation")
    width, height = dimensions
    identity_geometry = {"reference_object_ref": artifact["artifact_id"], "coordinate_space": "original", "unit": "pixel", "origin": "top_left",
        "frame_size": {"width": width, "height": height}, "original_size": {"width": width, "height": height, "unit": "pixel"},
        "rotation_degrees": 0, "bbox_xywh": [0, 0, width, height], "transform_to_original": {"method": "identity", "operations": []}}
    source_geometry = block["locator"].get("geometry")
    if source_geometry is not None and {k: v for k, v in source_geometry.items() if k != "bbox_xywh"} != {k: v for k, v in identity_geometry.items() if k != "bbox_xywh"}:
        raise ContractError("UNSUPPORTED: source must have original identity pixel geometry")
    locator = target["locator"]
    if {k: v for k, v in locator.items() if k != "geometry"} != {k: v for k, v in block["locator"].items() if k != "geometry"}:
        raise ContractError("request exact source locator mismatch")
    requested_geometry = locator.get("geometry", {})
    if {k: v for k, v in requested_geometry.items() if k != "bbox_xywh"} != {k: v for k, v in identity_geometry.items() if k != "bbox_xywh"}:
        raise ContractError("request exact identity ROI geometry required; no reason-based guessing")
    xywh = requested_geometry.get("bbox_xywh")
    if not isinstance(xywh, list) or len(xywh) != 4:
        raise ContractError("request exact ROI bounds missing")
    left, top, bw, bh = [image_region._integer(v) for v in xywh]
    box = [left, top, left + bw, top + bh]
    if not (0 <= left < box[2] <= width and 0 <= top < box[3] <= height):
        raise ContractError("request ROI outside actual source image")
    output_size = image_region._dimensions(bw, bh)
    # Pure edge-matrix arithmetic only: NEVER crop pixels or mutate geometry.
    geometry = image_region._geometry(block["locator"], artifact["artifact_id"], dimensions, box, output_size, False)
    expected_box = {"unit": "pixel", "origin": "top_left", "xyxy": box}
    if metadata["bbox"] != expected_box or metadata["geometry_to_original"] != geometry:
        raise ContractError("published crop does not match request exact ROI/geometry")
    image_region._recheck(safe_path(store.project_root(project_id), artifact["relative_path"]), identity, artifact["sha256"])
    return block, artifact, asset, geometry, output_size


def _verify(store, reqrow, request, source, bundle_id):
    project_id = reqrow["project_id"]
    derived = _publication._source(store, project_id, bundle_id, _published._Budget())
    bundle, original = derived["bundle"], source["bundle"]
    final = derived["final_relative_path"]
    if bundle_id == source["bundle_id"]:
        raise ContractError("a source Bundle is not a derived publication")
    metadata_artifact = _one([a for a in bundle["artifacts"] if a["relative_path"] == final + "/region.json" and a["kind"] == "structured_data"
                              and a["access_class"] == "project_local" and a["media_type"] == "application/json"], "sealed region metadata")
    metadata = _json(store, project_id, metadata_artifact, 1024**2)
    if (metadata["metadata_version"] != _publication._POLICY or metadata["source_bundle_id"] != source["bundle_id"]
            or metadata["source_bundle_sha256"] != digest(original) or metadata["source_manifest_sha256"] != source["manifest_sha256"]
            or metadata["implementation"] != _publication._implementation()):
        raise ContractError("derived publication source/version/code configuration mismatch")
    source_block, source_artifact, asset, geometry, dimensions = _target(store, project_id, source, request, metadata)
    anchor = _publication._anchor(store, project_id, source, source_block, source_artifact, _published._Budget())
    if metadata["primitive_source_bundle_id"] != anchor["bundle_id"]:
        raise ContractError("primitive owning source publication mismatch")
    image = _one([a for a in bundle["artifacts"] if a["artifact_id"] == metadata["published_artifact_id"] and a["relative_path"] == final + "/region.png"
                  and a["kind"] == "image" and a["media_type"] == "image/png" and a["access_class"] == "project_local"], "published derived PNG")
    if (metadata["published_relative_path"] != image["relative_path"] or metadata["actual_png"] != {"sha256": image["sha256"], "byte_length": image["byte_length"]}
            or image["artifact_revision"] != "sha256-" + image["sha256"]):
        raise ContractError("published PNG identity/hash/revision mismatch")
    data, _ = image_region._read(safe_path(store.project_root(project_id), image["relative_path"]), image_region.MAX_OUTPUT_BYTES)
    from PIL import Image
    with Image.open(io.BytesIO(data)) as decoded:
        if decoded.format != "PNG" or decoded.size != dimensions or getattr(decoded, "n_frames", 1) != 1:
            raise ContractError("published PNG actual dimensions/type mismatch")
        decoded.verify()
    if (hashlib.sha256(data).hexdigest(), len(data)) != (image["sha256"], image["byte_length"]):
        raise ContractError("published PNG actual SHA mismatch")
    schema_geometry = {k: deepcopy(v) for k, v in geometry.items() if k != "original_polygon_xy"}
    schema_geometry["reference_object_ref"] = image["artifact_id"]
    derived_block = _one([b for b in bundle["blocks"] if image["artifact_id"] in b["artifact_ids"] and metadata_artifact["artifact_id"] in b["artifact_ids"]], "derived metadata block")
    locator = deepcopy(source_block["locator"])
    locator["geometry"] = schema_geometry
    provenance = {"project_id": project_id, **source_artifact["asset_ref"], "block_id": source_block["block_id"],
                  "artifact_id": source_artifact["artifact_id"], "locator": source_block["locator"]}
    if (derived_block["kind"] != "metadata" or derived_block["origin"] != "native_extract" or derived_block["locator"] != locator
            or derived_block["artifact_ids"] != [image["artifact_id"], metadata_artifact["artifact_id"]]
            or derived_block["derived_from"] != [provenance] or derived_block["processing"]["evidence_refs"] != [provenance]
            or derived_block["processing"]["status"] != "extracted"):
        raise ContractError("derived block provenance/current-to-original geometry mismatch")
    def rebased(value):
        return _published._rebase(deepcopy(value), bundle_id, bundle["bundle_revision"])
    coverage = deepcopy(original["coverage"])
    coverage["discovered_counts"]["blocks"] += 1
    for entry in coverage["per_asset"]:
        if asset_key(entry["asset_ref"]) == asset_key(asset):
            for metric in entry["metrics"]:
                if metric["unit"] == "block":
                    metric["observed"] += 1
                    metric["processed"] += 1
                    if metric["expected"] is not None:
                        metric["expected"] += 1
    if (bundle["assets"] != original["assets"] or bundle["blocks"] != rebased(original["blocks"]) + [derived_block]
            or bundle["gaps"] != rebased(original["gaps"]) or bundle["contacts"] != rebased(original["contacts"])
            or bundle["coverage"] != coverage or bundle["artifacts"][:len(original["artifacts"])] != original["artifacts"]):
        raise ContractError("derived publication changed inherited evidence/state")
    intended, payload = _event(store, "image_region_derivation_intended", bundle["publish_id"], project_id)
    _, registered = _event(store, "publish_registered", bundle["publish_id"], project_id)
    _, published = _event(store, "bundle_published", bundle["publish_id"], project_id)
    specification = {"project_id": project_id, "source_bundle_id": source["bundle_id"], "source_manifest_sha256": source["manifest_sha256"],
        "source_bundle_sha256": digest(original), "source_publish_id": source["publish_id"], "source_artifact": source_artifact,
        "source_block": source_block, "bbox": metadata["bbox"], "resize": None, "implementation": metadata["implementation"]}
    key = digest([_publication._POLICY, specification])
    journal_key = store.connection.execute("SELECT idempotency_key FROM publications WHERE publish_id=? AND project_id=? AND state='published'", (bundle["publish_id"], project_id)).fetchone()
    provider = image["provider"]
    if (payload["specification"] != specification or payload["request_is_authorization"] is not False or payload["cost_minor"] != 0
            or payload["provider"] != provider or payload["job_id"] != image["job_id"]
            or payload["intention_plan_id"] != metadata["intention_plan_id"] or payload["request"] != metadata["request"]
            or payload["primitive_output_relative"] != metadata["primitive_output_relative"]
            or registered != {"operation": _publication._POLICY, "source_bundle_id": source["bundle_id"]}
            or published.get("bundle_id") != bundle_id or not journal_key or journal_key[0] != key
            or provider["provider_id"] != _publication._PROVIDER or provider["runtime"] != "local_cpu"
            or provider["implementation_version"] != "crop-" + digest(metadata["implementation"])[:32]
            or provider["config_version"] != "region-" + key[:40]
            or any(a["job_id"] != image["job_id"] or a["provider"] != provider for a in bundle["artifacts"][len(original["artifacts"]):])
            or metadata["job_id"] != image["job_id"] or metadata["provider"] != provider
            or derived_block["producer"] != provider or derived_block["processing"]["recovery_ref"] != image["job_id"]):
        raise ContractError("derived intention/config/provider/job/publication mismatch")
    _published._provider(store, provider)
    execution = store._check_extraction_job(project_id, source_artifact["asset_ref"], image["job_id"], provider, published=True)
    if (_published._decode(execution["receipt_json"] or "{}").get("publish_id") != bundle["publish_id"]
            or execution["estimated_cost_minor"] != 0 or execution["max_cost_minor"] != 0 or execution["reserved_cost_minor"] != 0):
        raise ContractError("derived producer publication receipt/cost mismatch")
    planrow = store.connection.execute("SELECT * FROM plans WHERE plan_id=? AND project_id=?", (metadata["intention_plan_id"], project_id)).fetchone()
    if not planrow:
        raise ContractError("durable intention Plan missing")
    plan = _published._decode(planrow["plan_json"])
    validate_plan(plan)
    if digest(plan) != payload["intention_plan_sha256"] or plan["program_validation"]["status"] != "pending":
        raise ContractError("durable intention Plan digest/status mismatch")
    planned = _one([j for a in plan["asset_plans"] for j in a["jobs"] if j["job_id"] == image["job_id"]], "intention producer job")
    if (planned["provider"] != provider or planned["operation"] != "native_extract" or planned["tier"] != "structured_local"
            or planned["side_effect"] != "local_read_or_derivation" or source_artifact["job_id"] not in planned["depends_on_job_ids"]):
        raise ContractError("intention source dependency/producer mismatch")
    final_plan = rebased(plan)
    final_plan.update(plan_id=derived["plan"]["plan_id"], program_validation=bundle["program_validation"])
    final_job = next(j for a in final_plan["asset_plans"] for j in a["jobs"] if j["job_id"] == image["job_id"])
    final_job["estimate"].update(state="measured", input_bytes=source_artifact["byte_length"], cache_bytes=image["byte_length"],
                                  wall_seconds=metadata["elapsed_crop_and_verification_seconds"])
    if final_plan != derived["plan"]:
        raise ContractError("sealed execution Plan differs from approved intention")
    report_artifact = _one([a for a in bundle["artifacts"] if a["relative_path"] == final + "/validation.json"], "publication validation report")
    report = _json(store, project_id, report_artifact, 1024**2)
    if any(report.get(k) != v for k, v in {"operation": _publication._POLICY, "publish_id": bundle["publish_id"], "status": "passed",
        "source_bundle_id": source["bundle_id"], "source_manifest_sha256": source["manifest_sha256"], "intention_plan_id": plan["plan_id"],
        "job_id": image["job_id"], "request_fulfilled": False, "semantic_read_executed": False, "scope_expanded": False}.items()):
        raise ContractError("derived publication validation receipt mismatch")
    return {"request_sha256": digest(request), "source_bundle_id": source["bundle_id"], "source_publish_id": source["publish_id"],
        "source_manifest_sha256": source["manifest_sha256"], "source_identity": _source_identity(store, source),
        "bundle_id": bundle_id, "publish_id": bundle["publish_id"], "manifest_sha256": derived["manifest_sha256"],
        "source_artifact_id": source_artifact["artifact_id"], "source_artifact_sha256": source_artifact["sha256"],
        "source_block_id": source_block["block_id"], "metadata_artifact_sha256": metadata_artifact["sha256"],
        "artifact_id": image["artifact_id"], "artifact_sha256": image["sha256"], "relative_path": image["relative_path"],
        "byte_length": image["byte_length"], "block_id": derived_block["block_id"], "geometry": schema_geometry,
        "job_id": image["job_id"], "provider": provider, "intention_event_id": intended["event_id"],
        "intention_sha256": digest(payload), "intention_plan_sha256": digest(plan)}


def reconcile_image_region(store, request_id, bundle_id):
    """Fulfil only an exact ROI already sealed by publish_image_region.

    Different genuine queued requests for the same ROI may reconcile the same
    publication. metadata.request describes the FIRST publication request, never
    authorizes the current one. Paused collection blocks new fulfilment; an
    existing fulfilled delivery may be read-only revalidated while paused.
    """
    with _published._fail_closed(), store.transaction():
        check_id(request_id)
        check_id(bundle_id)
        reqrow, request, source = _request(store, request_id)
        if reqrow["state"] not in {"queued", "fulfilled"} or request["requires_scope_expansion"]:
            return {"status": "blocked", "request_id": request_id, "reason": "request_not_queued_or_scope_expansion", "executed": False}
        if store.pipeline_state("collection") != "running" and reqrow["state"] != "fulfilled":
            return {"status": "blocked", "request_id": request_id, "reason": "collection paused", "executed": False}
        _source_identity(store, source)
        proof = _verify(store, reqrow, request, source, bundle_id)
        encoded = canonical(proof).decode()
        _schema(store)
        old = store.connection.execute("SELECT * FROM " + _TABLE + " WHERE request_id=?", (request_id,)).fetchone()
        event_payload = {"delivery_kind": _KIND,
            "task_id": reqrow["task_id"], "source_publish_id": source["publish_id"], "source_bundle_id": source["bundle_id"],
            "bundle_id": bundle_id, "publish_id": proof["publish_id"], "request_sha256": digest(request), "verification_sha256": digest(proof),
            "artifact_id": proof["artifact_id"], "artifact_sha256": proof["artifact_sha256"], "semantic_read_performed": False, "gaps_resolved": False}
        if old:
            if (reqrow["state"] != "fulfilled" or old["state"] != "fulfilled" or old["verification_json"] != encoded
                    or any(old[k] != reqrow[k] for k in ("project_id", "task_id"))
                    or old["bundle_id"] != bundle_id or old["publish_id"] != proof["publish_id"]
                    or old["source_bundle_id"] != source["bundle_id"] or old["source_publish_id"] != source["publish_id"]
                    or old["request_sha256"] != digest(request)):
                raise ContractError("fulfilled delivery immutable request/source/publication provenance changed")
            _, recorded = _event(store, "image_region_delivered", request_id, reqrow["project_id"])
            if recorded != event_payload:
                raise ContractError("fulfilled image-region delivery event provenance changed")
        else:
            if reqrow["state"] != "queued":
                raise ContractError("fulfilled request has no image-region delivery provenance")
            changed = store.connection.execute("UPDATE supplement_requests SET state='fulfilled' WHERE request_id=? AND state='queued' AND project_id=? AND task_id=? AND publish_id=? AND request_json=?",
                (request_id, reqrow["project_id"], reqrow["task_id"], source["publish_id"], reqrow["request_json"])).rowcount
            if changed != 1:
                raise ContractError("image-region request fulfilment CAS failed")
            store.connection.execute("INSERT INTO " + _TABLE + " VALUES(?,?,?,?,?,?,?,?,?,'fulfilled',?)",
                (request_id, reqrow["project_id"], reqrow["task_id"], source["publish_id"], source["bundle_id"],
                 bundle_id, proof["publish_id"], digest(request), encoded, now()))
            store.event("image_region_delivered", request_id, reqrow["project_id"], event_payload)
        return {"status": "fulfilled", "request_id": request_id, "project_id": reqrow["project_id"], "delivery_kind": _KIND,
            "delivery_state": "fulfilled", "source_bundle_id": source["bundle_id"], "bundle_id": bundle_id, "publish_id": proof["publish_id"],
            "artifact_id": proof["artifact_id"], "block_id": proof["block_id"], "relative_path": proof["relative_path"],
            "sha256": proof["artifact_sha256"], "byte_length": proof["byte_length"], "geometry": proof["geometry"],
            "idempotent": old is not None, "new_extraction_executed": False, "new_publication_created": False,
            "semantic_read_performed": False, "gaps_resolved": False}
