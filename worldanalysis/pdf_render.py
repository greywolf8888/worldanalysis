"""One-page, bounded, offline CPU PDF rasterization.

The explicitly admitted backend is pypdfium2==5.15.2 (bindings:
Apache-2.0 OR BSD-3-Clause; PDFium BSD-style/Apache and bundled third-party
notices). Only the ordinary, non-V8/non-XFA binary is admitted. No runtime
installation, model download, URL fetching, forms environment, JavaScript,
actions, attachment extraction, source modification, or output-path writes.

render_pdf_page(Path, one_based_page, options=None) returns actual PNG bytes
in ``png_bytes`` with width/height/provider fields. Failures have png_bytes=None.
The caller retains the bytes. Rasterization is NOT OCR/visual interpretation.
Page rotation/crop is handled by PDFium; no inverse geometry is invented.

Resource gates cover source bytes, page dimensions BEFORE bitmap allocation,
bitmap bytes, embedded image dimensions and encoded PNG bytes. PDFium's native
parser/codec internals are not an OS-enforced memory/time sandbox; metadata
states this limit rather than claiming a hard process-wide bound.
"""
from __future__ import annotations

import importlib.metadata
import io
import math
import re
import threading
import time
from pathlib import Path

PROVIDER_ID = "pypdfium2"
PROVIDER_VERSION = "5.15.2"
DEFAULT_OPTIONS = {
    "dpi": 144,
    "max_input_bytes": 128 * 1024 * 1024,
    "max_pixels": 16_000_000,
    "max_bitmap_bytes": 64 * 1024 * 1024,
    "max_output_bytes": 32 * 1024 * 1024,
    "max_source_image_pixels": 64_000_000,
    "max_resource_objects": 10000,
    "max_form_depth": 16,
}
LICENSE_SOURCES = [
    "https://pypi.org/project/pypdfium2/5.15.2/",
    "https://raw.githubusercontent.com/pypdfium2-team/pypdfium2/5.15.2/src/pypdfium2/__init__.py",
    "https://raw.githubusercontent.com/pypdfium2-team/pypdfium2/5.15.2/LICENSES/Apache-2.0.txt",
    "https://raw.githubusercontent.com/pypdfium2-team/pypdfium2/5.15.2/LICENSES/BSD-3-Clause.txt",
    "https://pdfium.googlesource.com/pdfium/+/refs/heads/main/LICENSE",
]
_LOCK = threading.RLock()  # PDFium is not generally thread-safe.


class _ResourceBlocked(Exception):
    pass


class _PngBuffer(io.BytesIO):
    def __init__(self, cap: int):
        super().__init__()
        self.cap = cap

    def write(self, value):
        if self.tell() + len(value) > self.cap:
            raise _ResourceBlocked("BLOCKED_RESOURCE: encoded PNG exceeds max_output_bytes")
        return super().write(value)


def _source_path(value, allowed_root=None) -> Path:
    raw = str(value)
    if (not raw or "\x00" in raw or raw.startswith(("//", "\\\\"))
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw)
            or ":" in (raw[2:] if re.match(r"^[A-Za-z]:[\\/]", raw) else raw)):
        raise ValueError("LOCAL_PATH_REQUIRED: no URL, network/device path or alternate data stream")
    path = Path(value).absolute()
    if ".." in path.parts:
        raise ValueError("UNSAFE_PATH: parent traversal")
    for component in (path, *path.parents):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise ValueError("UNSAFE_PATH: symbolic link/junction")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("LOCAL_FILE_REQUIRED")
    if allowed_root is not None and not path.is_relative_to(Path(allowed_root).resolve(strict=True)):
        raise ValueError("OUTSIDE_AUTHORIZED_ROOT")
    return path


