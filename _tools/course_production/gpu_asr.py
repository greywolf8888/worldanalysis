"""Optional local GPU transcription and independent candidate review."""
from contextlib import ExitStack, closing, contextmanager
import errno
import hashlib
from itertools import islice
import json
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import wave

from project import file_lock, local_child, media_source, original_media_path, put, read, sha
from asr_exact_cache import digest as exact_digest, pcm_digest, read_record, valid_part, write_record
from _tools.course_no_subtitles.speech import AUDIO_DECODE_REVISION, RATE, RUNTIME, audio_windows


GPU_BATCH_CHUNKS = 20
GPU_STREAM_REVISION = 1


@contextmanager
def gpu_slot(device, *, wait_seconds=600):
    """Allow one project process to load a recognizer on each shared device."""
    path = local_child(RUNTIME, f"tmp/course-asr-gpu-{device}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = monotonic() + wait_seconds
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(file_lock(path))
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN) and getattr(error, "winerror", None) != 33:
                    raise
                if monotonic() >= deadline:
                    raise RuntimeError("GPU 正在处理另一项目。已保存的取材可从原工作区接续") from error
                sleep(min(0.5, max(0, deadline - monotonic())))
            else:
                break
        yield


def select_device(executable, requested=None):
    proc = subprocess.run([str(executable), "--diagnostics"], capture_output=True, check=True, timeout=30)
    output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    devices = [{"index": int(index), "description": description, "memory_mib": int(memory)}
               for index, description, memory in re.findall(
                   r"\[(\d+)\]\s+gpu\s+name=Vulkan\d+\s+desc=(.*?)\s+mem=\d+/(\d+)\s+MiB", output
               )]
    choices = [row for row in devices if requested is None or row["index"] == requested]
    if not choices:
        raise ValueError("CrispASR 诊断中未找到指定或可用的 Vulkan 设备")
    return max(choices, key=lambda row: (row["memory_mib"], -row["index"]))


