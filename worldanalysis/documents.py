"""Bounded, offline document extraction (P1).

``probe(Path)`` never executes content. ``extract(Path, empty_existing_directory,
options=...)`` writes only into that directory and returns the unbound Bundle
adapter structure. Original small Office/PDF sources are retained by the caller.
No Office process, macro, formula, external relationship, script or URL is run.

Optional OCR is explicit: ``options['ocr'](image_path, options=ocr_options)``
returns a dict with text, provider_id, provider_version, processing_status equal
to 'extracted', and metadata.ocr_executed=True. This is compatible with
worldanalysis.ocr.ocr_image; no engine is imported or downloaded implicitly.
OCR runs once per retained image, only on image-only PDF pages by default.
It is a derived layer, not a replacement for the image or visual interpretation.
``metadata.derivations`` links each derived block to source blocks/files.
Optional ``options['render_pdf_page'](path, page_number, options=render_options)``
returns actual PNG bytes/width/height/provider/metadata.render_executed=True.
Only vector/unreadable/scan pages request it, never blank/native-only pages.
Page renders are real ``page_render`` artifacts referenced by deferred
hidden_object blocks, NOT extra authored image_occurrences. Scan pages prefer
OCR of a retained page render; readable native-text pages are not re-OCRed.

All limits are local options (see DEFAULT_LIMITS); hitting a limit returns a
resource gap and never silently truncates a text. Text blocks are <=16000 Unicode
characters. Native artifacts preserve the complete decoded string, including
newlines. JSON/CSV syntax errors do not discard the native text.
"""
from __future__ import annotations

import codecs
import csv
import hashlib
import importlib.metadata
import io
import json
import mimetypes
import posixpath
import re
import stat
import sys
import warnings
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

PROVIDER_ID = "worldanalysis.documents.native"
PROVIDER_VERSION = "1.0.0"
MAX_BLOCK_TEXT = 16000
DEFAULT_LIMITS = {
    "max_input_bytes": 128 * 1024 * 1024,
    "max_output_bytes": 256 * 1024 * 1024,
    "max_archive_members": 10000,
    "max_archive_uncompressed_bytes": 256 * 1024 * 1024,
    "max_member_bytes": 32 * 1024 * 1024,
    "max_compression_ratio": 1000,
    "max_xml_bytes": 16 * 1024 * 1024,
    "max_image_pixels": 64_000_000,
    "max_image_frames": 256,
    "max_pages": 10000,
    "max_cells": 500000,
    "max_blocks": 100000,
    "max_pdf_operations": 1_000_000,
    "max_pdf_stream_bytes": 32 * 1024 * 1024,
    "max_form_depth": 16,
}
MEDIA = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain", "md": "text/markdown", "json": "application/json",
    "csv": "text/csv", "srt": "application/x-subrip", "vtt": "text/vtt",
    "zip": "application/zip", "pdf": "application/pdf",
}
TEXT_SUFFIXES = {".txt": "txt", ".md": "md", ".markdown": "md", ".json": "json",
                 ".csv": "csv", ".srt": "srt", ".vtt": "vtt", ".yaml": "txt",
                 ".yml": "txt"}
UNSAFE_SUFFIXES = {".doc", ".ppt", ".xls", ".xlsb", ".docm", ".dotm", ".pptm",
                   ".potm", ".ppam", ".xlsm", ".xlam", ".exe", ".msi", ".dll",
                   ".com", ".scr", ".bat", ".cmd", ".ps1", ".vbs", ".js",
                   ".py", ".sh", ".rar", ".7z"}
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "v": "urn:schemas-microsoft-com:vml",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
}

def _q(prefix: str, name: str) -> str:
    return "{" + NS[prefix] + "}" + name

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]

class _Stop(Exception):
    def __init__(self, reason: str, kind: str = "resource_block"):
        super().__init__(reason)
        self.kind = kind


def _safe_member(name: str) -> bool:
    return bool(name and not name.startswith(("/", "\\")) and "\\" not in name
                and ":" not in name and not any(ord(c) < 32 for c in name)
                and all(p not in (".", "..", "") for p in name.rstrip("/").split("/")))


def _xml(data: bytes, limit: int) -> ET.Element:
    if len(data) > limit:
        raise _Stop("XML part exceeds max_xml_bytes")
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.I):
        raise _Stop("DTD/entity declarations are prohibited; no entity resolution performed", "unsupported")
    return ET.fromstring(data)


class _Package:
    """Read members in memory with bounded sizes; never extract a member path."""
    def __init__(self, path: Path, limits: dict):
        self.z = zipfile.ZipFile(path)
        self.limits = limits
        self.entries = self.z.infolist()
        if len(self.entries) > limits["max_archive_members"]:
            self.z.close()
            raise _Stop("Archive exceeds max_archive_members")
        self.by_name = {}
        self.issues = []
        total = 0
        folded = set()
        for index, info in enumerate(self.entries):
            why = []
            if not _safe_member(info.filename):
                why.append("unsafe member path")
            canon = info.filename.casefold().rstrip("/")
            if canon in folded:
                why.append("duplicate/case-colliding member path")
            folded.add(canon)
            if stat.S_ISLNK(info.external_attr >> 16):
                why.append("symbolic-link member")
            if info.flag_bits & 1:
                why.append("encrypted member; no password attempt")
            if info.file_size > limits["max_member_bytes"]:
                why.append("member exceeds max_member_bytes")
            if info.file_size > max(1, info.compress_size) * limits["max_compression_ratio"]:
                why.append("compression ratio exceeds max_compression_ratio")
            total += info.file_size
            if why:
                self.issues.append({"index": index, "name": info.filename, "reasons": why})
            self.by_name.setdefault(info.filename, info)
        if total > limits["max_archive_uncompressed_bytes"]:
            self.issues.append({"index": None, "name": None,
                                "reasons": ["archive exceeds max_archive_uncompressed_bytes"]})
        self.total = total

    def close(self):
        self.z.close()

    def check(self):
        if self.issues:
            reasons = "; ".join(why for issue in self.issues for why in issue["reasons"])
            raise _Stop("Unsafe/over-budget container: " + reasons)

    def read(self, name: str) -> bytes:
        if name not in self.by_name:
            raise KeyError("Missing package part: " + name)
        info = self.by_name[name]
        if info.file_size > self.limits["max_member_bytes"]:
            raise _Stop("Member exceeds max_member_bytes: " + name)
        with self.z.open(info) as f:
            data = f.read(self.limits["max_member_bytes"] + 1)
        if len(data) > self.limits["max_member_bytes"]:
            raise _Stop("Member expanded beyond max_member_bytes: " + name)
        return data

    def xml(self, name: str) -> ET.Element:
        return _xml(self.read(name), self.limits["max_xml_bytes"])

    def rels(self, part: str) -> dict:
        folder, name = posixpath.split(part)
        relpath = posixpath.join(folder, "_rels", name + ".rels")
        if relpath not in self.by_name:
            return {}
        result = {}
        for rel in self.xml(relpath):
            rid, target = rel.get("Id"), rel.get("Target", "")
            external = rel.get("TargetMode", "").lower() == "external"
            resolved = None
            if not external:
                resolved = posixpath.normpath(posixpath.join(folder, target)) if not target.startswith("/") else target[1:]
                if not _safe_member(resolved):
                    raise _Stop("Relationship escapes container: " + target, "unsupported")
            if rid in result:
                raise _Stop("Duplicate relationship id in " + part, "parse_failed")
            result[rid] = {"target": target, "part": resolved, "external": external,
                           "type": rel.get("Type", "").rsplit("/", 1)[-1]}
        return result

    def inventory(self) -> list:
        return [{"index": i, "name": x.filename, "size_bytes": x.file_size,
                 "compressed_bytes": x.compress_size, "is_directory": x.is_dir(),
                 "encrypted": bool(x.flag_bits & 1)} for i, x in enumerate(self.entries)]


