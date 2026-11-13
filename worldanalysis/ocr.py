"""One offline, CPU-only OCR adapter. No model download or URL input at runtime.

Only successful *inference*, not importing a package, is labelled OCR. Coordinates
refer to the oriented input; the inverse matrix maps them to the preserved source.
The caller remains responsible for retaining that source and registering lineage.
"""
from __future__ import annotations

from functools import lru_cache
from importlib import metadata as distributions
from pathlib import Path
import math
import re
import threading
import time
from typing import Any

PROVIDER_ID = "rapidocr_onnxruntime"
PROVIDER_VERSION = "1.4.4"
_MODEL_NAMES = ("ch_PP-OCRv4_det_infer.onnx", "ch_ppocr_mobile_v2.0_cls_infer.onnx", "ch_PP-OCRv4_rec_infer.onnx")
_LOCK = threading.RLock()
_IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1]


def _local_file(value: Any, root: Any = None) -> Path:
    raw = str(value)
    if (not raw or "\x00" in raw or raw.startswith(("//", "\\\\"))
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw)
            or ":" in (raw[2:] if re.match(r"^[A-Za-z]:[\\/]", raw) else raw)):
        raise ValueError("LOCAL_PATH_REQUIRED: network, device and URL inputs are not readers")
    path = Path(value).absolute()
    if ".." in path.parts:
        raise ValueError("UNSAFE_PATH: parent traversal")
    for component in (path, *path.parents):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise ValueError("UNSAFE_PATH: symlink or junction")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("LOCAL_FILE_REQUIRED")
    if root is not None and not resolved.is_relative_to(Path(root).resolve(strict=True)):
        raise ValueError("OUTSIDE_AUTHORIZED_ROOT")
    return resolved


def _multiply(a, b):
    return [sum(a[r * 3 + k] * b[k * 3 + c] for k in range(3)) for r in range(3) for c in range(3)]


def _map_polygon(polygon, matrix):
    return [[matrix[0] * x + matrix[1] * y + matrix[2],
             matrix[3] * x + matrix[4] * y + matrix[5]] for x, y in polygon]


def _orient(image, clockwise):
    from PIL import Image, ImageOps
    width, height = image.size
    orientation = int(image.getexif().get(274, 1))
    matrices = {
        1: _IDENTITY, 2: [-1, 0, width, 0, 1, 0, 0, 0, 1],
        3: [-1, 0, width, 0, -1, height, 0, 0, 1],
        4: [1, 0, 0, 0, -1, height, 0, 0, 1],
        5: [0, 1, 0, 1, 0, 0, 0, 0, 1],
        6: [0, 1, 0, -1, 0, height, 0, 0, 1],
        7: [0, -1, width, -1, 0, height, 0, 0, 1],
        8: [0, -1, width, 1, 0, 0, 0, 0, 1],
    }
    inverse = list(matrices.get(orientation, _IDENTITY))
    oriented = ImageOps.exif_transpose(image)
    ow, oh = oriented.size
    if clockwise == 90:
        oriented = oriented.transpose(Image.Transpose.ROTATE_270)
        inverse = _multiply(inverse, [0, 1, 0, -1, 0, oh, 0, 0, 1])
    elif clockwise == 180:
        oriented = oriented.transpose(Image.Transpose.ROTATE_180)
        inverse = _multiply(inverse, [-1, 0, ow, 0, -1, oh, 0, 0, 1])
    elif clockwise == 270:
        oriented = oriented.transpose(Image.Transpose.ROTATE_90)
        inverse = _multiply(inverse, [0, -1, ow, 1, 0, 0, 0, 0, 1])
    exif_rotation = {3: 180, 6: 90, 8: 270}.get(orientation, 0)
    return oriented.convert("RGB"), inverse, orientation, (exif_rotation + clockwise) % 360


@lru_cache(maxsize=1)
def _engine(model_dir: str, max_side: int):
    # RapidOCR 1.4.4 has bundled models and never fetches them here. Explicit
    # CPU flags avoid DirectML/CUDA provider auto-selection on this laptop.
    from rapidocr_onnxruntime import RapidOCR
    det, cls, rec = [str(Path(model_dir) / name) for name in _MODEL_NAMES]
    return RapidOCR(det_model_path=det, cls_model_path=cls, rec_model_path=rec,
                    det_use_cuda=False, cls_use_cuda=False, rec_use_cuda=False,
                    det_use_dml=False, cls_use_dml=False, rec_use_dml=False,
                    intra_op_num_threads=1, inter_op_num_threads=1,
                    det_limit_type="max", det_limit_side_len=min(960, max_side),
                    max_side_len=max_side, rec_batch_num=1, cls_batch_num=1)


