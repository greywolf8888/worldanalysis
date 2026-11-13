"""Append one bounded, unheard WAV to the original Plan/Bundle/publication journal.

Original media is stat-registered: its descriptor hash is NOT a media hash.  No
whole-source hash/copy, ASR, listening receipt, network, or new authorization is
performed. Intention Plan/grant/job commit before the cooperative bounded decoder.
Prepared attempts recover through Store, never automatically decode again.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import re
import sys
import time

from . import audio_window as primitive
from . import bundle_merge as published
from . import media
from .contracts import (ContractError, VALIDATOR_VERSION, asset_key, canonical,
                        check_id, digest, hash_file, now, reject_links, safe_path,
                        validate_bundle, validate_evidence, validate_plan,
                        validate_relative_path)
from .media_store import MediaRegistration
from .store import _write, new_id

_POLICY = "local-audio-window-publication-v1"
_PROVIDER = "local_audio_window"
_OUTPUT = re.compile(r"^(?:窗口派生|补采派生)/[A-Za-z0-9][A-Za-z0-9_.-]{0,140}\.wav$")
_MAX_METADATA = 2 * 1024**2
_RAM_RESERVATION = 64 * 1024**2


def _implementation():
    import av
    return {"publication": _POLICY, "publication_code_sha256": hash_file(Path(__file__))[0],
            "primitive_schema": primitive._SCHEMA, "primitive_code_sha256": hash_file(Path(primitive.__file__))[0],
            "media_code_sha256": hash_file(Path(media.__file__))[0], "av_version": av.__version__,
            "av_library_versions": {k: list(v) for k, v in av.library_versions.items()},
            "python_version": list(sys.version_info[:3]), "sample_rate": primitive.SAMPLE_RATE,
            "sample_format": "s16le", "channels": 1, "decoder_threads": 1,
            "protocol_whitelist": "pipe", "format_whitelist": media._FORMATS,
            "read_chunk_bytes": primitive._CHUNK, "cooperative_wall_seconds": primitive._WALL_SECONDS,
            "kernel_resource_isolation": False, "hundred_hour_acceptance": False}


def _one(values, label):
    if len(values) != 1:
        raise ContractError("unique " + label + " required")
    return values[0]


def _source(store, project_id, bundle_id):
    budget = published._Budget()
    return published._source(store, project_id, published._admit(store, project_id, [bundle_id], budget)[0], budget)


def _read(path, limit):
    reject_links(path)
    before = media._identity(path)
    if not 0 < before["size"] <= limit:
        raise ContractError("audio publication file byte bound exceeded")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) != before["size"] or len(data) > limit or media._identity(path) != before:
        raise ContractError("audio publication file changed during bounded read")
    return data


def _json(store, project_id, artifact, *, resolver=None):
    path = resolver(artifact["relative_path"]) if resolver else safe_path(store.project_root(project_id), artifact["relative_path"])
    data = _read(path, _MAX_METADATA)
    if (hashlib.sha256(data).hexdigest(), len(data)) != (artifact["sha256"], artifact["byte_length"]):
        raise ContractError("audio metadata actual hash/bytes mismatch")
    return published._decode(data)


def _window(start_ms, end_ms):
    if (type(start_ms) is not int or type(end_ms) is not int or start_ms < 0
            or not start_ms < end_ms or end_ms - start_ms > 300000):
        raise ContractError("exact half-open integer milliseconds: 0 < duration <= 300000 required")
    # 'time', not 'time_range', is the ORIGINAL schema's locator enum.
    return {"kind": "time", "precision": "exact", "start_ms": start_ms, "end_ms": end_ms}


def _native(store, source, asset_id, asset_revision, locator, *, require_current=True):
    """Re-derive authorization from sealed native media evidence, never strings."""
    bundle, project_id = source["bundle"], source["bundle"]["project_id"]
    asset = _one([a for a in bundle["assets"] if asset_key(a) == (asset_id, asset_revision)], "same-project media asset revision")
    registration = MediaRegistration(store)
    bound = store.connection.execute("SELECT * FROM asset_revisions WHERE asset_id=? AND asset_revision=?", asset_key(asset)).fetchone()
    registered = registration.validate(project_id, asset, bound, require_current=require_current)
    source_row = store.connection.execute("SELECT * FROM sources WHERE source_id=? AND project_id=? AND kind='local_media_stat'", (asset["source_ref"], project_id)).fetchone()
    if not source_row:
        raise ContractError("original registered local media scope missing")
    path, root = Path(source_row["local_path"]), store.root.resolve(strict=True)
    reject_links(path)
    if not path.is_absolute() or not path.resolve(strict=False).is_relative_to(root):
        raise ContractError("registered source outside exact authorized local workspace")
    if require_current:
        current = registration.current_path(project_id, asset)
        _, checked = primitive._source(store.root, current)
        if path != checked or str(checked) != source_row["local_path"]:
            raise ContractError("current registered source path must match exactly")
    snapshot = published._decode(registered["identity_json"])
    worker_identity = {"path": str(path), "size": snapshot["size_bytes"], "mtime_ns": snapshot["mtime_ns"],
                       "device": snapshot["device"], "inode": snapshot["inode"], "method": "local_stat_revision_not_full_content_hash"}
    descriptor = _one([a for a in bundle["artifacts"] if a["kind"] == "inventory"
        and asset_key(a["asset_ref"]) == asset_key(asset) and a["relative_path"] == registered["descriptor_relative_path"]], "media registration descriptor")
    if descriptor["access_class"] != "project_local" or _json(store, project_id, descriptor)["source_content_sha256"] is not None:
        raise ContractError("media registration is not a permitted project-local stat descriptor")
    ap = source["plans"][asset_key(asset)]
    native_job = _one([j for j in ap["jobs"] if j["tier"] == "native" and j["job_id"] == descriptor["job_id"]], "original native media job")
    provider = native_job["provider"]
    job = store._check_extraction_job(project_id, asset, native_job["job_id"], provider, published=True)
    if (provider["runtime"] != "local_cpu" or native_job["side_effect"] != "local_read_or_derivation"
            or any(job[k] != 0 for k in ("estimated_cost_minor", "max_cost_minor", "reserved_cost_minor"))
            or native_job["budget"]["max_cost_minor"] != 0 or native_job["estimate"]["cost_minor"] != 0):
        raise ContractError("zero-cost authorized local native media job required")
    permissions = {p["check_id"]: p for p in source["plan"]["permission_checks"]}
    grants = [permissions[i] for i in native_job["permission_check_ids"]]
    if not grants or any(p["state"] != "granted" or p["operation"] != "native_extract"
                            or p["scope_ref"] != asset_id or not p["grant_ref"] for p in grants):
        raise ContractError("original native media permission/grant missing")
    admission = store.connection.execute("SELECT project_id,asset_id,asset_revision,state,reserved_bytes,bundle_id,length(CAST(raw_json AS BLOB)) AS raw_bytes,length(CAST(limits_json AS BLOB)) AS limits_bytes FROM media_executions WHERE job_id=?", (job["job_id"],)).fetchone()
    if (not admission or admission["project_id"] != project_id or asset_key(admission) != asset_key(asset)
            or admission["state"] not in {"complete", "paused", "stopped"} or admission["reserved_bytes"] != 0
            or not admission["raw_bytes"] or admission["raw_bytes"] > published._MAX_SOURCE_JSON
            or not admission["limits_bytes"] or admission["limits_bytes"] > 65536):
        raise ContractError("bounded published verified media execution required")
    execution = store.connection.execute("SELECT * FROM media_executions WHERE job_id=?", (job["job_id"],)).fetchone()
    receipt = published._decode(job["receipt_json"] or "{}")
    owner = store.connection.execute("SELECT bundle_id FROM publications WHERE publish_id=? AND project_id=? AND state='published'", (receipt.get("publish_id"), project_id)).fetchone()
    if not owner or execution["bundle_id"] != owner["bundle_id"]:
        raise ContractError("media execution/native published receipt binding mismatch")
    raw = published._decode(execution["raw_json"])
    metadata_artifact = _one([a for a in bundle["artifacts"] if a["job_id"] == job["job_id"]
        and a["kind"] == "structured_data" and a["relative_path"].endswith("/metadata/" + asset_id + ".json")], "sealed native media metadata")
    metadata = _json(store, project_id, metadata_artifact)
    if (metadata != raw["metadata"] or raw["media_type"] != asset["media_type"]
            or raw["provider_id"] != provider["provider_id"] or raw["provider_version"] != provider["implementation_version"]
            or metadata.get("source_registration") != {"basis": "local_stat_snapshot_not_full_content_hash", "source_ref": asset["source_ref"], "source_content_sha256": None, "whole_source_copy_retained": False}
            or metadata.get("source_identity") != worker_identity
            or metadata.get("input_bytes_received") != execution["received_bytes"]):
        raise ContractError("native media metadata/registration/source/job mismatch")
    probe = metadata["probe"]
    audio = next((s for s in probe["streams"] if s["type"] == "audio"), None)
    if not audio:
        raise ContractError("native media has no verified audio stream")
    duration = audio.get("duration_ms")
    offset = audio.get("start_ms", 0) - probe.get("timeline_origin_ms", 0)
    if type(duration) is not int or duration <= 0:
        duration, offset = probe.get("duration_ms"), 0
    if (type(duration) is not int or duration <= 0 or type(offset) is not int
            or locator["start_ms"] < max(0, offset) or locator["end_ms"] > offset + duration):
        raise ContractError("requested exact window outside verified source audio duration")
    # An inventory's decoded PCM digest is not stored audio, a media hash, or a
    # listening receipt. Require its actual sealed descriptor + raw job mapping.
    witnesses = []
    raw_files = {f["key"]: f for f in raw["files"]}
    for block in bundle["blocks"]:
        if (asset_key(block["asset_ref"]) != asset_key(asset) or block["kind"] != "audio_segment"
                or block["origin"] != "native_extract" or block["processing"]["recovery_ref"] != job["job_id"]
                or block["locator"].get("kind") != "time" or block["locator"].get("precision") != "exact"):
            continue
        for item in bundle["artifacts"]:
            if item["artifact_id"] not in block["artifact_ids"] or item["job_id"] != job["job_id"] or item["kind"] != "inventory" or item["media_type"] != "application/json" or item["access_class"] != "project_local":
                continue
            pcm = _json(store, project_id, item)
            lo, hi = block["locator"]["start_ms"], block["locator"]["end_ms"]
            # Historical media.emit rounds BOTH source PCM endpoints to ms.
            # At 16 kHz/s16 mono this can differ by at most 32 bytes. This is
            # only old inventory admission; new WAV sample proofs stay exact.
            pcm_bytes = pcm.get("pcm_bytes")
            if (pcm.get("start_ms") != lo or pcm.get("end_ms") != hi or pcm.get("sample_rate") != 16000
                    or pcm.get("sample_format") != "s16le_mono" or type(pcm_bytes) is not int
                    or pcm_bytes <= 0 or pcm_bytes % 2 or abs(pcm_bytes - (hi - lo) * 32) > 32
                    or pcm.get("source_is_local_original") is not True or not re.fullmatch(r"[a-f0-9]{64}", str(pcm.get("pcm_sha256", "")))):
                continue
            matching = [b for b in raw["blocks"] if b["kind"] == "audio_segment" and b["origin"] == "native_extract"
                and b["locator"] == block["locator"] and any(k in raw_files and raw_files[k].get("sha256") == item["sha256"]
                    and raw_files[k].get("byte_length") == item["byte_length"] for k in b["file_keys"])]
            if not matching:
                raise ContractError("native PCM source descriptor lacks raw media job binding")
            if lo < locator["end_ms"] and hi > locator["start_ms"]:
                witnesses.append({"block": deepcopy(block), "artifact": deepcopy(item), "descriptor": pcm})
    if not witnesses:
        raise ContractError("source asset lacks corresponding published native_pcm_source window")
    versions = store.connection.execute("SELECT r.* FROM source_revisions r JOIN source_assets sa ON sa.source_id=r.source_id AND sa.source_revision=r.revision WHERE sa.source_id=? AND sa.asset_id=? AND sa.asset_revision=? ORDER BY r.revision", (asset["source_ref"], *asset_key(asset))).fetchall()
    if not versions or any(r["sha256"] != registered["descriptor_sha256"] or r["size_bytes"] != snapshot["size_bytes"] for r in versions):
        raise ContractError("original media source revision ledger mismatch")
    source_identity = {"source": dict(source_row), "asset": dict(bound), "versions": [dict(r) for r in versions],
                       "registered_stat": snapshot, "basis": "descriptor_and_registration_not_original_content_sha"}
    return {"asset": deepcopy(asset), "source_identity": source_identity, "source_stat_fingerprint": worker_identity,
            "registration": dict(registered), "source_local_path": str(path), "root": str(root),
            "native_job_id": job["job_id"], "native_provider": provider, "grant_refs": [p["grant_ref"] for p in grants],
            "native_execution": {k: execution[k] for k in ("job_id", "state", "bundle_id", "received_bytes", "limits_json")},
            "native_raw_sha256": digest(raw), "native_metadata_artifact": metadata_artifact,
            "registration_artifact": descriptor, "native_pcm_sources": witnesses,
            "audio_stream_index": audio["index"], "audio_start_ms": offset, "audio_end_ms": offset + duration,
            "source_content_sha256": None, "strict_media_history_reuse_supported": False}


def _request(store, request_id, project_id, source, native, locator, *, states=("queued",)):
    if request_id is None:
        return None
    check_id(request_id)
    from .local_supplement import _request as accepted
    row, request, original = accepted(store, request_id)
    if (row["state"] not in states or row["project_id"] != project_id or row["publish_id"] != source["publish_id"]
            or original["bundle_id"] != source["bundle_id"] or request["requires_scope_expansion"]
            or request["requested_outputs"] != ["audio_window"] or len(request["minimal_targets"]) != 1):
        raise ContractError("same-source queued single audio_window Analysis proposal required")
    target = request["minimal_targets"][0]
    validate_evidence(target, source["bundle"])
    # No reason parsing, coercion, block-locator substitutions, or geometry.
    if ("block_id" in target or "artifact_id" in target or asset_key(target) != asset_key(native["asset"])
            or target["locator"] != locator or any(type(target["locator"].get(k)) is not int for k in ("start_ms", "end_ms"))):
        raise ContractError("audio request exact asset-only half-open time locator mismatch")
    return {"request_id": request_id, "task_id": row["task_id"], "publish_id": row["publish_id"], "request_sha256": digest(request)}


def _budget(source, max_received_bytes=None):
    hard = source["plan"]["hard_budget"]
    spent = sum(j["estimate"]["input_bytes"] or 0 for a in source["plan"]["asset_plans"] for j in a["jobs"])
    received = min(primitive.DEFAULT_MAX_RECEIVED_BYTES, hard["max_input_bytes"] - spent)
    if max_received_bytes is not None:
        if type(max_received_bytes) is not int or not 0 < max_received_bytes <= received:
            raise ContractError("explicit max_received_bytes must be a positive integer within original remaining Plan/window budget")
        received = max_received_bytes
    cache = min(hard["max_cache_bytes"], 64 * 1024**2)
    if (hard["max_cost_minor"] != 0 or received <= 0 or hard["max_ram_bytes"] < _RAM_RESERVATION
            or cache < primitive.DEFAULT_MAX_OUTPUT_BYTES * 3 + _MAX_METADATA
            or hard["max_wall_seconds"] < primitive._WALL_SECONDS):
        raise ContractError("BLOCKED_RESOURCE: original Plan has insufficient zero-cost local audio budget")
    return {"policy_ref": _POLICY, "currency": hard["currency"], "max_cost_minor": 0,
            "max_input_bytes": received, "max_cache_bytes": cache, "max_ram_bytes": _RAM_RESERVATION,
            "max_vram_bytes": 0, "max_wall_seconds": primitive._WALL_SECONDS, "max_concurrency": 1}


def _spec(source, native, locator, implementation, budget):
    return {"project_id": source["bundle"]["project_id"], "source_bundle_id": source["bundle_id"],
            "source_publish_id": source["publish_id"], "source_bundle_sha256": digest(source["bundle"]),
            "source_manifest_sha256": source["manifest_sha256"], "native": native,
            "locator": locator, "implementation": implementation, "budget": budget}


def _plan(source, native, locator, provider, job, publish_id, budget, *, created=None, ids=None):
    plan, created = deepcopy(source["plan"]), created or now()
    plan.update(plan_id=new_id("plan"), created_at=created, policy_version=_POLICY,
        program_validation={"status": "pending", "validator_version": VALIDATOR_VERSION, "checked_at": None, "report_relative_path": None})
    asset = native["asset"]
    ref = {k: asset[k] for k in ("asset_id", "asset_revision")}
    evidence = {"kind": "original_evidence", "record_ref": source["publish_id"], "observed_at": created,
                "source_evidence": {"project_id": plan["project_id"], **ref, "locator": deepcopy(locator)}}
    intention = {"kind": "program_receipt", "record_ref": publish_id, "observed_at": created}
    observation, permission = ids or (new_id("observation"), new_id("permission"))
    plan["capability_observations"].append({"observation_id": observation, "provider": provider,
        "scope": {"source_ref": asset["source_ref"], "asset_ref": ref}, "capability": "seek", "state": "documented",
        "observed_at": created, "evidence_refs": [evidence, intention],
        "limitations": ["One <=300s local timestamped PCM window; source stat only, no whole-media SHA or strict historical reuse.",
                        "Bounded chunks/cooperative deadline, not kernel RAM/time isolation or hundred-hour acceptance; not ASR/listening.",
                        "received_bytes counts actual mixed-container probe/seek rereads, not only_audio_transferred."]})
    plan["permission_checks"].append({"check_id": permission, "operation": "native_extract", "scope_ref": asset["asset_id"],
        "state": "granted", "grant_ref": native["grant_refs"][0], "checked_at": created,
        "reason": "Existing sealed original local media grant; exact stat/scope checked. Request/model/source strings grant nothing; paid/network operations disabled."})
    next(a for a in plan["asset_plans"] if asset_key(a["asset_ref"]) == asset_key(asset))["jobs"].append({
        "job_id": job["job_id"], "operation": "native_extract", "tier": "structured_local",
        "reason": "Bounded audio_window.extract_audio_window native container derivation; no ASR, listening, whole-source hash or copy.",
        "basis_refs": [evidence, intention], "provider": provider,
        "selection": {"mode": "specific_locators", "locators": [deepcopy(locator)], "reason": "Explicit half-open source-global milliseconds, not reason-derived."},
        "capability_observation_ids": [observation], "permission_check_ids": [permission], "budget": deepcopy(budget),
        "estimate": {"state": "estimated", "input_bytes": None, "peak_ram_bytes": None, "peak_vram_bytes": None,
                     "cache_bytes": None, "wall_seconds": None, "cost_minor": 0, "basis_refs": [evidence, intention]},
        "side_effect": "local_read_or_derivation", "depends_on_job_ids": [native["native_job_id"]], "fallback_job_ids": [],
        "stop_conditions": ["permission_denied", "scope_changed", "budget_exhausted", "deterministic_failure", "submission_unknown"],
        "acceptance_checks": ["actual_format", "source_revision_match", "actual_files_readable", "time_alignment", "bytes_within_budget", "program_receipt"],
        "retry_policy": {"max_attempts": 1, "max_route_upgrades": 0, "repeat_deterministic_failure": False}})
    validate_plan(plan)
    return plan


def _proof(proof, native, locator, output_relative, budget, wav):
    request = {"root": native["root"], "source_relative": Path(native["source_local_path"]).relative_to(native["root"]).as_posix(),
               "output_relative": output_relative, "start_ms": locator["start_ms"], "end_ms": locator["end_ms"],
               "expected_identity": published._decode(native["registration"]["identity_json"]),
               "max_received_bytes": budget["max_input_bytes"], "max_output_bytes": primitive.DEFAULT_MAX_OUTPUT_BYTES}
    primitive._verify(proof, request, wav)
    if (proof.get("coverage_method") != "contiguous_timestamped_samples_half_open_no_gap_fill"
            or proof.get("received_bytes_scope") != "actual_container_bytes_including_probe_seek_rereads_and_mixed_streams"
            or proof.get("sha256_scope") != "derived_window_wav_bytes_only_not_original_media"
            or proof.get("actual_offset_basis") != "decoded_frame_pts_minus_container_origin"
            or proof.get("audio_stream_index") != native["audio_stream_index"]
            or proof.get("duration_ms") != locator["end_ms"] - locator["start_ms"]
            or proof.get("source_stat_fingerprint") != native["source_stat_fingerprint"]
            or any(proof.get(k) is not False for k in ("vad_applied", "asr_executed", "real_course_acceptance"))
            or type(proof.get("actual_first_decoded_offset_ms")) not in (int, float)
            or not locator["start_ms"] - 2000 <= proof["actual_first_decoded_offset_ms"] < locator["end_ms"]
            or any(type(proof.get(k)) is not int or proof[k] < 0 for k in ("read_calls", "seek_calls", "max_read_bytes", "peak_frame_bytes", "decoded_frames"))
            or proof["max_read_bytes"] > primitive._CHUNK or proof["peak_frame_bytes"] > primitive._CHUNK
            or not 0 < proof["decoded_frames"] <= primitive._MAX_PACKETS):
        raise ContractError("actual timestamp/bytes/rate/half-open audio proof mismatch")
    return proof


def _coverage(original, ref, locator, gap_id):
    coverage = deepcopy(original["coverage"])
    coverage["discovered_counts"]["blocks"] += 1
    own = next(a for a in coverage["per_asset"] if asset_key(a["asset_ref"]) == asset_key(ref))
    for metric in own["metrics"]:
        if metric["unit"] == "block":
            metric["observed"] += 1
            metric["processed"] += 1
            if metric["expected"] is not None:
                metric["expected"] += 1
    own["gap_ids"].append(gap_id)
    # Separate native-materialization metric: NEVER upgrade previous PCM/ASR or
    # full-source time ranges. No semantic/listening coverage is asserted.
    length = locator["end_ms"] - locator["start_ms"]
    own["metrics"].append({"unit": "millisecond", "expected": length, "observed": length,
        "processed": length, "excluded": 0, "method": "explicit_ranges", "ranges": [deepcopy(locator)]})
    return coverage


def _build(store, source, plan, job, provider, journal, stage, wav, metadata, elapsed):
    from .derived_publication import _artifact
    created, final = now(), journal["final_relative_path"]
    bundle = published._rebase(deepcopy(source["bundle"]), journal["bundle_id"], f"v{journal['revision']:04d}")
    bundle.update(bundle_id=journal["bundle_id"], bundle_revision=f"v{journal['revision']:04d}", publish_id=journal["publish_id"], created_at=created)
    bundle["publication"] = {"state": "published", "journal_ref": journal["publish_id"], "manifest_relative_path": final + "/manifest.json", "published_at": created}
    receipt = {"status": "passed", "validator_version": VALIDATOR_VERSION, "checked_at": created, "report_relative_path": final + "/validation.json"}
    bundle["program_validation"] = receipt
    ref = {k: metadata["specification"]["native"]["asset"][k] for k in ("asset_id", "asset_revision")}
    def add(name, data, kind="structured_data", media_type="application/json"):
        artifact = _artifact(stage, name, data, kind, media_type, ref, job, provider, created)
        artifact["relative_path"] = final + "/" + name
        bundle["artifacts"].append(artifact)
        return artifact
    clip = add("window.wav", wav, "clip", "audio/wav")
    metadata = deepcopy(metadata)
    metadata.update(published_artifact_id=clip["artifact_id"], published_relative_path=clip["relative_path"],
        actual_wav={"sha256": clip["sha256"], "byte_length": clip["byte_length"]}, elapsed_seconds=elapsed)
    note = add("audio-window.json", canonical(metadata))
    locator, native = metadata["specification"]["locator"], metadata["specification"]["native"]
    provenance = [{"project_id": bundle["project_id"], **ref, "locator": deepcopy(locator)}]
    for witness in native["native_pcm_sources"]:
        provenance.append({"project_id": bundle["project_id"], **ref, "block_id": witness["block"]["block_id"],
            "artifact_id": witness["artifact"]["artifact_id"], "locator": deepcopy(witness["block"]["locator"])})
    block = {"block_id": new_id("block"), "asset_ref": ref, "kind": "audio_segment", "origin": "native_extract",
        "locator": deepcopy(locator), "artifact_ids": [clip["artifact_id"], note["artifact_id"]], "producer": deepcopy(provider),
        "processing": {"status": "extracted", "reason": "Exact native container PCM WAV materialized; UNHEARD. Not ASR/text, semantic reading, full-media SHA or request fulfilment.",
                       "evidence_refs": deepcopy(provenance), "recovery_ref": job["job_id"]},
        "derived_from": provenance, "uncertainty": "Unheard native audio; WAV SHA covers derived bytes only. Original media stat/SHA limitation and every prior gap remain."}
    bundle["blocks"].append(block)
    gap = {"gap_id": new_id("gap"), "kind": "unprocessed", "severity": "important", "status": "open",
        "reason": "New native audio window is unheard; extraction/delivery proves neither semantic listening nor original whole-media content SHA.",
        "target_refs": [{"project_id": bundle["project_id"], **ref, "block_id": block["block_id"], "locator": deepcopy(locator)}], "resolution_refs": []}
    bundle["gaps"].append(gap)
    bundle["coverage"] = _coverage(source["bundle"], ref, locator, gap["gap_id"])
    final_plan = published._rebase(deepcopy(plan), bundle["bundle_id"], bundle["bundle_revision"])
    final_plan.update(plan_id=new_id("plan"), program_validation=receipt)
    local_job = next(j for a in final_plan["asset_plans"] for j in a["jobs"] if j["job_id"] == job["job_id"])
    local_job["estimate"].update(state="measured", input_bytes=metadata["proof"]["received_bytes"],
        cache_bytes=len(wav) + len(canonical(metadata)), wall_seconds=elapsed)
    report = {"status": "passed", "validator_version": VALIDATOR_VERSION, "operation": _POLICY, "publish_id": bundle["publish_id"],
        "source_bundle_id": source["bundle_id"], "source_manifest_sha256": source["manifest_sha256"], "intention_plan_id": plan["plan_id"],
        "job_id": job["job_id"], "checked_at": created, "request_fulfilled": False, "semantic_read_executed": False,
        "source_content_sha256": None, "strict_media_history_reuse_supported": False, "gaps_resolved": False,
        "coverage_scope": "new native PCM materialization only; not heard/ASR/full-source coverage",
        "checks": ["schema", "source_registration_exact_stat", "native_media_execution", "original_grant", "actual_wav_sha_bytes_rate", "timestamped_half_open_proof", "intention_plan_job", "all_prior_state_preserved"]}
    add("plan.json", canonical(final_plan))
    add("blocks.jsonl", b"".join(canonical(b) + b"\n" for b in bundle["blocks"]), media_type="application/x-ndjson")
    add("validation.json", canonical(report), "validation_report")
    validate_plan(final_plan)
    def resolver(relative):
        return safe_path(stage, relative[len(final) + 1:]) if relative.startswith(final + "/") else safe_path(store.project_root(bundle["project_id"]), relative)
    validate_bundle(bundle, store.project_root(bundle["project_id"]), path_resolver=resolver)
    if sum(p.stat().st_size for p in stage.rglob("*") if p.is_file()) + len(canonical(bundle)) > metadata["specification"]["budget"]["max_cache_bytes"]:
        raise ContractError("BLOCKED_RESOURCE: new audio evidence cache budget exceeded")
    return bundle, final_plan


def _event(store, kind, record_id, project_id):
    row = _one(store.connection.execute("SELECT * FROM events WHERE kind=? AND record_id=?", (kind, record_id)).fetchall(), kind + " provenance event")
    if row["project_id"] != project_id:
        raise ContractError("audio provenance event project mismatch")
    return row, published._decode(row["payload_json"])


def verify_audio_window_publication(store, row, bundle, *, staging=False):
    """Store._verify_publication hook for prepared/renamed/published audio bundles.

    Call only for artifacts owned by this publication and local_audio_window.
    Bounded actual bytes, original authorization/registration, intentions and
    preservation are verified even on recovery. Prepared/renamed records require
    current source stat; published historical reads do NOT stat/hash the media.
    Does not decode or write any file/DB state.
    """
    project_id, final = row["project_id"], row["final_relative_path"]
    base = row["staging_relative_path"] if staging else final
    def resolver(relative):
        return safe_path(store.project_root(project_id), base + relative[len(final):]) if relative.startswith(final + "/") else safe_path(store.project_root(project_id), relative)
    note = _one([a for a in bundle["artifacts"] if a["relative_path"] == final + "/audio-window.json" and a["kind"] == "structured_data"
                 and a["media_type"] == "application/json" and a["access_class"] == "project_local"], "sealed audio window metadata")
    metadata = _json(store, project_id, note, resolver=resolver)
    specification = metadata["specification"]
    source = _source(store, project_id, specification["source_bundle_id"])
    locator = specification["locator"]
    if locator != _window(locator["start_ms"], locator["end_ms"]):
        raise ContractError("audio exact time locator mismatch")
    old_native = specification["native"]
    native = _native(store, source, *asset_key(old_native["asset"]), locator, require_current=row["state"] != "published")
    budget = _budget(source, specification["budget"]["max_input_bytes"])
    if (metadata["metadata_version"] != _POLICY or specification != _spec(source, native, locator, _implementation(), budget)
            or metadata["source_content_sha256"] is not None or metadata["semantic_read_performed"] is not False):
        raise ContractError("audio source/version/configuration/stat specification mismatch")
    key = digest([_POLICY, specification])
    provider = metadata["provider"]
    if (row["idempotency_key"] != key or provider["provider_id"] != _PROVIDER or provider["runtime"] != "local_cpu"
            or provider["implementation_version"] != "window-" + digest(specification["implementation"])[:32]
            or provider["config_version"] != "audio-" + key[:40]):
        raise ContractError("audio provider/configuration/journal mismatch")
    published._provider(store, provider)
    wav = _one([a for a in bundle["artifacts"] if a["artifact_id"] == metadata["published_artifact_id"]
        and a["relative_path"] == final + "/window.wav" and a["kind"] == "clip" and a["media_type"] == "audio/wav" and a["access_class"] == "project_local"], "sealed WAV artifact")
    if (metadata["actual_wav"] != {"sha256": wav["sha256"], "byte_length": wav["byte_length"]}
            or metadata["published_relative_path"] != wav["relative_path"] or wav["artifact_revision"] != "sha256-" + wav["sha256"]):
        raise ContractError("audio published WAV identity mismatch")
    _proof(metadata["proof"], native, locator, metadata["primitive_output_relative"], budget, resolver(wav["relative_path"]))
    if (wav["sha256"], wav["byte_length"]) != (metadata["proof"]["sha256"], metadata["proof"]["byte_length"]):
        raise ContractError("audio WAV and half-open proof mismatch")
    event, intention = _event(store, "audio_window_derivation_intended", bundle["publish_id"], project_id)
    _, registered = _event(store, "publish_registered", bundle["publish_id"], project_id)
    if (intention["specification"] != specification or intention["provider"] != provider or intention["job_id"] != wav["job_id"]
            or intention["request"] != metadata["request"] or intention["request_is_authorization"] is not False
            or intention["cost_minor"] != 0 or intention["intention_plan_id"] != metadata["intention_plan_id"]
            or intention["primitive_output_relative"] != metadata["primitive_output_relative"]
            or registered != {"operation": _POLICY, "source_bundle_id": source["bundle_id"]}):
        raise ContractError("audio durable intention/event mismatch")
    if metadata["request"] is not None:
        expected_request = _request(store, metadata["request"]["request_id"], project_id, source, native, locator, states=("queued", "fulfilled"))
        if metadata["request"] != expected_request:
            raise ContractError("audio original publication proposal provenance mismatch")
    planrow = store.connection.execute("SELECT plan_json FROM plans WHERE plan_id=? AND project_id=?", (metadata["intention_plan_id"], project_id)).fetchone()
    if not planrow:
        raise ContractError("audio durable intention Plan missing")
    plan = published._decode(planrow[0])
    validate_plan(plan)
    if digest(plan) != intention["intention_plan_sha256"] or plan["program_validation"]["status"] != "pending":
        raise ContractError("audio intention Plan digest/state mismatch")
    expected_plan = _plan(source, native, locator, provider, {"job_id": wav["job_id"]}, bundle["publish_id"], budget,
        created=plan["created_at"], ids=(plan["capability_observations"][-1]["observation_id"], plan["permission_checks"][-1]["check_id"]))
    expected_plan["plan_id"] = plan["plan_id"]
    if expected_plan != plan:
        raise ContractError("audio intention Plan differs from bounded original-grant policy")
    plan_artifact = _one([a for a in bundle["artifacts"] if a["relative_path"] == final + "/plan.json"], "audio execution Plan")
    final_plan = _json(store, project_id, plan_artifact, resolver=resolver)
    expected_plan = published._rebase(deepcopy(plan), bundle["bundle_id"], bundle["bundle_revision"])
    expected_plan.update(plan_id=final_plan["plan_id"], program_validation=bundle["program_validation"])
    planned = next(j for a in expected_plan["asset_plans"] for j in a["jobs"] if j["job_id"] == wav["job_id"])
    planned["estimate"].update(state="measured", input_bytes=metadata["proof"]["received_bytes"],
        cache_bytes=wav["byte_length"] + len(canonical(metadata)), wall_seconds=metadata["elapsed_seconds"])
    if final_plan != expected_plan:
        raise ContractError("audio final measured Plan differs from intention")
    execution = store._check_extraction_job(project_id, native["asset"], wav["job_id"], provider, published=row["state"] == "published")
    if (any(execution[k] != 0 for k in ("estimated_cost_minor", "max_cost_minor", "reserved_cost_minor"))
            or row["state"] == "published" and published._decode(execution["receipt_json"] or "{}").get("publish_id") != bundle["publish_id"]):
        raise ContractError("audio execution receipt/cost mismatch")
    added = bundle["artifacts"][len(source["bundle"]["artifacts"]):]
    if (len(added) != 5 or any(a["job_id"] != wav["job_id"] or a["provider"] != provider or asset_key(a["asset_ref"]) != asset_key(native["asset"]) for a in added)):
        raise ContractError("audio new artifact producer/asset set mismatch")
    block = _one([b for b in bundle["blocks"] if wav["artifact_id"] in b["artifact_ids"] and note["artifact_id"] in b["artifact_ids"]], "unheard audio block")
    provenance = [{"project_id": project_id, **block["asset_ref"], "locator": locator}] + [{"project_id": project_id, **block["asset_ref"], "block_id": w["block"]["block_id"], "artifact_id": w["artifact"]["artifact_id"], "locator": w["block"]["locator"]} for w in native["native_pcm_sources"]]
    if (block["kind"] != "audio_segment" or block["origin"] != "native_extract" or block["locator"] != locator
            or "text" in block or block["producer"] != provider or block["artifact_ids"] != [wav["artifact_id"], note["artifact_id"]]
            or block["derived_from"] != provenance or block["processing"]["evidence_refs"] != provenance
            or block["processing"]["status"] != "extracted" or block["processing"]["recovery_ref"] != wav["job_id"]
            or "UNHEARD" not in block["processing"]["reason"]):
        raise ContractError("audio native unheard block/provenance mismatch")
    def rebase(value):
        return published._rebase(deepcopy(value), bundle["bundle_id"], bundle["bundle_revision"])
    old = source["bundle"]
    if (bundle["assets"] != old["assets"] or bundle["blocks"] != rebase(old["blocks"]) + [block]
            or bundle["artifacts"][:len(old["artifacts"])] != old["artifacts"] or bundle["contacts"] != rebase(old["contacts"])
            or bundle["gaps"][:-1] != rebase(old["gaps"]) or len(bundle["gaps"]) != len(old["gaps"]) + 1
            or bundle["source_catalog_revision"] != old["source_catalog_revision"]):
        raise ContractError("audio append changed original evidence/contacts/gap states")
    gap = bundle["gaps"][-1]
    if (gap["kind"] != "unprocessed" or gap["status"] != "open" or gap["resolution_refs"]
            or gap["target_refs"] != [{"project_id": project_id, **block["asset_ref"], "block_id": block["block_id"], "locator": locator}]
            or bundle["coverage"] != _coverage(old, block["asset_ref"], locator, gap["gap_id"])):
        raise ContractError("audio unheard gap/native-only coverage mismatch")
    report = _json(store, project_id, _one([a for a in added if a["relative_path"] == final + "/validation.json" and a["kind"] == "validation_report"], "audio validation report"), resolver=resolver)
    if any(report.get(k) != v for k, v in {"status": "passed", "operation": _POLICY, "publish_id": bundle["publish_id"], "source_bundle_id": source["bundle_id"],
        "source_manifest_sha256": source["manifest_sha256"], "intention_plan_id": plan["plan_id"], "job_id": wav["job_id"],
        "request_fulfilled": False, "semantic_read_executed": False, "source_content_sha256": None,
        "strict_media_history_reuse_supported": False, "gaps_resolved": False}.items()):
        raise ContractError("audio validation receipt mismatch")
    return {"source_bundle_id": source["bundle_id"], "source_publish_id": source["publish_id"], "source_manifest_sha256": source["manifest_sha256"],
        "source_identity": native["source_identity"], "bundle_id": bundle["bundle_id"], "publish_id": bundle["publish_id"],
        "manifest_sha256": row["manifest_sha256"], "artifact_id": wav["artifact_id"], "artifact_sha256": wav["sha256"],
        "relative_path": wav["relative_path"], "byte_length": wav["byte_length"], "block_id": block["block_id"], "locator": locator,
        "metadata_artifact_sha256": note["sha256"], "proof_sha256": digest(metadata["proof"]), "received_bytes": metadata["proof"]["received_bytes"],
        "job_id": wav["job_id"], "provider": provider, "intention_event_id": event["event_id"], "intention_plan_sha256": digest(plan),
        "source_content_sha256": None, "semantic_read_performed": False, "gaps_resolved": False}


def _result(bundle, request, reused):
    if request is None:
        return bundle
    return {"status": "published", "bundle": bundle, "bundle_id": bundle["bundle_id"], "publish_id": bundle["publish_id"],
            "request_id": request["request_id"], "request_state": "queued", "request_remains_queued": True, "reused": reused}


def publish_audio_window(store, project_id, source_bundle_id, asset_id, asset_revision, start_ms, end_ms, *, output_relative, request_id=None, max_received_bytes=None):
    """Publish one exact <=300s unheard window. output_relative is store.root-relative.

    Request remains queued. Repeats of the same source/config/window, including
    different genuine queued requests, reuse the publication; no output alias is
    created. Explicit max_received_bytes is part of the sealed budget/key; an
    existing primitive receipt must match exactly, never silently adopt its cap.
    Unknown/prepared entries require Store recovery, not another job/decoder.
    """
    with published._fail_closed():
        for ident in (project_id, source_bundle_id, asset_id, asset_revision):
            check_id(ident)
        locator = _window(start_ms, end_ms)
        validate_relative_path(output_relative)
        if not _OUTPUT.fullmatch(output_relative):
            raise ContractError("isolated workspace-relative 窗口派生/<safeid>.wav or 补采派生/<safeid>.wav required")
        if store.pipeline_state("collection") != "running":
            return {"status": "blocked", "reason": "collection paused", "project_id": project_id}
        with store.transaction():
            source = _source(store, project_id, source_bundle_id)
            native = _native(store, source, asset_id, asset_revision, locator)
            budget = _budget(source, max_received_bytes)
            request = _request(store, request_id, project_id, source, native, locator)
            implementation = _implementation()
            specification = _spec(source, native, locator, implementation, budget)
            key = digest([_POLICY, specification])
            previous = store.connection.execute("SELECT * FROM publications WHERE project_id=? AND idempotency_key=?", (project_id, key)).fetchone()
            if previous:
                if previous["state"] != "published":
                    raise ContractError("audio window already prepared/unknown; recover existing publish_id, never re-decode: " + previous["publish_id"])
                bundle = store.get_bundle(project_id, previous["bundle_id"])
                verify_audio_window_publication(store, previous, bundle)
                return _result(bundle, request, True)
            # An existing loose primitive result is NOT a publication. Verify its
            # exact caller-selected request/actual WAV before any durable new
            # intention/job. In particular, never auto-adopt a different IO cap.
            output = safe_path(store.root, output_relative, must_exist=False)
            manifest_path = output.with_name(output.name + ".manifest.json")
            reject_links(manifest_path)
            if manifest_path.exists():
                _proof(primitive._read_json(manifest_path), native, locator, output_relative, budget, output)
            elif output.exists():
                raise ContractError("window exists without recovery receipt; no new audio intention created")
            provider = store.register_provider(_PROVIDER, "window-" + digest(implementation)[:32], config_version="audio-" + key[:40])
            job = store.queue_job(project_id, "native_extract", provider, asset_id + ":" + asset_revision,
                config_version=provider["config_version"], estimated_cost_minor=0, max_cost_minor=0)
            if job["state"] != "queued":
                raise ContractError("existing audio derivation job requires reconciliation")
            publish_id, bundle_id = new_id("publish"), new_id("bundle")
            revision = store.connection.execute("SELECT COALESCE(MAX(revision),0)+1 FROM publications WHERE project_id=?", (project_id,)).fetchone()[0]
            final, staging = f"证据/v{revision:04d}", "证据/.staging-" + publish_id
            plan = _plan(source, native, locator, provider, job, publish_id, budget)
            store.connection.execute("INSERT INTO plans VALUES(?,?,?,?)", (plan["plan_id"], project_id, canonical(plan).decode(), now()))
            store.connection.execute("INSERT INTO publications(publish_id,project_id,bundle_id,revision,idempotency_key,state,staging_relative_path,final_relative_path,created_at) VALUES(?,?,?,?,?,'staging',?,?,?)", (publish_id, project_id, bundle_id, revision, key, staging, final, now()))
            store.event("audio_window_derivation_intended", publish_id, project_id, {"specification": specification, "provider": provider,
                "job_id": job["job_id"], "intention_plan_id": plan["plan_id"], "intention_plan_sha256": digest(plan),
                "primitive_output_relative": output_relative, "request": request, "request_is_authorization": False, "cost_minor": 0})
            store.event("publish_registered", publish_id, project_id, {"operation": _POLICY, "source_bundle_id": source_bundle_id})
            if store.connection.execute("UPDATE jobs SET state='running',updated_at=? WHERE job_id=? AND state='queued'", (now(), job["job_id"])).rowcount != 1:
                raise ContractError("audio derivation job changed before execution")
            store._check_extraction_job(project_id, native["asset"], job["job_id"], provider)
        journal = store.connection.execute("SELECT * FROM publications WHERE publish_id=?", (publish_id,)).fetchone()
        stage = store.project_root(project_id) / staging
        reject_links(stage)
        stage.mkdir(parents=True, exist_ok=False)
        if store.pipeline_state("collection") != "running":
            return {"status": "blocked", "reason": "collection paused before extraction; reconcile intention", "publish_id": publish_id}
        started = time.monotonic()
        proof = primitive.extract_audio_window(store.root, native["source_local_path"], output_relative, start_ms, end_ms,
            expected_identity=published._decode(native["registration"]["identity_json"]), max_received_bytes=budget["max_input_bytes"], max_output_bytes=primitive.DEFAULT_MAX_OUTPUT_BYTES)
        output = safe_path(store.root, output_relative)
        _proof(proof, native, locator, output_relative, budget, output)
        wav = _read(output, primitive.DEFAULT_MAX_OUTPUT_BYTES)
        metadata = {"metadata_version": _POLICY, "specification": specification, "provider": provider, "intention_plan_id": plan["plan_id"],
            "job_id": job["job_id"], "primitive_output_relative": output_relative, "proof": proof, "request": request,
            "source_role": "native_pcm_source", "source_content_sha256": None, "semantic_read_performed": False,
            "source_copy_retained": False, "strict_media_history_reuse_supported": False, "gaps_resolved": False}
        bundle, final_plan = _build(store, source, plan, job, provider, journal, stage, wav, metadata, time.monotonic() - started)
        manifest = canonical(bundle)
        if len(manifest) > published._MAX_MANIFEST_BYTES or len(canonical(final_plan)) > published._MAX_PLAN_BYTES:
            raise ContractError("audio publication manifest/Plan byte bound exceeded")
        _write(stage / "manifest.json", manifest)
        expected = deepcopy(source["expected"])
        expected.update({a["relative_path"]: {"sha256": a["sha256"], "byte_length": a["byte_length"]} for a in bundle["artifacts"]})
        expected[final + "/manifest.json"] = dict(zip(("sha256", "byte_length"), hash_file(stage / "manifest.json")))
        if len(expected) > published._MAX_FILES:
            raise ContractError("audio publication file count bound exceeded")
        with store.transaction():
            if store.pipeline_state("collection") != "running":
                raise ContractError("collection paused before seal; reconcile intention")
            current = _source(store, project_id, source_bundle_id)
            if _native(store, current, asset_id, asset_revision, locator) != native:
                raise ContractError("audio original registration/path/stat changed before seal")
            _request(store, request_id, project_id, current, native, locator)
            store.connection.execute("INSERT INTO plans VALUES(?,?,?,?)", (final_plan["plan_id"], project_id, canonical(final_plan).decode(), now()))
            if store.connection.execute("UPDATE publications SET state='validated',manifest_sha256=?,bundle_json=?,expected_files_json=? WHERE publish_id=? AND state='staging' AND bundle_json IS NULL", (hash_file(stage / "manifest.json")[0], manifest.decode(), canonical(expected).decode(), publish_id)).rowcount != 1:
                raise ContractError("audio publication changed before validation")
            prepared = store.connection.execute("SELECT * FROM publications WHERE publish_id=?", (publish_id,)).fetchone()
            store._verify_publication(prepared, staging=True)
            verify_audio_window_publication(store, prepared, bundle, staging=True)
        store._finish_publication(publish_id)
        return _result(store.get_bundle(project_id, bundle_id), request, False)