def probe(path: Path) -> dict:
    """Identify actual magic/container structure; a supported route is not completion."""
    path = Path(path)
    media, route, supported, reason = "application/octet-stream", "asset_only", False, "Unsupported binary/format; not executed"
    try:
        if not path.is_file():
            raise OSError("Input is not a regular file")
        with path.open("rb") as f:
            head = f.read(4096)
        suffix = path.suffix.lower()
        if head.startswith(b"MZ") or suffix in UNSAFE_SUFFIXES:
            reason = "Executable, macro-enabled, legacy or unavailable archive format; asset registration only, never executed"
            media = mimetypes.guess_type(path.name)[0] or media
        elif head.startswith(b"\xd0\xcf\x11\xe0"):
            reason = "Legacy/encrypted OLE container requires an approved isolated converter"
        elif head.lstrip(b"\xef\xbb\xbf\x00\x09\x0a\x0d\x20").startswith(b"%PDF-"):
            media, route, supported, reason = MEDIA["pdf"], "pdf_native_pagewise", True, "pypdf native text/images; OCR routed per page"
        elif head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            if path.stat().st_size > DEFAULT_LIMITS["max_input_bytes"]:
                return {"media_type": MEDIA["zip"], "route": "zip_inventory", "supported": True,
                        "reason": "Container recognized; extraction must enforce input budget"}
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
            macro = any("vbaproject" in n.lower() or n.lower().endswith("vbadata.xml") for n in names)
            if macro:
                reason = "Macro-bearing package detected; asset registration only, no body extraction"
            else:
                format_name = next((fmt for fmt, main in (("docx", "word/document.xml"),
                                   ("pptx", "ppt/presentation.xml"), ("xlsx", "xl/workbook.xml")) if main in names), "zip")
                media, route, supported = MEDIA[format_name], format_name + "_native" if format_name != "zip" else "zip_inventory", True
                reason = "Container structure recognized; member safety/resources checked before reading"
                if suffix in (".docx", ".pptx", ".xlsx") and format_name != suffix[1:]:
                    supported, route, reason = False, "asset_only", "Extension/container mismatch or missing Office main part"
        else:
            from PIL import Image
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(path) as image:
                        media = Image.MIME.get(image.format, "application/octet-stream")
                        if image.width * image.height > DEFAULT_LIMITS["max_image_pixels"]:
                            reason, route, supported = "Image recognized; extraction must enforce pixel budget", "image_native", True
                        else:
                            route, supported, reason = "image_native", True, "Image header recognized; full verification required"
            except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                if suffix in TEXT_SUFFIXES and not (b"\x00" in head and not head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE, codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE))):
                    fmt = TEXT_SUFFIXES[suffix]
                    media, route, supported, reason = MEDIA[fmt], fmt + "_native", True, "Native bounded text decode; no minimum-length threshold"
                elif suffix in (".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".webp"):
                    reason = "Image header invalid or decompression-bomb protection refused it"
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        reason = f"Cannot inspect input: {type(exc).__name__}: {exc}"
    return {"media_type": media, "route": route, "supported": supported, "reason": reason}


class _Result:
    def __init__(self, source: Path, output: Path, info: dict, options: dict):
        self.source, self.output, self.options = source, output, options
        self.limits = dict(DEFAULT_LIMITS)
        for k in self.limits:
            if k in options:
                value = options[k]
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise ValueError(k + " must be a positive integer")
                self.limits[k] = value
        self.data = {"media_type": info["media_type"], "provider_id": PROVIDER_ID,
                     "provider_version": PROVIDER_VERSION, "blocks": [], "files": [],
                     "gaps": [], "metrics": [], "enumeration_state": "complete",
                     "processing_status": "partial", "metadata": {
                         "route": info["route"], "source_name": source.name,
                         "limits": self.limits.copy(), "external_content_loaded": False,
                         "content_executed": False, "derivations": [], "image_inventory": [],
                         "synthetic": bool(options.get("synthetic", False)),
                         "dependencies": {"python": sys.version.split()[0]},
                     }}
        self.bytes_written = 0
        self.dedup = {}
        self.used_parts = set()
        self.ocr_cache = {}
        self.unit_counts = {}
        self.forced_status = None

    @property
    def meta(self):
        return self.data["metadata"]

    def dependency(self, name: str):
        try:
            self.meta["dependencies"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            self.meta["dependencies"][name] = "unavailable"

    def file(self, data: bytes, kind: str, media_type: str, extension: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        signature = (digest, kind, media_type)
        if signature in self.dedup:
            return self.dedup[signature]
        if self.bytes_written + len(data) > self.limits["max_output_bytes"]:
            raise _Stop("Output would exceed max_output_bytes; existing evidence retained")
        key = f"f{len(self.data['files']) + 1:06d}"
        name = key + "-" + kind.replace("_", "-") + "." + re.sub(r"[^a-z0-9]", "", extension.lower())[:12]
        destination = self.output / name
        # Generated flat names, exclusive create; never reuse input/member filenames.
        created = False
        try:
            with destination.open("xb") as f:
                created = True
                f.write(data)
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
        self.data["files"].append({"key": key, "relative_path": name, "kind": kind, "media_type": media_type})
        self.bytes_written += len(data)
        self.dedup[signature] = key
        return key

    def structured(self, value: Any, kind: str = "structured_data") -> str:
        return self.file(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"), kind, "application/json", "json")

    def block(self, kind: str, locator: dict, *, text: str | None = None,
              files: list | None = None, status: str = "extracted", reason: str = "Native content extracted without execution", origin: str = "native_extract") -> list[int]:
        chunks = [None] if text is None else [text[i:i + MAX_BLOCK_TEXT] for i in range(0, len(text), MAX_BLOCK_TEXT)] or [""]
        indexes = []
        if len(self.data["blocks"]) + len(chunks) > self.limits["max_blocks"]:
            raise _Stop("Block count exceeds max_blocks; no text silently truncated")
        for n, chunk in enumerate(chunks):
            loc = dict(locator)
            if "node_id" in loc and len(loc["node_id"]) > 500:
                original = loc["node_id"]
                loc["node_id"] = "node-" + hashlib.sha256(original.encode("utf-8")).hexdigest()
                self.meta.setdefault("locator_node_aliases", {})[loc["node_id"]] = original
            if "cell_range" in loc and not 1 <= len(loc["cell_range"]) <= 200:
                raise _Stop("Invalid/overlong cell range in source; raw cell record retained", "parse_failed")
            if text is not None and (len(chunks) > 1 or "char_start" in loc):
                base = locator.get("char_start", 0)
                loc["char_start"] = base + n * MAX_BLOCK_TEXT
                loc["char_end"] = loc["char_start"] + len(chunk)
            block = {"kind": kind, "origin": origin, "locator": loc, "file_keys": list(files or []),
                     "processing_status": status, "reason": reason[:4000]}
            if chunk is not None:
                block["text"] = chunk
            indexes.append(len(self.data["blocks"]))
            self.data["blocks"].append(block)
        return indexes

    def text(self, text: str, locator: dict, kind: str = "paragraph", **kwargs) -> list[int]:
        key = self.file(text.encode("utf-8"), "native_text", "text/plain; charset=utf-8", "txt")
        return self.block(kind, locator, text=text, files=[key], **kwargs)

    def gap(self, kind: str, reason: str, indexes: list | None = None, severity: str = "important"):
        self.data["gaps"].append({"kind": kind, "severity": severity, "reason": reason[:4000], "block_indexes": list(indexes or [])})

    def deferred(self, kind: str, locator: dict, reason: str, *, gap: str = "unprocessed", text: str | None = None, files: list | None = None) -> list[int]:
        indexes = self.block(kind, locator, text=text, files=files, status="deferred", reason=reason)
        self.gap(gap, reason, indexes)
        return indexes

    def image(self, data: bytes, locator: dict, source_ref: str, *, allow_ocr: bool = True) -> list[int]:
        self.dependency("Pillow")
        from PIL import Image
        ext = Path(source_ref).suffix.lower().lstrip(".")
        try:
            if ext in ("svg", "emf", "wmf"):
                if ext == "svg":
                    _xml(data, self.limits["max_xml_bytes"])
                media = {"svg": "image/svg+xml", "emf": "image/emf", "wmf": "image/wmf"}[ext]
                width = height = frames = None
                reason = "Actual embedded vector bytes retained; rendering/OCR/visual interpretation not performed"
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(data)) as img:
                        width, height = img.size
                        if width * height > self.limits["max_image_pixels"]:
                            raise _Stop("Image exceeds max_image_pixels")
                        media = Image.MIME.get(img.format, "application/octet-stream")
                        ext = {"JPEG": "jpg", "TIFF": "tiff"}.get(img.format, (img.format or "img").lower())
                        img.verify()
                    # verify() alone is only a header check for several formats
                    # (notably JPEG); decode every bounded frame to prove readability.
                    with Image.open(io.BytesIO(data)) as decoded:
                        frames = getattr(decoded, "n_frames", 1)
                        if frames > self.limits["max_image_frames"]:
                            raise _Stop("Image frame count exceeds max_image_frames")
                        total_pixels = 0
                        for frame in range(frames):
                            decoded.seek(frame)
                            total_pixels += decoded.width * decoded.height
                            if total_pixels > self.limits["max_image_pixels"]:
                                raise _Stop("Image total frame pixels exceed max_image_pixels")
                            decoded.load()
                reason = "Actual image bytes fully decoded for validation and retained unchanged; visual interpretation not performed"
            key = self.file(data, "image", media, ext or "img")
        except _Stop:
            raise
        except Exception as exc:
            indexes = self.deferred("image_occurrence", locator,
                                    f"Image validation failed ({source_ref}): {type(exc).__name__}: {exc}", gap="unreadable")
            self.meta["image_inventory"].append({"source_ref": source_ref, "block_indexes": indexes, "retained": False})
            return indexes
        indexes = self.deferred("image_occurrence", locator, reason, gap="semantic_gap", files=[key])
        self.meta["image_inventory"].append({"source_ref": source_ref, "file_key": key,
                                            "block_indexes": indexes, "retained": True,
                                            "width": width, "height": height, "frame_count": frames,
                                            "byte_preserving": True, "visual_interpreted": False})
        if allow_ocr and width is not None:
            self.ocr(key, indexes, locator)
        return indexes

    def ocr(self, key: str, indexes: list[int], locator: dict):
        callback = self.options.get("ocr")
        if not callable(callback):
            self.gap("unprocessed", "OCR not registered; original image retained, no OCR text fabricated", indexes)
            return
        if key not in self.ocr_cache:
            record = next(f for f in self.data["files"] if f["key"] == key)
            try:
                result = callback(self.output / record["relative_path"], options=self.options.get("ocr_options"))
                if not isinstance(result, dict):
                    raise ValueError("OCR callback must return a dict")
                self.ocr_cache[key] = result
            except Exception as exc:
                self.ocr_cache[key] = {"processing_status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
        result = self.ocr_cache[key]
        if (result.get("processing_status") != "extracted" or not result.get("metadata", {}).get("ocr_executed")
                or not isinstance(result.get("text"), str) or not result.get("provider_id") or not result.get("provider_version")):
            self.gap("unprocessed", "OCR unavailable/failed or lacks execution/provenance receipt: " + str(result.get("reason", "invalid callback result")), indexes)
            return
        text = result["text"]
        if not text:
            self.gap("unreadable", "OCR executed but returned no text; this does not establish a blank image", indexes)
            return
        ocr_key = self.file(text.encode("utf-8"), "native_text", "text/plain; charset=utf-8", "txt")
        derived = self.block("paragraph", locator, text=text, files=[ocr_key], origin="ocr",
                             reason="Derived OCR text; original image retained and interpretation remains uncertain")
        # Plain JSON only: callbacks cannot inject paths/files into the parser's artifacts.
        details = {k: result[k] for k in ("boxes", "confidence", "metadata") if k in result}
        try:
            details = json.loads(json.dumps(details, ensure_ascii=False))
        except (TypeError, ValueError):
            details = {"receipt_note": "Callback details were not JSON serializable"}
        self.meta["derivations"].append({"block_indexes": derived, "source_block_indexes": list(indexes),
                                         "source_file_keys": [key], "provider_id": result["provider_id"],
                                         "provider_version": result["provider_version"],
                                         "uncertainty": "OCR may contain recognition errors; not a visual interpretation", "metadata": details})

    def render_pdf_page(self, number: int, cause: str) -> dict | None:
        callback = self.options.get("render_pdf_page")
        if not callable(callback):
            return None
        locator = {"kind": "page", "precision": "page_only", "page_number": number,
                   "node_id": "pdf-page:" + str(number) + ":render"}
        record = {"page_number": number, "source_locator": dict(locator), "cause": cause,
                  "render_executed": False, "visual_interpreted": False}
        self.meta.setdefault("page_renders", []).append(record)
        try:
            options = dict(self.options.get("render_options") or {})
            remaining = self.limits["max_output_bytes"] - self.bytes_written
            if remaining <= 0:
                raise _Stop("No remaining output budget for page render")
            if "max_output_bytes" not in options:
                options["max_output_bytes"] = min(remaining, self.limits["max_member_bytes"])
            elif isinstance(options["max_output_bytes"], int):
                options["max_output_bytes"] = min(remaining, options["max_output_bytes"])
            result = callback(self.source, number, options=options)
            if not isinstance(result, dict):
                raise ValueError("PDF renderer must return a dict")
            reason = str(result.get("reason", "No renderer receipt"))
            record.update({"provider_id": result.get("provider_id"), "provider_version": result.get("provider_version"), "reason": reason})
            if (result.get("processing_status") != "extracted" or result.get("metadata", {}).get("render_executed") is not True
                    or not result.get("provider_id") or not result.get("provider_version")):
                indexes = self.deferred("hidden_object", locator, "PDF page render unavailable/blocked: " + reason,
                                        gap="resource_block" if "RESOURCE" in reason.upper() else "missing_visual")
                record["block_indexes"] = indexes
                return None
            data = result.get("png_bytes")
            if not isinstance(data, bytes) or not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Renderer receipt does not contain actual PNG bytes")
            if len(data) > remaining:
                raise _Stop("Rendered PNG exceeds remaining max_output_bytes")
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                if width * height > self.limits["max_image_pixels"]:
                    raise _Stop("Rendered PNG exceeds max_image_pixels")
                if image.format != "PNG" or getattr(image, "n_frames", 1) != 1 or (width, height) != (result.get("width"), result.get("height")):
                    raise ValueError("Rendered PNG dimensions/type do not match receipt")
                image.load()
            key = self.file(data, "page_render", "image/png", "png")
            indexes = self.deferred("hidden_object", locator,
                                    "Actual PDF page render retained; visual/layout semantics still uninterpreted",
                                    gap="semantic_gap", files=[key])
            details = json.loads(json.dumps(result.get("metadata", {}), ensure_ascii=False))
            record.update({"render_executed": True, "file_key": key, "block_indexes": indexes,
                           "width": width, "height": height, "metadata": details,
                           "uncertainty": "Page-only source locator; no glyph bbox or inverse rotation/crop mapping fabricated"})
            return record
        except _Stop:
            raise
        except Exception as exc:
            indexes = self.deferred("hidden_object", locator, "PDF renderer failed/invalid receipt: " + type(exc).__name__ + ": " + str(exc), gap="parse_failed")
            record.update({"block_indexes": indexes, "reason": type(exc).__name__ + ": " + str(exc)})
            return None

    def metric(self, unit: str, expected: int | None, observed: int, processed: int, ranges: list | None = None):
        self.data["metrics"].append({"unit": unit, "expected": expected, "observed": observed,
                                     "processed": processed, "excluded": 0,
                                     "method": "native_enumeration", "ranges": list(ranges or [])})

    def finish(self) -> dict:
        blocks = self.data["blocks"]
        if not any(b["kind"] == "image_occurrence" for b in blocks):
            rendered_pages = {item["page_number"] for item in self.meta.get("page_renders", []) if item.get("render_executed")}
            refs = [i for i, b in enumerate(blocks) if re.search(r"如下图|见下图|如图所示|as shown (?:below|in (?:the )?figure)", b.get("text", ""), re.I)
                    and b["locator"].get("page_number") not in rendered_pages]
            if refs:
                self.gap("missing_visual", "Text points to a figure but no image occurrence was discovered", refs)
        complete = self.data["enumeration_state"] == "complete"
        processed = sum(b["processing_status"] == "extracted" for b in blocks)
        visuals = [b for b in blocks if b["kind"] == "image_occurrence"]
        self.metric("block", len(blocks) if complete else None, len(blocks), processed)
        self.metric("visual_occurrence", len(visuals) if complete else None, len(visuals),
                    sum(b["processing_status"] == "extracted" for b in visuals))
        if self.forced_status:
            status = self.forced_status
        elif self.data["gaps"] or not complete or processed != len(blocks):
            status = "partial"
        else:
            status = "verified_complete"
        self.data["processing_status"] = status
        self.meta["output_bytes"] = self.bytes_written
        self.meta["discovered_counts"] = {"blocks": len(blocks), "visual_occurrences": len(visuals), "files": len(self.data["files"])}
        return self.data


def _decode(data: bytes, requested: str | None = None) -> tuple[str, str]:
    if requested:
        return data.decode(requested, errors="strict"), requested
    for bom, encoding in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                          (codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16")):
        if data.startswith(bom):
            return data.decode(encoding, errors="strict"), encoding
    if b"\x00" in data:
        raise UnicodeError("NUL bytes without a Unicode BOM; binary/unknown encoding, not guessed")
    try:
        return data.decode("utf-8", errors="strict"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("gb18030", errors="strict"), "gb18030"


def _text(r: _Result, fmt: str):
    data = r.source.read_bytes()
    text, encoding = _decode(data, r.options.get("encoding"))
    r.meta.update({"encoding": encoding, "source_bytes": len(data), "character_count": len(text)})
    raw_key = r.file(text.encode("utf-8"), "native_text", "text/plain; charset=utf-8", "txt")
    loc = {"kind": "asset", "precision": "exact", "char_start": 0, "char_end": len(text)}
    if fmt in ("txt", "md", "json"):
        raw_indexes = r.block("paragraph", loc, text=text, files=[raw_key])
        if fmt == "json":
            try:
                def reject_constant(value):
                    raise ValueError("Non-JSON numeric constant: " + value)
                value = json.loads(text, parse_constant=reject_constant)
                key = r.structured(value)
                r.block("metadata", {"kind": "node", "precision": "exact", "node_id": "json-root"},
                        files=[key], text=json.dumps({"root_type": type(value).__name__}))
            except (ValueError, RecursionError) as exc:
                r.gap("parse_failed", "JSON syntax/structure invalid; complete native text retained: " + str(exc), raw_indexes)
        if fmt == "md":
            for n, match in enumerate(re.finditer(r"!\[([^\]]*)\]\(([^\n]*?)\)|<img\b[^>]*>", text, re.I)):
                image_loc = {"kind": "asset", "precision": "exact", "char_start": match.start(), "char_end": match.end(), "node_id": "markdown-image:" + str(n)}
                r.deferred("image_occurrence", image_loc, "Markdown image reference retained but not fetched/opened: " + match.group(0), gap="missing_visual")
    elif fmt == "csv":
        r.block("metadata", loc, files=[raw_key], text="Complete original CSV decoded string retained")
        row_count, cell_count = 0, 0
        try:
            for n, row in enumerate(csv.reader(io.StringIO(text, newline=""), strict=True)):
                cell_count += len(row)
                if cell_count > r.limits["max_cells"]:
                    raise _Stop("CSV exceeds max_cells")
                row_count += 1
                key = r.structured({"row_index": n, "cells": row}, "table")
                r.block("table", {"kind": "node", "precision": "exact", "node_id": "csv-row:" + str(n)},
                        text=json.dumps(row, ensure_ascii=False), files=[key])
            r.meta["row_count"] = row_count
        except csv.Error as exc:
            r.gap("parse_failed", "CSV parse failed; native text retained: " + str(exc))
            r.data["enumeration_state"] = "partial"
    elif fmt in ("srt", "vtt"):
        _subtitles(r, text, raw_key, fmt)


_TIME = r"(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{3})"
_TIMELINE = re.compile(r"^[ \t]*" + _TIME + r"[ \t]*-->[ \t]*" + _TIME + r"([^\r\n]*)\r?$", re.M)

def _ms(groups: tuple) -> int:
    h, m, s, fraction = groups
    if int(m) >= 60 or int(s) >= 60:
        raise ValueError("Invalid minute/second in subtitle timestamp")
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(fraction)


def _subtitles(r: _Result, text: str, raw_key: str, fmt: str):
    r.block("metadata", {"kind": "asset", "precision": "exact"}, files=[raw_key], text="Complete original subtitle source retained, including cue labels/settings/directives")
    matches = list(_TIMELINE.finditer(text))
    r.meta["subtitle_format"] = fmt
    r.meta["subtitle_cues"] = []
    # Blank-separated groups keep malformed cues visible instead of dropping them.
    groups = list(re.finditer(r"\S[^\r\n]*(?:\r?\n(?![ \t]*\r?\n)[^\r\n]*)*", text))
    ranges = []
    for ordinal, group in enumerate(groups):
        body = group.group(0)
        timeline = _TIMELINE.search(body)
        if not timeline:
            is_directive = fmt == "vtt" and (body.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")))
            if not is_directive:
                indexes = r.block("note", {"kind": "node", "precision": "exact", "node_id": "subtitle-unparsed:" + str(ordinal)},
                                  files=[raw_key], text=body, status="deferred", reason="Unparsed subtitle group; source string retained")
                r.gap("temporal_gap", "Subtitle group lacks a valid timestamp; content not silently discarded", indexes)
            continue
        try:
            start, end = _ms(timeline.groups()[:4]), _ms(timeline.groups()[4:8])
            if end < start:
                raise ValueError("Subtitle end precedes start")
        except ValueError as exc:
            indexes = r.block("note", {"kind": "node", "precision": "exact", "node_id": "subtitle-invalid:" + str(ordinal)},
                              text=body, files=[raw_key], status="deferred", reason=str(exc))
            r.gap("temporal_gap", str(exc), indexes)
            continue
        cue = body[timeline.end():]
        cue = cue[2:] if cue.startswith("\r\n") else cue[1:] if cue.startswith("\n") else cue
        locator = {"kind": "time", "precision": "exact", "start_ms": start, "end_ms": end}
        indexes = r.block("paragraph", locator, text=cue, files=[raw_key])
        r.meta["subtitle_cues"].append({"source_label": body[:timeline.start()], "settings": timeline.groups()[8],
                                        "start_ms": start, "end_ms": end, "block_indexes": indexes})
        ranges.append(locator)
    r.meta["timestamp_line_count"] = len(matches)
    if not ranges:
        r.gap("parse_failed", "No valid subtitle cues found; native source retained")
    elif len(r.meta["subtitle_cues"]) != len(matches):
        r.gap("temporal_gap", "Some timestamp lines could not be accepted; source retained for recovery")


def _image_relation(r: _Result, pkg: _Package, part: str, rels: dict, rid: str | None, locator: dict, *, allow_ocr: bool = True):
    rel = rels.get(rid)
    if not rel:
        return r.deferred("image_occurrence", locator, "Missing image relationship: " + str(rid), gap="missing_visual")
    if rel["external"]:
        r.meta.setdefault("external_images", []).append({"source_part": part, "relationship_id": rid, "target": rel["target"]})
        return r.deferred("image_occurrence", locator, "External image not fetched (no external content permission)", gap="missing_visual")
    r.used_parts.add(rel["part"])
    try:
        return r.image(pkg.read(rel["part"]), locator, rel["part"], allow_ocr=allow_ocr)
    except KeyError:
        return r.deferred("image_occurrence", locator, "Image part absent: " + rel["part"], gap="missing_visual")


def _part_rel_inventory(r: _Result, pkg: _Package, part: str, rels: dict):
    for rid, rel in rels.items():
        if rel["external"]:
            r.meta.setdefault("external_relationships", []).append({"source_part": part, "relationship_id": rid, **rel, "loaded": False})
        if rel["type"] in ("oleObject", "package", "video", "audio", "control"):
            r.deferred("hidden_object", {"kind": "node", "precision": "exact", "node_id": part + ":rel:" + rid},
                       "Embedded object/media/code registered but not executed or opened: " + rel["type"], gap="unsupported")


def _orphans(r: _Result, pkg: _Package, prefix: str):
    for name in pkg.by_name:
        if name.startswith(prefix + "/media/") and not name.endswith("/") and name not in r.used_parts:
            locator = {"kind": "node", "precision": "exact", "node_id": "unreferenced-media:" + str(len(r.used_parts))}
            indexes = r.image(pkg.read(name), locator, name)
            r.gap("unknown_inventory", "Retained package media has no resolved displayed occurrence: " + name, indexes)
            r.used_parts.add(name)


def _word_nodes(node: ET.Element):
    """A paragraph owns its runs, not paragraphs nested inside a text box."""
    yield node
    for child in node:
        if child.tag != _q("w", "p"):
            yield from _word_nodes(child)


def _word_string(node: ET.Element, *, local_paragraph: bool = False) -> str:
    pieces = []
    for e in (_word_nodes(node) if local_paragraph else node.iter()):
        if e.tag in (_q("w", "t"), _q("w", "delText"), _q("w", "instrText"), _q("m", "t")):
            pieces.append(e.text or "")
        elif e.tag == _q("w", "tab"):
            pieces.append("\t")
        elif e.tag in (_q("w", "br"), _q("w", "cr")):
            pieces.append("\n")
        elif e.tag == _q("w", "noBreakHyphen"):
            pieces.append("\u2011")
        elif e.tag == _q("w", "softHyphen"):
            pieces.append("\u00ad")
    return "".join(pieces)


def _docx(r: _Result, pkg: _Package):
    parts = ["word/document.xml"]
    for name in pkg.by_name:
        if re.match(r"word/(?:header[^/]*|footer[^/]*|comments|footnotes|endnotes)\.xml$", name):
            parts.append(name)
    paragraph_index = 0
    r.meta["parts"] = []
    r.meta["hyperlinks"] = []
    r.meta["comment_anchors"] = []
    for part in parts:
        root, rels = pkg.xml(part), pkg.rels(part)
        expected = "document" if part == "word/document.xml" else "hdr" if "/header" in part else "ftr" if "/footer" in part else Path(part).stem
        if root.tag != _q("w", expected):
            raise _Stop("Unsupported/invalid Word part root or namespace: " + part, "parse_failed")
        _part_rel_inventory(r, pkg, part, rels)
        if part.endswith("comments.xml"):
            r.meta["comments"] = [{"attributes": {_local(k): v for k, v in c.attrib.items()}, "text": _word_string(c)} for c in root]
        role = ("header" if "/header" in part else "footer" if "/footer" in part else
                "comment" if part.endswith("comments.xml") else "note" if part.endswith(("footnotes.xml", "endnotes.xml")) else "paragraph")
        r.meta["parts"].append({"part": part, "role": role, "registered": True})
        # Emit tables/paragraphs in source order; table cells retain their own paragraph locators.
        table_no, pno = 0, -1
        for node in root.iter():
            if node.tag == _q("w", "altChunk"):
                r.deferred("hidden_object", {"kind": "node", "precision": "exact", "node_id": part + ":altChunk:" + str(pno)},
                           "Alternate-format Word chunk registered; content not executed or converted", gap="unsupported")
            if node.tag == _q("w", "tbl"):
                rows = [[_word_string(cell) for cell in row.findall("w:tc", NS)] for row in node.findall("w:tr", NS)]
                value = {"part": part, "table_index": table_no, "rows": rows,
                         "cell_properties_xml": [[ET.tostring(cell.find("w:tcPr", NS), encoding="unicode") if cell.find("w:tcPr", NS) is not None else None
                                                   for cell in row.findall("w:tc", NS)] for row in node.findall("w:tr", NS)]}
                key = r.structured(value, "table")
                r.block("table", {"kind": "node", "precision": "exact", "node_id": part + ":table:" + str(table_no)},
                        text=json.dumps(rows, ensure_ascii=False), files=[key])
                table_no += 1
            if node.tag != _q("w", "p"):
                continue
            paragraph = node
            pno += 1
            locator = {"kind": "paragraph", "precision": "exact", "paragraph_index": paragraph_index,
                       "node_id": part + ":p:" + str(pno)}
            text = _word_string(paragraph, local_paragraph=True)
            r.text(text, locator, role)
            offset = 0
            occurrence = 0
            for element in _word_nodes(paragraph):
                if element.tag in (_q("w", "t"), _q("w", "delText"), _q("w", "instrText"), _q("m", "t")):
                    offset += len(element.text or "")
                elif element.tag in (_q("w", "tab"), _q("w", "br"), _q("w", "cr"), _q("w", "noBreakHyphen"), _q("w", "softHyphen")):
                    offset += 1
                elif element.tag in (_q("a", "blip"), _q("v", "imagedata")):
                    rid = element.get(_q("r", "embed")) or element.get(_q("r", "link")) or element.get(_q("r", "id"))
                    iloc = {**locator, "node_id": locator["node_id"] + ":image:" + str(occurrence), "char_start": offset, "char_end": offset}
                    _image_relation(r, pkg, part, rels, rid, iloc)
                    occurrence += 1
                elif element.tag == _q("w", "hyperlink"):
                    rel = rels.get(element.get(_q("r", "id")), {})
                    r.meta["hyperlinks"].append({"paragraph_index": paragraph_index, "text": _word_string(element),
                                                  "target": rel.get("target"), "anchor": element.get(_q("w", "anchor")), "loaded": False})
                elif element.tag == _q("w", "commentReference"):
                    r.meta["comment_anchors"].append({"comment_id": element.get(_q("w", "id")), "paragraph_index": paragraph_index})
            for fno, math in enumerate(paragraph.iter(_q("m", "oMath"))):
                data = ET.tostring(math, encoding="utf-8")
                key = r.file(data, "formula", "application/xml", "xml")
                r.deferred("formula", {**locator, "node_id": locator["node_id"] + ":math:" + str(fno)},
                           "Native equation XML retained; mathematical layout/meaning not verified", gap="semantic_gap", files=[key], text=_word_string(math))
            if any(True for _ in paragraph.iter(_q("w", "vanish"))):
                r.deferred("hidden_object", locator, "Hidden Word text registered; not evaluated for intended display", text=text)
            if any(True for _ in paragraph.iter(_q("w", "del"))) or any(True for _ in paragraph.iter(_q("w", "ins"))):
                r.gap("semantic_gap", "Tracked insertions/deletions retained without accepting/rejecting revisions", [len(r.data["blocks"]) - 1])
            drawings = list(paragraph.iter(_q("w", "drawing"))) + list(paragraph.iter(_q("w", "pict")))
            if any(not any(e.tag in (_q("a", "blip"), _q("v", "imagedata")) for e in d.iter()) for d in drawings):
                r.deferred("hidden_object", locator, "Word vector/shape/drawing requires rendering; no synthetic figure produced", gap="missing_visual")
            paragraph_index += 1
    r.meta["paragraph_count"] = paragraph_index
    _orphans(r, pkg, "word")


def _drawing_string(node: ET.Element) -> str:
    return "".join((e.text or "") if e.tag == _q("a", "t") else "\n" if e.tag == _q("a", "br") else "\t" if e.tag == _q("a", "tab") else "" for e in node.iter())


def _ppt_part(r: _Result, pkg: _Package, part: str, number: int, role: str, *, inherited: bool = False):
    root, rels = pkg.xml(part), pkg.rels(part)
    if root.tag not in {_q("p", name) for name in ("sld", "notes", "sldLayout", "sldMaster")}:
        raise _Stop("Unsupported/invalid presentation part root or namespace: " + part, "parse_failed")
    _part_rel_inventory(r, pkg, part, rels)
    base = {"kind": "slide", "precision": "exact", "slide_number": number}
    for n, paragraph in enumerate(root.iter(_q("a", "p"))):
        if not inherited:
            r.text(_drawing_string(paragraph), {**base, "node_id": part + ":p:" + str(n)}, "note" if role == "notes" else "paragraph")
    for n, table in enumerate(root.iter(_q("a", "tbl"))):
        if not inherited:
            rows = [["\n".join(_drawing_string(p) for p in c.iter(_q("a", "p"))) for c in row.findall("a:tc", NS)] for row in table.findall("a:tr", NS)]
            key = r.structured({"part": part, "rows": rows}, "table")
            r.block("table", {**base, "node_id": part + ":table:" + str(n)}, files=[key], text=json.dumps(rows, ensure_ascii=False))
    for n, blip in enumerate(root.iter(_q("a", "blip"))):
        rid = blip.get(_q("r", "embed")) or blip.get(_q("r", "link"))
        _image_relation(r, pkg, part, rels, rid, {**base, "node_id": role + ":" + part + ":image:" + str(n)})
    for n, shape in enumerate(root.iter(_q("p", "sp"))):
        transform = shape.find("p:spPr/a:xfrm", NS)
        r.meta.setdefault("shape_inventory", []).append({"slide_number": number, "part": part,
                    "shape_index": n, "role": role, "transform_xml": ET.tostring(transform, encoding="unicode") if transform is not None else None})
        if role != "notes":
            r.deferred("hidden_object", {**base, "node_id": part + ":shape:" + str(n)},
                       "Inherited shape display/override not rendered" if inherited else "Shape text extracted, but geometry/style/overlap/flowchart meaning not rendered", gap="missing_visual")
    for n, chart in enumerate(root.iter(_q("c", "chart"))):
        rel = rels.get(chart.get(_q("r", "id")))
        keys = []
        if rel and not rel["external"] and rel["part"] in pkg.by_name:
            keys = [r.file(pkg.read(rel["part"]), "structured_data", "application/xml", "xml")]
        r.deferred("chart", {**base, "node_id": part + ":chart:" + str(n)},
                   "Chart source registered; appearance/meaning not interpreted", gap="semantic_gap", files=keys)
    for n, connector in enumerate(root.iter(_q("p", "cxnSp"))):
        r.deferred("hidden_object", {**base, "node_id": part + ":connector:" + str(n)},
                   "Connector/diagram edge not rendered or semantically interpreted", gap="missing_visual")
    for n, graphic in enumerate(root.iter(_q("a", "graphicData"))):
        if not graphic.get("uri", "").endswith(("/picture", "/table", "/chart")):
            if not any(x.tag == _q("a", "blip") for x in graphic.iter()):
                r.deferred("hidden_object", {**base, "node_id": part + ":graphic:" + str(n)},
                           "SmartArt/unknown graphic registered; rendering required", gap="missing_visual")
    return root, rels


def _pptx(r: _Result, pkg: _Package):
    root, rels = pkg.xml("ppt/presentation.xml"), pkg.rels("ppt/presentation.xml")
    if root.tag != _q("p", "presentation"):
        raise _Stop("Unsupported/invalid presentation root/namespace", "parse_failed")
    slide_ids = root.findall("p:sldIdLst/p:sldId", NS)
    if len(slide_ids) > r.limits["max_pages"]:
        raise _Stop("Slide count exceeds max_pages")
    r.meta["slides"] = []
    ranges = []
    processed = 0
    for number, item in enumerate(slide_ids, 1):
        rel = rels.get(item.get(_q("r", "id")))
        if not rel or rel["external"]:
            r.data["enumeration_state"] = "partial"
            r.deferred("hidden_object", {"kind": "slide", "precision": "exact", "slide_number": number}, "Slide part relationship missing/external", gap="parse_failed")
            continue
        gap_start = len(r.data["gaps"])
        part = rel["part"]
        sroot, srels = _ppt_part(r, pkg, part, number, "slide")
        hidden = sroot.get("show") in ("0", "false")
        description = {"slide_number": number, "part": part, "hidden": hidden, "notes_parts": []}
        loc = {"kind": "slide", "precision": "exact", "slide_number": number}
        key = r.structured({"slide_number": number, "part": part, "hidden": hidden}, "inventory")
        r.block("hidden_object" if hidden else "metadata", {**loc, "node_id": "slide-state:" + str(number)}, text=json.dumps({"hidden": hidden}), files=[key], reason="Slide hidden/display state explicitly registered; hidden slides are not skipped")
        for relation in srels.values():
            if relation["external"]:
                continue
            if relation["type"] == "notesSlide":
                _ppt_part(r, pkg, relation["part"], number, "notes")
                description["notes_parts"].append(relation["part"])
            if relation["type"] == "slideLayout":
                _, lrels = _ppt_part(r, pkg, relation["part"], number, "layout", inherited=True)
                for master in lrels.values():
                    if master["type"] == "slideMaster" and not master["external"]:
                        _ppt_part(r, pkg, master["part"], number, "master", inherited=True)
        r.meta["slides"].append(description)
        if len(r.data["gaps"]) == gap_start:
            processed += 1
            ranges.append(loc)
    r.metric("slide", len(slide_ids), len(r.meta["slides"]), processed, ranges)
    _orphans(r, pkg, "ppt")


def _col_name(index: int) -> str:
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out or "A"


def _xlsx_drawing(r: _Result, pkg: _Package, part: str, sheet_name: str):
    root, rels = pkg.xml(part), pkg.rels(part)
    _part_rel_inventory(r, pkg, part, rels)
    for n, anchor in enumerate(root):
        marker = anchor.find("xdr:from", NS)
        end = anchor.find("xdr:to", NS)
        def cell(node):
            if node is None:
                return None
            col, row = node.findtext("xdr:col", default="0", namespaces=NS), node.findtext("xdr:row", default="0", namespaces=NS)
            return _col_name(int(col) + 1) + str(int(row) + 1)
        first, last = cell(marker), cell(end)
        loc = {"kind": "sheet", "precision": "exact", "sheet_name": sheet_name, "node_id": part + ":anchor:" + str(n)}
        if first:
            loc["cell_range"] = first + (":" + last if last and last != first else "")
        r.meta.setdefault("drawing_anchors", []).append({"sheet_name": sheet_name, "part": part, "anchor_index": n,
                                                         "anchor_type": _local(anchor.tag), "from_cell": first, "to_cell": last,
                                                         "xml": ET.tostring(anchor, encoding="unicode")})
        for j, blip in enumerate(anchor.iter(_q("a", "blip"))):
            _image_relation(r, pkg, part, rels, blip.get(_q("r", "embed")) or blip.get(_q("r", "link")),
                            {**loc, "node_id": loc["node_id"] + ":image:" + str(j)})
        for j, chart in enumerate(anchor.iter(_q("c", "chart"))):
            rel = rels.get(chart.get(_q("r", "id")))
            keys = []
            if rel and not rel["external"] and rel["part"] in pkg.by_name:
                keys = [r.file(pkg.read(rel["part"]), "structured_data", "application/xml", "xml")]
            r.deferred("chart", {**loc, "node_id": loc["node_id"] + ":chart:" + str(j)},
                       "Spreadsheet chart source registered; meaning not interpreted", gap="semantic_gap", files=keys)
        if any(_local(x.tag) in ("sp", "cxnSp", "graphicFrame") for x in anchor.iter()) and not any(x.tag == _q("c", "chart") for x in anchor.iter()):
            r.deferred("hidden_object", loc, "Spreadsheet shape/graphic not rendered", gap="missing_visual")


def _xlsx(r: _Result, pkg: _Package):
    root, rels = pkg.xml("xl/workbook.xml"), pkg.rels("xl/workbook.xml")
    if root.tag != _q("s", "workbook"):
        raise _Stop("Unsupported/invalid workbook root/namespace", "parse_failed")
    strings = []
    if "xl/sharedStrings.xml" in pkg.by_name:
        strings = ["".join(t.text or "" for t in si.iter(_q("s", "t"))) for si in pkg.xml("xl/sharedStrings.xml")]
    sheets = root.findall("s:sheets/s:sheet", NS)
    if len(sheets) > r.limits["max_pages"]:
        raise _Stop("Sheet count exceeds max_pages")
    r.meta["sheets"] = []
    r.meta["defined_names"] = [{"attributes": dict(e.attrib), "text": e.text or ""} for e in root.findall("s:definedNames/s:definedName", NS)]
    cell_count, processed = 0, 0
    ranges = []
    for sheet in sheets:
        name, rel = sheet.get("name", "(unnamed)"), rels.get(sheet.get(_q("r", "id")))
        base = {"kind": "sheet", "precision": "exact", "sheet_name": name}
        if not rel or rel["external"]:
            r.data["enumeration_state"] = "partial"
            r.deferred("hidden_object", base, "Worksheet relationship missing/external", gap="parse_failed")
            continue
        part = rel["part"]
        sroot, srels = pkg.xml(part), pkg.rels(part)
        if sroot.tag != _q("s", "worksheet"):
            raise _Stop("Unsupported/invalid worksheet root/namespace: " + part, "parse_failed")
        gap_start = len(r.data["gaps"])
        info = {"sheet_name": name, "part": part, "state": sheet.get("state", "visible"),
                "dimension": sroot.find("s:dimension", NS).get("ref") if sroot.find("s:dimension", NS) is not None else None,
                "merged_ranges": [x.get("ref") for x in sroot.findall("s:mergeCells/s:mergeCell", NS)],
                "hidden_rows": [x.get("r") for x in sroot.findall("s:sheetData/s:row", NS) if x.get("hidden") in ("1", "true")],
                "hidden_columns": [dict(x.attrib) for x in sroot.findall("s:cols/s:col", NS) if x.get("hidden") in ("1", "true")],
                "formula_count": 0, "missing_formula_caches": [], "hyperlinks": []}
        for row_no, row in enumerate(sroot.findall("s:sheetData/s:row", NS)):
            values = []
            for c in row.findall("s:c", NS):
                cell_count += 1
                if cell_count > r.limits["max_cells"]:
                    raise _Stop("Workbook exceeds max_cells; previously extracted cells retained")
                ref, typ = c.get("r", "row" + str(row_no)), c.get("t", "n")
                raw = c.find("s:v", NS)
                cache = raw.text if raw is not None else None
                formula = c.find("s:f", NS)
                value = cache
                if typ == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(_q("s", "t")))
                elif typ == "s" and cache is not None:
                    try:
                        value = strings[int(cache)]
                    except (IndexError, ValueError):
                        r.gap("parse_failed", "Shared-string reference invalid at " + name + "!" + ref)
                record = {"cell": ref, "cell_type": typ, "value": value, "raw_cached_value": cache,
                          "style_index": c.get("s"), "formula": formula.text or "" if formula is not None else None,
                          "formula_attributes": dict(formula.attrib) if formula is not None else None,
                          "cache_available": cache is not None if formula is not None else None}
                values.append(record)
                if formula is not None:
                    info["formula_count"] += 1
                    fkey = r.structured(record, "formula")
                    ftext = "=" + (formula.text or "")
                    indexes = r.block("formula", {**base, "cell_range": ref}, files=[fkey], text=ftext,
                                      reason="Native formula and separate original cached value retained; never recalculated/external-linked")
                    if cache is None:
                        info["missing_formula_caches"].append(ref)
                        r.gap("unprocessed", "Formula cache missing at " + name + "!" + ref + "; formula not evaluated", indexes)
                    if formula.get("t") == "shared" and not formula.text:
                        r.gap("semantic_gap", "Shared-formula follower recorded without fabricating an expanded formula at " + name + "!" + ref, indexes)
            if values:
                key = r.structured({"sheet_name": name, "row_attributes": dict(row.attrib), "cells": values}, "table")
                cell_range = values[0]["cell"] + (":" + values[-1]["cell"] if len(values) > 1 else "")
                r.block("table", {**base, "cell_range": cell_range}, files=[key], text=json.dumps(values, ensure_ascii=False))
        for link in sroot.findall("s:hyperlinks/s:hyperlink", NS):
            rel_link = srels.get(link.get(_q("r", "id")), {})
            info["hyperlinks"].append({"attributes": dict(link.attrib), "target": rel_link.get("target"), "loaded": False})
        for drawing in sroot.findall("s:drawing", NS):
            drel = srels.get(drawing.get(_q("r", "id")))
            if drel and not drel["external"]:
                _xlsx_drawing(r, pkg, drel["part"], name)
            else:
                r.deferred("hidden_object", base, "Spreadsheet drawing relationship missing/external", gap="missing_visual")
        for relation in srels.values():
            if relation["type"] in ("comments", "threadedComment") and not relation["external"]:
                comments = pkg.xml(relation["part"])
                for n, comment in enumerate(comments.iter(_q("s", "comment"))):
                    text = "".join(t.text or "" for t in comment.iter(_q("s", "t")))
                    r.text(text, {**base, "cell_range": comment.get("ref", "A1"), "node_id": "comment:" + str(n)}, "comment")
        for tag in ("legacyDrawing", "legacyDrawingHF", "oleObjects", "controls", "extLst"):
            if sroot.find("s:" + tag, NS) is not None:
                r.deferred("hidden_object", base, "Spreadsheet " + tag + " registered; unsupported display/object content not executed", gap="unsupported")
        key = r.structured(info, "inventory")
        r.block("hidden_object" if info["state"] != "visible" else "metadata", base,
                text=json.dumps(info, ensure_ascii=False), files=[key], reason="Sheet values/formulas, merges and hidden states separately registered")
        r.meta["sheets"].append(info)
        if len(r.data["gaps"]) == gap_start:
            processed += 1
            ranges.append(base)
        _part_rel_inventory(r, pkg, part, srels)
    if any("/externalLinks/" in name for name in pkg.by_name):
        r.deferred("hidden_object", {"kind": "asset", "precision": "exact"}, "External workbook links registered but not loaded or evaluated", gap="unsupported")
    r.meta["cell_count"] = cell_count
    r.metric("sheet", len(sheets), len(r.meta["sheets"]), processed, ranges)
    _orphans(r, pkg, "xl")


def _zip_inventory(r: _Result, pkg: _Package):
    inventory = pkg.inventory()
    r.meta["archive_members"] = inventory
    r.meta["archive_issues"] = pkg.issues
    key = r.structured({"members": inventory, "issues": pkg.issues, "uncompressed_bytes": pkg.total}, "inventory")
    for member in inventory:
        representable = _safe_member(member["name"]) and len(member["name"].rstrip("/")) <= 2000
        locator = {"kind": "archive_member", "precision": "exact", "member_path": member["name"].rstrip("/")} if representable else {"kind": "node", "precision": "exact", "node_id": "archive-entry:" + str(member["index"])}
        r.block("metadata", locator, files=[key], text=json.dumps(member, ensure_ascii=False),
                status="not_applicable" if member["is_directory"] else "deferred",
                reason="Member enumerated only; not extracted, executed, or recursively processed")
    if inventory:
        r.gap("unprocessed", "ZIP member inventory only; member content deliberately not executed/extracted")
    if pkg.issues:
        r.gap("resource_block", "Unsafe/over-budget archive members registered; no extraction attempted", severity="blocking")
        r.forced_status = "blocked"


def _pdf_matrix(left, right):
    a, b, c, d, e, f = left
    g, h, i, j, k, l = right
    return (a*g + c*h, b*g + d*h, a*i + c*j, b*i + d*j, a*k + c*l + e, b*k + d*l + f)


def _pdf(r: _Result):
    r.dependency("pypdf")
    from pypdf import PdfReader
    from pypdf.generic import ContentStream
    reader = PdfReader(str(r.source), strict=True)
    if reader.is_encrypted:
        r.forced_status = "blocked"
        r.data["enumeration_state"] = "unknown"
        r.gap("access", "Encrypted PDF; no password guess/decrypt attempt", severity="blocking")
        return
    total = len(reader.pages)
    r.meta["page_count"] = total
    r.meta["pages"] = []
    r.meta["pdf_text_spans"] = []
    r.meta["pdf_image_placements"] = []
    if total > r.limits["max_pages"]:
        raise _Stop("PDF page count exceeds max_pages")
    root = reader.trailer.get("/Root", {})
    if hasattr(root, "get_object"):
        root = root.get_object()
    if root.get("/OpenAction") or root.get("/AA") or (root.get("/Names") and any(x in root["/Names"] for x in ("/JavaScript", "/EmbeddedFiles"))):
        r.deferred("hidden_object", {"kind": "asset", "precision": "exact"}, "PDF actions/attachments registered but not executed/opened", gap="unsupported")
    if root.get("/OCProperties"):
        r.deferred("hidden_object", {"kind": "asset", "precision": "exact"}, "PDF optional-content layers registered; visibility not evaluated/rendered", gap="unprocessed")
    processed_pages = 0
    ranges = []
    for page_no, page in enumerate(reader.pages, 1):
        loc = {"kind": "page", "precision": "page_only", "page_number": page_no}
        gap_start = len(r.data["gaps"])
        page_info = {"page_number": page_no, "width_points": float(page.mediabox.width),
                     "height_points": float(page.mediabox.height), "rotation_degrees": int(page.get("/Rotate", 0)),
                     "text_characters": 0, "image_occurrences": 0, "vector_operations": 0,
                     "route": "unknown", "native_text_extracted": False}
        spans = []
        try:
            contents = page.get_contents()
            if contents is not None and len(contents.get_data()) > r.limits["max_pdf_stream_bytes"]:
                raise _Stop("Decoded PDF page content exceeds max_pdf_stream_bytes")
            def visitor(text, cm, tm, font, size):
                if text:
                    spans.append({"text": text, "current_matrix": [float(x) for x in cm],
                                  "text_matrix": [float(x) for x in tm], "font_size": float(size)})
            native_failed = False
            try:
                native = page.extract_text(visitor_text=visitor) or ""
            except Exception as exc:
                native, native_failed = "", True
                r.deferred("paragraph", loc, "Native PDF text extraction failed; image enumeration still attempted: " + type(exc).__name__ + ": " + str(exc), gap="parse_failed")
            page_info["text_characters"] = len(native)
            if native:
                r.text(native, loc)
                page_info["native_text_extracted"] = True
            r.meta["pdf_text_spans"].append({"page_number": page_no, "spans": spans,
                                            "coordinate_note": "Raw pypdf matrices only; glyph bbox/reading order not guaranteed, no invented bbox"})
            image_blocks = []
            op_count = 0
            inline_index = 0
            # Use source content instructions for occurrence enumeration, not unique image resources.
            def walk(stream, resources, path=(), active=(), depth=0, ctm=(1., 0., 0., 1., 0., 0.)):
                nonlocal op_count, inline_index
                if depth > r.limits["max_form_depth"]:
                    raise _Stop("PDF form nesting exceeds max_form_depth")
                if stream is None:
                    return
                content = stream if isinstance(stream, ContentStream) else ContentStream(stream, reader)
                if len(content.get_data()) > r.limits["max_pdf_stream_bytes"]:
                    raise _Stop("Decoded PDF form content exceeds max_pdf_stream_bytes")
                matrix_stack = []
                for operands, operator in content.operations:
                    op_count += 1
                    if op_count > r.limits["max_pdf_operations"]:
                        raise _Stop("PDF instruction count exceeds max_pdf_operations")
                    if operator == b"q":
                        matrix_stack.append(ctm)
                    elif operator == b"Q":
                        if matrix_stack:
                            ctm = matrix_stack.pop()
                        else:
                            r.gap("parse_failed", "Unbalanced PDF graphics-state restore on page " + str(page_no))
                    elif operator == b"cm":
                        ctm = _pdf_matrix(ctm, tuple(float(x) for x in operands))
                    if operator in (b"m", b"l", b"c", b"v", b"y", b"re", b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"sh"):
                        page_info["vector_operations"] += 1
                    names = None
                    source = None
                    if operator == b"Do":
                        resource_name = str(operands[0])
                        xobjects = resources.get("/XObject", {})
                        if hasattr(xobjects, "get_object"):
                            xobjects = xobjects.get_object()
                        reference = xobjects.get(resource_name)
                        if reference is None:
                            r.gap("missing_visual", "PDF Do instruction references an absent XObject on page " + str(page_no))
                            continue
                        obj = reference.get_object()
                        names = path + (resource_name,)
                        if obj.get("/Subtype") == "/Form":
                            identity = (getattr(reference, "idnum", id(obj)), getattr(reference, "generation", 0))
                            if identity in active:
                                r.data["enumeration_state"] = "partial"
                                r.gap("parse_failed", "Cyclic PDF form references stopped on page " + str(page_no))
                                continue
                            nested = obj.get("/Resources", resources)
                            if hasattr(nested, "get_object"):
                                nested = nested.get_object()
                            form_matrix = tuple(float(x) for x in obj.get("/Matrix", (1, 0, 0, 1, 0, 0)))
                            walk(obj, nested, names, active + (identity,), depth + 1, _pdf_matrix(ctm, form_matrix))
                            continue
                        if obj.get("/Subtype") != "/Image":
                            r.deferred("hidden_object", loc, "Unsupported PDF XObject subtype registered, never executed: " + str(obj.get("/Subtype")), gap="unsupported")
                            continue
                        width, height = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
                        if width * height > r.limits["max_image_pixels"]:
                            raise _Stop("PDF image exceeds max_image_pixels")
                        source = "/".join(names)
                    elif operator == b"INLINE IMAGE":
                        settings = operands.get("settings", {}) if isinstance(operands, dict) else {}
                        width = int(settings.get("/W", settings.get("/Width", 0)))
                        height = int(settings.get("/H", settings.get("/Height", 0)))
                        if width * height > r.limits["max_image_pixels"]:
                            raise _Stop("Inline PDF image exceeds max_image_pixels")
                        names = ("~" + str(inline_index) + "~",)
                        source = "inline:" + str(inline_index)
                        inline_index += 1
                    if names is None:
                        continue
                    image_no = page_info["image_occurrences"]
                    page_info["image_occurrences"] += 1
                    iloc = {**loc, "node_id": "pdf-page:" + str(page_no) + ":image:" + str(image_no)}
                    try:
                        image = page.images[names[0] if len(names) == 1 else list(names)]
                        indexes = r.image(image.data, iloc, image.name, allow_ocr=False)
                        a, b, c, d, e, f = ctm
                        corners = [(e, f), (a + e, b + f), (c + e, d + f), (a + c + e, b + d + f)]
                        xs, ys = [p[0] for p in corners], [p[1] for p in corners]
                        r.meta["pdf_image_placements"].append({"page_number": page_no, "source_resource_path": source,
                                                                 "block_indexes": indexes, "content_matrix": list(ctm),
                                                                 "bbox_xywh": [min(xs), min(ys), max(xs)-min(xs), max(ys)-min(ys)],
                                                                 "unit": "pdf_point", "origin": "bottom_left",
                                                                 "coordinate_note": "Unrotated PDF user space; page Rotate, crop, clipping and mask appearance not applied",
                                                                 "representation": "pypdf native image extraction, not page rendering"})
                    except _Stop:
                        raise
                    except Exception as exc:
                        indexes = r.deferred("image_occurrence", iloc, "PDF native image extraction failed: " + type(exc).__name__ + ": " + str(exc), gap="missing_visual")
                    image_blocks.extend(indexes)
            resources = page.get("/Resources", {})
            if hasattr(resources, "get_object"):
                resources = resources.get_object()
            walk(contents, resources)
            suspect_text = "\ufffd" in native or "\x00" in native
            readable_native = bool(native.strip()) and not native_failed and not suspect_text
            need_render = bool(page_info["vector_operations"] or native_failed or suspect_text or (not readable_native and page_info["image_occurrences"]))
            page_info["render_attempted"] = need_render and callable(r.options.get("render_pdf_page"))
            rendered = r.render_pdf_page(page_no, "vector_or_unreadable_page") if need_render else None
            page_info["page_render_retained"] = bool(rendered)
            if readable_native:
                page_info["route"] = "native_text"
            elif suspect_text:
                page_info["route"] = "native_text_needs_review"
                r.gap("unreadable", "Native PDF text contains replacement/NUL characters; page-local OCR remains a derived layer")
            elif page_info["image_occurrences"]:
                page_info["route"] = "image_ocr"
            elif page_info["vector_operations"]:
                page_info["route"] = "page_render_ocr_required"
                if not rendered:
                    r.deferred("hidden_object", loc, "Textless vector-only PDF page needs a renderer/OCR; pypdf cannot rasterize it", gap="missing_visual")
            elif native_failed:
                page_info["route"] = "native_text_failed"
            else:
                page_info["route"] = "blank_native"
                key = r.structured({"page_number": page_no, "blank_by_native_enumeration": True}, "inventory")
                r.block("metadata", loc, files=[key], text="No native text/image/paint operation discovered on this page")
            if not readable_native and need_render:
                if rendered:
                    # Prefer one correctly composed/rotated page OCR, not the same
                    # text once per native image and again per page rendering.
                    r.ocr(rendered["file_key"], rendered["block_indexes"], loc)
                    page_info["ocr_source"] = "page_render"
                else:
                    for index in image_blocks:
                        block = r.data["blocks"][index]
                        if block["file_keys"]:
                            r.ocr(block["file_keys"][0], [index], block["locator"])
                    if image_blocks:
                        page_info["ocr_source"] = "embedded_image"
                    if page_info["image_occurrences"] and not callable(r.options.get("ocr")):
                        r.gap("unprocessed", "Image-only PDF page requires OCR; engine not registered, page retained as partial", image_blocks)
            if page_info["vector_operations"]:
                r.deferred("chart", loc,
                           "PDF vector paint operations retained in actual page render; visual meaning not interpreted" if rendered else "PDF vector paint operations discovered; appearance/layout not rendered or interpreted",
                           gap="semantic_gap" if rendered else "missing_visual", files=[rendered["file_key"]] if rendered else None)
            if page.get("/Annots"):
                r.deferred("comment", loc, "PDF annotations/forms registered; not flattened/rendered or executed", gap="unprocessed")
            if len(r.data["gaps"]) == gap_start:
                processed_pages += 1
                ranges.append(loc)
        except _Stop:
            r.meta["pages"].append(page_info)
            raise
        except Exception as exc:
            r.data["enumeration_state"] = "partial"
            r.deferred("metadata", loc, "PDF page parse failed: " + type(exc).__name__ + ": " + str(exc), gap="parse_failed")
            if not page_info.get("render_attempted") and callable(r.options.get("render_pdf_page")):
                page_info["render_attempted"] = True
                rendered = r.render_pdf_page(page_no, "native_page_parse_failed")
                page_info["page_render_retained"] = bool(rendered)
                if rendered and not page_info.get("native_text_extracted"):
                    r.ocr(rendered["file_key"], rendered["block_indexes"], loc)
                    page_info["route"] = "render_fallback_after_parse_failure"
        r.meta["pages"].append(page_info)
    r.metric("page", total, len(r.meta["pages"]), processed_pages, ranges)


def extract(path: Path, output_dir: Path, *, options: dict | None = None) -> dict:
    """Extract local evidence safely; caller owns authorization, retention and publication.

    The output directory must already exist, be empty and not be a symlink.
    Unsupported/bad/encrypted files return explicit gaps instead of pseudo-text.
    Invalid caller options/directory preconditions raise ValueError.
    """
    path, output_dir = Path(path), Path(output_dir)
    if not output_dir.is_dir() or output_dir.is_symlink() or any(output_dir.iterdir()):
        raise ValueError("output_dir must be an existing empty, non-symlink directory")
    options = {} if options is None else dict(options)
    info = probe(path)
    r = _Result(path, output_dir, info, options)
    try:
        if not path.is_file():
            r.forced_status, r.data["enumeration_state"] = "blocked", "unknown"
            r.gap("access", info["reason"], severity="blocking")
        elif path.stat().st_size > r.limits["max_input_bytes"]:
            raise _Stop("Input exceeds max_input_bytes; not fully read")
        elif not info["supported"]:
            r.forced_status = "blocked"
            r.data["enumeration_state"] = "unknown"
            r.deferred("metadata", {"kind": "asset", "precision": "exact"}, info["reason"], gap="unsupported")
        elif info["route"] in ("txt_native", "md_native", "json_native", "csv_native", "srt_native", "vtt_native"):
            _text(r, info["route"].split("_")[0])
        elif info["route"] == "image_native":
            r.image(path.read_bytes(), {"kind": "asset", "precision": "exact"}, path.name)
        elif info["route"] == "pdf_native_pagewise":
            _pdf(r)
        else:
            pkg = _Package(path, r.limits)
            try:
                if info["route"] == "zip_inventory":
                    _zip_inventory(r, pkg)
                else:
                    r.meta["package_member_count"] = len(pkg.entries)
                    r.meta["package_uncompressed_bytes"] = pkg.total
                    pkg.check()
                    macro = any("vbaproject" in n.lower() for n in pkg.by_name)
                    if macro:
                        r.forced_status = "blocked"
                        r.deferred("metadata", {"kind": "asset", "precision": "exact"}, "Macro-bearing package detected; no body extracted", gap="unsupported")
                    elif info["route"] == "docx_native":
                        _docx(r, pkg)
                    elif info["route"] == "pptx_native":
                        _pptx(r, pkg)
                    elif info["route"] == "xlsx_native":
                        _xlsx(r, pkg)
            finally:
                pkg.close()
    except _Stop as exc:
        r.data["enumeration_state"] = "partial" if r.data["blocks"] else "unknown"
        r.gap(exc.kind, str(exc), severity="blocking")
        if not r.data["blocks"]:
            r.forced_status = "blocked"
    except Exception as exc:
        r.data["enumeration_state"] = "partial" if r.data["blocks"] else "unknown"
        r.gap("parse_failed", type(exc).__name__ + ": " + str(exc), severity="blocking")
        if not r.data["blocks"]:
            r.forced_status = "failed"
    return r.finish()
