"""Import a bounded LOCAL material package into Store; never contact Baidu.

The source declaration is unverified. Text may be incomplete and images are
occurrences, not verified video keyframes. No media, fetching, ASR or OCR runs.
"""
from __future__ import annotations

import argparse
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
from .policy import Policy
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


def _validate_manifest(value, policy):
    _fields(value, {"schema_version", "material_id", "title", "source", "transcript", "note", "images"},
            {"schema_version", "material_id", "title", "source"})
    if value["schema_version"] != "1.0":
        raise ContractError("unsupported material schema_version")
    check_id(value["material_id"])
    _string(value["title"])
    source = value["source"]
    _fields(source, {"provider", "file_id", "account_ref", "path", "duration_ms"}, {"provider", "file_id", "path"})
    if source["provider"] != "baidu":
        raise ContractError("v1 source provider must be baidu (declared only)")
    check_id(source["file_id"])
    if "account_ref" in source:
        check_id(source["account_ref"])
    source["path"] = policy.require_cloud_path(source["path"])
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
    image_paths = {item["path"] for item in records if item["role"] == "image"}
    base_rel = "采集输入/platform-materials/" + project_id + "/" + digest(value["material_id"])
    snapshot = safe_path(root, base_rel + "/input.json", must_exist=False)
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
            continue
        limit = limits["max_image_bytes" if record["role"] == "image" else "max_text_bytes"]
        data = _read(path, min(limit, max(0, limits["max_total_bytes"] - total)))
        _check_bytes(data, record["role"])
        if record["role"] != "image":
            _text_resources(data, record["path"], image_paths)
        total += len(data)
        sha = hashlib.sha256(data).hexdigest()
        record.update({"state": "present", "sha256": sha, "size_bytes": len(data)})
        if record["role"] == "image":
            readable, reason = _image_readable(data, record["path"])
            if not readable:
                record.update({"state": "unreadable", "validation_reason": reason})
                continue  # Never copy unvalidated candidate bytes to the snapshot.
        rel = base_rel + "/objects/" + sha + path.suffix.lower()
        record["snapshot_path"] = rel
        payloads[rel] = data
    normalized = {key: value[key] for key in ("schema_version", "material_id", "title", "source")}
    normalized["attachments"] = records
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


def import_materials(store, project_id, manifest, *, limits=None, policy=None):
    """Publish/reuse a local Bundle. Missing or repaired attachments change revision."""
    if store.pipeline_state("collection") != "running":
        return {"status": "blocked", "reason": "collection paused", "project_id": project_id}
    limits = _limits(limits)
    with _import_lock(store, project_id) as acquired:
        if not acquired:
            return {"status": "blocked", "reason": "material import lock exists; retry after the owner finishes; stale locks are never stolen", "project_id": project_id}
        snapshot = _prepare_package(store, project_id, manifest, limits=limits, policy=policy)
        # Explicit config includes this importer and the shared parser implementation.
        config = "platform-materials-" + digest({"adapter": hash_file(Path(__file__))[0], "documents": hash_file(Path(documents.__file__))[0], "limits": limits})[:40]
        return store.ingest(project_id, [snapshot], extract, options={"root": str(store.root), "limits": limits}, config_version=config)


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
