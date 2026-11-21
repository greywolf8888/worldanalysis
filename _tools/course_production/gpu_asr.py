"""Optional local GPU transcription and independent candidate review."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import wave

from project import local_child, media_source, original_media_path, put, read, sha
from _tools.course_no_subtitles.speech import RUNTIME


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
                         "model_sha256": sha(self.model)}

    def transcribe(self, source, duration, track, language, offset=0, *, cache=None):
        if cache is None:
            return self.transcribe_windows(source, duration, track, language, [(None, None, offset)])[0]
        # Multiples of the recognizer's 30-second windows keep the current chunk grid.
        rows, decoded = [], []
        for left in range(0, int(duration) + 1, 600):
            if left >= duration:
                break
            right = min(left + 600, duration)
            path = cache / f"{left:06}.json"
            if path.is_file():
                part = read(path)
            else:
                # The first decode begins at the container start. Later seeks use that same clock.
                request = (float(left), right - left, max(left, offset))
                result, = self.transcribe_windows(source, duration, track, language, [request])
                window, = self.decoded_windows
                part = {"cues": result, "window": window}
                put(path, part)
            rows.extend(part["cues"])
            decoded.append(part["window"])
        self.decoded_windows = decoded
        return rows

    def transcribe_windows(self, source, duration, track, language, windows):
        """Decode separate original time windows and load the model once for the batch."""
        scratch = local_child(RUNTIME, "tmp")
        scratch.mkdir(parents=True, exist_ok=True)
        self.decoded_windows = []
        with TemporaryDirectory(prefix="course-asr-", dir=scratch) as folder:
            outputs = []
            outfile_args = []
            for index, (start, length, offset) in enumerate(windows):
                output = Path(folder) / f"transcript-{index:02}"
                wav = output.with_suffix(".wav")
                seek = ["-ss", str(start)] if start is not None else []
                limit = ["-t", str(length)] if length is not None else []
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
                                *seek, "-i", str(source), *limit, "-map", f"0:a:{track}", "-vn", "-ar", "16000",
                                "-ac", "1", "-c:a", "pcm_s16le", str(wav)], check=True)
                with wave.open(str(wav)) as pcm:
                    samples = pcm.getnframes()
                    self.decoded_windows.append({"requested_start": start or 0, "requested_length": length,
                                                 "timeline_start": offset, "samples": samples,
                                                 "sample_rate": pcm.getframerate()})
                if not samples:
                    outputs.append((None, offset))
                    continue
                outfile_args.extend(["-f", wav.name, "-of", output.name])
                outputs.append((output.with_suffix(".json"), offset))
            if not outfile_args:
                return [[] for _ in windows]
            backend = (["--backend", self.backend, "--strict-pipeline", "--chunk-seconds", "30",
                        "--chunk-overlap", "0"] if self.backend in {"sensevoice", "qwen3"} else ["-nf", "-mc", "0"])
            seconds = sum(length if length is not None else duration for _, length, _ in windows)
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
        signature = {"scan_sha256": sha(scan), "requested": ranges or [], **recognizer.identity}
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