class VulkanASR:
    def __init__(self, executable, model, backend, device=None):
        self.executable = Path(executable).resolve(strict=True)
        self.model = Path(model).resolve(strict=True)
        self.backend = backend
        self.device = select_device(self.executable, device)
        self.decoded_windows = []
        self.identity = {"backend": backend, "device": self.device, "runtime_sha256": sha(self.executable),
                         "model_sha256": sha(self.model), "stream_revision": GPU_STREAM_REVISION}

    def transcribe(self, source, duration, track, language, offset=0, *, cache=None, shared_context=None,
                   use_shared_cache=True):
        if cache is None:
            return self.transcribe_windows(source, duration, track, language, [(None, None, offset)])[0]
        # The decoder's fixed chunks are 30 seconds. Group them from one stream so
        # independent seeks and resampler startup cannot drop samples between batches.
        rows, decoded = [], []
        self.shared_cache_stats = {"project_checkpoints": 0, "shared_hits": 0, "fresh_groups": 0,
                                   "shared_write_failures": 0}
        shared = None
        if shared_context is not None:
            if sha(Path(source)) != shared_context["audio_sha256"]:
                raise ValueError("共享 ASR 取材前音源指纹发生变化")
            if not hasattr(self, "_decoder_sha256"):
                executable = shutil.which("ffmpeg")
                if not executable:
                    raise ValueError("共享 ASR 取材需要已定位的 FFmpeg")
                self._decoder_sha256 = sha(Path(executable))
            if use_shared_cache:
                shared = local_child(RUNTIME, "cache/course-asr-exact")
        start_sample = 0
        with closing(audio_windows(source, 0, None, "fixed", track)) as stream:
            while True:
                chunks = [sample for _, sample in islice(stream, GPU_BATCH_CHUNKS)]
                if not chunks and start_sample:
                    break
                start = start_sample / RATE
                count = sum(map(len, chunks)) // 2
                part_path = cache / f"{start_sample:012}.json"
                window = {"requested_start": start, "requested_length": count / RATE,
                          "timeline_start": offset + start, "samples": count, "sample_rate": RATE}
                key = shared_path = None
                if shared_context is not None:
                    key = {"input": shared_context, "recognizer": self.identity,
                           "decoder_sha256": self._decoder_sha256, "audio_decode_revision": AUDIO_DECODE_REVISION,
                           "fixed_seconds": 30, "group_chunks": GPU_BATCH_CHUNKS, "track": track,
                           "language": language, "duration": duration, "offset": offset,
                           "window": window, "pcm_sha256": pcm_digest(chunks)}
                    key_sha = exact_digest(key)
                    if shared is not None:
                        shared_path = local_child(shared, f"{key_sha[:2]}/{key_sha}.json")
                part = read_record(part_path, key, duration) if key is not None else None
                if part is None:
                    try:
                        previous = read(part_path)
                    except (OSError, ValueError):
                        previous = None
                    # Legacy local checkpoints were scoped to this project's source/settings key.
                    # They may complete that same project, but never populate the global cache.
                    if valid_part(previous, window, duration):
                        part = previous
                if part is not None:
                    self.shared_cache_stats["project_checkpoints"] += 1
                elif shared_path is not None and (cached := read_record(shared_path, key, duration)) is not None:
                    part = cached
                    write_record(part_path, key, part, duration)
                    self.shared_cache_stats["shared_hits"] += 1
                else:
                    request = (start, count / RATE, offset + start, chunks)
                    result, = self._transcribe_pcm(duration, language, [request])
                    observed, = self.decoded_windows
                    part = {"cues": result, "window": observed}
                    if key is not None and valid_part(part, window, duration):
                        write_record(part_path, key, part, duration)
                        if shared_path is not None:
                            try:
                                write_record(shared_path, key, part, duration)
                            except OSError:
                                self.shared_cache_stats["shared_write_failures"] += 1
                    else:
                        put(part_path, part)
                    self.shared_cache_stats["fresh_groups"] += 1
                rows.extend(part["cues"])
                decoded.append(part["window"])
                start_sample += count
                if len(chunks) < GPU_BATCH_CHUNKS:
                    break
        self.decoded_windows = decoded
        return rows

    def transcribe_windows(self, source, duration, track, language, windows):
        """Decode separate original time windows and load the model once for the batch."""
        inputs = [(start or 0, length, offset,
                   (chunk for _, chunk in audio_windows(source, start or 0, length, "fixed", track)))
                  for start, length, offset in windows]
        return self._transcribe_pcm(duration, language, inputs)

    def _transcribe_pcm(self, duration, language, windows):
        scratch = local_child(RUNTIME, "tmp")
        scratch.mkdir(parents=True, exist_ok=True)
        self.decoded_windows = []
        with TemporaryDirectory(prefix="course-asr-", dir=scratch) as folder:
            outputs = []
            outfile_args = []
            for index, (start, length, offset, chunks) in enumerate(windows):
                output = Path(folder) / f"transcript-{index:02}"
                wav = output.with_suffix(".wav")
                # Decode PCM before writing the WAV header. Source timestamps can jitter
                # after resampling or seeking and make FFmpeg's WAV muxer reject valid samples.
                with wave.open(str(wav), "wb") as pcm:
                    pcm.setparams((1, 2, RATE, 0, "NONE", ""))
                    for chunk in chunks:
                        pcm.writeframesraw(chunk)
                    samples = pcm.getnframes()
                    self.decoded_windows.append({"requested_start": start, "requested_length": length,
                                                 "timeline_start": offset, "samples": samples,
                                                 "sample_rate": RATE})
                if not samples:
                    outputs.append((None, offset))
                    continue
                outfile_args.extend(["-f", wav.name, "-of", output.name])
                outputs.append((output.with_suffix(".json"), offset))
            if not outfile_args:
                return [[] for _ in windows]
            backend = (["--backend", self.backend, "--strict-pipeline", "--chunk-seconds", "30",
                        "--chunk-overlap", "0"] if self.backend in {"sensevoice", "qwen3"} else ["-nf", "-mc", "0"])
            seconds = sum(length if length is not None else duration for _, length, _, _ in windows)
            with gpu_slot(self.device["index"]):
                process = subprocess.run([str(self.executable), *backend, "--gpu-backend", "vulkan",
                                          "-dev", str(self.device["index"]),
                                          "-m", str(self.model), "-l", language, "-t", "4", "-bs", "1", "-bo", "1",
                                          "-oj", *outfile_args], cwd=folder, check=True, capture_output=True,
                                         timeout=max(180, seconds * 2))
            log = process.stderr.decode("utf-8", "replace")
            no_speech = set(re.findall(r"(?m)^crispasr: warning: no speech detected in '([^'\r\n]+)'\s*$", log))
            transcripts = []
            for result, offset in outputs:
                if result is None:
                    transcripts.append([])
                    continue
                if not result.is_file():
                    if result.with_suffix(".wav").name in no_speech:
                        transcripts.append([])
                        continue
                    reason = log.strip().splitlines()
                    raise ValueError("CrispASR 未写入转写文件。" + (" " + reason[-1] if reason else ""))
                rows = []
                for line in read(result)["transcription"]:
                    left = offset + line["offsets"]["from"] / 1000
                    right = min(duration, offset + line["offsets"]["to"] / 1000)
                    if 0 <= left < right:
                        rows.append({"start": round(left, 3), "end": round(right, 3), "text": line["text"].strip()})
                transcripts.append(rows)
            return transcripts


