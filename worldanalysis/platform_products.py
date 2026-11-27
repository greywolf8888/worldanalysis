"""Read-only checks of locally obtained platform manuscripts and courseware.

Readability is not a claim about video identity, transcript completeness or
source-frame coverage. No browser, network, Office, OCR or model is invoked.
The existing bounded document parsers run with an in-memory output sink.
"""
from __future__ import annotations

import codecs
import hashlib
import io
from pathlib import Path, PurePosixPath
import re
import stat
import warnings

from . import documents
from .contracts import ContractError, check_id, safe_path, scan_secrets, validate_relative_path
from .platform_materials import DEFAULT_LIMITS as MATERIAL_LIMITS, _check_bytes, _fields, _json, _read, _string


_FORMATS = {"manuscript": {".txt", ".md", ".srt", ".vtt"},
            "courseware": {".pptx", ".pdf", ".json"}}
IMAGE_SET_SCHEMA = "worldanalysis.rendered-native-pages.v1"
SOURCE_FRAMES_SCHEMA = "worldanalysis.source-video-frames.v1"
MAX_FRAME_RECEIPT_BYTES = 16 * 1024
IMAGE_SET_LIMITS = {"max_manifest_bytes": 1024**2, "max_pages": 1000,
                    "max_total_bytes": 64 * 1024**2, "max_image_bytes": 20 * 1024**2,
                    "max_page_pixels": 16_000_000, "max_total_pixels": 64_000_000}
_PAGE_FORMATS = {"jpeg": ({".jpg", ".jpeg"}, b"\xff\xd8\xff", "JPEG"),
                 "png": ({".png"}, b"\x89PNG\r\n\x1a\n", "PNG")}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TEXT_ROUTES = {"txt_native", "md_native", "srt_native", "vtt_native"}
_ROUTES = {".txt": "txt_native", ".md": "md_native", ".srt": "srt_native",
           ".vtt": "vtt_native", ".pptx": "pptx_native", ".pdf": "pdf_native_pagewise"}
