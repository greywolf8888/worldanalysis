"""Read-only, one-shot source-frame decoder. This module never admits products.

The publishing caller must separately bind an actual execution and external QA.
The file-only API is also usable for measured inspection of an existing course.
No caller-supplied executable, cwd, decoder budget, DB or network is accepted.
"""
from __future__ import annotations
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import time
import warnings

CHECKOUT = Path(__file__).resolve().parent.parent
LIMITS = {"max_manifest_bytes": 1024**2, "max_frames": 1000, "max_image_bytes": 4 * 1024**2,
          "max_receipt_bytes": 16 * 1024, "max_total_bytes": 64 * 1024**2,
          "max_frame_pixels": 16_000_000, "max_work_pixels": 512_000_000,
          "max_chunk_work_pixels": 64_000_000, "max_inflight_frames": 1, "timeout_seconds": 90}
SCHEMA = "worldanalysis.source-frame-decoder-report.v1"


class SourceFrameValidationError(ValueError):
    pass


def need(condition, message):
    if not condition:
        raise SourceFrameValidationError(message)


def _json(data):
    def pairs(items):
        out = {}
        for key, value in items:
            need(key not in out, "duplicate JSON key")
            out[key] = value
        return out
    def invalid(value):
        raise SourceFrameValidationError("nonfinite JSON number")
    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)


def _positive(value, bound, label):
    need(type(value) is int and 0 < value <= bound, label + " bound exceeded")
    return value


def _sha(value):
    need(isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value), "invalid SHA-256")
    return value


def _number(value):
    need(type(value) in (int, float) and math.isfinite(value) and value >= 0, "invalid frame time")
    return value


def _no_links(path):
    for item in [*reversed(path.parents), path]:
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        need(not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400), "links/reparse points forbidden")


def _root(value):
    supplied = Path(value)
    need(".." not in supplied.parts, "noncanonical parent component in decoder root")
    root = supplied.absolute()
    need(root == CHECKOUT or CHECKOUT in root.parents, "decoder root must remain inside this checkout")
    if os.name == "nt":
        need(root.drive.upper() == "G:", "decoder data must remain on G:")
    _no_links(root)
    root = root.resolve(strict=True)
    need(root == CHECKOUT or CHECKOUT in root.parents, "resolved decoder root must remain inside this checkout")
    need(root.is_dir(), "decoder root does not exist")
    return root


def _parts(name):
    need(isinstance(name, str) and 0 < len(name) <= 2048 and "\\" not in name and ":" not in name,
         "invalid relative dependency path")
    parts = name.split("/")
    need(all(p and p not in {".", ".."} and not p.endswith((".", " ")) and not any(ord(c) < 32 for c in p) for p in parts), "unsafe dependency path")
    return parts


def _path(root, name):
    path = root.joinpath(*_parts(name))
    _no_links(path)
    return path


def _snapshot(path):
    info = path.stat()
    need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "regular files without hardlinks required")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read(root, row, bound):
    size = _positive(row["size_bytes"], bound, "dependency bytes")
    path = _path(root, row["relative_path"])
    before = _snapshot(path)
    need(before[2] == size, "dependency size changed")
    with path.open("rb") as stream:
        data = stream.read(size + 1)
    _no_links(path)
    need(_snapshot(path) == before and len(data) == size and hashlib.sha256(data).hexdigest() == _sha(row["sha256"]), "dependency bytes/hash changed")
    return data


def _identity(source):
    need(isinstance(source, dict), "source object required")
    for key in ("account_ref", "file_id", "source_revision", "path"):
        need(isinstance(source.get(key), str) and source[key], "source identity missing")
    need(source["path"].startswith("/") and "\\" not in source["path"], "cloud source path required")
    _positive(source["size_bytes"], 2**63 - 1, "source bytes")
    return {k: source[k] for k in ("account_ref", "file_id", "source_revision", "size_bytes")}


def _peak_rss():
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            *[(key, ctypes.c_size_t) for key in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]
            counters = Counters(); counters.cb = ctypes.sizeof(counters)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            need(bool(psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb)), "GetProcessMemoryInfo failed")
            return int(counters.PeakWorkingSetSize), "Windows PeakWorkingSetSize"
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == "darwin" else value * 1024), "getrusage.ru_maxrss"
    except Exception:
        return None, "unknown"


