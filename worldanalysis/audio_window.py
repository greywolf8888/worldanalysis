"""One bounded local audio window, not ASR, a Bundle, or a supplement delivery.

Only synthetic inputs have been tested; this primitive does not establish real
course acceptance. ``expected_identity`` is the exact ``media_store.identity``
registration snapshot. It is stat evidence, never a whole-source content hash.
The adjacent receipt/prepared sidecar is per-output recovery state, not a catalog.
"""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid
import wave

from . import media
from .contracts import canonical, reject_links
from .media_store import identity as stat_identity
from .ocr import _local_file
from .policy import Policy, contained_path, local_relative

SAMPLE_RATE = media.SAMPLE_RATE
DEFAULT_MAX_RECEIVED_BYTES = 64 * 1024**2
DEFAULT_MAX_OUTPUT_BYTES = 44 + 300 * SAMPLE_RATE * 2
_CHUNK = 1024**2
_MAX_JSON = 64 * 1024
_MAX_PACKETS = 32768
_WALL_SECONDS = 60
_PREROLL_SECONDS = 2
_SCHEMA = "worldanalysis.audio-window.v1"


class AudioWindowError(ValueError):
    """Invalid authorization, unsupported timing/track, or inconsistent evidence."""


def _positive(value, maximum, name):
    if type(value) is not int or not 0 < value <= maximum:
        raise AudioWindowError("INVALID_" + name)
    return value


def _relative(value):
    if not isinstance(value, str) or "\\" in value or len(value) > 1800:
        raise AudioWindowError("INVALID_RELATIVE_PATH")
    return local_relative(value)


def _source(root, path):
    root = Path(root).absolute()
    reject_links(root)
    if ".." in root.parts or str(root).startswith(("//", "\\\\")):
        raise AudioWindowError("LOCAL_ROOT_REQUIRED")
    root = root.resolve(strict=True)
    if not root.is_dir() or (os.name == "nt" and root.drive.upper() != "G:"):
        raise AudioWindowError("AUTHORIZED_G_ROOT_REQUIRED")
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = contained_path(root, _relative(str(path)))
    source = _local_file(candidate, root)
    if not stat.S_ISREG(source.stat().st_mode) or source.stat().st_nlink != 1:
        raise AudioWindowError("REGULAR_UNLINKED_SOURCE_REQUIRED")
    return root, source


def _check_source(root, source, expected, worker_identity, reader=None):
    if (_local_file(source, root) != source or stat_identity(source) != expected
            or media._identity(source) != worker_identity):
        raise AudioWindowError("SOURCE_IDENTITY_CHANGED")
    if reader is not None:
        info = os.fstat(reader.stream.fileno())
        if (not stat.S_ISREG(info.st_mode)
                or {"size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
                    "device": info.st_dev, "inode": info.st_ino,
                    "basis": "local_stat_snapshot_not_full_content_hash"} != expected):
            raise AudioWindowError("OPEN_SOURCE_IDENTITY_CHANGED")


class _Reader(media.CountingReader):
    """The existing real-byte counter, with a synchronous cooperative deadline."""
    def __init__(self, source, cap, deadline):
        super().__init__(source, cap, _CHUNK)
        self.deadline, self.failure = deadline, None

    def check(self):
        if time.monotonic() > self.deadline:
            self.failure = media.MediaLimit("BLOCKED_RESOURCE: audio window wall deadline")
            raise self.failure

    def read(self, size=-1):
        self.check()
        try:
            return super().read(size)
        except media.MediaLimit as exc:
            self.failure = exc
            raise

    def seek(self, offset, whence=0):
        self.check()
        return super().seek(offset, whence)


