"""Bounded local crops of *already published* image artifacts; no acquisition.

``bbox`` is explicitly ``{'unit': 'pixel', 'origin': 'top_left',
'xyxy': [left, top, right, bottom]}``, using integer pixel edges (right/bottom
exclusive). ``resize=(width, height)`` uses nearest-neighbour sampling. Matrices
map continuous pixel EDGES, not integer sample indices, current -> original.
Only PNG/JPEG single-frame, ordinary 8-bit decoded raster modes are admitted.

The returned/sidecar document is a LOCAL MATERIALIZATION DESCRIPTOR, NOT a
Bundle artifact, publication, job, read receipt, or supplement fulfillment.
No artifact ID is invented and no Store/business-table write is performed.
The only disk writes are exclusive creates of the requested PNG and its small
``.manifest.json`` sidecar below the project's ``补采派生`` directory. A partial
pair after an I/O failure is retained and rejected, never overwritten/deleted.
A complete repeat is re-derived and byte/hash/geometry verified, not acquired.

Resource gates bound encoded files, raster dimensions and PNG buffering; Pillow
codec internals are not an OS-enforced process-memory/time sandbox. No network,
OCR, rendering, external command, or source-provided script is invoked.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import math
import os
from pathlib import Path
import re
import stat

from .contracts import (ContractError, canonical, check_id, reject_links,
                        safe_path, validate_relative_path)

MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_DIMENSION = 16_384
MAX_MANIFEST_BYTES = 64 * 1024
_IMPLEMENTATION = "worldanalysis-local-image-region-1"
_IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1]
_OUTPUT = re.compile(r"补采派生/[A-Za-z0-9][A-Za-z0-9_-]{0,99}\.png\Z")


def _fail(reason):
    raise ContractError(reason)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("finite non-bool numeric geometry required")
    try:
        finite = math.isfinite(value)
    except (OverflowError, ValueError):
        finite = False
    if not finite:
        _fail("finite non-bool numeric geometry required")
    return value


def _integer(value):
    value = _number(value)
    if int(value) != value:
        _fail("integer pixel edges/dimensions required; no rounding or guessing")
    return int(value)


def _dimensions(width, height):
    width, height = _integer(width), _integer(height)
    if min(width, height) <= 0 or max(width, height) > MAX_DIMENSION or width * height > MAX_PIXELS:
        _fail("BLOCKED_RESOURCE: raster dimensions/pixels exceed local crop limit")
    return width, height


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(path, cap):
    """Reject links before/after open and bind bytes to fd + pathname identity."""
    reject_links(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > cap:
        _fail("BLOCKED_RESOURCE: non-regular or oversized local file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        # CPython/Windows pathname stat and fd fstat can give different ctime
        # meanings. Compare stable file-ID/size/mtime across interfaces, but
        # compare ctime as well within each interface before/after reading.
        if not stat.S_ISREG(opened.st_mode) or _identity(before)[:4] != _identity(opened)[:4]:
            _fail("local file identity changed while opening")
        data = stream.read(cap + 1)
        after = os.fstat(stream.fileno())
    reject_links(path)
    if len(data) > cap or len(data) != opened.st_size:
        _fail("BLOCKED_RESOURCE: local file changed size/exceeded byte limit")
    if _identity(after) != _identity(opened) or _identity(path.stat()) != _identity(before):
        _fail("local file identity changed while reading")
    return data, _identity(before)


def _recheck(path, identity, expected_sha):
    data, current = _read(path, MAX_SOURCE_BYTES)
    if current != identity or hashlib.sha256(data).hexdigest() != expected_sha:
        _fail("sealed source SHA/identity changed during crop")


def _multiply(left, right):
    result = [sum(left[r * 3 + k] * right[k * 3 + c] for k in range(3))
              for r in range(3) for c in range(3)]
    if not all(math.isfinite(v) for v in result):
        _fail("UNSUPPORTED: non-finite composed transform")
    return result


def _map(matrix, x, y):
    return [matrix[0] * x + matrix[1] * y + matrix[2],
            matrix[3] * x + matrix[4] * y + matrix[5]]


def _geometry(locator, artifact_id, size, box, output_size, resized):
    width, height = size
    left, top, right, bottom = box
    source = locator.get("geometry")
    if source is None:
        reference, original, rotation, operations = artifact_id, {"width": width, "height": height, "unit": "pixel"}, 0, []
        matrix = _IDENTITY
    else:
        if (source.get("reference_object_ref") != artifact_id
                or source.get("unit") != "pixel" or source.get("origin") != "top_left"
                or source.get("original_size", {}).get("unit") != "pixel"
                or source.get("frame_size") != {"width": width, "height": height}):
            _fail("UNSUPPORTED: source geometry is not bound to these top-left pixel dimensions")
        reference, original = source["reference_object_ref"], deepcopy(source["original_size"])
        rotation = _number(source["rotation_degrees"])
        for key in ("width", "height"):
            if _number(original[key]) <= 0:
                _fail("UNSUPPORTED: invalid original dimensions")
        declared = source.get("bbox_xywh", [])
        if len(declared) != 4:
            _fail("UNSUPPORTED: missing exact source geometry bounds")
        x, y, bw, bh = [_number(v) for v in declared]
        if not (0 <= x <= left < right <= x + bw <= width and 0 <= y <= top < bottom <= y + bh <= height):
            _fail("crop extends outside the exact source block geometry")
        transform = source["transform_to_original"]
        operations = list(transform["operations"])
        if transform["method"] == "identity":
            if (rotation != 0 or operations or source["coordinate_space"] != "original"
                    or original != {"width": width, "height": height, "unit": "pixel"}):
                _fail("UNSUPPORTED: source identity mapping is not proved")
            matrix = _IDENTITY
        elif transform["method"] == "matrix_3x3":
            matrix = [_number(v) for v in transform.get("matrix", [])]
            if len(matrix) != 9 or matrix[6:] != [0, 0, 1]:
                _fail("UNSUPPORTED: only explicit affine current-to-original matrices are admitted")
            determinant = matrix[0] * matrix[4] - matrix[1] * matrix[3]
            if not math.isfinite(determinant) or determinant == 0:
                _fail("UNSUPPORTED: singular source transform")
        else:
            _fail("UNSUPPORTED: unverified explicit/perspective geometry; no inverse guessed")
    out_width, out_height = output_size
    # Crop inverse = translation(left, top). Resize inverse = diagonal scale.
    # Column vectors: original = source_to_original @ crop_inverse @ resize_inverse @ output.
    local = [(right - left) / out_width, 0, left,
             0, (bottom - top) / out_height, top, 0, 0, 1]
    composed = _multiply(matrix, local)
    polygon = [_map(composed, x, y) for x, y in ((0, 0), (out_width, 0), (out_width, out_height), (0, out_height))]
    if any(not all(math.isfinite(v) for v in point) or not (0 <= point[0] <= original["width"] and 0 <= point[1] <= original["height"]) for point in polygon):
        _fail("UNSUPPORTED: crop cannot be proved within original reference coordinates")
    return {"reference_object_ref": reference, "coordinate_space": "derived", "unit": "pixel", "origin": "top_left",
            "frame_size": {"width": out_width, "height": out_height}, "original_size": original,
            "rotation_degrees": rotation, "bbox_xywh": [0, 0, out_width, out_height],
            "transform_to_original": {"method": "matrix_3x3", "operations": operations + ["crop"] + (["resize"] if resized else []), "matrix": composed},
            "original_polygon_xy": polygon}


class _PngBuffer(io.BytesIO):
    def write(self, value):
        if self.tell() + len(value) > MAX_OUTPUT_BYTES:
            _fail("BLOCKED_RESOURCE: encoded PNG exceeds output byte limit")
        return super().write(value)


def _create(path, data):
    reject_links(path)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    reject_links(path)


def crop_region(store, project_id, bundle_id, block_id, artifact_id, bbox, output_relative, *, resize=None):
    """Materialize one exact local crop; raise ContractError on uncertainty.

    Existing source rotation is handled ONLY by a verified explicit affine
    current->original matrix; it is composed, not re-inferred from angle labels.
    EXIF auto-orientation is never applied. Unsupported geometry fails closed.
    The manifest's reference_object_ref names the EXISTING source reference;
    frame_size/current_size describe this unnamed local output. This descriptor
    must not be submitted as a schema Bundle artifact without real publication.
    """
    bundle = store.get_bundle(project_id, bundle_id)  # FIRST: authoritative Store verification, not an orphan manifest.
    for ident in (project_id, bundle_id, block_id, artifact_id):
        check_id(ident)
    if (bundle["project_id"] != project_id or bundle["bundle_id"] != bundle_id
            or bundle["publication"]["state"] != "published"):
        _fail("cross-project/version or unpublished source")
    blocks = [b for b in bundle["blocks"] if b["block_id"] == block_id]
    artifacts = [a for a in bundle["artifacts"] if a["artifact_id"] == artifact_id]
    if len(blocks) != 1 or len(artifacts) != 1:
        _fail("source block/artifact not unique in the exact verified Bundle")
    block, artifact = blocks[0], artifacts[0]
    if artifact_id not in block["artifact_ids"] or artifact["asset_ref"] != block["asset_ref"]:
        _fail("artifact does not belong to the exact source block/asset revision")
    if artifact["access_class"] != "project_local" or artifact["kind"] not in {"image", "page_render", "keyframe"}:
        _fail("restricted_local or non-image source artifact is forbidden")
    locator = deepcopy(block["locator"])
    if locator.get("precision") != "exact":
        _fail("UNSUPPORTED: source locator is not exact; no location guessed")
    if not isinstance(bbox, dict) or set(bbox) != {"unit", "origin", "xyxy"} or bbox["unit"] != "pixel" or bbox["origin"] != "top_left":
        _fail("explicit pixel/top_left/xyxy bbox required; no normalized guessing")
    if not isinstance(bbox["xyxy"], (list, tuple)) or len(bbox["xyxy"]) != 4:
        _fail("four pixel edge coordinates required")
    box = [_integer(v) for v in bbox["xyxy"]]
    left, top, right, bottom = box
    if not (0 <= left < right and 0 <= top < bottom):
        _fail("empty/reversed/negative crop bounds")
    if resize is not None and (not isinstance(resize, (list, tuple)) or len(resize) != 2):
        _fail("resize requires explicit width/height")
    output_size = _dimensions(*(resize if resize is not None else (right - left, bottom - top)))
    if not isinstance(output_relative, str) or not _OUTPUT.fullmatch(output_relative):
        _fail("output must be project-local 补采派生/<safeid>.png; evidence/original overwrite forbidden")
    root = Path(store.project_root(project_id))
    raw_root = str(root)
    if not root.is_absolute() or raw_root.startswith(("//", "\\\\")):
        _fail("strict local project root required; no network path")
    reject_links(root)
    relative = artifact["relative_path"]
    validate_relative_path(relative)
    if "\\" in relative:
        _fail("ambiguous source relative path")
    manifest_relative = bundle["publication"]["manifest_relative_path"]
    validate_relative_path(manifest_relative)
    sealed_prefix = manifest_relative.rsplit("/", 1)[0] + "/"
    if not sealed_prefix.startswith("证据/") or not relative.startswith(sealed_prefix):
        _fail("source image must reside in this Bundle's immutable evidence version")
    if (not isinstance(artifact["byte_length"], int) or isinstance(artifact["byte_length"], bool)
            or not 0 < artifact["byte_length"] <= MAX_SOURCE_BYTES):
        _fail("BLOCKED_RESOURCE: sealed source encoded bytes exceed crop limit")
    source = safe_path(root, relative)
    data, identity = _read(source, MAX_SOURCE_BYTES)
    source_sha = hashlib.sha256(data).hexdigest()
    if (source_sha != artifact["sha256"] or len(data) != artifact["byte_length"]
            or artifact["artifact_revision"] != "sha256-" + source_sha):
        _fail("sealed source SHA/size/revision mismatch")
    from PIL import Image, __version__ as pillow_version
    try:
        with Image.open(io.BytesIO(data)) as image:
            size = _dimensions(*image.size)  # before load/crop/resize allocations
            if (image.format not in {"PNG", "JPEG"} or Image.MIME.get(image.format) != artifact["media_type"]
                    or getattr(image, "n_frames", 1) != 1 or image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}):
                _fail("UNSUPPORTED: source format/frames/mode is not admitted for faithful local crop")
            if image.getexif().get(274, 1) != 1:
                _fail("UNSUPPORTED: EXIF orientation is not an explicit source-coordinate mapping")
            if right > size[0] or bottom > size[1]:
                _fail("crop bounds outside actual source dimensions")
            geometry = _geometry(locator, artifact_id, size, box, output_size, resize is not None)
            image.load()
            _recheck(source, identity, source_sha)  # after Pillow decode
            region = image.crop(tuple(box))
            try:
                if region.mode in {"P", "1"}:
                    converted = region.convert("RGBA" if region.mode == "P" else "L")
                    region.close()
                    region = converted
                if resize is not None:
                    scaled = region.resize(output_size, resample=Image.Resampling.NEAREST)
                    region.close()
                    region = scaled
                region.info.clear()  # do not copy source EXIF, text, ICC, or other metadata
                with _PngBuffer() as buffer:
                    region.save(buffer, format="PNG")
                    png = buffer.getvalue()
            finally:
                region.close()
    except ContractError:
        raise
    except (OSError, ValueError, SyntaxError) as exc:
        raise ContractError("UNSUPPORTED: source image decode/encode failed") from exc
    _recheck(source, identity, source_sha)
    sha = hashlib.sha256(png).hexdigest()
    source_ref = {"project_id": project_id, "bundle_id": bundle_id, "bundle_revision": bundle["bundle_revision"],
                  **deepcopy(block["asset_ref"]), "block_id": block_id, "artifact_id": artifact_id, "locator": locator}
    sidecar_relative = output_relative + ".manifest.json"
    manifest = {"descriptor_version": _IMPLEMENTATION, "descriptor_kind": "local_image_region_not_bundle_artifact",
                "project_id": project_id, "source_publish_id": bundle["publish_id"],
                "source_bundle_sha256": hashlib.sha256(canonical(bundle)).hexdigest(),
                "source_ref": source_ref, "source_artifact_sha256": source_sha,
                "source_artifact_revision": artifact["artifact_revision"], "source_relative_path": relative,
                "source_dimensions": {"width": size[0], "height": size[1]},
                "bbox": {"unit": "pixel", "origin": "top_left", "xyxy": box},
                "resize": list(output_size) if resize is not None else None,
                "output_relative_path": output_relative, "manifest_relative_path": sidecar_relative,
                "media_type": "image/png", "new_artifact_sha256": sha, "byte_length": len(png),
                "dimensions": {"width": output_size[0], "height": output_size[1]},
                "current_size": {"width": output_size[0], "height": output_size[1], "unit": "pixel"},
                "geometry": geometry,
                "derived_from": [{"evidence_ref": source_ref, "sha256": source_sha, "artifact_revision": artifact["artifact_revision"]}],
                "pixel_fidelity": "decoded source pixels within requested crop only" + ("; explicit nearest-neighbour resize" if resize is not None else "; lossless PNG crop"),
                "sampling": "nearest; inverse matrix maps pixel edges, sample centres use x+0.5,y+0.5",
                "implementation": {"name": _IMPLEMENTATION, "pillow_version": pillow_version},
                "limits": {"source_encoded_bytes": MAX_SOURCE_BYTES, "output_encoded_bytes": MAX_OUTPUT_BYTES, "pixels": MAX_PIXELS, "dimension": MAX_DIMENSION},
                "scope": {"network_used": False, "new_collection": False, "ocr_executed": False, "render_executed": False,
                          "scripts_executed": False, "whole_page_or_course_read": False, "business_state_updated": False,
                          "publication_created": False, "permission_expanded": False}}
    encoded_manifest = canonical(manifest)
    if len(encoded_manifest) > MAX_MANIFEST_BYTES:
        _fail("BLOCKED_RESOURCE: descriptor exceeds small sidecar limit")
    output = safe_path(root, output_relative, must_exist=False)
    sidecar = safe_path(root, sidecar_relative, must_exist=False)
    if output.exists() or sidecar.exists():
        if not output.is_file() or not sidecar.is_file():
            _fail("incomplete/colliding crop pair retained; choose a new safe output ID")
        previous, _ = _read(sidecar, MAX_MANIFEST_BYTES)
        existing, _ = _read(output, MAX_OUTPUT_BYTES)
        if previous != encoded_manifest or hashlib.sha256(existing).hexdigest() != sha or len(existing) != len(png):
            _fail("existing crop SHA/parameters/geometry mismatch; never overwrite")
        _recheck(source, identity, source_sha)
        return {**manifest, "reused": True}
    output.parent.mkdir(exist_ok=True)  # fixed one-level local generated region only
    reject_links(output.parent)
    try:
        _create(output, png)
        written, _ = _read(output, MAX_OUTPUT_BYTES)
        if hashlib.sha256(written).hexdigest() != sha or len(written) != len(png):
            _fail("materialized PNG SHA mismatch; incomplete output retained")
        _recheck(source, identity, source_sha)
        _create(sidecar, encoded_manifest)  # immutable create-only completion descriptor
        saved, _ = _read(sidecar, MAX_MANIFEST_BYTES)
        if saved != encoded_manifest:
            _fail("materialized descriptor mismatch; incomplete output retained")
        _recheck(source, identity, source_sha)
    except OSError as exc:
        raise ContractError("crop create-only materialization failed; existing/partial files retained") from exc
    return {**manifest, "reused": False}