def _receipt(data, frame, image, source):
    receipt = _json(data)
    required = {"schema", "source", "requested_ms", "frame_callback", "source_identity_check", "helper_sha256", "relative_path", "sha256", "bytes", "width", "height", "format"}
    need(isinstance(receipt, dict) and required <= set(receipt) and not set(receipt) - required - {"observed_at", "media_time_ms", "locator", "playback_duration_ms"}, "invalid frame receipt fields")
    need(receipt["schema"] == "worldanalysis.browser-source-frame.v1" and receipt["format"] == "jpeg", "browser JPEG receipt required")
    need(_identity(receipt["source"]) == source and receipt["source_identity_check"] == frame["source_identity_check"], "receipt source/proof mismatch")
    need(receipt["relative_path"] == image["relative_path"] and receipt["sha256"] == image["sha256"]
         and receipt["bytes"] == image["size_bytes"] and receipt["width"] == frame["width"] and receipt["height"] == frame["height"]
         and type(receipt["requested_ms"]) is int and receipt["requested_ms"] == frame["requested_ms"], "receipt/image/target mismatch")
    actual = _number(receipt["frame_callback"]["mediaTime"]) * 1000
    need(abs(actual - frame["media_time_ms"]) <= 0.001 and abs(actual - frame["requested_ms"]) <= 80 + 1e-6, "rVFC time mismatch")
    need(receipt.get("locator", "requestVideoFrameCallback") == "requestVideoFrameCallback", "receipt locator mismatch")
    if "media_time_ms" in receipt:
        need(abs(_number(receipt["media_time_ms"]) - actual) <= 0.001, "receipt mediaTime mismatch")
    if "playback_duration_ms" in receipt:
        _number(receipt["playback_duration_ms"])
    return _sha(receipt["helper_sha256"])


def _decode(data, frame):
    from PIL import Image, ImageFile
    need(data.startswith(bytes.fromhex("ffd8ff")) and not ImageFile.LOAD_TRUNCATED_IMAGES, "strict JPEG required")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data), formats=["JPEG"]) as image:
            need(image.format == "JPEG" and image.size == (frame["width"], frame["height"]) and getattr(image, "n_frames", 1) == 1, "JPEG dimensions/format mismatch")
            image.verify()
        with Image.open(io.BytesIO(data), formats=["JPEG"]) as image:
            image.load()