def refine_candidates(project, recognizer, lesson=None, ranges=None):
    from transcript import valid_span

    media = read(project.ledger / "media-summary.json")
    if ranges and lesson is None:
        raise ValueError("按区间复识必须指定讲次")
    if lesson is not None and all(row["lesson"] != lesson for row in media["videos"]):
        raise ValueError(f"未找到第 {lesson} 讲")
    reports = []
    for item in media["videos"]:
        number = item["lesson"]
        if lesson is not None and number != lesson:
            continue
        scan = project.work / "媒体补查" / f"L{number:02}.json"
        path = project.work / "媒体补查" / f"L{number:02}-复识.json"
        signature = {"scan_sha256": sha(scan), "requested": ranges or [],
                     "audio_decode_revision": AUDIO_DECODE_REVISION, **recognizer.identity}
        identity = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        history = path.parent / "复识历史" / f"L{number:02}" / f"{identity}.json"
        if history.is_file() and read(history).get("complete"):
            result = read(history)
            put(path, result)
            reports.append(result)
            continue
        cues = read(scan)["audio"]["uncovered_candidates"]
        duration = item["duration_s"]
        meta = read(project.ledger / f"{number:02}.json")
        audio_file = meta.get("audio_source", item["source"])
        sound = (media_source(project.raw, project.work, meta) if audio_file == item["source"]
                 else original_media_path(project.raw, project.work, audio_file, meta.get("audio_embedded")))
        groups = []
        if ranges:
            groups = [{"start": a, "end": b} for a, b in (valid_span(row, duration) for row in ranges)]
        else:
            for candidate in cues:
                left = max(0, candidate["start"] - .45)
                right = min(duration, candidate["end"] + .45)
                if groups and left <= groups[-1]["end"] and right - groups[-1]["start"] < 25:
                    groups[-1]["end"] = max(right, groups[-1]["end"])
                else:
                    groups.append({"start": left, "end": right})
        result = {"lesson": number, "signature": signature, "windows": groups, "complete": False}
        if history.is_file():
            for group, previous in zip(groups, read(history)["windows"]):
                if "segments" in previous:
                    group["segments"] = previous["segments"]
        put(history, result)
        for index in range(0, len(groups), 24):
            batch = [row for row in groups[index:index+24] if "segments" not in row]
            if not batch:
                continue
            windows = [(row["start"], row["end"] - row["start"], row["start"]) for row in batch]
            texts = recognizer.transcribe_windows(sound, duration, meta["audio_track"], meta["language"], windows)
            for group, segments in zip(batch, texts):
                group["segments"] = segments
            put(history, result)
        result["complete"] = True
        put(history, result)
        put(path, result)
        reports.append(result)
    put(project.work / "媒体补查" / "语音复识.json", {"lessons": reports})
