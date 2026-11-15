"""Read-only checks of locally obtained platform manuscripts and courseware.

Readability is not a claim about video identity, transcript completeness or
source-frame coverage. No browser, network, Office, OCR or model is invoked.
The existing bounded document parsers run with an in-memory output sink.
"""
from __future__ import annotations

import codecs
import hashlib
from pathlib import Path
import re

from . import documents
from .contracts import ContractError, safe_path, scan_secrets
from .platform_materials import DEFAULT_LIMITS as MATERIAL_LIMITS, _check_bytes, _read


_FORMATS = {"manuscript": {".txt", ".md", ".srt", ".vtt"},
            "courseware": {".pptx", ".pdf"}}
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


def inspect_product(store, product, relative_path) -> dict:
    """Inspect a real file beneath ``store.root`` without altering it.

    Bad paths, links, media disguised as text and resource violations raise
    ContractError. Unreadable/unsupported/empty products return readable=False.
    A successful result only verifies this local file, not its claimed source.
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