def _validate(ticket):
    started, cpu = time.monotonic(), time.process_time()
    need(isinstance(ticket, dict) and set(ticket) == {"root", "manifest"}, "fixed decoder ticket required")
    root, descriptor = _root(ticket["root"]), ticket["manifest"]
    need(isinstance(descriptor, dict) and {"relative_path", "sha256", "size_bytes"} <= set(descriptor)
         and not set(descriptor) - {"relative_path", "sha256", "size_bytes", "frame_count"}, "invalid manifest descriptor")
    manifest_path = PurePosixPath(descriptor["relative_path"])
    need(len(manifest_path.parts) == 4 and manifest_path.parts[:2] == ("采集输入", "platform-source-frames")
         and manifest_path.name == "frames-" + _sha(descriptor["sha256"])[:24] + ".json", "canonical execution capture path required")
    raw = _read(root, descriptor, LIMITS["max_manifest_bytes"])
    manifest = _json(raw)
    need(isinstance(manifest, dict) and set(manifest) == {"schema", "source", "selection", "frames"}
         and manifest["schema"] == "worldanalysis.source-video-frames.v1", "canonical source-frame manifest required")
    source = _identity(manifest["source"])
    need(set(manifest["source"]) == {"account_ref", "file_id", "source_revision", "path", "size_bytes", "frame_count"}, "invalid declared source")
    need(isinstance(manifest["selection"], dict) and set(manifest["selection"]) == {"evidence_ref", "basis"}
         and all(isinstance(v, str) and v for v in manifest["selection"].values()), "selection evidence required")
    frames = manifest["frames"]
    need(isinstance(frames, list), "frames array required")
    count = _positive(len(frames), LIMITS["max_frames"], "frame count")
    need(type(manifest["source"]["frame_count"]) is int and manifest["source"]["frame_count"] == count
         and descriptor.get("frame_count", count) == count, "frame count mismatch")
    parent = PurePosixPath(descriptor["relative_path"]).parent
    images, receipts, names, chunks = [], [], {descriptor["relative_path"].casefold()}, []
    total, pixels, chunk_pixels, chunk_start, previous = len(raw), 0, 0, 1, (-1, -1)
    required = {"sequence", "relative_path", "sha256", "size_bytes", "width", "height", "requested_ms", "media_time_ms", "format", "locator", "source_identity_check", "receipt_relative_path", "receipt_sha256"}
    for index, frame in enumerate(frames, 1):
        need(isinstance(frame, dict) and set(frame) == required and type(frame["sequence"]) is int and frame["sequence"] == index, "canonical frame fields/sequence required")
        need(type(frame["requested_ms"]) is int and 0 <= frame["requested_ms"] <= 2**53 - 1, "integer target required")
        times = (frame["requested_ms"], _number(frame["media_time_ms"]))
        need(times[0] > previous[0] and times[1] >= previous[1] and abs(times[1] - times[0]) <= 80 + 1e-6, "frame time order/tolerance mismatch")
        previous = times
        work = _positive(frame["width"], LIMITS["max_frame_pixels"], "width") * _positive(frame["height"], LIMITS["max_frame_pixels"], "height")
        _positive(work, LIMITS["max_frame_pixels"], "frame pixels")
        pixels += work
        need(pixels <= LIMITS["max_work_pixels"], "source-frame decode work pixel budget exceeded")
        if chunk_pixels + work > LIMITS["max_chunk_work_pixels"]:
            chunks.append({"first_sequence": chunk_start, "last_sequence": index - 1, "work_pixels": chunk_pixels})
            chunk_start, chunk_pixels = index, 0
        chunk_pixels += work
        need(frame["format"] == "jpeg" and frame["locator"] == "requestVideoFrameCallback"
             and frame["source_identity_check"] == {"file_id_match": True, "version_token_match": True}
             and all(v is True for v in frame["source_identity_check"].values()), "real JPEG/rVFC identity facts required")
        local_image, local_receipt = frame["relative_path"], frame["receipt_relative_path"]
        need(PurePosixPath(local_image).suffix.lower() in {".jpg", ".jpeg"} and PurePosixPath(local_receipt).suffix.lower() == ".json", "JPEG/receipt extensions required")
        image = {"relative_path": (parent / local_image).as_posix(), "sha256": _sha(frame["sha256"]), "size_bytes": _positive(frame["size_bytes"], LIMITS["max_image_bytes"], "image bytes")}
        receipt_name = (parent / local_receipt).as_posix()
        # Validate the relative components before joining, so '../' cannot cancel.
        _parts(local_image); _parts(local_receipt)
        receipt_size = _positive(_snapshot(_path(root, receipt_name))[2], LIMITS["max_receipt_bytes"], "receipt bytes")
        receipt = {"relative_path": receipt_name, "sha256": _sha(frame["receipt_sha256"]), "size_bytes": receipt_size}
        for row in (image, receipt):
            need(row["relative_path"].casefold() not in names, "duplicate dependency path")
            names.add(row["relative_path"].casefold())
            need(_snapshot(_path(root, row["relative_path"]))[2] == row["size_bytes"], "declared dependency size mismatch")
            total += row["size_bytes"]
        need(total <= LIMITS["max_total_bytes"], "source-frame total byte budget exceeded")
        images.append(image); receipts.append(receipt)
    chunks.append({"first_sequence": chunk_start, "last_sequence": count, "work_pixels": chunk_pixels})
    helper, inflight, peak_frames, validated_chunks = None, 0, 0, []
    for chunk in chunks:
        decoded_work = 0
        for index in range(chunk["first_sequence"] - 1, chunk["last_sequence"]):
            frame, image, receipt = frames[index], images[index], receipts[index]
            need(time.monotonic() - started <= LIMITS["timeout_seconds"], "source-frame validation deadline")
            receipt_helper = _receipt(_read(root, receipt, LIMITS["max_receipt_bytes"]), frame, image, source)
            need(helper is None or helper == receipt_helper, "capture helper changed within product")
            helper = receipt_helper
            inflight += 1; peak_frames = max(peak_frames, inflight)
            need(inflight <= LIMITS["max_inflight_frames"], "inflight frame budget exceeded")
            try:
                _decode(_read(root, image, LIMITS["max_image_bytes"]), frame)
            finally:
                inflight -= 1
            decoded_work += frame["width"] * frame["height"]
            need(decoded_work <= LIMITS["max_chunk_work_pixels"], "chunk work budget exceeded")
        need(decoded_work == chunk["work_pixels"], "decoded chunk work mismatch")
        validated_chunks.append({**chunk, "work_pixels": decoded_work, "decoded_frames": chunk["last_sequence"] - chunk["first_sequence"] + 1})
    # Hash-only closing read catches any file changed after its decode.
    for row in [descriptor, *images, *receipts]:
        _read(root, row, LIMITS["max_manifest_bytes"] if row is descriptor else LIMITS["max_image_bytes"])
    peak, method = _peak_rss()
    return {"schema": SCHEMA, "status": "validated", "scope": "local_decoder_readability_only_not_quality_or_authorization",
            "manifest_sha256": descriptor["sha256"], "source": source, "helper_sha256": helper, "limits": LIMITS,
            "frame_count": count, "receipt_count": count, "total_pixels": pixels, "max_frame_pixels": max(f["width"] * f["height"] for f in frames),
            "peak_inflight_frame_decodes": peak_frames, "work_chunks": validated_chunks, "total_image_bytes": sum(r["size_bytes"] for r in images),
            "total_receipt_bytes": sum(r["size_bytes"] for r in receipts), "logical_total_bytes": total, "bytes_read": total * 2,
            "dependencies": images + receipts, "wall_seconds": time.monotonic() - started, "cpu_seconds": time.process_time() - cpu,
            "peak_process_rss_bytes": peak, "peak_rss_measurement": method, "python": sys.executable, "pid": os.getpid()}