def ocr_image(path: Path, *, options: dict | None = None) -> dict:
    """Read one authorized local image using the pinned CPU OCR backend.

    Options: allowed_root, max_input_bytes (32 MiB), max_pixels (16M),
    rotation_degrees (clockwise, multiples of 90), max_side_len (2000),
    model_dir (already reviewed local weights only). Original bytes are never
    rewritten. Unsupported/missing weights never trigger network access.
    """
    options = dict(options or {})
    result = {"text": "", "boxes": [], "confidence": None,
              "provider_id": PROVIDER_ID, "provider_version": PROVIDER_VERSION,
              "processing_status": "unsupported", "reason": "OCR_NOT_EXECUTED",
              "metadata": {"ocr_executed": False, "runtime": "local_cpu",
                           "weights_license": "Apache-2.0", "lossy_recognition": True}}
    start = time.monotonic()
    try:
        source = _local_file(path, options.get("allowed_root"))
        size = source.stat().st_size
        byte_cap = int(options.get("max_input_bytes", 32 * 1024 * 1024))
        pixel_cap = int(options.get("max_pixels", 16_000_000))
        max_side = int(options.get("max_side_len", 2000))
        rotation = int(options.get("rotation_degrees", 0)) % 360
        if min(byte_cap, pixel_cap) <= 0 or not 32 <= max_side <= 4096 or rotation not in (0, 90, 180, 270):
            raise ValueError("INVALID_OCR_LIMIT_OR_ROTATION")
        if size > byte_cap:
            result["reason"] = "BLOCKED_RESOURCE: image input byte limit"
            return result
        from PIL import Image
        with Image.open(source) as image:
            width, height = image.size
            if width * height > pixel_cap:
                result["reason"] = "BLOCKED_RESOURCE: image pixel limit (checked before decode)"
                return result
            if getattr(image, "n_frames", 1) != 1:
                result["reason"] = "UNSUPPORTED_MULTIFRAME_IMAGE: enumerate frames explicitly"
                return result
            oriented, inverse, exif_orientation, degrees = _orient(image, rotation)
        fw, fh = oriented.size
        result["metadata"].update({
            "original_path": str(source), "input_bytes": size,
            "original_width": width, "original_height": height,
            "width": fw, "height": fh, "frame_size": {"width": fw, "height": fh},
            "original_size": {"width": width, "height": height, "unit": "pixel"},
            "exif_orientation": exif_orientation, "rotation_degrees": degrees,
            "coordinate_space": "derived" if inverse != _IDENTITY else "original",
            "unit": "pixel", "origin": "top_left",
            "transform_to_original": {"method": "matrix_3x3" if inverse != _IDENTITY else "identity",
                                      "operations": ["rotate", "origin_change"] if inverse != _IDENTITY else [],
                                      **({"matrix": inverse} if inverse != _IDENTITY else {})},
            "source_retention": "caller_must_retain_original; this adapter never overwrites it"})
        try:
            version = distributions.version(PROVIDER_ID)
        except distributions.PackageNotFoundError:
            result["reason"] = "OCR_DEPENDENCY_MISSING: rapidocr_onnxruntime==1.4.4"
            return result
        result["provider_version"] = version
        if version != PROVIDER_VERSION:
            result["reason"] = "OCR_VERSION_NOT_ADMITTED: expected 1.4.4"
            return result
        import rapidocr_onnxruntime
        default_models = Path(rapidocr_onnxruntime.__file__).parent / "models"
        models = Path(options.get("model_dir", default_models))
        if not models.is_dir() or any(not (models / name).is_file() for name in _MODEL_NAMES):
            result["reason"] = "OCR_WEIGHTS_MISSING: no download attempted"
            return result
        # This option is for copies of the admitted bundled models, not arbitrary
        # models with a new or unspecified licence.
        if models.resolve() != default_models.resolve():
            import hashlib
            def model_hash(path):
                digest = hashlib.sha256()
                with _local_file(path).open("rb") as handle:
                    for data in iter(lambda: handle.read(65536), b""):
                        digest.update(data)
                return digest.digest()
            for name in _MODEL_NAMES:
                if ((models / name).stat().st_size != (default_models / name).stat().st_size
                        or model_hash(models / name) != model_hash(default_models / name)):
                    result["reason"] = "OCR_WEIGHTS_NOT_ADMITTED: differs from reviewed pinned wheel"
                    return result
        with _LOCK:
            raw, elapsed = _engine(str(models.resolve()), max_side)(oriented)
        result["metadata"].update({"ocr_executed": True, "backend_stage_seconds": elapsed,
                                   "rotation_scope": "EXIF/manual orientation; classifier may rotate individual text crops"})
        boxes = []
        for item in raw or []:
            polygon, text, confidence = item[:3]
            polygon = [[float(x), float(y)] for x, y in polygon]
            confidence = float(confidence)
            if (len(polygon) != 4 or not math.isfinite(confidence) or not 0 <= confidence <= 1
                    or any(not math.isfinite(v) for xy in polygon for v in xy)):
                raise ValueError("OCR_INVALID_GEOMETRY_OR_CONFIDENCE")
            if not all(-1 <= x <= fw + 1 and -1 <= y <= fh + 1 for x, y in polygon):
                raise ValueError("OCR_BOX_OUTSIDE_FRAME")
            xs, ys = zip(*polygon)
            if max(xs) <= min(xs) or max(ys) <= min(ys):
                raise ValueError("OCR_DEGENERATE_BOX")
            boxes.append({"text": str(text), "bbox": [min(xs), min(ys), max(xs), max(ys)],
                          "bbox_xywh": [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)],
                          "polygon": polygon, "original_polygon": _map_polygon(polygon, inverse),
                          "confidence": max(0.0, min(1.0, confidence)), "unit": "pixel", "origin": "top_left"})
        result.update({"text": "\n".join(b["text"] for b in boxes), "boxes": boxes,
                       "confidence": sum(b["confidence"] for b in boxes) / len(boxes) if boxes else None,
                       "processing_status": "extracted",
                       "reason": "OCR_EXECUTED" if boxes else "OCR_EXECUTED_NO_TEXT_DETECTED"})
    except (OSError, ValueError) as exc:
        result["processing_status"] = "failed"
        result["reason"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        # Provider failures are evidence gaps, not an invented OCR transcript.
        result["processing_status"] = "failed"
        result["reason"] = f"OCR_BACKEND_FAILED: {type(exc).__name__}: {exc}"
    finally:
        result["metadata"]["elapsed_seconds"] = time.monotonic() - start
    return result