def _audit_images(page, limits):
    """Dimension/resource audit without decoding XObject streams or fetching links."""
    visited, active = set(), set()
    count, largest = 0, 0

    def walk(resources, depth):
        nonlocal count, largest
        if depth > limits["max_form_depth"]:
            raise _ResourceBlocked("BLOCKED_RESOURCE: PDF form/resource nesting limit")
        if hasattr(resources, "get_object"):
            resources = resources.get_object()
        xobjects = resources.get("/XObject", {})
        if hasattr(xobjects, "get_object"):
            xobjects = xobjects.get_object()
        for reference in xobjects.values():
            identity = (getattr(reference, "idnum", id(reference)), getattr(reference, "generation", 0))
            if identity in active:
                raise ValueError("CYCLIC_PDF_FORM: refusing native render")
            if identity in visited:
                continue
            visited.add(identity)
            count += 1
            if count > limits["max_resource_objects"]:
                raise _ResourceBlocked("BLOCKED_RESOURCE: PDF resource-object count")
            obj = reference.get_object()
            if obj.get("/Subtype") == "/Image":
                width, height = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
                if min(width, height) <= 0:
                    raise ValueError("INVALID_PDF_IMAGE_DIMENSIONS")
                largest = max(largest, width * height)
                if width * height > limits["max_source_image_pixels"]:
                    raise _ResourceBlocked("BLOCKED_RESOURCE: embedded image exceeds max_source_image_pixels")
            elif obj.get("/Subtype") == "/Form":
                active.add(identity)
                walk(obj.get("/Resources", resources), depth + 1)
                active.remove(identity)
    walk(page.get("/Resources", {}), 0)
    return {"resource_objects": count, "largest_source_image_pixels": largest}


