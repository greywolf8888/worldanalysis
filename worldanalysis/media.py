"""Bounded local audio/video extraction; one FFmpeg/PyAV demux per invocation.

Runtime never accepts a URL or downloads weights. PCM is s16 for one <=300 s
window; only that window becomes float32 for faster-whisper. Synchronous sinks
are a one-slot, byte-capped queue: slow ASR/OCR stops demux reads. Checkpoints
are SQLite FULL transactions, not an in-memory cursor. Sparse visual candidates
are explicitly NOT a guarantee that every teaching operation was captured.
"""
from __future__ import annotations

from contextlib import contextmanager
from importlib import metadata as distributions
from pathlib import Path
import hashlib
import io
import json
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any

from .ocr import _local_file

PROVIDER_ID = "bounded-pyav-faster-whisper"
PROVIDER_VERSION = "p3.3"
SAMPLE_RATE = 16000
_DEFAULT_MODEL = Path(__file__).resolve().parents[1] / ".runtime/site-packages/_worldanalysis_models/faster-whisper-tiny"
_FORMATS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,wav,mp3,flac,ogg,aac,avi,mpeg,mpegts,asf"


class MediaLimit(RuntimeError):
    pass


class PauseMedia(RuntimeError):
    pass


class CappedBuffer(io.BytesIO):
    def __init__(self, cap):
        super().__init__()
        self.cap = int(cap)

    def write(self, data):
        if self.tell() + len(data) > self.cap:
            raise MediaLimit("BLOCKED_RESOURCE: encoded frame byte cap")
        return super().write(data)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _limits(options=None):
    cfg = {"window_seconds": 300, "overlap_seconds": 2,
           "read_chunk_bytes": 1024 * 1024, "probe_max_received_bytes": 1024 * 1024,
           "input_byte_cap": 8 * 1024**3, "pending_audio_byte_cap": 64 * 1024**2,
           "pending_image_byte_cap": 32 * 1024**2, "cache_byte_cap": 96 * 1024**2,
           "ram_byte_cap": 4 * 1024**3, "final_evidence_byte_cap": 512 * 1024**2,
           "frame_max_pixels": 10_000_000, "visual_probe_seconds": 1,
           "visual_period_seconds": 30, "scene_threshold": 14, "local_change_threshold": 32,
           "max_records": 10000, "max_result_bytes": 32 * 1024**2,
           "max_windows": None, "wall_timeout_seconds": 3600,
           "ocr_enabled": True, "asr_enabled": True, "device": "cpu", "cpu_threads": 2,
           "model_dir": str(_DEFAULT_MODEL), "language": None, "synthetic": False}
    cfg.update(options or {})
    cfg["model_dir"] = str(cfg["model_dir"])
    if cfg.get("allowed_root") is not None:
        cfg["allowed_root"] = str(cfg["allowed_root"])
    if cfg["device"] != "cpu":
        raise ValueError("CPU_CALIBRATION_ONLY: GPU requires a separately measured admission")
    positive = ("read_chunk_bytes", "probe_max_received_bytes", "input_byte_cap", "pending_audio_byte_cap",
                "pending_image_byte_cap", "cache_byte_cap", "ram_byte_cap", "final_evidence_byte_cap",
                "frame_max_pixels", "visual_probe_seconds", "visual_period_seconds", "max_records",
                "max_result_bytes", "wall_timeout_seconds", "cpu_threads")
    if any(not isinstance(cfg[k], (int, float)) or not math.isfinite(cfg[k]) or cfg[k] <= 0 for k in positive):
        raise ValueError("INVALID_POSITIVE_RESOURCE_LIMIT")
    win, overlap = cfg["window_seconds"], cfg["overlap_seconds"]
    if not 0 < win <= 300 or not 0 <= overlap < win or overlap > 2:
        raise ValueError("WINDOW_LIMIT: 0 <= overlap <= 2 < window <= 300 seconds")
    # float32 conversion + original s16 + transient copies, one current window.
    current = int(win * SAMPLE_RATE) * 2
    if current * 5 > cfg["pending_audio_byte_cap"] or current * 5 + cfg["pending_image_byte_cap"] > cfg["cache_byte_cap"]:
        raise MediaLimit("BLOCKED_RESOURCE: PCM/float window reservation exceeds queue/cache byte cap")
    if cfg["final_evidence_byte_cap"] < 1024**2:
        raise MediaLimit("BLOCKED_RESOURCE: final budget below 1 MiB checkpoint/stop reserve")
    if cfg["max_windows"] is not None and (not isinstance(cfg["max_windows"], int) or cfg["max_windows"] <= 0):
        raise ValueError("INVALID_MAX_WINDOWS")
    if cfg["read_chunk_bytes"] > 1024**2:
        raise ValueError("READ_CHUNK_EXCEEDS_ONE_MIB")
    return cfg


def _safe_output(path, source=None):
    path = Path(path).absolute()
    if ".." in path.parts or str(path).startswith(("//", "\\\\")):
        raise ValueError("UNSAFE_OUTPUT_PATH")
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("UNSAFE_OUTPUT_LINK")
    path.mkdir(parents=True, exist_ok=True)
    path = path.resolve(strict=True)
    if source is not None and Path(source).is_relative_to(path):
        raise ValueError("OUTPUT_MUST_NOT_CONTAIN_SOURCE")
    return path


def _identity(source):
    stat = source.stat()
    # No full-file hashing/pre-read. This is explicitly a local stat revision,
    # not a cryptographic assertion that unchanged timestamps imply same bytes.
    return {"path": str(source), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "device": stat.st_dev, "inode": stat.st_ino, "method": "local_stat_revision_not_full_content_hash"}


