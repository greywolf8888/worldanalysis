"""Import a bounded LOCAL material package into Store; never contact Baidu.

The source declaration is unverified. Text may be incomplete and images are
occurrences, not verified video keyframes. No media, fetching, ASR or OCR runs.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from . import documents
from .contracts import ContractError, canonical, check_id, digest, hash_file, reject_links, safe_path, scan_secrets, validate_relative_path
from .platform_identity import material_content, remember_material_location
from .policy import GateError, Policy
from .store import Store

PROVIDER_ID = "worldanalysis.platform_materials.local"
PROVIDER_VERSION = "1.0.0"
DEFAULT_LIMITS = {"max_total_bytes": 64 * 1024**2, "max_image_bytes": 20 * 1024**2,
                  "max_text_bytes": 8 * 1024**2}
MAX_MANIFEST_BYTES = 1024**2
MAX_IMAGES = 1000
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp"}
_EMBED = re.compile(r"!\s*\[|<\s*(?:img|iframe|video|audio|source|object|embed|script|link|style|svg)\b", re.I)
_LOCAL_IMAGE = re.compile(r"!\[[^\]\n]*\]\(([^)\r\n]*)\)")


def _fields(value, allowed, required):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        raise ContractError("material package has missing or unknown fields")


def _string(value, maximum=500):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ContractError("invalid material package string")
    return value


def _integer(value):
    if type(value) is not int or value < 0:
        raise ContractError("time/size must be a nonnegative integer")
    return value


def _limits(value):
    result = dict(DEFAULT_LIMITS)
    if value:
        if set(value) - set(result):
            raise ContractError("unknown material limit")
        result.update(value)
    if any(type(n) is not int or n <= 0 for n in result.values()):
        raise ContractError("material limits must be positive integer bytes")
    return result


def _read(path, limit):
    reject_links(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ContractError("material must be a regular file within its byte limit")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ContractError("material file bound exceeded")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    reject_links(path)
    if len(data) > limit or len(data) != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ContractError("material changed during bounded read")
    return data


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError("duplicate JSON key")
            result[key] = value
        return result
    try:
        result = json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ContractError("nonfinite JSON")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError("invalid material JSON") from exc
    scan_secrets(result)
    if re.search(r"[a-z][a-z0-9+.-]*://|\bdata:", canonical(result).decode(), re.I):
        raise ContractError("URLs are not allowed in material manifest/metadata")
    return result


def _check_bytes(data, role):
    # Do not mistake a renamed video/audio file for a broken image or subtitle.
    head = data[:4096]
    media = (head[4:8] == b"ftyp" or head.startswith((b"OggS", b"fLaC", b"ID3", b"FLV", b"\x1aE\xdf\xa3", b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"))
             or (head[:4] == b"RIFF" and head[8:12] != b"WEBP")
             or (len(head) > 1 and head[0] == 255 and head[1] & 224 == 224)
             or (len(head) > 376 and head[0] == head[188] == head[376] == 71))
    if media or head.startswith((b"MZ", b"\x7fELF", b"PK\x03\x04", b"%PDF-")):
        raise ContractError("media/executable/container bytes are forbidden in material packages")
    if role != "image":
        try:
            text, _ = documents._decode(data)
        except UnicodeError as exc:
            raise ContractError("text material cannot be decoded safely") from exc
        if any(ord(c) < 32 and c not in "\t\n\r" for c in text):
            raise ContractError("binary/control content in text material")
        scan_secrets(text)


def _text_resources(data, path, images):
    """Keep declared local image syntax literally; never resolve remote embeds."""
    text, _ = documents._decode(data)
    for match in _LOCAL_IMAGE.finditer(text):
        target = match.group(1).strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        validate_relative_path(target)
        relative = (Path(path).parent / target).as_posix()
        if relative not in images:
            raise ContractError("Markdown image must name a declared local image attachment")
    if _EMBED.search(_LOCAL_IMAGE.sub("", text)):
        raise ContractError("HTML/remote/reference embeds are forbidden; use declared local image links")


class _ImageCheck(documents._Result):
    """Use the shared bounded full decoder without writing candidate bytes."""
    def file(self, data, kind, media_type, extension):
        if kind != "image" or "." + extension not in IMAGE_SUFFIXES:
            raise documents._Stop("Decoded image format is not an allowed raster type", "unreadable")
        return "validated-image-only"


def _image_readable(data, path):
    check = _ImageCheck(Path(path), Path("."), {"media_type": "image/unknown", "route": "validation_only"}, {})
    try:
        check.image(data, {"kind": "asset", "precision": "exact"}, path, allow_ocr=False)
    except documents._Stop as exc:
        return False, exc.reason
    if check.meta["image_inventory"][-1]["retained"]:
        return True, "Allowed raster format fully decoded within image limits"
    return False, check.data["gaps"][-1]["reason"]


def _source_frame_state(store, project_id, context):
    """Actual completed execution + current issued QA + Store retained artifact.

    Context fields are only references; none is a trust or budget assertion.
    """
    if store.connection.in_transaction:
        raise ContractError("source-frame binding cannot initialize inside an outer transaction")
    if store.pipeline_state("collection") != "running":
        raise GateError("collection paused")
    from .platform_first import PlatformFirst
    from .platform_execution import SourceFrameExecution
    from .platform_products import verify_retained_artifact
    _fields(context, {"job_id", "quality_review_path", "manifest_sha256", "quality_review_sha256", "input_revision"},
            {"job_id", "quality_review_path", "manifest_sha256", "quality_review_sha256", "input_revision"})
    check_id(context["job_id"]); validate_relative_path(context["quality_review_path"])
    coordinator = PlatformFirst(store)
    execution = SourceFrameExecution(store, coordinator)
    job, info, run, review, receipt = execution._completion_evidence(context["job_id"], context["quality_review_path"])
    if job["project_id"] != project_id or job["state"] != "verified_complete" or not execution.status(job["job_id"])["quality_accepted"]:
        raise ContractError("source-frame material requires a completed current execution")
    expected = {"job_id": job["job_id"], "input_revision": job["input_revision"],
                "quality_review_path": receipt["quality_review"]["relative_path"],
                "manifest_sha256": receipt["manifest"]["sha256"], "quality_review_sha256": receipt["quality_review"]["sha256"]}
    if context != expected:
        raise ContractError("source-frame material context is not the current issued manifest/QA")
    completed = execution._events("source_frames_completed", job["job_id"])
    if not completed or any(completed[-1].get(k) != receipt[k] for k in ("manifest", "quality_review")):
        raise ContractError("source-frame material cannot substitute another approved review")
    _, row = coordinator._product(run, info["file_id"], "courseware")
    artifact = _json(row["artifact_json"].encode()) if row["artifact_json"] else None
    if (not artifact or not verify_retained_artifact(store, artifact)
            or any(artifact[k] != receipt["manifest"][k] for k in ("relative_path", "sha256", "size_bytes"))):
        raise ContractError("source-frame retained Store artifact missing or changed")
    binding = artifact.get("metadata", {}).get("source_frame_execution", {})
    if (binding.get("job_id") != job["job_id"] or binding.get("input_revision") != job["input_revision"]
            or binding.get("helper_sha256") != info["task_facts"]["helper_sha256"]
            or binding.get("quality_review") != receipt["quality_review"]
            or binding.get("manifest_sha256") != receipt["manifest"]["sha256"]):
        raise ContractError("source-frame Store artifact is not execution/QA bound")
    document = _json(_read(safe_path(store.root, artifact["relative_path"]), MAX_MANIFEST_BYTES))
    return {"context": expected, "artifact": artifact, "manifest": document,
            "coordinator": coordinator, "execution": execution, "info": info}


def _bound_source_package(store, project_id, value, manifest_path):
    state = _source_frame_state(store, project_id, value["source_frame_execution"])
    artifact, document = state["artifact"], state["manifest"]
    expected_source = document["source"]
    if (value["material_id"] != "source-frames-" + expected_source["file_id"]
            or any(value["source"].get(k) != expected_source[k] for k in ("account_ref", "file_id", "source_revision", "size_bytes"))
            or manifest_path.parent != safe_path(store.root, artifact["relative_path"]).parent):
        raise ContractError("source-frame material source/root/identity mismatch")
    expected_images = [{"path": f["relative_path"], "source_kind": "claimed_video_frame", "timestamp_ms": int(f["media_time_ms"])} for f in document["frames"]]
    if value.get("images") != expected_images:
        raise ContractError("source-frame material must retain the entire exact current frame collection")
    return state


def _validate_manifest(value, policy):
    _fields(value, {"schema_version", "material_id", "title", "source", "transcript", "note", "images", "source_frame_execution"},
            {"schema_version", "material_id", "title", "source"})
    if value["schema_version"] != "1.0":
        raise ContractError("unsupported material schema_version")
    check_id(value["material_id"])
    _string(value["title"])
    source = value["source"]
    _fields(source, {"provider", "file_id", "account_ref", "path", "duration_ms", "source_revision", "size_bytes"}, {"provider", "file_id", "path"})
    if source["provider"] != "baidu":
        raise ContractError("v1 source provider must be baidu (declared only)")
    check_id(source["file_id"])
    if "account_ref" in source:
        check_id(source["account_ref"])
    source["path"] = policy.require_cloud_path(source["path"])
    if "source_revision" in source:
        check_id(source["source_revision"])
    if "size_bytes" in source:
        _integer(source["size_bytes"])
    if "duration_ms" in source:
        _integer(source["duration_ms"])
    records = []
    if "transcript" in value:
        item = value["transcript"]
        _fields(item, {"path", "kind", "origin", "source_method"}, {"path", "kind", "origin", "source_method"})
        allowed = {"timed_subtitle": {".srt", ".vtt"}, "plain_transcript": {".txt"}}
        if item["kind"] not in allowed or item["origin"] not in {"asr", "native_extract"}:
            raise ContractError("invalid transcript kind/origin")
        _string(item["source_method"])
        if Path(_string(item["path"], 2000)).suffix.lower() not in allowed[item["kind"]]:
            raise ContractError("transcript extension does not match kind")
        records.append({"role": "transcript", **item})
    if "note" in value:
        _fields(value["note"], {"path"}, {"path"})
        if Path(_string(value["note"]["path"], 2000)).suffix.lower() not in {".md", ".txt"}:
            raise ContractError("note must be local Markdown or text")
        records.append({"role": "note", **value["note"]})
    images = value.get("images", [])
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ContractError("images must be a list of at most 1000 occurrences")
    for ordinal, item in enumerate(images):
        _fields(item, {"path", "source_kind", "timestamp_ms"}, {"path", "source_kind"})
        if Path(_string(item["path"], 2000)).suffix.lower() not in IMAGE_SUFFIXES:
            raise ContractError("only bounded raster image attachments are accepted")
        if item["source_kind"] not in {"platform_note_image", "claimed_video_frame"}:
            raise ContractError("invalid declared image source kind")
        if item.get("timestamp_ms") is not None:
            _integer(item["timestamp_ms"])
        records.append({"role": "image", **item, "timestamp_ms": item.get("timestamp_ms"), "occurrence": ordinal})
    if not records:
        raise ContractError("at least one text or image declaration is required")
    return records


def _atomic(path, data):
    reject_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_links(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix="material-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        reject_links(path)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _prepare_package(store, project_id, manifest, *, limits=None, policy=None):
    """Private prepare step: caller holds _import_lock through Store.ingest."""
    limits = _limits(limits)
    store.project_root(project_id)
    root = store.root.resolve()
    supplied = Path(manifest)
    supplied = supplied if supplied.is_absolute() else root / supplied
    try:
        relative = supplied.relative_to(root).as_posix()
    except ValueError as exc:
        raise ContractError("manifest must be inside the workspace") from exc
    manifest = safe_path(root, relative)
    original = _read(manifest, min(MAX_MANIFEST_BYTES, limits["max_total_bytes"]))
    value = _json(original)
    records = _validate_manifest(value, policy or Policy())
    source_state = _bound_source_package(store, project_id, value, manifest) if "source_frame_execution" in value else None
    if source_state and any(limits[k] > DEFAULT_LIMITS[k] for k in DEFAULT_LIMITS):
        raise ContractError("source-frame material byte budgets cannot be raised")
    source_frames = {f["relative_path"]: f for f in source_state["manifest"]["frames"]} if source_state else {}
    image_paths = {item["path"] for item in records if item["role"] == "image"}
    base_rel = "采集输入/platform-materials/" + project_id + "/" + digest(value["material_id"])
    snapshot = safe_path(root, base_rel + "/input.json", must_exist=False)
    previous = None
    if snapshot.exists():
        previous = _json(_read(snapshot, MAX_MANIFEST_BYTES))
        identity = lambda source: (source["provider"], source["file_id"], source.get("account_ref"))
        if identity(previous["source"]) != identity(value["source"]):
            raise ContractError("material_id cannot be reused for another declared source")
    total, payloads = len(original), {}
    for record in records:
        path = safe_path(manifest.parent, record["path"], must_exist=False)
        record.update({"state": "missing", "sha256": None, "size_bytes": 0, "snapshot_path": None})
        if not path.exists():
            if source_state and record["role"] == "image":
                raise ContractError("bound source-frame image is missing")
            continue
        limit = limits["max_image_bytes" if record["role"] == "image" else "max_text_bytes"]
        if source_state and record["role"] == "image":
            limit = min(limit, 4 * 1024**2)
        data = _read(path, min(limit, max(0, limits["max_total_bytes"] - total)))
        _check_bytes(data, record["role"])
        if record["role"] != "image":
            _text_resources(data, record["path"], image_paths)
        total += len(data)
        sha = hashlib.sha256(data).hexdigest()
        record.update({"state": "present", "sha256": sha, "size_bytes": len(data)})
        if record["role"] == "image":
            if source_state:
                frame = source_frames[record["path"]]
                if (sha, len(data)) != (frame["sha256"], frame["size_bytes"]):
                    raise ContractError("bound source-frame bytes changed before snapshot")
                record["source_frame"] = frame
            else:
                readable, reason = _image_readable(data, record["path"])
                if not readable:
                    record.update({"state": "unreadable", "validation_reason": reason})
                    continue  # Never copy unvalidated candidate bytes to the snapshot.
        rel = base_rel + "/objects/" + sha + path.suffix.lower()
        record["snapshot_path"] = rel
        payloads[rel] = data
    normalized = {key: value[key] for key in ("schema_version", "material_id", "title", "source")}
    normalized["attachments"] = records
    if source_state:
        normalized["source_frame_execution"] = source_state["context"]
    remember_material_location(store, project_id, normalized)
    # Published evidence retains the original observed path. A later location
    # observation lives in SQLite, not a new extraction input or publication.
    if previous is not None and material_content(previous) == material_content(normalized):
        normalized = previous
    snapshot_data = canonical(normalized)
    if len(snapshot_data) > MAX_MANIFEST_BYTES:
        raise ContractError("normalized material snapshot exceeds 1 MiB")
    for relative, data in payloads.items():
        path = safe_path(root, relative, must_exist=False)
        if path.exists():
            if hash_file(path) != (hashlib.sha256(data).hexdigest(), len(data)):
                raise ContractError("material snapshot object was modified")
        else:
            _atomic(path, data)
    if not snapshot.exists() or _read(snapshot, MAX_MANIFEST_BYTES) != snapshot_data:
        _atomic(snapshot, snapshot_data)
    return snapshot


def extract(path, output_dir, *, options=None):
    """Store raw interface. Reverify every snapshot object before extraction."""
    options = options or {}
    root = Path(options["root"])
    limits = _limits(options.get("limits"))
    value = _json(_read(Path(path), MAX_MANIFEST_BYTES))
    r = documents._Result(Path(path), Path(output_dir),
                          {"media_type": "application/vnd.worldanalysis.material-package+json", "route": "local_platform_material_package"},
                          {"max_output_bytes": limits["max_total_bytes"] * 4})
    r.data.update({"provider_id": PROVIDER_ID, "provider_version": PROVIDER_VERSION})
    r.meta.update({"material_package_import": True, "title": value["title"], "source_material_id": value["material_id"],
                   "source_video_declared": {**value["source"], "identity_status": "declared_unverified"},
                   "package_limits": limits, "attachment_inventory": value["attachments"],
                   "source_identity_verified": False, "transcript_completeness_verified": False,
                   "visual_coverage_verified": False, "online_acquisition_performed": False,
                   "text_is_literal": True, "new_generation_performed": False})
    total = 0
    image_paths = {item["path"] for item in value["attachments"] if item["role"] == "image"}
    for item in value["attachments"]:
        loc = {"kind": "node", "precision": "unknown", "node_id": item["role"] + ":" + str(item.get("occurrence", 0)),
               "uncertainty_reason": "Local package occurrence; original video association/time is unverified"}
        if item["role"] == "image" and item.get("timestamp_ms") is not None:
            loc.update({"kind": "time", "precision": "approximate", "start_ms": item["timestamp_ms"], "end_ms": item["timestamp_ms"]})
        if item["state"] in {"missing", "unreadable"}:
            unreadable = item["state"] == "unreadable"
            r.deferred("image_occurrence" if item["role"] == "image" else "note", loc,
                       ("Unvalidated image bytes were not copied: " + item.get("validation_reason", "unreadable")) if unreadable else "Declared local attachment is missing: " + item["path"],
                       gap="unreadable" if unreadable else "missing_visual" if item["role"] == "image" else "missing_asset")
            continue
        limit = limits["max_image_bytes" if item["role"] == "image" else "max_text_bytes"]
        data = _read(safe_path(root, item["snapshot_path"]), min(limit, limits["max_total_bytes"] - total))
        total += len(data)
        if (hashlib.sha256(data).hexdigest(), len(data)) != (item["sha256"], item["size_bytes"]):
            raise ContractError("material snapshot changed before extraction")
        _check_bytes(data, item["role"])
        if item["role"] != "image":
            _text_resources(data, item["path"], image_paths)
        if item["role"] == "image":
            try:
                indexes = r.image(data, loc, item["path"], allow_ocr=False)
            except documents._Stop as exc:
                indexes = r.deferred("image_occurrence", loc, exc.reason, gap=exc.kind)
            r.meta.setdefault("image_source_declarations", []).append({"occurrence": item["occurrence"], "source_kind": item["source_kind"],
                                                                       "timestamp_ms": item.get("timestamp_ms"), "block_indexes": indexes,
                                                                       "video_frame_verified": False})
            continue
        text, encoding = documents._decode(data)
        if item["role"] == "note":
            r.text(text, loc, kind="note", origin="provider_ai_note", reason="Imported platform note; not a verbatim transcript or newly generated text")
            continue
        r.meta["transcript_declaration"] = {key: item[key] for key in ("kind", "origin", "source_method")}
        r.meta["transcript_encoding"] = encoding
        first = len(r.data["blocks"])
        if item["kind"] == "timed_subtitle":
            key = r.file(text.encode("utf-8"), "native_text", "text/plain; charset=utf-8", "txt")
            documents._subtitles(r, text, key, Path(item["path"]).suffix.lower()[1:])
        else:
            r.text(text, loc)
        for block in r.data["blocks"][first:]:
            if block["kind"] != "metadata":
                block["origin"] = item["origin"]
                block["reason"] = "Imported declared transcript; no ASR executed. Source method: " + item["source_method"]
                if block["locator"]["kind"] == "time":
                    block["locator"].update({"precision": "approximate", "uncertainty_reason": "Cue timestamps retained from local subtitle; video identity/timing not independently verified"})
    r.gap("uncertain_origin", "Source identity is declared_unverified; this local import did not read back the Baidu account, file or path")
    r.gap("temporal_gap", "Course/video full transcript coverage is unverified; cue count or cue interval union cannot prove completeness")
    r.gap("missing_visual", "Whole-video visual coverage and true keyframe provenance remain unverified, including silent operations")
    return r.finish()


def _source_frame_snapshot(value, project_id, state):
    """Validate the actual consumed snapshot, not only its source_frame hint."""
    if not isinstance(value, dict) or value.get("schema_version") != "1.0":
        raise ContractError("invalid bound source-frame snapshot")
    document = state["manifest"]
    source = document["source"]
    material_id = "source-frames-" + source["file_id"]
    identity = {"provider": "baidu", **{k: source[k] for k in ("account_ref", "file_id", "source_revision", "size_bytes")}}
    declared = value.get("source", {})
    if (value.get("material_id") != material_id or not isinstance(declared, dict)
            or canonical({k: declared.get(k) for k in identity}) != canonical(identity)
            or canonical(value.get("source_frame_execution")) != canonical(state["context"])):
        raise ContractError("source-frame consumed snapshot identity/context mismatch")
    Policy().require_cloud_path(declared.get("path"))
    records = value.get("attachments")
    if not isinstance(records, list) or any(not isinstance(r, dict) or r.get("role") not in {"image", "transcript", "note"} for r in records):
        raise ContractError("invalid source-frame snapshot attachments")
    base = "采集输入/platform-materials/" + project_id + "/" + digest(material_id)
    expected = [{"role": "image", "path": f["relative_path"], "source_kind": "claimed_video_frame",
                 "timestamp_ms": int(f["media_time_ms"]), "occurrence": ordinal, "state": "present",
                 "sha256": f["sha256"], "size_bytes": f["size_bytes"],
                 "snapshot_path": base + "/objects/" + f["sha256"] + Path(f["relative_path"]).suffix.lower(),
                 "source_frame": f} for ordinal, f in enumerate(document["frames"])]
    actual = [r for r in records if r["role"] == "image"]
    if not expected or canonical(actual) != canonical(expected):
        raise ContractError("source-frame consumed snapshot must be the complete present ledger projection")
    return value


def _extract_source_frames(path, output_dir, *, options=None, snapshot=None, source_state=None, project_id=None):
    """Emit byte-identical Store-admitted JPEGs; no second native decoder."""
    options = options or {}
    root = Path(options["root"])
    limits = _limits(options.get("limits"))
    if snapshot is None or source_state is None or project_id is None:
        raise ContractError("source-frame emitter requires one parsed and ledger-bound snapshot")
    value = _source_frame_snapshot(snapshot, project_id, source_state)
    if value.get("source_frame_execution") != options.get("source_frame_execution"):
        raise ContractError("source-frame snapshot binding changed")
    r = documents._Result(Path(path), Path(output_dir),
                          {"media_type": "application/vnd.worldanalysis.material-package+json", "route": "local_platform_material_package"},
                          {"max_output_bytes": limits["max_total_bytes"] * 4})
    r.data.update({"provider_id": "worldanalysis.platform_materials.source_frames", "provider_version": PROVIDER_VERSION})
    r.meta.update({"material_package_import": True, "title": value["title"], "source_material_id": value["material_id"],
                   "source_video_declared": {**value["source"], "identity_status": "declared_unverified"},
                   "package_limits": limits, "attachment_inventory": value["attachments"],
                   "source_identity_verified": False, "transcript_completeness_verified": False,
                   "visual_coverage_verified": False, "online_acquisition_performed": False,
                   "text_is_literal": True, "new_generation_performed": False})
    r.meta.update({"source_frame_execution": value["source_frame_execution"], "retained_store_artifact_verified": True,
                   "external_visual_review_bound": True, "image_validation_scope": "completed_execution_retained_hash_readback"})
    total = 0
    image_paths = {item["path"] for item in value["attachments"] if item["role"] == "image"}
    for item in value["attachments"]:
        loc = {"kind": "node", "precision": "unknown", "node_id": item["role"] + ":" + str(item.get("occurrence", 0)),
               "uncertainty_reason": "Local package occurrence; original video association/time is unverified"}
        if item["role"] == "image" and item.get("timestamp_ms") is not None:
            loc.update({"kind": "time", "precision": "approximate", "start_ms": item["timestamp_ms"], "end_ms": item["timestamp_ms"]})
        if item["role"] == "image" and item["state"] != "present":
            raise ContractError("bound source-frame image must be present")
        if item["state"] in {"missing", "unreadable"}:
            unreadable = item["state"] == "unreadable"
            r.deferred("image_occurrence" if item["role"] == "image" else "note", loc,
                       ("Unvalidated image bytes were not copied: " + item.get("validation_reason", "unreadable")) if unreadable else "Declared local attachment is missing: " + item["path"],
                       gap="unreadable" if unreadable else "missing_visual" if item["role"] == "image" else "missing_asset")
            continue
        limit = limits["max_image_bytes" if item["role"] == "image" else "max_text_bytes"]
        if item["role"] == "image":
            limit = min(limit, 4 * 1024**2)
        path = safe_path(root, item["snapshot_path"])
        if path.stat().st_nlink != 1:
            raise ContractError("hardlinked source-frame snapshot forbidden")
        data = _read(path, min(limit, limits["max_total_bytes"] - total))
        total += len(data)
        if (hashlib.sha256(data).hexdigest(), len(data)) != (item["sha256"], item["size_bytes"]):
            raise ContractError("material snapshot changed before extraction")
        _check_bytes(data, item["role"])
        if item["role"] != "image":
            _text_resources(data, item["path"], image_paths)
        if item["role"] == "image":
            frame = item["source_frame"]
            if ((frame["sha256"], frame["size_bytes"]) != (item["sha256"], len(data))
                    or frame["format"] != "jpeg" or frame["width"] * frame["height"] > 16_000_000 or len(data) > 4 * 1024**2):
                raise ContractError("source-frame snapshot no longer matches retained proof")
            key = r.file(data, "image", "image/jpeg", "jpg")
            indexes = r.deferred("image_occurrence", loc,
                "Unchanged JPEG from completed execution and external review; hash readback, no recapture or new interpretation",
                gap="semantic_gap", files=[key])
            r.meta["image_inventory"].append({"source_ref": item["path"], "file_key": key, "block_indexes": indexes,
                "retained": True, "width": frame["width"], "height": frame["height"], "frame_count": 1,
                "byte_preserving": True, "visual_interpreted": False})
            r.meta.setdefault("image_source_declarations", []).append({"occurrence": item["occurrence"], "source_kind": item["source_kind"],
                                                                       "timestamp_ms": item.get("timestamp_ms"), "block_indexes": indexes,
                                                                       "video_frame_verified": False})
            continue
        text, encoding = documents._decode(data)
        if item["role"] == "note":
            r.text(text, loc, kind="note", origin="provider_ai_note", reason="Imported platform note; not a verbatim transcript or newly generated text")
            continue
        r.meta["transcript_declaration"] = {key: item[key] for key in ("kind", "origin", "source_method")}
        r.meta["transcript_encoding"] = encoding
        first = len(r.data["blocks"])
        if item["kind"] == "timed_subtitle":
            key = r.file(text.encode("utf-8"), "native_text", "text/plain; charset=utf-8", "txt")
            documents._subtitles(r, text, key, Path(item["path"]).suffix.lower()[1:])
        else:
            r.text(text, loc)
        for block in r.data["blocks"][first:]:
            if block["kind"] != "metadata":
                block["origin"] = item["origin"]
                block["reason"] = "Imported declared transcript; no ASR executed. Source method: " + item["source_method"]
                if block["locator"]["kind"] == "time":
                    block["locator"].update({"precision": "approximate", "uncertainty_reason": "Cue timestamps retained from local subtitle; video identity/timing not independently verified"})
    r.gap("uncertain_origin", "Source identity is declared_unverified; this local import did not read back the Baidu account, file or path")
    r.gap("temporal_gap", "Course/video full transcript coverage is unverified; cue count or cue interval union cannot prove completeness")
    r.gap("missing_visual", "Whole-video visual coverage and true keyframe provenance remain unverified, including silent operations")
    return r.finish()


@contextmanager
def _import_lock(store, project_id):
    """Project-wide process lock; existing/stale locks are never stolen."""
    store.project_root(project_id)
    path = safe_path(store.root, "采集输入/platform-materials/" + project_id + "/import.lock", must_exist=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_links(path)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        yield False
        return
    try:
        os.write(descriptor, canonical({"project_id": project_id, "pid": os.getpid()}))
        os.fsync(descriptor)
        yield True
    finally:
        owned = os.fstat(descriptor)
        os.close(descriptor)
        reject_links(path)
        current = path.stat()
        if (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
            raise ContractError("material import lock was replaced; refusing to remove it")
        path.unlink()


# Compatibility baseline of the merged 1.0.0 importer. Identity/location-only
# changes must not invalidate its existing verified extraction jobs. The AST
# fingerprint excludes input preparation, but includes every extraction helper.
_LEGACY_ADAPTER_SHA256 = "6b890194818f23a4c45d703c9aa63afe75361cd8a226b6b21c2640b1e99b1b1b"
_LEGACY_EXTRACTION_SHA256 = "8159e9b67a70552650c61a42b05d9bdb72832133c6819be0d3300dc7a0bd0fd4"


def _processing_config(limits):
    names = {"PROVIDER_ID", "PROVIDER_VERSION", "DEFAULT_LIMITS", "MAX_MANIFEST_BYTES", "MAX_IMAGES",
             "IMAGE_SUFFIXES", "_EMBED", "_LOCAL_IMAGE", "_fields", "_string", "_integer", "_limits",
             "_read", "_json", "_check_bytes", "_text_resources", "_ImageCheck", "_image_readable", "extract"}
    tree = ast.parse(_read(Path(__file__), 1024**2).decode("utf-8-sig"))
    nodes = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
             or isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)]
    def semantic_node(node):
        if isinstance(node, ast.AST):
            # Optional empty AST fields differ across supported Python versions.
            return [type(node).__name__, {key: semantic_node(value) for key, value in ast.iter_fields(node)
                                          if value is not None and value != []}]
        if isinstance(node, list):
            return [semantic_node(value) for value in node]
        if isinstance(node, bytes):
            return {"bytes": node.hex()}
        return node
    semantic = digest(semantic_node(ast.Module(body=nodes, type_ignores=[])))
    adapter = _LEGACY_ADAPTER_SHA256 if semantic == _LEGACY_EXTRACTION_SHA256 else semantic
    return "platform-materials-" + digest({"adapter": adapter, "documents": hash_file(Path(documents.__file__))[0], "limits": limits})[:40]


def import_materials(store, project_id, manifest, *, limits=None, policy=None):
    """Publish/reuse a local Bundle. Missing or repaired attachments change revision."""
    if store.pipeline_state("collection") != "running":
        return {"status": "blocked", "reason": "collection paused", "project_id": project_id}
    limits = _limits(limits)
    with _import_lock(store, project_id) as acquired:
        if not acquired:
            return {"status": "blocked", "reason": "material import lock exists; retry after the owner finishes; stale locks are never stolen", "project_id": project_id}
        snapshot = _prepare_package(store, project_id, manifest, limits=limits, policy=policy)
        # Ordinary extraction and its audited AST/config remain unchanged.
        normalized = _json(_read(snapshot, MAX_MANIFEST_BYTES))
        if "source_frame_execution" not in normalized:
            config = _processing_config(limits)
            return store.ingest(project_id, [snapshot], extract, options={"root": str(store.root), "limits": limits}, config_version=config)
        context = normalized["source_frame_execution"]
        _source_frame_state(store, project_id, context)
        def source_extractor(path, output_dir, *, options=None):
            _source_frame_state(store, project_id, context)
            value = _json(_read(Path(path), MAX_MANIFEST_BYTES))
            current = _source_frame_state(store, project_id, context)
            result = _extract_source_frames(path, output_dir, options=options,
                snapshot=value, source_state=current, project_id=project_id)
            _source_frame_state(store, project_id, context)
            return result
        source_extractor.provider_id = "worldanalysis.platform_materials.source_frames"
        source_extractor.provider_version = PROVIDER_VERSION
        names = {"_source_frame_state", "_bound_source_package", "_source_frame_snapshot", "_extract_source_frames", "_prepare_package", "import_materials"}
        tree = ast.parse(_read(Path(__file__), 1024**2).decode("utf-8-sig"))
        implementation = ast.dump(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[]), include_attributes=False)
        config = "source-frame-materials-" + digest({"adapter": digest(implementation), "ordinary": _processing_config(limits)})[:40]
        return store.ingest(project_id, [snapshot], source_extractor,
            options={"root": str(store.root), "limits": limits, "source_frame_execution": context}, config_version=config)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--manifest", required=True)
    for name, default in DEFAULT_LIMITS.items():
        parser.add_argument("--" + name.replace("_", "-"), type=int, default=default)
    args = parser.parse_args(argv)
    with Store(args.root) as store:
        result = import_materials(store, args.project, args.manifest,
                                  limits={name: getattr(args, name) for name in DEFAULT_LIMITS})
    print(json.dumps({key: result[key] for key in ("project_id", "bundle_id", "status", "reason") if key in result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