def render_pdf_page(path: Path, page_number: int, options: dict | None = None) -> dict:
    """Render a single authorized local PDF page, one-based, using CPU only.

    Options: DEFAULT_OPTIONS plus allowed_root. No automatic lower-resolution
    fallback: oversize pages explicitly return blocked and retain no pretend PNG.
    Changing dpi is a new explicit caller decision; no silent image shrinking.
    """
    start = time.monotonic()
    result = {"processing_status": "unsupported", "reason": "PDF_RENDER_NOT_EXECUTED",
              "png_bytes": None, "width": None, "height": None, "media_type": "image/png",
              "provider_id": PROVIDER_ID, "provider_version": PROVIDER_VERSION,
              "metadata": {"render_executed": False, "runtime": "local_cpu", "network_used": False,
                           "actions_executed": False, "forms_initialized": False, "annotations_rendered": False,
                           "license": "Apache-2.0 OR BSD-3-Clause (bindings); PDFium/bundled notices retained",
                           "license_sources": LICENSE_SOURCES,
                           "coordinate_scope": "rendered-page pixels only; source rotation/crop handled by PDFium; no inverse transform claimed",
                           "resource_limit_scope": "Input/target bitmap/PNG/dimensions guarded; native parser and codec internals are not process-isolated"}}
    pdf = page = bitmap = pil = None
    try:
        supplied = dict(options or {})
        limits = dict(DEFAULT_OPTIONS)
        for key in limits:
            if key in supplied:
                limits[key] = supplied[key]
            if key != "dpi" and (not isinstance(limits[key], int) or isinstance(limits[key], bool) or limits[key] <= 0):
                raise ValueError("INVALID_RENDER_LIMIT: " + key)
        dpi = limits["dpi"]
        if isinstance(dpi, bool) or not isinstance(dpi, (int, float)) or not math.isfinite(dpi) or not 0 < dpi <= 600:
            raise ValueError("INVALID_RENDER_DPI: must be finite, >0 and <=600")
        if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
            raise ValueError("PAGE_NUMBER_MUST_BE_ONE_BASED_INTEGER")
        source = _source_path(path, supplied.get("allowed_root"))
        if source.stat().st_size > limits["max_input_bytes"]:
            raise _ResourceBlocked("BLOCKED_RESOURCE: PDF exceeds max_input_bytes")
        with source.open("rb") as file:
            data = file.read(limits["max_input_bytes"] + 1)
        if len(data) > limits["max_input_bytes"]:
            raise _ResourceBlocked("BLOCKED_RESOURCE: PDF grew beyond max_input_bytes")
        if b"%PDF-" not in data[:1024]:
            raise ValueError("PDF_MAGIC_REQUIRED")
        result["metadata"].update({"page_number": page_number, "input_bytes": len(data), "limits": limits})
        # Reject encryption even if an empty password would open it; never guess.
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            result.update({"processing_status": "blocked", "reason": "ENCRYPTED_PDF: no password/decrypt attempt"})
            return result
        if page_number > len(reader.pages):
            raise ValueError("PAGE_OUT_OF_RANGE")
        result["metadata"].update(_audit_images(reader.pages[page_number - 1], limits))
        try:
            installed = importlib.metadata.version(PROVIDER_ID)
        except importlib.metadata.PackageNotFoundError:
            result["reason"] = "PDF_RENDER_DEPENDENCY_MISSING: pypdfium2==" + PROVIDER_VERSION
            return result
        result["provider_version"] = installed
        if installed != PROVIDER_VERSION:
            result["reason"] = "PDF_RENDER_VERSION_NOT_ADMITTED: expected " + PROVIDER_VERSION
            return result
        import pypdfium2 as pdfium
        flags = tuple(pdfium.PDFIUM_INFO.flags)
        if any("V8" in str(flag).upper() or "XFA" in str(flag).upper() for flag in flags):
            result["reason"] = "PDF_RENDER_BUILD_NOT_ADMITTED: V8/XFA-enabled binary"
            return result
        result["metadata"].update({"pdfium_version": str(pdfium.PDFIUM_INFO), "pdfium_flags": list(flags)})
        with _LOCK:
            try:
                pdf = pdfium.PdfDocument(data)
                width_points, height_points = pdf.get_page_size(page_number - 1)
                if not all(math.isfinite(v) and v > 0 for v in (width_points, height_points)):
                    raise ValueError("INVALID_PDF_PAGE_DIMENSIONS")
                scale = dpi / 72.0
                width, height = math.ceil(width_points * scale), math.ceil(height_points * scale)
                bitmap_bytes = width * height * 4
                result["metadata"].update({"page_size_canvas_units": {"width": width_points, "height": height_points},
                                           "dpi_requested": dpi, "scale": scale, "predicted_pixels": width * height,
                                           "predicted_bitmap_bytes": bitmap_bytes,
                                           "preflight_before_bitmap_allocation": True,
                                           "user_unit_note": "PDFium canvas units; nondefault UserUnit is not interpreted as physical DPI"})
                if width * height > limits["max_pixels"]:
                    raise _ResourceBlocked("BLOCKED_RESOURCE: target page exceeds max_pixels before bitmap allocation")
                if bitmap_bytes > limits["max_bitmap_bytes"]:
                    raise _ResourceBlocked("BLOCKED_RESOURCE: target page exceeds max_bitmap_bytes before allocation")
                page = pdf[page_number - 1]
                result["metadata"]["source_page_rotation_degrees"] = page.get_rotation()
                bitmap = page.render(scale=scale, rotation=0, may_draw_forms=False, draw_annots=False,
                                     force_bitmap_format=pdfium.raw.FPDFBitmap_BGRA, rev_byteorder=True,
                                     fill_color=(255, 255, 255, 255), limit_image_cache=True)
                pil = bitmap.to_pil()
                if pil.size != (width, height):
                    raise ValueError("RENDER_SIZE_CHANGED: refusing unverifiable dimensions")
                with _PngBuffer(limits["max_output_bytes"]) as buffer:
                    pil.save(buffer, format="PNG")
                    png = buffer.getvalue()
                result.update({"processing_status": "extracted", "reason": "PDF_PAGE_RENDERED_NOT_INTERPRETED",
                               "png_bytes": png, "width": width, "height": height})
                result["metadata"].update({"render_executed": True, "png_bytes": len(png),
                                           "frame_size": {"width": width, "height": height}, "origin": "top_left",
                                           "uniform_rgba_channels": all(lo == hi for lo, hi in pil.getextrema())})
            finally:
                # Pixel views must be released before their underlying bitmap.
                for resource in (pil, bitmap, page, pdf):
                    if resource is not None:
                        resource.close()
    except _ResourceBlocked as exc:
        result.update({"processing_status": "blocked", "reason": str(exc)})
    except (OSError, ValueError, TypeError) as exc:
        result.update({"processing_status": "failed", "reason": type(exc).__name__ + ": " + str(exc)})
    except Exception as exc:
        result.update({"processing_status": "failed", "reason": "PDF_RENDER_FAILED: " + type(exc).__name__ + ": " + str(exc)})
    finally:
        result["metadata"]["elapsed_seconds"] = round(time.monotonic() - start, 6)
    return result