class CountingReader(io.RawIOBase):
    """Seekable local file, real returned bytes including repeated index reads."""
    def __init__(self, source, byte_cap, chunk=1024**2):
        self.stream = open(source, "rb", buffering=0)
        self.byte_cap, self.chunk = int(byte_cap), int(chunk)
        self.received = self.read_calls = self.seek_calls = self.max_read = 0

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.stream.tell()
    def seek(self, offset, whence=0):
        self.seek_calls += 1
        return self.stream.seek(offset, whence)

    def read(self, size=-1):
        if size < 0:
            raise MediaLimit("UNBOUNDED_READ_REJECTED")
        size = min(int(size), self.chunk)
        if size == 0:
            return b""
        if self.received >= self.byte_cap:
            if self.stream.tell() >= os.fstat(self.stream.fileno()).st_size:
                return b""
            raise MediaLimit("BLOCKED_RESOURCE: actual input byte cap")
        data = self.stream.read(min(size, self.byte_cap - self.received))
        self.received += len(data)
        self.read_calls += 1
        self.max_read = max(self.max_read, len(data))
        return data

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def close(self):
        if not self.closed:
            self.stream.close()
        super().close()


def _open(reader, cfg):
    import av
    # Demuxer whitelist also rejects local playlists/concat that could otherwise
    # cause FFmpeg to open unregistered files or network URLs behind our reader.
    return av.open(reader, mode="r", buffer_size=32768,
                   options={"protocol_whitelist": "file,pipe", "format_whitelist": _FORMATS,
                            "probesize": str(min(262144, int(cfg["probe_max_received_bytes"]))),
                            "analyzeduration": "1000000", "enable_drefs": "0"})


def _description(container, source, reader):
    streams = []
    for stream in container.streams:
        kind = stream.type
        ctx = stream.codec_context
        duration = round(float(stream.duration * stream.time_base) * 1000) if stream.duration is not None and stream.time_base else None
        item = {"index": stream.index, "type": kind, "codec": ctx.name if ctx else None,
                "duration_ms": duration,
                "start_ms": round(float((stream.start_time or 0) * stream.time_base) * 1000) if stream.time_base else 0}
        if kind == "video":
            item.update(width=ctx.width, height=ctx.height, display_rotation_degrees=stream.metadata.get("rotate", "0"))
        if kind == "audio":
            item.update(sample_rate=ctx.sample_rate, channels=ctx.channels)
        streams.append(item)
    video = any(s["type"] == "video" for s in streams)
    return {"media_type": "video/" + container.format.name.split(",")[0] if video else "audio/" + container.format.name.split(",")[0],
            "provider_id": "pyav", "provider_version": distributions.version("av"),
            "duration_ms": round(container.duration / 1000) if container.duration is not None else None,
            "timeline_origin_ms": round((container.start_time or 0) / 1000),
            "streams": streams, "size_bytes": source.stat().st_size,
            "input_bytes_received": reader.received,
            "source_capabilities": {"kind": "local_file", "seekable": True,
                                    "authorized_connector_reader": False, "http_range": "unsupported_not_probed",
                                    "network_saving_claim": False},
            "processing_status": "extracted", "reason": "LOCAL_CONTAINER_METADATA_PROBED_NOT_CONTENT_ACCEPTANCE"}


def probe_media(path) -> dict:
    """Bounded local metadata probe, not network/credential/Range discovery."""
    start = time.monotonic()
    reader = None
    try:
        source = _local_file(path)
        cfg = _limits()
        reader = CountingReader(source, cfg["probe_max_received_bytes"], cfg["read_chunk_bytes"])
        with _open(reader, cfg) as container:
            result = _description(container, source, reader)
        result["input_bytes_received"] = reader.received
        result["elapsed_seconds"] = time.monotonic() - start
        return result
    except Exception as exc:
        return {"media_type": "application/octet-stream", "provider_id": "pyav", "provider_version": None,
                "processing_status": "blocked" if isinstance(exc, MediaLimit) else "unsupported",
                "reason": f"{type(exc).__name__}: {exc}", "streams": [], "duration_ms": None,
                "input_bytes_received": reader.received if reader else 0,
                "elapsed_seconds": time.monotonic() - start}
    finally:
        if reader:
            reader.close()


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _disk_bytes(root):
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        if any((Path(directory) / name).is_symlink() for name in dirs):
            raise ValueError("UNSAFE_CHECKPOINT_LINK")
        for name in files:
            path = Path(directory) / name
            if path.is_symlink() or path.stat().st_nlink > 1:
                raise ValueError("UNSAFE_CHECKPOINT_FILE_LINK")
            total += path.stat().st_size
    return total


