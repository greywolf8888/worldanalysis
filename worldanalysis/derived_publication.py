"""Append-only local image-region evidence in Store's ONE publication journal.

No acquisition, semantic reading, request fulfilment, catalog, or extra business
DB.  Artifact has no geometry field in the original schema: a new metadata block
binds the new PNG's current pixel frame and current->original transform.  This is
another view of an existing occurrence, not a newly discovered source occurrence.
The inherited blocks, artifacts, gaps, contacts, jobs and receipts are retained.

A pending, schema-checked intention Plan, zero-cost local job and staging journal
are committed BEFORE crop_region runs.  Interrupted staging is never replayed;
prepared publications recover through Store.recover_publications normally.  The
primitive's loose files/sidecar are NOT evidence until copied, checked and sealed
in a new Bundle.  A request, when supplied, remains queued even after publication.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
from pathlib import Path
import re
import sys
import time

from . import bundle_merge as _published
from . import image_region
from .contracts import (ContractError, VALIDATOR_VERSION, asset_key, canonical,
                        check_id, digest, hash_file, now, reject_links, safe_path,
                        validate_bundle, validate_evidence, validate_plan,
                        validate_relative_path)
from .store import _write, new_id

_POLICY = "local-image-region-publication-v1"
_PROVIDER = "local_image_region"


def _implementation():
    from PIL import __version__
    return {"publication": _POLICY,
            "publication_code_sha256": hash_file(Path(__file__))[0],
            "primitive": image_region._IMPLEMENTATION,
            "primitive_code_sha256": hash_file(Path(image_region.__file__))[0],
            "pillow_version": __version__, "python_version": list(sys.version_info[:3])}


def _source(store, project_id, bundle_id, budget):
    row = _published._admit(store, project_id, [bundle_id], budget)[0]
    return _published._source(store, project_id, row, budget)


def _select(source, block_id, artifact_id):
    bundle = source["bundle"]
    blocks = [b for b in bundle["blocks"] if b["block_id"] == block_id]
    artifacts = [a for a in bundle["artifacts"] if a["artifact_id"] == artifact_id]
    if len(blocks) != 1 or len(artifacts) != 1:
        raise ContractError("unique exact source block/artifact required")
    block, artifact = blocks[0], artifacts[0]
    if (artifact_id not in block["artifact_ids"] or artifact["asset_ref"] != block["asset_ref"]
            or artifact["kind"] not in {"image", "page_render"}
            or artifact["access_class"] != "project_local"
            or block["locator"].get("precision") != "exact"):
        raise ContractError("source block/asset/locator/access/kind is not an admitted sealed image")
    asset = next(a for a in bundle["assets"] if asset_key(a) == asset_key(artifact["asset_ref"]))
    return block, artifact, asset


def _anchor(store, project_id, source, block, artifact, budget):
    """crop_region requires the artifact's owning immutable evidence directory.

    A composed snapshot can inherit a PNG from an earlier publication.  Resolve
    its owner through the SAME journal, and prove identical block/artifact/asset
    before calling the primitive; do not weaken crop_region's path check.
    """
    if artifact["relative_path"].startswith(source["final_relative_path"] + "/"):
        return source
    match = re.match(r"^(证据/v[0-9]{4,})/", artifact["relative_path"])
    owner = store.connection.execute("SELECT bundle_id FROM publications WHERE project_id=? AND state='published' AND final_relative_path=?",
        (project_id, match[1] if match else "")).fetchone()
    if not owner:
        raise ContractError("sealed source image has no owning publication")
    anchor = _source(store, project_id, owner["bundle_id"], budget)
    old_block, old_artifact, _ = _select(anchor, block["block_id"], artifact["artifact_id"])
    if (old_artifact != artifact or _published._rebase(old_block, source["bundle_id"], source["bundle"]["bundle_revision"]) != block):
        raise ContractError("inherited source block/producer/locator changed since owning publication")
    return anchor


def _preflight(store, project_id, artifact, block, bbox, resize, output_relative):
    if (not isinstance(bbox, dict) or set(bbox) != {"unit", "origin", "xyxy"}
            or bbox["unit"] != "pixel" or bbox["origin"] != "top_left"
            or not isinstance(bbox["xyxy"], (list, tuple)) or len(bbox["xyxy"]) != 4):
        raise ContractError("explicit pixel/top_left/xyxy bbox required")
    box = [image_region._integer(v) for v in bbox["xyxy"]]
    left, top, right, bottom = box
    if not (0 <= left < right and 0 <= top < bottom):
        raise ContractError("invalid exact crop bounds")
    if resize is not None and (not isinstance(resize, (list, tuple)) or len(resize) != 2):
        raise ContractError("resize requires width/height")
    size = image_region._dimensions(*(resize if resize is not None else (right - left, bottom - top)))
    validate_relative_path(output_relative)
    if not image_region._OUTPUT.fullmatch(output_relative):
        raise ContractError("output must be project-local 补采派生/<safeid>.png")
    root = store.project_root(project_id)
    if not root.is_absolute() or str(root).startswith(("//", "\\\\")):
        raise ContractError("strict local project root required")
    if type(artifact["byte_length"]) is not int or not 0 < artifact["byte_length"] <= image_region.MAX_SOURCE_BYTES:
        raise ContractError("BLOCKED_RESOURCE: sealed source encoded byte limit")
    path = safe_path(root, artifact["relative_path"])
    data, identity = image_region._read(path, image_region.MAX_SOURCE_BYTES)
    if (hashlib.sha256(data).hexdigest() != artifact["sha256"] or len(data) != artifact["byte_length"]
            or artifact["artifact_revision"] != "sha256-" + artifact["sha256"]):
        raise ContractError("sealed source bitmap hash/revision mismatch")
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        dimensions = image_region._dimensions(*image.size)
        if (image.format not in {"PNG", "JPEG"} or Image.MIME.get(image.format) != artifact["media_type"]
                or getattr(image, "n_frames", 1) != 1 or image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}
                or image.getexif().get(274, 1) != 1 or right > dimensions[0] or bottom > dimensions[1]):
            raise ContractError("unsupported source bitmap format/orientation/dimensions/bounds")
        geometry = image_region._geometry(block["locator"], artifact["artifact_id"], dimensions, box, size, resize is not None)
    return {"unit": "pixel", "origin": "top_left", "xyxy": box}, list(size) if resize is not None else None, geometry, dimensions, identity


def _request(store, request_id, project_id, source, block, artifact, box, dimensions):
    if request_id is None:
        return None
    check_id(request_id)
    # Genuine accepted get_result + original proposal or exact request API event
    # digest; a caller's model fields/queued row alone are not sufficient.
    from .local_supplement import _request as accepted_request
    row, request, original = accepted_request(store, request_id)
    if (row["state"] != "queued" or row["project_id"] != project_id
            or row["publish_id"] != source["publish_id"] or original["bundle_id"] != source["bundle_id"]
            or request["requires_scope_expansion"] or set(request["requested_outputs"]) != {"original_image"}
            or len(request["minimal_targets"]) != 1):
        raise ContractError("request must be same-project/task/publication queued original_image proposal without scope expansion")
    target = request["minimal_targets"][0]
    validate_evidence(target, source["bundle"])
    locator, actual = target["locator"], block["locator"]
    expected_geometry = {"reference_object_ref": artifact["artifact_id"], "coordinate_space": "original",
        "unit": "pixel", "origin": "top_left", "frame_size": {"width": dimensions[0], "height": dimensions[1]},
        "original_size": {"width": dimensions[0], "height": dimensions[1], "unit": "pixel"},
        "rotation_degrees": 0, "bbox_xywh": [box[0], box[1], box[2] - box[0], box[3] - box[1]],
        "transform_to_original": {"method": "identity", "operations": []}}
    if (asset_key(target) != asset_key(artifact["asset_ref"]) or target.get("artifact_id") != artifact["artifact_id"]
            or target.get("block_id", block["block_id"]) != block["block_id"]
            or {k: v for k, v in locator.items() if k != "geometry"} != {k: v for k, v in actual.items() if k != "geometry"}
            or locator.get("geometry") != expected_geometry):
        raise ContractError("request exact source artifact/locator/region bbox mismatch; nothing guessed from reason")
    # For this first publication route request coordinates are native pixel edges,
    # not an unproved inverse of a transformed original-coordinate request.
    if "geometry" in actual:
        g = actual["geometry"]
        if (g["coordinate_space"] != "original" or g["transform_to_original"] != {"method": "identity", "operations": []}
                or g["original_size"] != expected_geometry["original_size"]):
            raise ContractError("request region needs an original pixel identity source")
    return {"request_id": request_id, "task_id": row["task_id"], "publish_id": row["publish_id"],
            "request_sha256": digest(request), "state": "queued"}


def _plan(source, asset, provider, job, block, intention_ref, created):
    plan = deepcopy(source["plan"])
    plan.update(plan_id=new_id("plan"), created_at=created, policy_version=_POLICY,
                program_validation={"status": "pending", "validator_version": VALIDATOR_VERSION,
                                    "checked_at": None, "report_relative_path": None})
    evidence = {"kind": "original_evidence", "record_ref": source["publish_id"], "observed_at": created,
                "source_evidence": {"project_id": source["bundle"]["project_id"], **block["asset_ref"],
                                    "block_id": block["block_id"], "locator": deepcopy(block["locator"])}}
    intention = {"kind": "program_receipt", "record_ref": intention_ref, "observed_at": created}
    observation, permission = new_id("observation"), new_id("permission")
    # Scope comes from a verified sealed local source/Plan, NEVER the request.
    grants = [p for p in plan["permission_checks"] if p["scope_ref"] == asset["asset_id"]
              and p["state"] == "granted" and p["operation"] == "native_extract"]
    if not grants:
        raise ContractError("sealed source has no existing local derivation grant")
    budget = {"policy_ref": _POLICY, "currency": plan["hard_budget"]["currency"], "max_cost_minor": 0,
              "max_input_bytes": image_region.MAX_SOURCE_BYTES, "max_cache_bytes": image_region.MAX_OUTPUT_BYTES * 2 + image_region.MAX_MANIFEST_BYTES,
              "max_ram_bytes": 512 * 1024**2, "max_vram_bytes": 0, "max_wall_seconds": 3600, "max_concurrency": 1}
    for key, value in budget.items():
        if key.startswith("max_"):
            # Historical limits remain evidence; the snapshot records an added
            # bounded local phase, not a claim that old executions cost nothing.
            plan["hard_budget"][key] = max(plan["hard_budget"][key], value)
    plan["capability_observations"].append({"observation_id": observation, "provider": deepcopy(provider),
        "scope": {"source_ref": asset["source_ref"], "asset_ref": deepcopy(block["asset_ref"])},
        "capability": "render_region", "state": "verified_available", "observed_at": created,
        "evidence_refs": [evidence, intention], "limitations": ["PNG/JPEG sealed image/page_render only; integer pixel crop; optional nearest resize; no OCR or semantic read.",
            "Pillow allocation gates are not an OS-enforced RAM/time sandbox."]})
    plan["permission_checks"].append({"check_id": permission, "operation": "native_extract", "scope_ref": asset["asset_id"],
        "state": "granted", "grant_ref": grants[0]["grant_ref"], "checked_at": created,
        "reason": "Existing explicit project-local source grant, bounded local crop only; no acquisition, paid operation, request-derived permission or scope expansion."})
    local_job = {"job_id": job["job_id"], "operation": "native_extract", "tier": "structured_local",
        "reason": "Local image_region.crop_region derivation, NOT whole-asset re-extraction, rendering, OCR or visual interpretation. Store producer contract uses native_extract.",
        "basis_refs": [evidence, intention], "provider": deepcopy(provider),
        "selection": {"mode": "specific_locators", "locators": [deepcopy(block["locator"])], "reason": "Exact source block; pixel bbox/config is durably pinned in derivation intention, not guessed."},
        "capability_observation_ids": [observation], "permission_check_ids": [permission], "budget": budget,
        "estimate": {"state": "estimated", "input_bytes": None, "peak_ram_bytes": None, "peak_vram_bytes": None,
            "cache_bytes": None, "wall_seconds": None, "cost_minor": 0, "basis_refs": [evidence, intention]},
        "side_effect": "local_read_or_derivation", "depends_on_job_ids": [job_id for job_id in dict.fromkeys(
            a["job_id"] for a in source["bundle"]["artifacts"] if a["artifact_id"] in block["artifact_ids"])],
        "fallback_job_ids": [], "stop_conditions": ["permission_denied", "scope_changed", "budget_exhausted", "deterministic_failure", "submission_unknown"],
        "acceptance_checks": ["actual_format", "source_revision_match", "actual_files_readable", "coordinates_mapped", "bytes_within_budget", "program_receipt"],
        "retry_policy": {"max_attempts": 1, "max_route_upgrades": 0, "repeat_deterministic_failure": False}}
    next(p for p in plan["asset_plans"] if asset_key(p["asset_ref"]) == asset_key(asset))["jobs"].append(local_job)
    validate_plan(plan, registered_providers={(j["provider"]["provider_id"], j["provider"]["implementation_version"])
                                             for a in plan["asset_plans"] for j in a["jobs"]})
    return plan


def _verify_crop(store, project_id, anchor, block, artifact, box, resize, output, geometry, dimensions, identity, descriptor):
    """Independent source pins, actual bytes, full pixel comparison and geometry.

    No descriptor/sidecar is promoted to a business artifact; it is checked against
    authoritative inputs then reduced to explicit, freshly constructed metadata.
    """
    source_path = safe_path(store.project_root(project_id), artifact["relative_path"])
    image_region._recheck(source_path, identity, artifact["sha256"])
    expected_ref = {"project_id": project_id, "bundle_id": anchor["bundle_id"],
        "bundle_revision": anchor["bundle"]["bundle_revision"], **block["asset_ref"], "block_id": block["block_id"],
        "artifact_id": artifact["artifact_id"], "locator": block["locator"]}
    pins = {"descriptor_version": image_region._IMPLEMENTATION, "descriptor_kind": "local_image_region_not_bundle_artifact",
        "project_id": project_id, "source_publish_id": anchor["publish_id"], "source_bundle_sha256": digest(anchor["bundle"]),
        "source_ref": expected_ref, "source_artifact_sha256": artifact["sha256"], "source_artifact_revision": artifact["artifact_revision"],
        "source_relative_path": artifact["relative_path"], "source_dimensions": {"width": dimensions[0], "height": dimensions[1]},
        "bbox": box, "resize": resize, "output_relative_path": output, "manifest_relative_path": output + ".manifest.json",
        "media_type": "image/png", "geometry": geometry}
    if not isinstance(descriptor, dict) or any(descriptor.get(k) != v for k, v in pins.items()):
        raise ContractError("primitive descriptor source/config/geometry mismatch")
    data, _ = image_region._read(safe_path(store.project_root(project_id), output), image_region.MAX_OUTPUT_BYTES)
    sha, size = hashlib.sha256(data).hexdigest(), len(data)
    if descriptor.get("new_artifact_sha256") != sha or descriptor.get("byte_length") != size:
        raise ContractError("crop actual bitmap hash/size mismatch")
    from PIL import Image
    with Image.open(source_path) as source, Image.open(io.BytesIO(data)) as actual:
        expected = source.crop(tuple(box["xyxy"]))
        try:
            if expected.mode in {"P", "1"}:
                converted = expected.convert("RGBA" if expected.mode == "P" else "L")
                expected.close()
                expected = converted
            if resize is not None:
                scaled = expected.resize(tuple(resize), resample=Image.Resampling.NEAREST)
                expected.close()
                expected = scaled
            if actual.format != "PNG" or actual.size != expected.size or actual.mode != expected.mode:
                raise ContractError("crop actual PNG dimensions/mode mismatch")
            actual.load()
            if hashlib.sha256(actual.tobytes()).digest() != hashlib.sha256(expected.tobytes()).digest():
                raise ContractError("crop actual pixels do not match exact sealed source region")
        finally:
            expected.close()
    image_region._recheck(source_path, identity, artifact["sha256"])
    return data, sha, size


def _artifact(stage, relative, data, kind, media, ref, job, provider, created):
    path = stage / relative
    _write(path, data)
    sha, size = hash_file(path)
    return {"artifact_id": new_id("artifact"), "artifact_revision": "sha256-" + sha,
            "asset_ref": deepcopy(ref), "job_id": job["job_id"], "provider": deepcopy(provider),
            "kind": kind, "relative_path": relative, "media_type": media, "byte_length": size,
            "sha256": sha, "created_at": created, "access_class": "project_local"}


def _build(store, source, plan, job, provider, journal, stage, png, geometry, metadata, elapsed):
    created, final = now(), journal["final_relative_path"]
    bundle = _published._rebase(deepcopy(source["bundle"]), journal["bundle_id"], f"v{journal['revision']:04d}")
    bundle.update(bundle_id=journal["bundle_id"], bundle_revision=f"v{journal['revision']:04d}", publish_id=journal["publish_id"], created_at=created)
    bundle["publication"] = {"state": "published", "journal_ref": journal["publish_id"], "manifest_relative_path": final + "/manifest.json", "published_at": created}
    receipt = {"status": "passed", "validator_version": VALIDATOR_VERSION, "checked_at": created, "report_relative_path": final + "/validation.json"}
    bundle["program_validation"] = receipt
    ref = metadata["source_artifact"]["asset_ref"]
    def add(name, data, kind="structured_data", media="application/json"):
        item = _artifact(stage, name, data, kind, media, ref, job, provider, created)
        item["relative_path"] = final + "/" + name
        bundle["artifacts"].append(item)
        return item
    image = add("region.png", png, "image", "image/png")
    metadata = deepcopy(metadata)
    metadata.update(published_artifact_id=image["artifact_id"], published_relative_path=image["relative_path"],
                    actual_png={"sha256": image["sha256"], "byte_length": image["byte_length"]},
                    elapsed_crop_and_verification_seconds=elapsed)
    note = add("region.json", canonical(metadata))
    schema_geometry = {k: deepcopy(v) for k, v in geometry.items() if k != "original_polygon_xy"}
    schema_geometry["reference_object_ref"] = image["artifact_id"]
    locator = deepcopy(metadata["source_block"]["locator"])
    locator["geometry"] = schema_geometry
    provenance = {"project_id": bundle["project_id"], **ref, "block_id": metadata["source_block"]["block_id"],
                  "artifact_id": metadata["source_artifact"]["artifact_id"], "locator": deepcopy(metadata["source_block"]["locator"])}
    block = {"block_id": new_id("block"), "asset_ref": deepcopy(ref), "kind": "metadata", "origin": "native_extract",
        "locator": locator, "artifact_ids": [image["artifact_id"], note["artifact_id"]], "producer": deepcopy(provider),
        "processing": {"status": "extracted", "reason": "Exact local crop/geometry metadata materialized. NOT OCR, semantic reading, a new source visual occurrence, or supplement fulfilment.",
                       "evidence_refs": [deepcopy(provenance)], "recovery_ref": job["job_id"]},
        "derived_from": [provenance], "uncertainty": "Only the requested source pixels and coordinate transform are verified; source gaps and semantic state are unchanged."}
    bundle["blocks"].append(block)
    bundle["coverage"]["discovered_counts"]["blocks"] += 1
    coverage = next(c for c in bundle["coverage"]["per_asset"] if asset_key(c["asset_ref"]) == asset_key(ref))
    for metric in coverage["metrics"]:
        if metric["unit"] == "block":
            metric["observed"] += 1
            metric["processed"] += 1
            if metric["expected"] is not None:
                metric["expected"] += 1
    final_plan = _published._rebase(deepcopy(plan), bundle["bundle_id"], bundle["bundle_revision"])
    final_plan.update(plan_id=new_id("plan"), program_validation=receipt)
    planned = next(j for a in final_plan["asset_plans"] for j in a["jobs"] if j["job_id"] == job["job_id"])
    planned["estimate"].update(state="measured", input_bytes=metadata["source_artifact"]["byte_length"],
        cache_bytes=len(png), wall_seconds=elapsed)
    # Reads are repeated for verification. input_bytes is the size of distinct
    # encoded source input, not a fabricated count of every internal I/O read.
    report = {"validator_version": VALIDATOR_VERSION, "publish_id": bundle["publish_id"], "checked_at": created,
        "status": "passed", "operation": _POLICY, "source_bundle_id": source["bundle_id"],
        "source_manifest_sha256": source["manifest_sha256"], "intention_plan_id": plan["plan_id"],
        "job_id": job["job_id"], "request": metadata["request"], "request_fulfilled": False,
        "semantic_read_executed": False, "scope_expanded": False,
        "checks": ["schema", "actual_pixels", "source_identity", "geometry", "producer_plan", "registered_provider", "historical_state_preserved", "files", "publication_journal"],
        "geometry_policy": "Block locator frame references new PNG; matrix maps its pixel edges to original source coordinates. Original source reference/polygon live in hashed region metadata."}
    add("plan.json", canonical(final_plan))
    add("blocks.jsonl", b"".join(canonical(b) + b"\n" for b in bundle["blocks"]), media="application/x-ndjson")
    add("validation.json", canonical(report), "validation_report")
    validate_plan(final_plan)
    def resolver(relative):
        return safe_path(stage, relative[len(final) + 1:]) if relative.startswith(final + "/") else safe_path(store.project_root(bundle["project_id"]), relative)
    validate_bundle(bundle, store.project_root(bundle["project_id"]), path_resolver=resolver)
    return bundle, final_plan


def _result(bundle, request, reused):
    if request is None:
        return bundle
    return {"status": "published", "bundle": bundle, "bundle_id": bundle["bundle_id"],
            "publish_id": bundle["publish_id"], "request_id": request["request_id"],
            "request_state": "queued", "request_remains_queued": True, "reused": reused}


def publish_image_region(store, project_id, source_bundle_id, block_id, artifact_id, bbox, *, output_relative, resize=None, request_id=None):
    """Publish one exact crop as a new original-schema Bundle, or blocked report.

    Source must already be in local authorized scope. request_id is a validated
    proposal, NOT a grant. It is never fulfilled here. Same source snapshot,
    bitmap, locator, crop, resize and actual code/provider configuration reuse
    one publication across requests. output_relative is merely the primitive's
    scratch destination: a reused publication creates no alias at a new path.
    Unknown/prepared executions require existing journal recovery, never recrop.
    """
    with _published._fail_closed():
        for ident in (project_id, source_bundle_id, block_id, artifact_id):
            check_id(ident)
        if store.pipeline_state("collection") != "running":
            return {"status": "blocked", "reason": "collection paused", "project_id": project_id}
        budget = _published._Budget()
        with store.transaction():
            source = _source(store, project_id, source_bundle_id, budget)
            block, artifact, asset = _select(source, block_id, artifact_id)
            anchor = _anchor(store, project_id, source, block, artifact, budget)
            box, resize, geometry, dimensions, identity = _preflight(store, project_id, artifact, block, bbox, resize, output_relative)
            from .local_supplement import _source_identity
            native = _source_identity(store, source)
            request = _request(store, request_id, project_id, source, block, artifact, box["xyxy"], dimensions)
            implementation = _implementation()
            specification = {"project_id": project_id, "source_bundle_id": source_bundle_id,
                "source_manifest_sha256": source["manifest_sha256"], "source_bundle_sha256": digest(source["bundle"]),
                "source_publish_id": source["publish_id"], "source_artifact": artifact, "source_block": block,
                "bbox": box, "resize": resize, "implementation": implementation}
            key = digest([_POLICY, specification])
            previous = store.connection.execute("SELECT * FROM publications WHERE project_id=? AND idempotency_key=?", (project_id, key)).fetchone()
            if previous:
                if previous["state"] != "published":
                    raise ContractError("derivation already prepared/unknown; recover/reconcile publish_id, never automatically recrop: " + previous["publish_id"])
                _published._admit(store, project_id, [previous["bundle_id"]], _published._Budget())
                return _result(store.get_bundle(project_id, previous["bundle_id"]), request, True)
            if store.pipeline_state("collection") != "running":
                return {"status": "blocked", "reason": "collection paused", "project_id": project_id}
            provider = store.register_provider(_PROVIDER, "crop-" + digest(implementation)[:32], config_version="region-" + key[:40])
            _published._provider(store, provider)
            job = store.queue_job(project_id, "native_extract", provider, asset["asset_id"] + ":" + asset["asset_revision"],
                                  config_version=provider["config_version"], estimated_cost_minor=0, max_cost_minor=0)
            if job["state"] != "queued":
                raise ContractError("existing derivation job requires reconciliation, not replay")
            publish_id, bundle_id = new_id("publish"), new_id("bundle")
            revision = store.connection.execute("SELECT COALESCE(MAX(revision),0)+1 FROM publications WHERE project_id=?", (project_id,)).fetchone()[0]
            final, staging = f"证据/v{revision:04d}", "证据/.staging-" + publish_id
            plan = _plan(source, asset, provider, job, block, publish_id, now())
            store.connection.execute("INSERT INTO plans VALUES(?,?,?,?)", (plan["plan_id"], project_id, canonical(plan).decode(), now()))
            store.connection.execute("INSERT INTO publications(publish_id,project_id,bundle_id,revision,idempotency_key,state,staging_relative_path,final_relative_path,created_at) VALUES(?,?,?,?,?,'staging',?,?,?)",
                (publish_id, project_id, bundle_id, revision, key, staging, final, now()))
            store.event("image_region_derivation_intended", publish_id, project_id,
                {"specification": specification, "provider": provider, "job_id": job["job_id"], "intention_plan_id": plan["plan_id"],
                 "intention_plan_sha256": digest(plan), "primitive_output_relative": output_relative, "request": request,
                 "request_is_authorization": False, "cost_minor": 0})
            store.event("publish_registered", publish_id, project_id, {"operation": _POLICY, "source_bundle_id": source_bundle_id})
            changed = store.connection.execute("UPDATE jobs SET state='running',updated_at=? WHERE job_id=? AND state='queued'", (now(), job["job_id"])).rowcount
            if changed != 1:
                raise ContractError("derivation job changed before execution")
            store._check_extraction_job(project_id, artifact["asset_ref"], job["job_id"], provider)
        journal = store.connection.execute("SELECT * FROM publications WHERE publish_id=?", (publish_id,)).fetchone()
        stage = store.project_root(project_id) / staging
        reject_links(stage)
        stage.mkdir(parents=True, exist_ok=False)
        # The publication journal/intention/job are now durably committed. A
        # fault anywhere below leaves a non-published, non-replayable record.
        if store.pipeline_state("collection") != "running":
            return {"status": "blocked", "reason": "collection paused before crop; reconcile prepared publication", "publish_id": publish_id}
        started = time.monotonic()
        descriptor = image_region.crop_region(store, project_id, anchor["bundle_id"], block_id, artifact_id, box, output_relative, resize=resize)
        png, sha, size = _verify_crop(store, project_id, anchor, block, artifact, box, resize, output_relative, geometry, dimensions, identity, descriptor)
        metadata = {"metadata_version": _POLICY, "intention_plan_id": plan["plan_id"], "job_id": job["job_id"],
            "provider": provider, "implementation": implementation, "source_bundle_id": source_bundle_id,
            "source_bundle_sha256": digest(source["bundle"]), "source_manifest_sha256": source["manifest_sha256"],
            "primitive_source_bundle_id": anchor["bundle_id"], "source_block": block, "source_artifact": artifact,
            "bbox": box, "resize": resize, "geometry_to_original": geometry,
            "primitive_output_relative": output_relative, "actual_png": {"sha256": sha, "byte_length": size},
            "request": request, "request_remains_queued": request is not None,
            "semantics": {"new_source_occurrence": False, "semantic_read": False, "ocr": False, "render": False,
                          "network": False, "scope_expanded": False, "gap_resolved": False}}
        bundle, final_plan = _build(store, source, plan, job, provider, journal, stage, png, geometry, metadata, time.monotonic() - started)
        manifest = canonical(bundle)
        if len(manifest) > _published._MAX_MANIFEST_BYTES or len(canonical(final_plan)) > _published._MAX_PLAN_BYTES:
            raise ContractError("derived publication manifest/Plan byte limit")
        _write(stage / "manifest.json", manifest)
        expected = deepcopy(source["expected"])
        expected.update({a["relative_path"]: {"sha256": a["sha256"], "byte_length": a["byte_length"]} for a in bundle["artifacts"]})
        expected[final + "/manifest.json"] = dict(zip(("sha256", "byte_length"), hash_file(stage / "manifest.json")))
        if len(expected) > _published._MAX_FILES:
            raise ContractError("derived publication file count limit")
        with store.transaction():
            if store.pipeline_state("collection") != "running":
                raise ContractError("collection paused before seal; reconcile prepared publication")
            if _source_identity(store, source) != native:
                raise ContractError("native source path/SHA/version/identity changed during derivation")
            _source(store, project_id, source_bundle_id, _published._Budget())
            _request(store, request_id, project_id, source, block, artifact, box["xyxy"], dimensions)
            store._check_extraction_job(project_id, artifact["asset_ref"], job["job_id"], provider)
            store.connection.execute("INSERT INTO plans VALUES(?,?,?,?)", (final_plan["plan_id"], project_id, canonical(final_plan).decode(), now()))
            changed = store.connection.execute("UPDATE publications SET state='validated',manifest_sha256=?,bundle_json=?,expected_files_json=? WHERE publish_id=? AND state='staging' AND bundle_json IS NULL",
                (hash_file(stage / "manifest.json")[0], manifest.decode(), canonical(expected).decode(), publish_id)).rowcount
            if changed != 1:
                raise ContractError("derivation publication changed before seal")
            prepared = store.connection.execute("SELECT * FROM publications WHERE publish_id=?", (publish_id,)).fetchone()
            store._verify_publication(prepared, staging=True)
        # Store owns rename, exact-file check, readonly sealing, published DB +
        # producer receipts in ONE transaction, and restart recovery. Inherited
        # producer receipts are not overwritten by this new publication.
        store._finish_publication(publish_id)
        return _result(store.get_bundle(project_id, bundle_id), request, False)