def validate_source_frame_files(root, manifest):
    """File-only measured inspection; it cannot approve/publish a material."""
    root = _root(str(root))
    payload = json.dumps({"root": str(root), "manifest": manifest}, ensure_ascii=False, allow_nan=False).encode("utf-8")
    need(len(payload) <= 16 * 1024, "decoder ticket too large")
    if os.name == "nt":
        expected = Path("D:/桌面/awesome-dsh/.dsh-home/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe")
        need(Path(sys.executable).resolve() == expected.resolve() and sys.version_info[:2] == (3, 12), "existing bundled Python 3.12 required")
    started = time.monotonic()
    try:
        completed = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve())], input=payload,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(CHECKOUT),
                                   timeout=LIMITS["timeout_seconds"], check=False)
    except subprocess.TimeoutExpired as exc:
        raise SourceFrameValidationError("source-frame validation hard timeout (child terminated)") from exc
    need(len(completed.stdout) <= 2 * 1024**2 and len(completed.stderr) <= 4096, "decoder report bound exceeded")
    if completed.returncode != 0:
        raise SourceFrameValidationError("source-frame validation failed: " + completed.stderr.decode("utf-8", "replace")[:500])
    report = _json(completed.stdout)
    need(report.get("schema") == SCHEMA and report.get("status") == "validated" and report.get("limits") == LIMITS
         and report.get("manifest_sha256") == manifest["sha256"], "decoder report/binding mismatch")
    report["subprocess_wall_seconds"] = time.monotonic() - started
    return report


def _main():
    try:
        data = sys.stdin.buffer.read(16 * 1024 + 1)
        need(len(data) <= 16 * 1024, "decoder ticket too large")
        report = _validate(_json(data))
        sys.stdout.buffer.write(json.dumps(report, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        return 0
    except Exception as exc:
        sys.stderr.buffer.write((type(exc).__name__ + ": " + str(exc)[:500]).encode("utf-8", "replace"))
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