def _token(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise AudioWindowError("NON_REGULAR_OUTPUT")
    return info.st_dev, info.st_ino


def _own(path, owned):
    owned.append((path, _token(path)))


def _cleanup(owned):
    # Never glob/delete a previous attempt's temp, an unrelated final, or a link.
    for path, token in reversed(owned):
        try:
            if _token(path) == token:
                path.unlink()
        except (FileNotFoundError, AudioWindowError):
            pass


def _new_temp(parent, suffix, owned):
    path = parent / (".audio-window-" + uuid.uuid4().hex + suffix)
    handle = path.open("xb")
    _own(path, owned)
    return path, handle


def _link(source, destination, owned):
    reject_links(destination)
    os.link(source, destination)  # atomic create-only, never replace/rename over a file
    _own(destination, owned)


def _write_json(parent, destination, value, owned):
    data = canonical(value)
    if len(data) > _MAX_JSON:
        raise media.MediaLimit("BLOCKED_RESOURCE: audio window receipt size")
    temp, handle = _new_temp(parent, ".json.part", owned)
    with handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    _link(temp, destination, owned)


def _read_json(path):
    reject_links(path)
    if path.stat().st_size > _MAX_JSON:
        raise AudioWindowError("WINDOW_RECEIPT_TOO_LARGE")
    with path.open("rb") as handle:
        data = handle.read(_MAX_JSON + 1)
    if len(data) > _MAX_JSON:
        raise AudioWindowError("WINDOW_RECEIPT_TOO_LARGE")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise AudioWindowError("DUPLICATE_RECEIPT_KEY")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs)
    except (ValueError, RecursionError) as exc:
        raise AudioWindowError("INVALID_WINDOW_RECEIPT") from exc