_UNICODE_BOMS = (codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE,
                 codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
# Match the entire small platform message, not words inside lesson content.
_PLATFORM_MESSAGE = re.compile(
    r"(?:(?:AI)?(?:文稿|课件)?(?:生成失败|加载失败|获取失败)(?:请稍后重试|点击重试|重新生成)?"
    r"|暂无(?:文稿|课件|内容)|(?:正在|正在为您)生成(?:文稿|课件)(?:请稍候|请稍后)?"
    r"|当前视频(?:暂)?不支持生成(?:文稿|课件)"
    r"|(?:今日|本月)?(?:文稿|课件|AI生成)(?:免费)?次数已用完"
    r"|开通(?:超级会员|会员)即可(?:解锁|生成|查看)(?:文稿|课件))",
    re.I,
)


class _Inspection(documents._Result):
    """Reuse document parsing while discarding derived bytes after inspection."""

    def file(self, data, kind, media_type, extension):
        signature = (hashlib.sha256(data).hexdigest(), kind, media_type)
        if signature in self.dedup:
            return self.dedup[signature]
        if self.bytes_written + len(data) > self.limits["max_output_bytes"]:
            raise documents._Stop("Inspection exceeds document output byte budget")
        key = f"f{len(self.data['files']) + 1:06d}"
        self.data["files"].append({"key": key, "kind": kind,
                                   "media_type": media_type})
        self.dedup[signature] = key
        # This counter limits parsed bytes; nothing is written to a file.
        self.bytes_written += len(data)
        return key


def _text_check(data):
    # The shared binary guard treats FF FE as an MPEG sync prefix. Recognized
    # Unicode BOMs must be decoded before applying textual control checks.
    if not data.startswith(_UNICODE_BOMS):
        _check_bytes(data, "transcript")
    try:
        text, encoding = documents._decode(data)
    except UnicodeError as exc:
        raise ContractError("manuscript cannot be decoded safely") from exc
    if any(ord(c) < 32 and c not in "\t\n\r" for c in text):
        raise ContractError("binary/control content in manuscript")
    scan_secrets(text)
    return text, encoding


def _message_only(text):
    compact = re.sub(r"[\s，。！？、：:,.!…]+", "", text)
    return len(compact) <= 120 and bool(_PLATFORM_MESSAGE.fullmatch(compact))


def _parse(path, info, limits):
    result = _Inspection(path, None, info, limits)
    package = None
    try:
        if info["route"] in _TEXT_ROUTES:
            documents._text(result, info["route"].split("_")[0])
        elif info["route"] == "pdf_native_pagewise":
            documents._pdf(result)
        elif info["route"] == "pptx_native":
            package = documents._Package(path, result.limits)
            package.check()
            documents._pptx(result, package)
    except documents._Stop as exc:
        if exc.kind == "resource_block":
            raise ContractError(str(exc)) from exc
        result.forced_status = "failed"
        result.data["enumeration_state"] = "partial"
        result.gap(exc.kind, str(exc), severity="blocking")
    except Exception as exc:
        result.forced_status = "failed"
        result.data["enumeration_state"] = "partial"
        result.gap("parse_failed", f"{type(exc).__name__}: {exc}", severity="blocking")
    finally:
        if package is not None:
            package.close()
    return result.finish()


def _positive(value, limit, label):
    if type(value) is not int or not 0 < value <= limit:
        raise ContractError("invalid or over-budget image-set " + label)
    return value


def _sha256(value):
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ContractError("invalid image-set SHA-256")
    return value


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _snapshot(path, *, no_hardlinks=False):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or (no_hardlinks and info.st_nlink != 1):
        raise ContractError("image-set files must be regular files without hardlinks")
    return _file_identity(info)


def _read_dependency(store, record, *, no_hardlinks=True):
    name = validate_relative_path(record["relative_path"])
    size = _positive(record["size_bytes"], store.max_file_bytes, "retained bytes")
    sha = _sha256(record["sha256"])
    path = safe_path(store.root, name)
    before = _snapshot(path, no_hardlinks=no_hardlinks)
    data = _read(path, size)
    if (len(data) != size or hashlib.sha256(data).hexdigest() != sha
            or before != _snapshot(path, no_hardlinks=no_hardlinks)):
        raise ContractError("image-set dependency bytes/hash changed or disagree with the manifest")
    return data


def verify_retained_artifact(store, artifact) -> bool:
    """Hash only the retained manifest/file and its initially validated pages.

    Accepts legacy single-file artifacts without dependencies. Image-set
    dependencies use root-relative paths, SHA-256 and size_bytes, not decoder
    output. No image/document parsing, directory enumeration or writes occur.
    Malformed, missing, symlinked, changed or over-budget artifacts return
    False. Image-set files also reject hardlinks.
    """
    try:
        if not isinstance(artifact, dict):
            return False
        maximum = _positive(store.max_file_bytes, 2**63 - 1, "file limit")
        relative = validate_relative_path(artifact["relative_path"])
        image_set = artifact.get("format") == "image_set"
        if Path(relative).suffix.lower() == ".json" and not image_set:
            return False  # A manifest must never silently lose its dependencies.
        records = [artifact]
        if image_set:
            deps = artifact["dependencies"]
            meta, metrics = artifact["metadata"], artifact["metrics"]
            if (artifact.get("product") != "courseware" or artifact.get("readable") is not True
                    or Path(relative).suffix.lower() != ".json"
                    or meta.get("image_set_schema") not in {IMAGE_SET_SCHEMA, SOURCE_FRAMES_SCHEMA}
                    or not isinstance(deps, list)):
                return False
            frames = meta["image_set_schema"] == SOURCE_FRAMES_SCHEMA
            count_key = "frame_count" if frames else "page_count"
            count = _positive(metrics[count_key], IMAGE_SET_LIMITS["max_pages"], "retained image count")
            receipt_count = metrics.get("receipt_count", 0) if frames else 0
            if (type(receipt_count) is not int or not 0 <= receipt_count <= count
                    or len(deps) != count + receipt_count
                    or count != _positive(meta["declared_source"][count_key], IMAGE_SET_LIMITS["max_pages"], "retained source count")):
                return False
            _positive(artifact["size_bytes"], IMAGE_SET_LIMITS["max_manifest_bytes"], "manifest bytes")
            parent = PurePosixPath(relative).parent
            images = receipts = 0
            for dep in deps:
                _fields(dep, {"relative_path", "sha256", "size_bytes"}, {"relative_path", "sha256", "size_bytes"})
                name = validate_relative_path(dep["relative_path"])
                if not PurePosixPath(name).is_relative_to(parent):
                    return False
                suffix = Path(name).suffix.lower()
                if frames and suffix == ".json":
                    _positive(dep["size_bytes"], min(maximum, MAX_FRAME_RECEIPT_BYTES), "receipt bytes")
                    receipts += 1
                elif suffix in {".jpg", ".jpeg", ".png"}:
                    _positive(dep["size_bytes"], min(maximum, IMAGE_SET_LIMITS["max_image_bytes"]), "image bytes")
                    images += 1
                else:
                    return False
            if images != count or receipts != receipt_count:
                return False
            records += deps
            if sum(r["size_bytes"] for r in records) > IMAGE_SET_LIMITS["max_total_bytes"]:
                return False
            if "source_frame_execution" in meta:
                from .source_frame_validator import LIMITS
                binding = meta["source_frame_execution"]
                if (not frames or binding["profile"] != "authorized_source_frames.v1" or receipt_count != count
                        or binding["manifest_sha256"] != artifact["sha256"]
                        or metrics["resource_validation"]["limits"] != LIMITS
                        or metrics["resource_validation"]["peak_inflight_frame_decodes"] != 1):
                    return False
                check_id(binding["job_id"]); check_id(binding["parent_job_id"])
                _sha256(binding["input_revision"]); _sha256(binding["helper_sha256"])
                _positive(metrics["total_pixels"], LIMITS["max_work_pixels"], "retained decode work")
                _positive(metrics["max_frame_pixels"], LIMITS["max_frame_pixels"], "retained frame pixels")
                for dep in deps:
                    if Path(dep["relative_path"]).suffix.lower() != ".json":
                        _positive(dep["size_bytes"], min(maximum, LIMITS["max_image_bytes"]), "retained source image bytes")
                quality = binding["quality_review"]
                _fields(quality, {"relative_path", "sha256", "size_bytes"}, {"relative_path", "sha256", "size_bytes"})
                _positive(quality["size_bytes"], min(maximum, 64 * 1024), "bound quality bytes")
                records.append(quality)
        elif "dependencies" in artifact:
            return False
        seen = set()
        for record in records:
            name = validate_relative_path(record["relative_path"])
            if name.casefold() in seen:
                return False
            seen.add(name.casefold())
            _positive(record["size_bytes"], maximum, "retained bytes")
            _read_dependency(store, record, no_hardlinks=image_set)
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def _decode_page(data, row):
    from PIL import Image, ImageFile
    suffixes, magic, image_format = _PAGE_FORMATS[row["format"]]
    if not data.startswith(magic) or Path(row["relative_path"]).suffix.lower() not in suffixes:
        raise ContractError("image-set page magic/extension disagrees with its format")
    if ImageFile.LOAD_TRUNCATED_IMAGES:
        raise ContractError("strict image decoding is required")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if (image.format != image_format or image.size != (row["width"], row["height"])
                        or getattr(image, "n_frames", 1) != 1):
                    raise ContractError("image-set page format/dimensions/frames mismatch")
                image.verify()
            # JPEG verify() alone does not decode its entropy stream.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ContractError("image-set page cannot be fully decoded") from exc


def _image_dependencies(store, images, relative_path, manifest_bytes, *, extra_bytes=0):
    """One bounded decoder/hash/path core shared by displayed pages and frames."""
    dependencies, names = [], set()
    total_bytes, total_pixels = manifest_bytes + extra_bytes, 0
    parent = PurePosixPath(relative_path).parent
    for row in images:
        if not isinstance(row["format"], str) or row["format"] not in _PAGE_FORMATS:
            raise ContractError("image-set image must be JPEG or PNG")
        width = _positive(row["width"], IMAGE_SET_LIMITS["max_page_pixels"], "width")
        height = _positive(row["height"], IMAGE_SET_LIMITS["max_page_pixels"], "height")
        _positive(width * height, IMAGE_SET_LIMITS["max_page_pixels"], "image pixels")
        size = _positive(row["bytes"], min(store.max_file_bytes, IMAGE_SET_LIMITS["max_image_bytes"]), "image bytes")
        total_bytes += size
        total_pixels += width * height
        if total_bytes > IMAGE_SET_LIMITS["max_total_bytes"] or total_pixels > IMAGE_SET_LIMITS["max_total_pixels"]:
            raise ContractError("image-set exceeds total byte/pixel budget")
        _sha256(row["sha256"])
        local = validate_relative_path(row["relative_path"])
        name = (parent / local).as_posix()
        if name.casefold() in names:
            raise ContractError("image-set images must have distinct local paths")
        names.add(name.casefold())
        dependencies.append({"relative_path": name, "sha256": row["sha256"], "size_bytes": size})
    for row, dep in zip(images, dependencies):
        try:
            _decode_page(_read_dependency(store, dep), row)
        except OSError as exc:
            raise ContractError("image-set image is missing or inaccessible") from exc
    return dependencies, sum(d["size_bytes"] for d in dependencies), total_pixels


def _image_json(data):
    try:
        return _json(data)
    except (ValueError, OverflowError, RecursionError) as exc:
        # The common JSON guard canonicalizes values after parsing; exponents
        # such as 1e999 must still fail as contract errors, not float overflow.
        raise ContractError("invalid image-set JSON") from exc


def _frame_time(value):
    # Exclude booleans, NaN/Inf and unsafe JSON-number ranges without rounding
    # away requestVideoFrameCallback's fractional mediaTime milliseconds.
    if type(value) not in {int, float} or not 0 <= value <= 2**53 - 1:
        raise ContractError("frame times must be finite nonnegative milliseconds")
    return value


def _frame_receipt(store, frame, parent):
    local = validate_relative_path(frame["receipt_relative_path"])
    if Path(local).suffix.lower() != ".json":
        raise ContractError("frame capture receipt must be a local JSON file")
    relative = (parent / local).as_posix()
    try:
        path = safe_path(store.root, relative)
        size = _positive(path.stat().st_size, min(store.max_file_bytes, MAX_FRAME_RECEIPT_BYTES), "receipt bytes")
        dep = {"relative_path": relative, "sha256": _sha256(frame["receipt_sha256"]), "size_bytes": size}
        if not isinstance(_image_json(_read_dependency(store, dep)), dict):
            raise ContractError("frame capture receipt must be a JSON object")
        return dep
    except OSError as exc:
        raise ContractError("frame capture receipt is missing or inaccessible") from exc


def _inspect_source_frames(store, manifest, data, response):
    """Validate local images and declared capture facts, not their truth/coverage."""
    _fields(manifest, {"schema", "source", "selection", "frames"}, {"schema", "source", "selection", "frames"})
    frames = manifest["frames"]
    if not isinstance(frames, list):
        raise ContractError("source-video frames must be an array")
    count = _positive(len(frames), IMAGE_SET_LIMITS["max_pages"], "frame count")
    source = manifest["source"]
    required_source = {"file_id", "path", "source_revision", "frame_count"}
    _fields(source, required_source | {"account_ref", "size_bytes"}, required_source)
    check_id(source["file_id"])
    _string(source["path"], 2000)
    _string(source["source_revision"])
    if not source["path"].startswith("/") or "\\" in source["path"]:
        raise ContractError("source-video frames require a stable platform path")
    if _positive(source["frame_count"], IMAGE_SET_LIMITS["max_pages"], "source frame count") != count:
        raise ContractError("source-video frame count disagrees with its declaration")
    if "account_ref" in source:
        check_id(source["account_ref"])
    if "size_bytes" in source:
        _positive(source["size_bytes"], 2**63 - 1, "source video bytes")
    selection = manifest["selection"]
    _fields(selection, {"evidence_ref", "basis"}, {"evidence_ref", "basis"})
    _string(selection["evidence_ref"], 2000)
    _string(selection["basis"], 2000)
    required = {"sequence", "relative_path", "sha256", "size_bytes", "width", "height",
                "requested_ms", "media_time_ms", "locator", "source_identity_check"}
    images, receipts = [], []
    previous = (-1, -1)
    parent = PurePosixPath(response["relative_path"]).parent
    for sequence, frame in enumerate(frames, 1):
        _fields(frame, required | {"format", "receipt_relative_path", "receipt_sha256"}, required)
        if type(frame["sequence"]) is not int or frame["sequence"] != sequence:
            raise ContractError("source-video frame sequences must be continuous from 1")
        times = (_frame_time(frame["requested_ms"]), _frame_time(frame["media_time_ms"]))
        if any(value < prior for value, prior in zip(times, previous)):
            raise ContractError("source-video frame request/media times must not move backwards")
        previous = times
        if frame["locator"] != "requestVideoFrameCallback":
            raise ContractError("source-video frames require the declared rVFC locator")
        checks = frame["source_identity_check"]
        _fields(checks, {"file_id_match", "version_token_match"}, {"file_id_match", "version_token_match"})
        if checks["file_id_match"] is not True or checks["version_token_match"] is not True:
            raise ContractError("source-video capture declares a source identity mismatch")
        local = validate_relative_path(frame["relative_path"])
        fmt = frame.get("format", {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png"}.get(Path(local).suffix.lower()))
        images.append({**frame, "bytes": frame["size_bytes"], "format": fmt})
        if ("receipt_relative_path" in frame) != ("receipt_sha256" in frame):
            raise ContractError("frame receipt path and SHA-256 must be provided together")
        if "receipt_relative_path" in frame:
            receipts.append(_frame_receipt(store, frame, parent))
    receipt_bytes = sum(r["size_bytes"] for r in receipts)
    dependencies, image_bytes, total_pixels = _image_dependencies(
        store, images, response["relative_path"], len(data), extra_bytes=receipt_bytes)
    response.update(format="image_set", readable=True, dependencies=dependencies + receipts,
                    reason="Local source-video frame captures are readable; source identity, timing accuracy and video coverage remain unverified")
    response["metrics"].update(frame_count=count, image_count=count, receipt_count=len(receipts),
                               total_image_bytes=image_bytes, total_receipt_bytes=receipt_bytes,
                               total_pixels=total_pixels, frame_enumeration_state="complete",
                               first_requested_ms=frames[0]["requested_ms"], last_requested_ms=frames[-1]["requested_ms"],
                               first_media_time_ms=frames[0]["media_time_ms"], last_media_time_ms=frames[-1]["media_time_ms"])
    response["metadata"].update(validation_scope="local_image_set_readability_only", route="source_video_frames",
                                image_set_schema=SOURCE_FRAMES_SCHEMA, declared_source=source,
                                declared_selection=selection, selection_verified=False,
                                representation="remote_source_frame_capture", not_native_container_bytes=True,
                                frame_sequence_complete=True, video_coverage_verified=False,
                                timing_accuracy_verified=False, receipt_semantics_verified=False,
                                capture_identity_claims_verified=False, server_md5_verified=False)
    if not verify_retained_artifact(store, response):
        raise ContractError("source-video frame set changed during inspection")


def _inspect_image_set(store, data, response):
    manifest = _image_json(data)
    if isinstance(manifest, dict) and manifest.get("schema") == SOURCE_FRAMES_SCHEMA:
        _inspect_source_frames(store, manifest, data, response)
        return
    if not isinstance(manifest, dict) or manifest.get("schema") != IMAGE_SET_SCHEMA:
        response["reason"] = "Unsupported courseware JSON schema"
        return
    _fields(manifest, {"schema", "pages"}, {"schema", "pages"})
    pages = manifest["pages"]
    if not isinstance(pages, list):
        raise ContractError("image-set pages must be an array")
    count = _positive(len(pages), IMAGE_SET_LIMITS["max_pages"], "page count")
    required = {"source", "page", "width", "height", "bytes", "format", "sha256", "pixel_sha256",
                "representation", "not_native_container_bytes", "relative_path"}
    source = None
    for number, row in enumerate(pages, 1):
        _fields(row, required | {"schema", "observed_at"}, required)
        if type(row["page"]) is not int or row["page"] != number:
            raise ContractError("image-set page numbers must be ordered and continuous from 1")
        declared = row["source"]
        source_fields = {"file_id", "path", "source_revision", "page_count"}
        _fields(declared, source_fields, source_fields)
        check_id(declared["file_id"])
        _string(declared["path"], 2000)
        _string(declared["source_revision"])
        if not declared["path"].startswith("/") or "\\" in declared["path"]:
            raise ContractError("image-set source must declare a stable platform path")
        if _positive(declared["page_count"], IMAGE_SET_LIMITS["max_pages"], "source page count") != count:
            raise ContractError("image-set does not contain all declared source pages")
        if source is not None and declared != source:
            raise ContractError("image-set pages disagree on stable source identity")
        source = declared
        if (row["representation"] != "normally_displayed_platform_page"
                or row["not_native_container_bytes"] is not True):
            raise ContractError("image-set must be explicitly labelled as displayed pages, not a native container")
        if "schema" in row and row["schema"] != "worldanalysis.browser-courseware-page.v1":
            raise ContractError("unsupported image-set page schema")
        if "observed_at" in row:
            _string(row["observed_at"], 100)
        _sha256(row["pixel_sha256"])
    dependencies, image_bytes, total_pixels = _image_dependencies(store, pages, response["relative_path"], len(data))
    response.update(format="image_set", readable=True, dependencies=dependencies,
                    reason="Local displayed pages are readable; native container, source identity and video coverage remain unverified")
    response["metrics"].update(page_count=count, image_count=count, total_image_bytes=image_bytes,
                               total_pixels=total_pixels, page_enumeration_state="complete")
    response["metadata"].update(validation_scope="local_image_set_readability_only", route="rendered_native_pages",
                                image_set_schema=IMAGE_SET_SCHEMA, declared_source=source,
                                representation="normally_displayed_platform_page", not_native_container_bytes=True,
                                page_sequence_complete=True, video_coverage_verified=False,
                                server_md5_verified=False, browser_display_verified=False,
                                pixel_sha256_scope="browser_reported_rgba_not_cross_decoder_verified",
                                pixel_sha256_verified=False)
    # Re-hash all retained inputs after decoding; record only this bounded set.
    if not verify_retained_artifact(store, response):
        raise ContractError("image-set changed during inspection")


def inspect_source_frame_execution(store, coordinator, job_id, quality_review_path, *, run_id=None):
    """Read-only evidence: actual execution + current files + bound external QA.

    No schema declaration, trust flag or caller budget selects this profile.
    This function never publishes, reconciles, or writes migration events.
    """
    from .platform_execution import SourceFrameExecution
    from .source_frame_validator import validate_source_frame_files, SourceFrameValidationError
    execution = SourceFrameExecution(store, coordinator)
    job, info, run, review, receipt = execution._completion_evidence(job_id, quality_review_path, run_id=run_id)
    descriptor = receipt["manifest"]
    data = _read_dependency(store, descriptor)
    manifest = _image_json(data)
    try:
        report = validate_source_frame_files(store.root, descriptor)
    except (SourceFrameValidationError, OSError) as exc:
        raise ContractError(str(exc)) from exc
    expected_source = {k: info["source"][k] for k in ("account_ref", "file_id", "source_revision", "size_bytes")}
    if (report["source"] != expected_source or report["helper_sha256"] != info["task_facts"]["helper_sha256"]
            or report["frame_count"] != len(info["task_facts"]["requested_ms"])):
        raise ContractError("source-frame decoder/execution binding mismatch")
    confirmed_job, _, _, _, confirmed = execution._completion_evidence(job_id, quality_review_path, run_id=run_id)
    if confirmed != receipt or any(confirmed_job[k] != job[k] for k in ("input_revision", "provider_json", "request_id")):
        raise ContractError("source-frame execution/QA changed during inspection")
    response = {"product": "courseware", **{k: descriptor[k] for k in ("relative_path", "sha256", "size_bytes")},
                "format": "image_set", "readable": True, "dependencies": report["dependencies"],
                "reason": "Execution-bound source frames readable; independent manifest-bound quality review retained",
                "metrics": {"frame_count": report["frame_count"], "image_count": report["frame_count"],
                            "receipt_count": report["receipt_count"], "total_image_bytes": report["total_image_bytes"],
                            "total_receipt_bytes": report["total_receipt_bytes"], "total_pixels": report["total_pixels"],
                            "max_frame_pixels": report["max_frame_pixels"], "frame_enumeration_state": "complete",
                            "first_requested_ms": manifest["frames"][0]["requested_ms"], "last_requested_ms": manifest["frames"][-1]["requested_ms"],
                            "first_media_time_ms": manifest["frames"][0]["media_time_ms"], "last_media_time_ms": manifest["frames"][-1]["media_time_ms"],
                            "resource_validation": {k: v for k, v in report.items() if k != "dependencies"}},
                "metadata": {"validation_scope": "source_frame_execution_and_external_review_binding", "route": "source_video_frames",
                             "image_set_schema": SOURCE_FRAMES_SCHEMA, "declared_source": manifest["source"],
                             "declared_selection": manifest["selection"], "selection_verified": False,
                             "representation": "remote_source_frame_capture", "not_native_container_bytes": True,
                             "frame_sequence_complete": True, "receipt_semantics_verified": True, "external_quality_review_bound": True,
                             "video_coverage_verified": False, "timing_accuracy_verified": False, "source_identity_verified": False,
                             "source_frames_verified": False, "capture_identity_claims_verified": False, "server_md5_verified": False,
                             "transcript_completeness_verified": False, "online_acquisition_performed": False,
                             "external_content_loaded": False, "ocr_executed": False, "files_written": 0,
                             "source_frame_execution": {"profile": "authorized_source_frames.v1", "job_id": job_id,
                                  "input_revision": job["input_revision"], "parent_job_id": info["task_facts"]["parent_job_id"],
                                  "manifest_sha256": descriptor["sha256"], "helper_sha256": info["task_facts"]["helper_sha256"],
                                  "quality_review": receipt["quality_review"]}}}
    if not verify_retained_artifact(store, response):
        raise ContractError("source-frame set or bound quality review changed during inspection")
    return response


def inspect_product(store, product, relative_path) -> dict:
    """Inspect a real file beneath ``store.root`` without altering it.

    Bad paths, links, media disguised as text and resource violations raise
    ContractError, as do invalid/incomplete declared image sets. Other
    unreadable/unsupported/empty products return readable=False. Courseware
    JSON requires IMAGE_SET_SCHEMA or SOURCE_FRAMES_SCHEMA; image/receipt
    paths are relative to the manifest directory.
    Success verifies local readability only, never the claimed source/video.
    """
    if product not in _FORMATS:
        raise ContractError("product must be manuscript or courseware")
    try:
        path = safe_path(store.root, relative_path)
    except OSError as exc:
        raise ContractError("product file does not exist or is inaccessible") from exc
    limit = min(store.max_file_bytes, documents.DEFAULT_LIMITS["max_input_bytes"])
    if product == "manuscript":
        limit = min(limit, MATERIAL_LIMITS["max_text_bytes"])
    elif path.suffix.lower() == ".json":
        limit = min(limit, IMAGE_SET_LIMITS["max_manifest_bytes"])
    if limit <= 0:
        raise ContractError("product byte limit must be positive")
    before = path.stat()
    data = _read(path, limit)
    sha = hashlib.sha256(data).hexdigest()
    response = {"product": product, "relative_path": relative_path,
                "sha256": sha, "size_bytes": len(data), "format": path.suffix.lower().lstrip("."),
                "readable": False, "metrics": {}, "reason": "Unsupported product format",
                "metadata": {"validation_scope": "local_file_readability_only",
                             "source_identity_verified": False,
                             "transcript_completeness_verified": False,
                             "source_frames_verified": False,
                             "online_acquisition_performed": False,
                             "external_content_loaded": False,
                             "ocr_executed": False, "files_written": 0}}
    if product == "courseware" and path.suffix.lower() == ".json":
        _snapshot(path, no_hardlinks=True)
        _inspect_image_set(store, data, response)
        after_data = _read(path, limit)
        if _file_identity(before) != _file_identity(path.stat()) or hashlib.sha256(after_data).hexdigest() != sha:
            raise ContractError("product changed during inspection")
        return response
    text = None
    if product == "manuscript":
        text, encoding = _text_check(data)
        response["metrics"].update(character_count=len(text),
                                   non_whitespace_characters=sum(not c.isspace() for c in text))
        response["metadata"]["encoding"] = encoding
    elif not (data[:4096].lstrip(b"\xef\xbb\xbf\x00\x09\x0a\x0d\x20").startswith(b"%PDF-")
              or data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\xd0\xcf\x11\xe0"))):
        # The common guard catches renamed video/audio/executable files. This
        # does not authorize arbitrary text as courseware; probing below must
        # still recognize an allowed PDF/PPTX container.
        _check_bytes(data, "transcript")
    del data
    info = documents.probe(path)
    response["metadata"]["route"] = info["route"]
    allowed_route = info["route"] == _ROUTES.get(path.suffix.lower())
    if path.suffix.lower() not in _FORMATS[product] or not info["supported"] or not allowed_route:
        response["reason"] = "Unsupported product format: " + info["reason"]
    elif text is not None and not text.strip():
        response["reason"] = "Manuscript is empty or whitespace only"
    elif text is not None and _message_only(text):
        response["reason"] = "Only a platform error, entitlement or generation placeholder was obtained"
    else:
        limits = dict(documents.DEFAULT_LIMITS)
        limits["max_input_bytes"] = limit
        limits["max_output_bytes"] = min(limits["max_output_bytes"], store.max_file_bytes * 4)
        parsed = _parse(path, info, limits)
        meta, blocks, gaps = parsed["metadata"], parsed["blocks"], parsed["gaps"]
        response["metrics"].update(
            block_count=len(blocks), gap_count=len(gaps),
            image_count=sum(x.get("retained") is True for x in meta.get("image_inventory", [])),
            page_count=meta.get("page_count", 0), slide_count=len(meta.get("slides", [])),
            cue_count=len(meta.get("subtitle_cues", [])),
            container_enumeration_state=parsed["enumeration_state"],
        )
        response["metadata"]["gap_kinds"] = sorted({g["kind"] for g in gaps})
        bad = parsed["processing_status"] in {"failed", "blocked"} or any(
            g["severity"] == "blocking" or g["kind"] in {"parse_failed", "unreadable", "access"}
            for g in gaps)
        if product == "manuscript" and path.suffix.lower() in {".srt", ".vtt"}:
            bad = bad or not meta.get("subtitle_cues") or any(g["kind"] == "temporal_gap" for g in gaps)
            bad = bad or not any(b["kind"] == "paragraph" and b.get("text", "").strip() for b in blocks)
            if meta.get("subtitle_cues"):
                response["metrics"]["first_cue_ms"] = min(c["start_ms"] for c in meta["subtitle_cues"])
                response["metrics"]["last_cue_ms"] = max(c["end_ms"] for c in meta["subtitle_cues"])
        if product == "courseware":
            bad = bad or not (meta.get("page_count", 0) or meta.get("slides"))
            bad = bad or parsed["enumeration_state"] != "complete"
        response["readable"] = not bad
        response["reason"] = ("Local product is readable; source identity and video coverage remain unverified"
                              if not bad else "Local product is empty, unreadable, incomplete or blocked by the document parser")
    after_data = _read(path, limit)
    after = path.stat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if identity(before) != identity(after) or hashlib.sha256(after_data).hexdigest() != sha:
        raise ContractError("product changed during inspection")
    return response
