"""Optional local GPU transcription and independent candidate review."""
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from project import ROOT, local_child, put, read, sha


class VulkanASR:
    def __init__(self, executable, model, backend):
        self.executable = Path(executable).resolve(strict=True)
        self.model = Path(model).resolve(strict=True)
        self.backend = backend
        self.identity = {"backend": backend, "runtime_sha256": sha(self.executable),
                         "model_sha256": sha(self.model)}

    def transcribe(self, source, duration, track, language, offset=0):
        return self.transcribe_windows(source, duration, track, language, [(None, None, offset)])[0]

    def transcribe_windows(self, source, duration, track, language, windows):
        """Decode separate original time windows and load the model once for the batch."""
        scratch = local_child(ROOT, ".runtime/tmp")
        scratch.mkdir(parents=True, exist_ok=True)
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
                outfile_args.extend(["-f", wav.name, "-of", output.name])
                outputs.append((output.with_suffix(".json"), offset))
            backend = (["--backend", "sensevoice", "--strict-pipeline", "--chunk-seconds", "30",
                        "--chunk-overlap", "0"] if self.backend == "sensevoice" else ["-nf", "-mc", "0"])
            seconds = sum(length if length is not None else duration for _, length, _ in windows)
            process = subprocess.run([str(self.executable), *backend, "--gpu-backend", "vulkan", "-dev", "0",
                                      "-m", str(self.model), "-l", language, "-t", "4", "-bs", "1", "-bo", "1",
                                      "-oj", *outfile_args], cwd=folder, check=True, capture_output=True,
                                     timeout=max(180, seconds * 2))
            transcripts = []
            for result, offset in outputs:
                if not result.is_file():
                    reason = process.stderr.decode("utf-8", "replace").strip().splitlines()
                    raise ValueError("CrispASR 未写入转写文件。" + (" " + reason[-1] if reason else ""))
                rows = []
                for line in read(result)["transcription"]:
                    left = offset + line["offsets"]["from"] / 1000
                    right = min(duration, offset + line["offsets"]["to"] / 1000)
                    if 0 <= left < right:
                        rows.append({"start": round(left, 3), "end": round(right, 3), "text": line["text"].strip()})
                transcripts.append(rows)
            return transcripts


def refine_candidates(project, recognizer):
    media = read(project.ledger / "media-summary.json")
    reports = []
    for item in media["videos"]:
        number = item["lesson"]
        scan = project.work / "媒体补查" / f"L{number:02}.json"
        path = project.work / "媒体补查" / f"L{number:02}-复识.json"
        signature = {"scan_sha256": sha(scan), **recognizer.identity}
        if path.is_file() and read(path).get("signature") == signature:
            reports.append(read(path))
            continue
        cues = read(scan)["audio"]["uncovered_candidates"]
        duration = item["duration_s"]
        meta = read(project.ledger / f"{number:02}.json")
        sound = local_child(project.raw, meta.get("audio_source", item["source"]))
        groups = []
        for candidate in cues:
            left = max(0, candidate["start"] - .45)
            right = min(duration, candidate["end"] + .45)
            if groups and left <= groups[-1]["end"] and right - groups[-1]["start"] < 25:
                groups[-1]["end"] = max(right, groups[-1]["end"])
            else:
                groups.append({"start": left, "end": right})
        for index in range(0, len(groups), 24):
            batch = groups[index:index+24]
            windows = [(row["start"], row["end"] - row["start"], row["start"]) for row in batch]
            texts = recognizer.transcribe_windows(sound, duration, meta["audio_track"], meta["language"], windows)
            for group, segments in zip(batch, texts):
                group["segments"] = segments
        result = {"lesson": number, "signature": signature, "windows": groups}
        put(path, result)
        reports.append(result)
    put(project.work / "媒体补查" / "语音复识.json", {"lessons": reports})