def _wav_hash(path, size):
    """Hash only the bounded derived WAV, with bounded reads even if it changes."""
    reject_links(path)
    before = media._identity(path)
    if before["size"] != size:
        raise AudioWindowError("WINDOW_WAV_SIZE_MISMATCH")
    digest, received = hashlib.sha256(), 0
    with path.open("rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise AudioWindowError("NON_REGULAR_OUTPUT")
        while received <= size:
            chunk = handle.read(min(65536, size + 1 - received))
            if not chunk:
                break
            received += len(chunk)
            digest.update(chunk)
    if received != size or media._identity(path) != before:
        raise AudioWindowError("WINDOW_WAV_CHANGED")
    return digest.hexdigest()


def _verify(manifest, request, wav):
    count = (request["end_ms"] - request["start_ms"]) * 16
    if (type(manifest) is not dict or manifest.get("schema") != _SCHEMA
            or manifest.get("request") != request or manifest.get("processing_status") != "extracted"
            or manifest.get("sample_count") != count or manifest.get("byte_length") != 44 + count * 2
            or manifest.get("sample_rate") != SAMPLE_RATE or manifest.get("channels") != 1
            or manifest.get("sample_format") != "s16le"
            or manifest.get("source_content_sha256") is not None
            or manifest.get("does_not_prove_whole_source_content_sha") is not True
            or manifest.get("source_copy_retained") is not False
            or type(manifest.get("received_bytes")) is not int
            or not 0 < manifest["received_bytes"] <= request["max_received_bytes"]):
        raise AudioWindowError("WINDOW_RECEIPT_BINDING_MISMATCH")
    if _wav_hash(wav, manifest["byte_length"]) != manifest.get("sha256"):
        raise AudioWindowError("WINDOW_WAV_SHA256_MISMATCH")
    with wave.open(str(wav), "rb") as check:
        if (check.getnchannels(), check.getsampwidth(), check.getframerate(), check.getnframes()) != (1, 2, SAMPLE_RATE, count):
            raise AudioWindowError("WINDOW_WAV_FORMAT_MISMATCH")
    return manifest


def _decode(root, source, request, worker_identity, output, owned):
    import av

    reader = _Reader(source, request["max_received_bytes"], time.monotonic() + _WALL_SECONDS)
    temp = None
    try:
        _check_source(root, source, request["expected_identity"], worker_identity, reader)
        cfg = {"probe_max_received_bytes": min(Policy().max_probe_bytes, request["max_received_bytes"])}
        with media._open(reader, cfg) as container:
            audio = next(iter(container.streams.audio), None)
            if audio is None:
                raise AudioWindowError("UNSUPPORTED_NO_AUDIO_TRACK")
            context = audio.codec_context
            if not 0 < context.sample_rate <= 384000 or not 0 < context.channels <= 8:
                raise AudioWindowError("UNSUPPORTED_AUDIO_DIMENSIONS")
            context.thread_count = 1
            # FFmpeg's decoder allocation bound, plus checks before each conversion.
            context.options = {**context.options, "max_samples": str(_CHUNK // 8)}
            origin = Fraction(container.start_time or 0, av.time_base)
            start, end = request["start_ms"] * 16, request["end_ms"] * 16
            seek_ms = max(0, request["start_ms"] - 1000)
            if request["start_ms"]:
                try:
                    container.seek(int((origin + Fraction(seek_ms, 1000)) * av.time_base), backward=True, any_frame=False)
                except Exception as exc:
                    if reader.failure:
                        raise reader.failure from exc
                    # Never fall back to decoding a long prefix/full source.
                    raise AudioWindowError("UNSUPPORTED_BOUNDED_SEEK") from exc
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            temp, handle = _new_temp(output.parent, ".wav.part", owned)
            cursor, packets, frames, peak = start, 0, 0, 0
            first_stamp = previous_end = None
            with handle, wave.open(handle, "wb") as sink:
                sink.setnchannels(1)
                sink.setsampwidth(2)
                sink.setframerate(SAMPLE_RATE)
                sink.setnframes(end - start)

                def feed(frame):
                    nonlocal cursor, peak
                    reader.check()
                    if frame.pts is None or not frame.time_base:
                        raise AudioWindowError("UNSUPPORTED_AUDIO_TIMESTAMP_MISSING")
                    stamp = round((Fraction(frame.pts * frame.time_base) - origin) * SAMPLE_RATE)
                    size = frame.samples * 2
                    if size > _CHUNK or len(frame.planes) != 1 or frame.format.name != "s16":
                        raise media.MediaLimit("BLOCKED_RESOURCE: resampled audio chunk")
                    peak = max(peak, size)
                    lo, hi = max(cursor, stamp), min(end, stamp + frame.samples)
                    if hi <= lo:
                        return
                    if lo != cursor:
                        raise AudioWindowError("UNSUPPORTED_AUDIO_TIMESTAMP_GAP")
                    if 44 + (hi - start) * 2 > request["max_output_bytes"]:
                        raise media.MediaLimit("BLOCKED_RESOURCE: audio window output byte cap")
                    data = memoryview(frame.planes[0])[(lo - stamp) * 2:(hi - stamp) * 2]
                    if len(data) != (hi - lo) * 2:
                        raise AudioWindowError("AUDIO_PLANE_SIZE_MISMATCH")
                    sink.writeframesraw(data)
                    cursor = hi

                for packet in container.demux(audio):
                    reader.check()
                    packets += 1
                    if packets > _MAX_PACKETS or packet.size > _CHUNK:
                        raise media.MediaLimit("BLOCKED_RESOURCE: audio packet bound")
                    for frame in packet.decode():
                        reader.check()
                        frames += 1
                        if frames > _MAX_PACKETS:
                            raise media.MediaLimit("BLOCKED_RESOURCE: decoded audio frame count")
                        if frame.pts is None or not frame.time_base or not frame.sample_rate:
                            raise AudioWindowError("UNSUPPORTED_AUDIO_TIMESTAMP_MISSING")
                        stamp = Fraction(frame.pts * frame.time_base) - origin
                        raw_size = sum(p.buffer_size for p in frame.planes)
                        if (frame.samples > frame.sample_rate or raw_size > _CHUNK
                                or frame.sample_rate > 384000 or len(frame.layout.channels) > 8):
                            raise media.MediaLimit("BLOCKED_RESOURCE: decoded audio chunk")
                        peak = max(peak, raw_size)
                        if first_stamp is None:
                            first_stamp = stamp
                            if stamp < Fraction(request["start_ms"], 1000) - _PREROLL_SECONDS:
                                raise AudioWindowError("UNSUPPORTED_SEEK_NOT_NEAR_WINDOW")
                        if previous_end is not None and abs(stamp - previous_end) > Fraction(1, frame.sample_rate):
                            raise AudioWindowError("UNSUPPORTED_AUDIO_TIMESTAMP_DISCONTINUITY")
                        previous_end = stamp + Fraction(frame.samples, frame.sample_rate)
                        if previous_end - first_stamp > Fraction(request["end_ms"] - request["start_ms"], 1000) + _PREROLL_SECONDS + 1:
                            raise media.MediaLimit("BLOCKED_RESOURCE: decoded audio duration")
                        for result in resampler.resample(frame):
                            feed(result)
                        if cursor == end:
                            break
                    if cursor == end:
                        break
                if cursor < end:
                    for result in resampler.resample(None):
                        feed(result)
                if cursor != end or first_stamp is None:
                    raise AudioWindowError("UNSUPPORTED_WINDOW_NOT_FULLY_COVERED")
            # wave has patched the bounded WAV header; the underlying handle is closed.
            _check_source(root, source, request["expected_identity"], worker_identity, reader)
            with temp.open("r+b") as durable:
                os.fsync(durable.fileno())
            size = 44 + (end - start) * 2
            manifest = {"schema": _SCHEMA, "processing_status": "extracted", "request": request,
                        "source_stat_fingerprint": worker_identity,
                        "does_not_prove_whole_source_content_sha": True,
                        "source_content_sha256": None, "source_copy_retained": False,
                        "sha256_scope": "derived_window_wav_bytes_only_not_original_media",
                        "sha256": _wav_hash(temp, size), "byte_length": size,
                        "sample_rate": SAMPLE_RATE, "channels": 1, "sample_format": "s16le",
                        "sample_count": end - start, "duration_ms": request["end_ms"] - request["start_ms"],
                        "coverage_method": "contiguous_timestamped_samples_half_open_no_gap_fill",
                        "seek_requested_ms": seek_ms,
                        "actual_first_decoded_offset_ms": float(first_stamp * 1000),
                        "actual_offset_basis": "decoded_frame_pts_minus_container_origin",
                        "received_bytes": reader.received,
                        "received_bytes_scope": "actual_container_bytes_including_probe_seek_rereads_and_mixed_streams",
                        "read_calls": reader.read_calls, "seek_calls": reader.seek_calls,
                        "max_read_bytes": reader.max_read, "peak_frame_bytes": peak,
                        "audio_stream_index": audio.index, "decoded_frames": frames,
                        "vad_applied": False, "asr_executed": False, "real_course_acceptance": False}
            return temp, manifest
    except Exception as exc:
        if reader.failure is not None:
            raise reader.failure from exc
        raise
    finally:
        reader.close()


def extract_audio_window(root, path, output_relative, start_ms, end_ms, *, expected_identity,
                         max_received_bytes=DEFAULT_MAX_RECEIVED_BYTES,
                         max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES) -> dict:
    """Extract exactly [start_ms, end_ms), in source/container-relative milliseconds.

    A new isolated root-relative *.wav and *.wav.manifest.json are create-only.
    A durable *.wav.prepared.json reconciles interrupted publication without any
    second source read/decode. Existing files are verified, never overwritten.
    Source identity/scope is rechecked even on reuse; WAV SHA is not source SHA.
    Unsupported tracks/seek/timestamps raise AudioWindowError; budget exhaustion
    raises media.MediaLimit. No silence/VAD filtering, source copy, DB or services.
    """
    if (type(start_ms) is not int or type(end_ms) is not int or start_ms < 0
            or end_ms <= start_ms or end_ms - start_ms > 300000):
        raise AudioWindowError("INVALID_WINDOW: integer milliseconds, 0 < duration <= 300000")
    policy = Policy()
    _positive(max_received_bytes, policy.max_cache_bytes, "RECEIVED_BYTE_BUDGET")
    _positive(max_output_bytes, min(DEFAULT_MAX_OUTPUT_BYTES, policy.max_ram_bytes), "OUTPUT_BYTE_BUDGET")
    if 44 + (end_ms - start_ms) * 32 > max_output_bytes:
        raise media.MediaLimit("BLOCKED_RESOURCE: requested WAV exceeds output byte cap")
    root, source = _source(root, path)
    if type(expected_identity) is not dict or canonical(stat_identity(source)) != canonical(expected_identity):
        raise AudioWindowError("SOURCE_REGISTRATION_IDENTITY_MISMATCH")
    expected_identity = dict(expected_identity)
    worker_identity = media._identity(source)
    relative = _relative(output_relative)
    output = contained_path(root, relative, must_exist=False)
    if output.suffix.lower() != ".wav" or output.parent == root or source.is_relative_to(output.parent):
        raise AudioWindowError("ISOLATED_WINDOW_WAV_PATH_REQUIRED")
    manifest_path = output.with_name(output.name + ".manifest.json")
    prepared_path = output.with_name(output.name + ".prepared.json")
    for target in (output, manifest_path, prepared_path):
        reject_links(target)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Existing cross-platform OS lock: crash releases it, no stale-PID deletion.
    with media._output_lock(output.parent):
        _check_source(root, source, expected_identity, worker_identity)
        request = {"root": str(root), "source_relative": source.relative_to(root).as_posix(),
                   "output_relative": relative, "start_ms": start_ms, "end_ms": end_ms,
                   "expected_identity": expected_identity, "max_received_bytes": max_received_bytes,
                   "max_output_bytes": max_output_bytes}
        if manifest_path.exists():
            value = _verify(_read_json(manifest_path), request, output)
            _check_source(root, source, expected_identity, worker_identity)
            return value
        owned, success = [], False
        try:
            if prepared_path.exists():
                prepared = _read_json(prepared_path)
                if type(prepared) is not dict or set(prepared) != {"request", "manifest", "staging_relative"} or prepared["request"] != request:
                    raise AudioWindowError("WINDOW_PREPARED_BINDING_MISMATCH")
                staging = contained_path(root, _relative(prepared["staging_relative"]))
                if staging.parent != output.parent or not staging.name.startswith(".audio-window-") or not staging.name.endswith(".wav.part"):
                    raise AudioWindowError("INVALID_WINDOW_STAGING_PATH")
                value = _verify(prepared["manifest"], request, staging)
            else:
                if output.exists():
                    raise AudioWindowError("WINDOW_EXISTS_WITHOUT_RECOVERY_RECEIPT")
                staging, value = _decode(root, source, request, worker_identity, output, owned)
                _check_source(root, source, expected_identity, worker_identity)
                _write_json(output.parent, prepared_path,
                            {"request": request, "manifest": value, "staging_relative": staging.relative_to(root).as_posix()}, owned)
            _check_source(root, source, expected_identity, worker_identity)
            if output.exists():
                if not os.path.samefile(staging, output):
                    raise AudioWindowError("WINDOW_CREATE_ONLY_CONFLICT")
                _verify(value, request, output)
            else:
                _link(staging, output, owned)
            _write_json(output.parent, manifest_path, value, owned)
            _check_source(root, source, expected_identity, worker_identity)
            success = True
            return value
        finally:
            # Final outputs survive success; clean only files this invocation owns.
            if success:
                owned = [(p, token) for p, token in owned if p not in (output, manifest_path)]
            _cleanup(owned)