class Store:
    def __init__(self, root, cfg, identity):
        self.root, self.cfg = root, cfg
        _disk_bytes(root)
        self.db = sqlite3.connect(root / ".media-checkpoint.sqlite3")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.executescript("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS windows(start INTEGER PRIMARY KEY,end INTEGER NOT NULL,value TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS frames(time INTEGER PRIMARY KEY,value TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,value TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS prepared(start INTEGER PRIMARY KEY,value TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS prepared_frames(time INTEGER PRIMARY KEY,value TEXT NOT NULL);")
        stable = {k: cfg[k] for k in ("window_seconds", "overlap_seconds", "visual_probe_seconds", "visual_period_seconds",
                                      "scene_threshold", "local_change_threshold", "asr_enabled", "ocr_enabled", "model_dir", "language")}
        binding = {"source": identity, "algorithm": PROVIDER_VERSION, "config": stable}
        old = self.get("binding")
        if old is not None and old != binding:
            self.db.close()
            raise ValueError("CHECKPOINT_BINDING_MISMATCH: source/stat/config changed; use a new output revision")
        self.set("binding", binding)
        # Verify only bounded derived artifacts, never hash the entire source.
        for table in ("windows", "frames"):
            for (value,) in self.db.execute(f"SELECT value FROM {table}"):
                record = json.loads(value)
                for file in record.get("files", []):
                    path = root / file["relative_path"]
                    if not path.is_file() or path.stat().st_size != file["byte_length"] or _sha(path) != file["sha256"]:
                        self.db.close()
                        raise ValueError("CHECKPOINT_ARTIFACT_MISMATCH: missing or modified evidence; no blind replay")

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        data = _json(value)
        if len(data) > 1024**2:
            raise MediaLimit("BLOCKED_RESOURCE: checkpoint metadata byte cap")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, data.decode()))

    def put(self, table, stamp, value, end=None):
        if self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] >= self.cfg["max_records"]:
            raise MediaLimit("BLOCKED_RESOURCE: checkpoint record limit")
        data = _json(value)
        if len(data) > 1024**2:
            raise MediaLimit("BLOCKED_RESOURCE: individual checkpoint record limit")
        with self.db:
            if table == "windows":
                self.db.execute("INSERT OR REPLACE INTO windows VALUES(?,?,?)", (stamp, end, data.decode()))
            else:
                self.db.execute("INSERT OR REPLACE INTO frames VALUES(?,?)", (stamp, data.decode()))

    def write(self, relative, data, kind, media_type):
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        _safe_output(destination.parent)
        digest = hashlib.sha256(data).hexdigest()
        if destination.exists():
            if not destination.is_file() or destination.is_symlink() or _sha(destination) != digest:
                raise ValueError("ARTIFACT_COLLISION: refusing to overwrite different bytes")
        else:
            import shutil
            if shutil.disk_usage(self.root).free < len(data) + 1024**2:
                raise MediaLimit("BLOCKED_RESOURCE: free disk reserve; existing evidence retained")
            if len(data) + _disk_bytes(self.root) + 1024**2 > self.cfg["final_evidence_byte_cap"]:
                raise MediaLimit("BLOCKED_RESOURCE: final evidence budget; existing evidence retained")
            temp = destination.with_suffix(destination.suffix + ".pending")
            if temp.exists():
                # Orphan is ours only when deterministic bytes match exactly.
                if not temp.is_file() or _sha(temp) != digest:
                    raise ValueError("ORPHAN_ARTIFACT_MISMATCH")
            else:
                with temp.open("xb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
            os.replace(temp, destination)
        return {"key": relative.replace("/", "-").replace(".", "-"), "relative_path": relative,
                "kind": kind, "media_type": media_type, "byte_length": len(data), "sha256": digest}


class ASR:
    def __init__(self, cfg):
        self.model = None
        self.reason = "ASR_DISABLED" if not cfg["asr_enabled"] else None
        self.cfg = cfg
        self.version = "1.2.1"
        if self.reason:
            return
        root = Path(cfg["model_dir"])
        try:
            root = _local_file(root / "model.bin").parent
            if any(not (root / name).is_file() for name in ("tokenizer.json", "config.json", "vocabulary.txt")):
                self.reason = "ASR_WEIGHTS_MISSING: offline files incomplete; no download attempted"
                return
            expected_hashes = {"model.bin": "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1",
                               "config.json": "a73a28cdfe1c43ccc7202fa333d1f89c202477271407ae9a7f19afa52039cac8",
                               "tokenizer.json": "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
                               "vocabulary.txt": "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"}
            expected_sizes = {"model.bin": 75538270, "config.json": 2249, "tokenizer.json": 2203239, "vocabulary.txt": 459861}
            if any((root / name).stat().st_size != expected_sizes[name]
                   or _sha(_local_file(root / name)) != digest for name, digest in expected_hashes.items()):
                self.reason = "ASR_WEIGHTS_NOT_ADMITTED: expected reviewed MIT tiny revision"
                return
            self.version = distributions.version("faster-whisper")
            if self.version != "1.2.1":
                self.reason = "ASR_VERSION_NOT_ADMITTED: expected 1.2.1"
                return
            from faster_whisper import WhisperModel
            self.model = WhisperModel(str(root), device="cpu", compute_type="int8", cpu_threads=int(cfg["cpu_threads"]),
                                      num_workers=1, local_files_only=True)
        except FileNotFoundError:
            self.reason = "ASR_WEIGHTS_MISSING: no download attempted"
        except Exception as exc:
            self.reason = f"ASR_UNAVAILABLE: {type(exc).__name__}: {exc}"

    def transcribe(self, pcm, start_ms, end_ms):
        started = time.monotonic()
        if self.model is None:
            return {"status": "unsupported", "reason": self.reason, "segments": [], "elapsed_seconds": 0}
        try:
            import numpy as np
            # Crucial: ndarray is <= one outer window, never the media path.
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            audio *= 1.0 / 32768.0
            segments, info = self.model.transcribe(audio, language=self.cfg["language"], beam_size=1,
                                                   vad_filter=False, condition_on_previous_text=False,
                                                   word_timestamps=True)
            result = []
            for segment in segments:  # Must consume the lazy inference generator.
                if len(result) >= 4000:
                    raise MediaLimit("ASR_SEGMENT_LIMIT")
                a = max(start_ms, min(end_ms, start_ms + round(segment.start * 1000)))
                b = max(a, min(end_ms, start_ms + round(segment.end * 1000)))
                text = segment.text.strip()
                if len(text) > 16000:
                    raise MediaLimit("ASR_SEGMENT_TEXT_LIMIT")
                words = [{"start_ms": max(a, min(b, start_ms + round(w.start * 1000))),
                          "end_ms": max(a, min(b, start_ms + round(w.end * 1000))), "text": w.word}
                         for w in (segment.words or [])]
                result.append({"start_ms": a, "end_ms": b, "text": text, "words": words})
            return {"status": "extracted", "reason": "ASR_EXECUTED" if result else "ASR_EXECUTED_NO_TEXT; not proof of silence",
                    "segments": result, "language": info.language, "elapsed_seconds": time.monotonic() - started,
                    "provider_id": "faster-whisper", "provider_version": self.version, "runtime": "local_cpu"}
        except Exception as exc:
            # Deterministic failure is not retried on every remaining window.
            self.reason = f"ASR_FAILED: {type(exc).__name__}: {exc}"
            self.model = None
            return {"status": "failed", "reason": self.reason, "segments": [], "elapsed_seconds": time.monotonic() - started}


def _time_locator(start, end):
    return {"kind": "time", "precision": "exact", "start_ms": max(0, int(start)), "end_ms": max(0, int(end))}


def _union(ranges):
    merged = []
    for a, b in sorted(ranges):
        if not merged or a > merged[-1][1]: merged.append([a, b])
        else: merged[-1][1] = max(merged[-1][1], b)
    return sum(b - a for a, b in merged), merged


def _dedupe_segments(current, previous):
    """Boundary-only time AND text alignment; original hypotheses stay on disk."""
    import re
    def norm(text):
        return re.sub(r"[\W_]+", "", text.casefold())
    previous_words = [w for s in previous for w in s.get("words", [])]
    kept = []
    for segment in current:
        if any(norm(segment["text"]) == norm(p["text"]) and segment["start_ms"] < p["end_ms"]
               and segment["end_ms"] > p["start_ms"] for p in previous):
            continue
        words = segment.get("words", [])
        if words and previous_words:
            remaining = [w for w in words if not any(norm(w["text"]) and norm(w["text"]) == norm(p["text"])
                         and w["start_ms"] < p["end_ms"] and w["end_ms"] > p["start_ms"] for p in previous_words)]
            if not remaining:
                continue
            if len(remaining) != len(words):
                segment = {**segment, "text": "".join(w["text"] for w in remaining).strip(),
                           "start_ms": remaining[0]["start_ms"], "end_ms": remaining[-1]["end_ms"], "words": remaining}
        kept.append(segment)
    return kept


def _assemble(store, extra_gap=None):
    probe = store.get("probe", {})
    blocks, files, gaps, derivations, windows, frames = [], [], [], [], [], []
    observed, processed = [], []
    previous_text = []
    for (value,) in store.db.execute("SELECT value FROM windows ORDER BY start"):
        record = json.loads(value)
        windows.append({k: v for k, v in record.items() if k not in ("asr", "files")})
        files.extend(record["files"])
        raw = len(blocks)
        locator = _time_locator(record["start_ms"], record["end_ms"])
        observed.append((record["start_ms"], record["end_ms"]))
        blocks.append({"kind": "audio_segment", "origin": "native_extract", "locator": locator,
                       "file_keys": [record["files"][0]["key"]], "processing_status": "extracted",
                       "reason": "Source PCM window decoded; descriptor is not a transcript; original remains the local source"})
        asr = record["asr"]
        if asr["status"] == "extracted":
            processed.append((record["start_ms"], record["end_ms"]))
            kept = _dedupe_segments(asr["segments"], previous_text)
            previous_text = asr["segments"]
            for segment in kept or ([] if asr["segments"] else [{**locator, "text": ""}]):
                index = len(blocks)
                blocks.append({"kind": "audio_segment", "origin": "asr",
                               "locator": _time_locator(segment["start_ms"], segment["end_ms"]),
                               "text": segment["text"], "file_keys": [record["files"][-1]["key"]],
                               "processing_status": "extracted", "reason": asr["reason"] + "; ASR hypothesis, not human verification"})
                derivations.append({"block_indexes": [index], "source_block_indexes": [raw], "source_file_keys": [],
                                    "provider_id": "faster-whisper", "provider_version": asr["provider_version"],
                                    "runtime": "local_cpu", "uncertainty": "ASR may omit or hallucinate words; boundary hypotheses retained"})
        else:
            gaps.append({"kind": "unprocessed", "severity": "important", "reason": asr["reason"], "block_indexes": [raw]})
    for (value,) in store.db.execute("SELECT value FROM frames ORDER BY time"):
        record = json.loads(value)
        files.extend(record["files"])
        raw = len(blocks)
        key = record["files"][0]["key"]
        locator = _time_locator(record["time_ms"], record["time_ms"])
        locator["geometry"] = {"reference_object_ref": key, "coordinate_space": "original", "unit": "pixel", "origin": "top_left",
                               "frame_size": record["size"], "original_size": {**record["size"], "unit": "pixel"},
                               "rotation_degrees": 0, "bbox_xywh": [0, 0, record["size"]["width"], record["size"]["height"]],
                               "transform_to_original": {"method": "identity", "operations": []}}
        blocks.append({"kind": "image_occurrence", "origin": "native_extract", "locator": locator,
                       "file_keys": [key], "processing_status": "extracted", "reason": "Actual decoded frame: " + ",".join(record["triggers"])})
        frames.append({k: v for k, v in record.items() if k not in ("files", "ocr")})
        ocr = record["ocr"]
        if ocr.get("processing_status") == "extracted" and ocr.get("metadata", {}).get("ocr_executed") is True:
            indexes = []
            text = ocr["text"]
            for offset in range(0, max(1, len(text)), 16000):
                indexes.append(len(blocks))
                blocks.append({"kind": "paragraph", "origin": "ocr", "locator": locator, "text": text[offset:offset + 16000],
                               "file_keys": [record["files"][-1]["key"], key], "processing_status": "extracted",
                               "reason": ocr["reason"] + f"; OCR fragment chars {offset}..{min(offset + 16000, len(text))}; full text, boxes and transforms retained in JSON"})
            derivations.append({"block_indexes": indexes, "source_block_indexes": [raw], "source_file_keys": [key],
                                "provider_id": ocr["provider_id"], "provider_version": ocr["provider_version"],
                                "runtime": "local_cpu", "uncertainty": "OCR does not interpret the entire image"})
        else:
            gaps.append({"kind": "unprocessed", "severity": "important", "reason": ocr.get("reason", "OCR_NOT_EXECUTED"), "block_indexes": [raw]})
    complete = store.get("decode_complete", False)
    if probe.get("streams") and any(s["type"] == "video" for s in probe["streams"]):
        gaps.append({"kind": "semantic_gap", "severity": "important", "reason": "Lossy periodic + sampled scene/local-pixel changes; no guarantee of all visual operations, brief text, speech pointers or semantics", "block_indexes": []})
    if not complete:
        stop = store.get("stop_reason", "MEDIA_NOT_ENUMERATED_TO_EOF")
        gaps.append({"kind": "resource_block" if "RESOURCE" in (stop or "") else "temporal_gap", "severity": "important", "reason": stop or "MEDIA_NOT_ENUMERATED_TO_EOF", "block_indexes": []})
    gaps.extend(store.get("extra_gaps", []))
    if extra_gap:
        gaps.append(extra_gap)
    obs, obs_ranges = _union(observed)
    done, done_ranges = _union(processed)
    audio_streams = [s for s in probe.get("streams", []) if s["type"] == "audio"]
    expected = audio_streams[0]["duration_ms"] if audio_streams else None
    metrics = []
    if audio_streams:
        metrics.append({"unit": "millisecond", "expected": expected, "observed": obs, "processed": done, "excluded": 0,
                        "method": "full_audio_windows", "ranges": [_time_locator(a, b) for a, b in done_ranges]})
    if any(s["type"] == "video" for s in probe.get("streams", [])):
        metrics.append({"unit": "visual_occurrence", "expected": None, "observed": len(frames), "processed": len(frames), "excluded": 0,
                        "method": "sparse_visual_sampling", "ranges": [_time_locator(f["time_ms"], f["time_ms"]) for f in frames]})
    runs = [json.loads(r[0]) for r in store.db.execute("SELECT value FROM runs ORDER BY id")]
    return {"media_type": probe.get("media_type", "application/octet-stream"), "provider_id": PROVIDER_ID,
            "provider_version": PROVIDER_VERSION, "blocks": blocks, "files": files, "gaps": gaps,
            "metrics": metrics, "enumeration_state": "complete" if complete else "partial",
            "processing_status": "partial" if blocks else "blocked",
            "metadata": {"probe": probe, "source_identity": store.get("binding", {}).get("source"),
                         "derivations": derivations, "audio_windows": windows, "visual_frames": frames,
                         "audio_observed_ranges": [_time_locator(a, b) for a, b in obs_ranges],
                         "runs": runs, "input_bytes_received": sum(r["input_bytes_received"] for r in runs),
                         "input_bytes_not_attributed_to_audio_windows": sum(r["input_bytes_received"] for r in runs) - sum(w["input_bytes_received"] for w in windows),
                         "input_byte_attribution": "actual reader bytes since previous audio emission, includes buffered/index/seek reads; not intrinsic per-time encoded size",
                         "queue": {"max_pending_audio_windows": 1, "max_pending_images": 1, "backpressure": "synchronous sinks stop demux reads",
                                   "audio_byte_cap": store.cfg["pending_audio_byte_cap"], "image_byte_cap": store.cfg["pending_image_byte_cap"],
                                   "cache_byte_cap": store.cfg["cache_byte_cap"], "read_chunk_byte_cap": store.cfg["read_chunk_bytes"]},
                         "recovery_policy": "Completed decode windows and prepared inference receipts are idempotent. A provider/config/source revision change needs a new output revision; deterministic failed windows are not silently re-inferred.",
                         "timestamp_unit": "ms", "full_input_hash_performed": False,
                         "visual_content_completeness": "unknown_lossy", "network_saving_claim": False,
                         "overlap_deduplication": "time-and-text/word alignment; raw segment and word hypotheses retained",
                         "peak_pcm_bytes": store.get("peak_pcm_bytes", 0),
                         "decoder_padding_trimmed_samples": store.get("decoder_padding_trimmed_samples", 0),
                         "synthetic": store.cfg["synthetic"], "production_acceptance": "pending_real_authorized_course_input"}}


def _process(source, root, cfg):
    import av
    store = Store(root, cfg, _identity(source))
    reader = None
    started = time.monotonic()
    before = _identity(source)
    try:
        if store.get("decode_complete", False):
            result = _assemble(store)
            store.db.close()
            return result
        reader = CountingReader(source, min(cfg["probe_max_received_bytes"], cfg["input_byte_cap"]), cfg["read_chunk_bytes"])
        with _open(reader, cfg) as container:
            reader.byte_cap = int(cfg["input_byte_cap"])
            probe = _description(container, source, reader)
            store.set("probe", probe)
            audio = next(iter(container.streams.audio), None)
            video = next(iter(container.streams.video), None)
            selected = [s for s in (audio, video) if s is not None]
            if not selected:
                raise ValueError("UNSUPPORTED_MEDIA: no audio/video streams")
            extra = []
            if len(selected) < len(container.streams):
                extra.append({"kind": "unsupported", "severity": "important", "reason": "Only first audio/video streams processed; other enumerated tracks/subtitles remain unprocessed", "block_indexes": []})
            store.set("extra_gaps", extra)
            for stream in selected:
                stream.thread_type = "SLICE"
                stream.codec_context.thread_count = 1
            if video and (video.codec_context.width * video.codec_context.height > cfg["frame_max_pixels"]
                          or video.codec_context.width * video.codec_context.height * 6 > cfg["pending_image_byte_cap"]):
                raise MediaLimit("BLOCKED_RESOURCE: video frame dimensions exceed image reservation")
            last = store.db.execute("SELECT start,end FROM windows ORDER BY start DESC LIMIT 1").fetchone()
            resume_ms = max(0, last[0] + round((cfg["window_seconds"] - cfg["overlap_seconds"]) * 1000)) if last else 0
            if not audio:
                last_frame = store.db.execute("SELECT max(time) FROM frames").fetchone()[0]
                resume_ms = last_frame + 1 if last_frame is not None else 0
            origin = probe["timeline_origin_ms"]
            audio_end_sample = round((float(((audio.start_time or 0) + audio.duration) * audio.time_base) - origin / 1000) * SAMPLE_RATE) if audio and audio.duration is not None and audio.time_base else None
            trimmed_padding_samples = 0
            if resume_ms:
                container.seek((resume_ms + origin) * 1000, backward=True, any_frame=False)
            asr = ASR(cfg) if audio else None
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE) if audio else None
            window_samples = round(cfg["window_seconds"] * SAMPLE_RATE)
            step_samples = round((cfg["window_seconds"] - cfg["overlap_seconds"]) * SAMPLE_RATE)
            buffer = bytearray()
            base_sample = max(0, round(resume_ms * SAMPLE_RATE / 1000))
            incoming_cursor = base_sample
            already_until_sample = round(last[1] * SAMPLE_RATE / 1000) if last else base_sample
            new_samples, emitted, read_mark = 0, 0, 0
            boundary_time = time.monotonic()
            peak_pcm, next_probe, last_kept, previous_gray = 0, resume_ms, None, None

            def emit():
                nonlocal emitted, read_mark, boundary_time, new_samples
                a = round(base_sample * 1000 / SAMPLE_RATE)
                b = round((base_sample + len(buffer) // 2) * 1000 / SAMPLE_RATE)
                if b <= a:
                    return
                # Decoder/IO timing excludes the downstream ASR wall time.
                decode_seconds = time.monotonic() - boundary_time
                descriptor = {"start_ms": a, "end_ms": b, "sample_rate": SAMPLE_RATE, "sample_format": "s16le_mono",
                              "pcm_bytes": len(buffer), "pcm_sha256": hashlib.sha256(buffer).hexdigest(),
                              "source_is_local_original": True, "synthetic": cfg["synthetic"]}
                prepared = store.db.execute("SELECT value FROM prepared WHERE start=?", (a,)).fetchone()
                if prepared:
                    prepared = json.loads(prepared[0])
                    if prepared["descriptor"] != descriptor:
                        raise ValueError("CHECKPOINT_PREPARED_PCM_MISMATCH")
                    result = prepared["asr"]
                else:
                    result = asr.transcribe(buffer, a, b)
                    data = _json({"descriptor": descriptor, "asr": result})
                    if len(data) > 1024**2:
                        raise MediaLimit("BLOCKED_RESOURCE: prepared transcript byte cap")
                    with store.db:
                        store.db.execute("INSERT INTO prepared VALUES(?,?)", (a, data.decode()))
                files = [store.write(f"audio/window-{a:012d}.json", _json(descriptor), "inventory", "application/json")]
                if result["status"] == "extracted":
                    files.append(store.write(f"transcripts/window-{a:012d}.json", _json(result), "transcript", "application/json"))
                record = {"start_ms": a, "end_ms": b, "pcm_bytes": len(buffer),
                          "input_bytes_received": reader.received - read_mark, "decode_elapsed_seconds": decode_seconds,
                          "asr_elapsed_seconds": result["elapsed_seconds"], "asr_status": result["status"],
                          "asr": result, "files": files}
                store.put("windows", a, record, b)
                with store.db:
                    store.db.execute("DELETE FROM prepared WHERE start=?", (a,))
                emitted += 1
                new_samples = 0
                read_mark = reader.received
                boundary_time = time.monotonic()
                if cfg["max_windows"] is not None and emitted >= cfg["max_windows"]:
                    raise PauseMedia("CHECKPOINT_PAUSE: configured new-window limit")

            def feed(frame):
                nonlocal base_sample, incoming_cursor, new_samples, peak_pcm, trimmed_padding_samples
                if frame.pts is not None and frame.time_base:
                    stamp = round((float(frame.pts * frame.time_base) - origin / 1000) * SAMPLE_RATE)
                else:
                    stamp = incoming_cursor
                if frame.samples * 2 > cfg["read_chunk_bytes"] or frame.samples * 2 + len(buffer) * 5 > cfg["pending_audio_byte_cap"]:
                    raise MediaLimit("BLOCKED_RESOURCE: resampled PCM frame/queue reservation")
                data = frame.to_ndarray().tobytes()
                count = len(data) // 2
                if audio_end_sample is not None and stamp + count > audio_end_sample:
                    kept_count = max(0, audio_end_sample - stamp)
                    trimmed_padding_samples += count - kept_count
                    data = data[:kept_count * 2]
                    count = kept_count
                incoming_cursor = stamp + count
                trim = max(0, base_sample + len(buffer) // 2 - stamp)
                if trim >= count:
                    return
                stamp += trim
                data = data[trim * 2:]
                target = base_sample + len(buffer) // 2
                if stamp > target + 2:
                    # Do not silently fabricate a continuous silent region.
                    if new_samples:
                        emit()
                    buffer.clear()
                    base_sample = stamp
                    extra.append({"kind": "temporal_gap", "severity": "important", "reason": f"Audio timestamp discontinuity: {round(target * 1000 / SAMPLE_RATE)}..{round(stamp * 1000 / SAMPLE_RATE)} ms; no synthetic PCM inserted", "block_indexes": []})
                    store.set("extra_gaps", extra)
                offset = 0
                while offset < len(data):
                    amount = min(window_samples * 2 - len(buffer), len(data) - offset)
                    append_start = base_sample + len(buffer) // 2
                    buffer.extend(data[offset:offset + amount])
                    new_samples += max(0, append_start + amount // 2 - max(append_start, already_until_sample))
                    peak_pcm = max(peak_pcm, len(buffer))
                    offset += amount
                    if len(buffer) == window_samples * 2:
                        emit()
                        del buffer[:step_samples * 2]
                        base_sample += step_samples

            for packet in container.demux(selected):
                for frame in packet.decode():
                    if packet.stream.type == "audio":
                        for resampled in resampler.resample(frame):
                            feed(resampled)
                    elif packet.stream.type == "video":
                        if frame.pts is None or not frame.time_base:
                            raise ValueError("VIDEO_TIMESTAMP_MISSING: cannot assign source-global time")
                        stamp = round(float(frame.pts * frame.time_base) * 1000) - origin
                        if stamp < next_probe or stamp < resume_ms:
                            continue
                        next_probe = stamp + round(cfg["visual_probe_seconds"] * 1000)
                        if frame.width * frame.height > cfg["frame_max_pixels"] or frame.width * frame.height * 6 > cfg["pending_image_byte_cap"]:
                            raise MediaLimit("BLOCKED_RESOURCE: dynamic video dimensions")
                        if store.db.execute("SELECT 1 FROM frames WHERE time=?", (stamp,)).fetchone():
                            continue
                        from PIL import ImageChops, ImageStat
                        image = frame.to_image()
                        gray = image.convert("L").resize((64, 36))
                        triggers = []
                        if last_kept is None or stamp - last_kept >= cfg["visual_period_seconds"] * 1000:
                            triggers.append("periodic_floor")
                        if previous_gray is not None:
                            difference = ImageChops.difference(gray, previous_gray)
                            if ImageStat.Stat(difference).mean[0] >= cfg["scene_threshold"]:
                                triggers.append("sampled_scene_change")
                            # Local tile change catches numbers in otherwise static
                            # screens; it is NOT a guarantee/semantic detector.
                            if max(ImageStat.Stat(difference.crop((x, y, x + 8, y + 6))).mean[0]
                                   for y in range(0, 36, 6) for x in range(0, 64, 8)) >= cfg["local_change_threshold"]:
                                triggers.append("sampled_local_pixel_change_including_silent_activity")
                        previous_gray = gray
                        if not triggers:
                            continue
                        encoded = CappedBuffer(cfg["pending_image_byte_cap"] // 2)
                        image.save(encoded, format="PNG")
                        if encoded.tell() > cfg["pending_image_byte_cap"]:
                            raise MediaLimit("BLOCKED_RESOURCE: encoded frame byte cap")
                        file = store.write(f"frames/frame-{stamp:012d}.png", encoded.getvalue(), "keyframe", "image/png")
                        from .ocr import ocr_image
                        prepared_frame = store.db.execute("SELECT value FROM prepared_frames WHERE time=?", (stamp,)).fetchone()
                        if prepared_frame:
                            prepared_frame = json.loads(prepared_frame[0])
                            if prepared_frame["image_sha256"] != file["sha256"]:
                                raise ValueError("CHECKPOINT_PREPARED_IMAGE_MISMATCH")
                            ocr = prepared_frame["ocr"]
                        else:
                            ocr = ocr_image(root / file["relative_path"], options={"max_input_bytes": cfg["pending_image_byte_cap"], "max_pixels": cfg["frame_max_pixels"]}) if cfg["ocr_enabled"] else {"processing_status": "unsupported", "reason": "OCR_DISABLED", "metadata": {"ocr_executed": False}}
                            data = _json({"image_sha256": file["sha256"], "ocr": ocr})
                            if len(data) > 1024**2:
                                raise MediaLimit("BLOCKED_RESOURCE: prepared OCR byte cap")
                            with store.db:
                                store.db.execute("INSERT INTO prepared_frames VALUES(?,?)", (stamp, data.decode()))
                        frame_files = [file]
                        if ocr.get("metadata", {}).get("ocr_executed"):
                            frame_files.append(store.write(f"frames/frame-{stamp:012d}.ocr.json", _json(ocr), "structured_data", "application/json"))
                        store.put("frames", stamp, {"time_ms": stamp, "triggers": triggers,
                                                   "size": {"width": image.width, "height": image.height},
                                                   "ocr": ocr, "files": frame_files})
                        with store.db:
                            store.db.execute("DELETE FROM prepared_frames WHERE time=?", (stamp,))
                        last_kept = stamp
            if audio:
                for frame in resampler.resample(None):
                    feed(frame)
                if new_samples:
                    emit()  # Never repeat a trailing buffer containing only overlap.
            if _identity(source) != before:
                raise ValueError("SOURCE_CHANGED_DURING_READ")
            store.set("decode_complete", True)
            store.set("stop_reason", None)
            store.set("peak_pcm_bytes", peak_pcm)
            store.set("decoder_padding_trimmed_samples", trimmed_padding_samples)
    except Exception as exc:
        store.set("stop_reason", f"{type(exc).__name__}: {exc}")
        store.set("decode_complete", False)
    finally:
        if reader:
            with store.db:
                store.db.execute("INSERT INTO runs(value) VALUES(?)", (_json({"input_bytes_received": reader.received,
                                 "read_calls": reader.read_calls, "seek_calls": reader.seek_calls,
                                 "max_read_bytes": reader.max_read, "elapsed_seconds": time.monotonic() - started,
                                 "mixed_source_demux_passes": 1, "resume_seek_ms": locals().get("resume_ms", 0)}).decode(),))
            reader.close()
    result = _assemble(store)
    result["metadata"]["peak_pcm_bytes"] = store.get("peak_pcm_bytes", locals().get("peak_pcm", 0))
    result["metadata"]["decoder_padding_trimmed_samples"] = store.get("decoder_padding_trimmed_samples", 0)
    store.db.close()
    return result


@contextmanager
def _output_lock(root):
    path = root / ".media.lock"
    if path.exists() and (path.is_symlink() or path.stat().st_nlink > 1):
        raise ValueError("UNSAFE_LOCK_PATH")
    handle = path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()  # OS releases the lock even on a crash; no stale PID guessing.


class _WindowsMemoryJob:
    """Kernel-enforced aggregate commit limit for a worker and its descendants."""
    def __init__(self, process, cap):
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_ = [("per_process", c.c_int64), ("per_job", c.c_int64), ("flags", w.DWORD),
                        ("min_ws", c.c_size_t), ("max_ws", c.c_size_t), ("active", w.DWORD),
                        ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(c.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_limit", c.c_size_t), ("job_limit", c.c_size_t),
                        ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]
        self.api, self.type = c.WinDLL("kernel32", use_last_error=True), Extended
        self.api.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.QueryInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        info = Extended()
        info.basic.flags = 0x200 | 0x2000  # JOB_MEMORY and KILL_ON_JOB_CLOSE
        info.job_limit = int(cap)
        if (not self.handle or not self.api.SetInformationJobObject(self.handle, 9, c.byref(info), c.sizeof(info))
                or not self.api.AssignProcessToJobObject(self.handle, int(process._handle))):
            error = c.get_last_error()
            if self.handle:
                self.api.CloseHandle(self.handle)
            raise MediaLimit(f"RAM_HARD_LIMIT_UNAVAILABLE: Windows job error {error}; no unbounded fallback")

    def peak(self):
        import ctypes as c
        info = self.type()
        if self.api.QueryInformationJobObject(self.handle, 9, c.byref(info), c.sizeof(info), None):
            return int(info.peak_job)
        return None

    def close(self):
        self.api.CloseHandle(self.handle)


def _subprocess_main():
    # Parent attaches the memory job BEFORE sending this admission message.
    request = json.loads(sys.stdin.readline(131072))
    cfg = request["cfg"]
    if os.name != "nt":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (int(cfg["ram_byte_cap"]), int(cfg["ram_byte_cap"])))
    result = _process(Path(request["source"]), Path(request["root"]), cfg)
    data = _json(result)
    if len(data) > cfg["max_result_bytes"]:
        raise MediaLimit("BLOCKED_RESOURCE: result byte cap; checkpoint retained")
    destination = Path(request["root"]) / ".media-worker-result.json"
    temp = destination.with_suffix(".pending")
    # Guarded owned output; previous final result is not a published artifact.
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, destination)


def _failure(reason, status="blocked"):
    return {"media_type": "application/octet-stream", "provider_id": PROVIDER_ID, "provider_version": PROVIDER_VERSION,
            "blocks": [], "files": [], "gaps": [{"kind": "resource_block" if "RESOURCE" in reason or "RAM_" in reason else "unsupported",
            "severity": "blocking", "reason": reason, "block_indexes": []}], "metrics": [],
            "enumeration_state": "unknown", "processing_status": status, "metadata": {"no_network_fallback": True}}


def _available_ram():
    if os.name == "nt":
        import ctypes as c
        class Memory(c.Structure):
            _fields_ = [("length", c.c_uint32), ("load", c.c_uint32)] + [(name, c.c_uint64) for name in ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
        memory = Memory()
        memory.length = c.sizeof(memory)
        if c.windll.kernel32.GlobalMemoryStatusEx(c.byref(memory)):
            return int(memory.available)
    else:
        try:
            return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError, AttributeError):
            pass
    return None


def extract_media(path: Path, output_dir: Path, *, options=None) -> dict:
    """Extract into an owned local directory, with hard worker RAM admission.

    Only local files are implemented; connectors/HTTP Range are unsupported.
    Defaults: CPU int8/one worker, 300s windows/2s overlap, 1 MiB IO,
    64 MiB audio queue, 32 MiB image slot, 96 MiB cache, 4 GiB aggregate
    worker memory, 512 MiB final evidence. Lower limits stop with a gap and a
    durable checkpoint; final evidence is never deleted to free cache.
    """
    process, job = None, None
    try:
        cfg = _limits(options)
        available_ram = _available_ram()
        if "ram_byte_cap" not in (options or {}) and available_ram is not None:
            cfg["ram_byte_cap"] = min(cfg["ram_byte_cap"], int(available_ram * 0.35))
        source = _local_file(path, cfg.get("allowed_root"))
        root = _safe_output(output_dir, source)
        _disk_bytes(root)
        with _output_lock(root):
            result_path = root / ".media-worker-result.json"
            # Prevent returning a stale result if this invocation fails.
            previous_result_mtime = result_path.stat().st_mtime_ns if result_path.exists() else None
            env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", OMP_NUM_THREADS=str(int(cfg["cpu_threads"])))
            process = subprocess.Popen([sys.executable, "-c", "from worldanalysis.media import _subprocess_main; _subprocess_main()"],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                       env=env, cwd=str(Path(__file__).resolve().parents[1]))
            if os.name == "nt":
                job = _WindowsMemoryJob(process, cfg["ram_byte_cap"])
            errors = bytearray()
            def drain():
                for chunk in iter(lambda: process.stderr.read(4096), b""):
                    if len(errors) < 16384:
                        errors.extend(chunk[:16384 - len(errors)])
            thread = threading.Thread(target=drain, daemon=True)
            thread.start()
            process.stdin.write(_json({"source": str(source), "root": str(root), "cfg": cfg}) + b"\n")
            process.stdin.close()
            try:
                code = process.wait(timeout=cfg["wall_timeout_seconds"])
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                code = -1
                errors.extend(b" WALL_TIMEOUT: durable checkpoint retained")
            thread.join(timeout=5)
            peak = job.peak() if job else None
            if code != 0:
                reason = f"WORKER_FAILED_OR_RAM_LIMIT: exit {code}; {errors.decode('utf-8', errors='replace')}; checkpoint retained, not auto-replayed"
                if (root / ".media-checkpoint.sqlite3").is_file():
                    try:
                        recovered = Store(root, cfg, _identity(source))
                        result = _assemble(recovered, {"kind": "resource_block", "severity": "blocking", "reason": reason, "block_indexes": []})
                        recovered.db.close()
                        result["enumeration_state"] = "partial"
                        result["metadata"]["worker_failure_reconciled"] = True
                        return result
                    except Exception:
                        pass  # Unsafe/malformed checkpoint is not promoted to evidence.
                return _failure(reason)
            if not result_path.is_file() or result_path.stat().st_size > cfg["max_result_bytes"]:
                return _failure("RESULT_MISSING_OR_RESOURCE_LIMIT: checkpoint retained")
            if previous_result_mtime is not None and result_path.stat().st_mtime_ns == previous_result_mtime:
                return _failure("STALE_WORKER_RESULT_REJECTED")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["metadata"]["ram"] = {"hard_cap_bytes": cfg["ram_byte_cap"], "peak_worker_commit_bytes": peak,
                                         "enforcement": "Windows JobObject aggregate commit" if job else "POSIX RLIMIT_AS worker address space",
                                         "workers": 1, "device": "cpu", "host_available_ram_at_admission": available_ram,
                                         "includes": "decode, ASR, OCR, queue; parent transport bounded separately"}
            return result
    except Exception as exc:
        return _failure(f"{type(exc).__name__}: {exc}")
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        if job:
            job.close()
        if process:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
