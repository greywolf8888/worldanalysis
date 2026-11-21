"""Extract a course's audio once into short timestamped cues. Preserve the raw package."""
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys

from project import ROOT, VIDEO_EXTENSIONS, original_media_path, put, read, recovery_files, sha, write

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from _tools.course_no_subtitles.speech import MODEL, MODEL_SHA, captions, load_model, read_media, transcribe


def stamp(s):
    return f"{int(s)//3600:02}:{int(s)//60%60:02}:{s%60:05.2f}"


def natural(name):
    return tuple((0,int(s)) if s.isdigit() else (1,s.lower()) for s in re.split(r"(\d+)",name.strip()))


def classify(sources, embedded=()):
    videos = sorted((row for row in sources if row["extension"] in VIDEO_EXTENSIONS),
                    key=lambda row: (natural(row["file"]), row["file"]))
    videos.extend(sorted(embedded, key=lambda row: (
        natural(row["embedded"]["document"]), not row["embedded"]["pages"],
        row["embedded"]["pages"], row["embedded"]["member"],
    )))
    if len({row["file"] for row in videos}) != len(videos):
        raise ValueError("原视频与附件内嵌视频的路径标识发生重复")
    for number, row in enumerate(videos, 1):
        row["lesson"] = number
    return videos


def subtitle_choice(video, options, documents):
    allowed = {row["file"]: row for row in documents
               if Path(row["file"]).suffix.lower() in {".srt", ".vtt"} and row.get("text_format") != "md"}
    if "subtitle" in options:
        requested = options["subtitle"]
        if requested is not None and requested not in allowed:
            raise ValueError(f"原字幕路径未登记为带时码的字幕 {requested}")
        return allowed.get(requested), []
    stem = Path(video["file"]).with_suffix("").as_posix().casefold()
    candidates = []
    for path, record in allowed.items():
        base = Path(path).with_suffix("").as_posix().casefold()
        if base == stem or (base.startswith(stem + ".") and re.fullmatch(
            r"zh(?:-hans|-hant|-cn|-tw)?|en(?:-us|-gb)?|ja|ko|yue", base[len(stem) + 1:]
        )):
            candidates.append(record)
    return (candidates[0] if len(candidates) == 1 else None), [item["file"] for item in candidates]


def document_cues(result, duration):
    if any(gap["kind"] in {"parse_failed", "temporal_gap"} for gap in result["gaps"]):
        raise ValueError("字幕包含无法读取的时间段")
    cues = []
    for block in result["blocks"]:
        pos = block["locator"]
        text = block.get("text", "").strip()
        if pos.get("kind") != "time" or not text:
            continue
        start, end = pos["start_ms"] / 1000, pos["end_ms"] / 1000
        if not 0 <= start < end <= duration + .25:
            raise ValueError("字幕时间超出原片")
        if end > duration:
            end = duration
        if start < end:
            cues.append({"start": start, "end": end, "text": text})
    if not cues:
        raise ValueError("未找到可读的字幕时间段")
    return sorted(cues, key=lambda cue: (cue["start"], cue["end"]))


def embedded_cues(source, output, duration):
    from worldanalysis.documents import extract

    output.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(source),
                           "-map", "0:s:0", "-c:s", "srt", str(output)],
                          capture_output=True, timeout=120)
    if proc.returncode:
        raise ValueError("内嵌字幕无法转换为文字")
    return document_cues(extract(output, output.parent / f"{output.stem}-内容"), duration)


def source_time(value, fallback=0.0):
    return float(value) if value not in (None, "N/A") else fallback


def probe_video(source):
    try:
        meta = read_media(source)
    except subprocess.CalledProcessError as error:
        reason = (error.stderr or b"").decode("utf-8", errors="replace").strip()
        raise ValueError(reason.splitlines()[0] if reason else "原视频探测失败") from error
    duration = source_time(meta["format"].get("duration"))
    if not math.isfinite(duration) or duration <= 0 or not any(
        stream["codec_type"] == "video" for stream in meta["streams"]
    ):
        raise ValueError("没有可定位的原视频时间轴")
    return meta, duration


def probe_course_video(source, recovered):
    try:
        meta, duration = probe_video(source)
    except ValueError:
        if recovered is None:
            raise
        meta, duration = probe_video(recovered)
        return recovered, meta, duration
    if recovered is not None:
        raise ValueError("原件已可探测，请核对后移除恢复设置")
    return source, meta, duration


def prepare_project_media(project, videos, documents, gpu=None):
    settings_file = project.work / "媒体设置.json"
    settings = read(settings_file) if settings_file.is_file() else {}
    if not isinstance(settings, dict) or set(settings) - {str(row["lesson"]) for row in videos}:
        raise ValueError("媒体设置须用本课程的讲号作为键")
    results = []
    unreadable = []
    verified_cpu = False
    sources = {row["file"]: row for row in videos}
    for item in videos:
        number = item["lesson"]
        source = original_media_path(project.raw, project.work, item["file"], item.get("embedded"))
        destination = project.ledger / f"{number:02}.json"
        prefs = settings.get(str(number), {})
        if not isinstance(prefs, dict) or set(prefs) - {"language", "audio_track", "subtitle", "voiced_reference", "recovery"}:
            raise ValueError(f"第 {number} 讲的媒体设置含未知字段")
        language = prefs.get("language", "auto")
        if language not in {"auto", "zh", "en", "ja", "ko", "yue"}:
            raise ValueError(f"第 {number} 讲语言应为 auto、zh、en、ja、ko 或 yue")
        subtitle, candidates = subtitle_choice(item, prefs, documents)
        signature = {"version": 2, "raw_sha256": item["sha256"], "file": item["file"],
                     "settings": prefs, "subtitle_sha256": subtitle["sha256"] if subtitle else None,
                     "asr": gpu.identity if gpu else MODEL_SHA}
        if "embedded" in item:
            signature["embedded"] = item["embedded"]
        recovery = recovered = None
        try:
            if prefs.get("recovery") is not None:
                recovered, manifest, spec = recovery_files(project.work, prefs["recovery"], item["sha256"])
                recovery = {"manifest": prefs["recovery"], "manifest_sha256": sha(manifest),
                            "file_sha256": spec["sha256"], "scope": spec["scope"]}
                signature["recovery"] = recovery
            old = read(destination) if destination.is_file() else {}
            if old.get("prepare_signature") == signature and (project.work / f"{number:02} 转写.md").is_file():
                results.append(old)
                print(f"第 {number} 讲 · 复用时间轴", flush=True)
                continue
            source, meta, duration = probe_course_video(source, recovered)
        except ValueError as error:
            unreadable.append({"lesson": number, "source": item["file"],
                               "sha256": item["sha256"], "reason": str(error)})
            print(f"第 {number} 讲 · 原件待补查 · {item['file']}", flush=True)
            continue
        origin = source_time(meta["format"].get("start_time"))
        sound = source
        audio_meta = meta
        audio_origin = origin
        alignment = None
        reference = prefs.get("voiced_reference")
        if reference is not None:
            from review_media import align_video

            if reference not in sources or reference == item["file"]:
                raise ValueError(f"第 {number} 讲的补录须指向包内另一原视频")
            if any(stream["codec_type"] == "audio" for stream in meta["streams"]):
                raise ValueError(f"第 {number} 讲已有正式音轨，保留原声")
            sound = original_media_path(project.raw, project.work, reference, sources[reference].get("embedded"))
            audio_meta = read_media(sound)
            audio_origin = source_time(audio_meta["format"].get("start_time"))
            if abs(source_time(audio_meta["format"].get("duration")) - duration) > .5 or not any(
                stream["codec_type"] == "audio" for stream in audio_meta["streams"]
            ):
                raise ValueError(f"第 {number} 讲的有声件缺音轨或时长相差超过半秒")
            alignment = align_video(source, sound, duration, (origin, audio_origin))
        audio = [stream for stream in audio_meta["streams"] if stream["codec_type"] == "audio"]
        track = prefs.get("audio_track", 0)
        if type(track) is not int or not 0 <= track < max(len(audio), 1):
            raise ValueError(f"第 {number} 讲音轨不存在 {track}")
        offset = max(source_time(audio[track].get("start_time"), audio_origin) - audio_origin, 0) if audio else 0.0
        rows = []
        cues = []
        subtitle_kind = None
        notes = []
        method = "no_audio"
        if len(candidates) > 1 and not subtitle:
            notes.append("同名字幕有多个候选，核对后可在媒体设置指定")
        try:
            if subtitle:
                if not subtitle["result"]:
                    raise ValueError("配套字幕无法提取")
                cues = document_cues(read(project.work / subtitle["result"]), duration)
                subtitle_kind = "original_subtitle"
            elif "subtitle" not in prefs and sum(s["codec_type"] == "subtitle" for s in meta["streams"]) == 1:
                cues = embedded_cues(source, project.work / "内嵌字幕" / f"{number:02}.srt", duration)
                subtitle_kind = "embedded_subtitle"
        except ValueError as error:
            if "subtitle" in prefs:
                raise ValueError(f"第 {number} 讲指定字幕不可用 {error}") from error
            notes.append(str(error))

        original_cues = cues
        audio_scope = []
        if audio:
            if gpu:
                identity = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
                cache = project.work / "音轨取材" / f"{number:02}" / identity
                rows = gpu.transcribe(sound, duration, track, language, offset, cache=cache)
                audio_scope = gpu.decoded_windows
                cues = [{"start": row["start"], "end": row["end"], "text": row["text"]}
                        for row in rows if row["text"]]
                method = gpu.backend + "_vulkan"
            else:
                if not verified_cpu:
                    if sha(MODEL / "model.int8.onnx") != MODEL_SHA:
                        raise ValueError("本地 SenseVoiceSmall INT8 模型哈希不符")
                    verified_cpu = True
                load_model(language)
                rows = transcribe(sound, 0, None, "pause", 1, None, audio_track=track)
                for row in rows:
                    row["start"] += offset
                    row["end"] += offset
                    row["times"] = [t + offset for t in row["times"]]
                cues = captions(rows)
                audio_scope = [{"timeline_start": row["start"], "samples": round((row["end"] - row["start"]) * 16000),
                                "sample_rate": 16000} for row in rows]
                method = "sensevoice_cpu"
        asr_cues = cues if audio else []
        if original_cues and not asr_cues:
            cues, method = original_cues, subtitle_kind
        record = {"lesson": number, "source": item["file"], "sha256": item["sha256"],
                  "model_sha256": (gpu.identity["model_sha256"] if gpu else MODEL_SHA) if audio else None,
                  "duration_s": duration, "media": meta, "timeline_origin_s": origin, "audio_track": track,
                  "audio_offset_s": offset, "language": language, "extraction": method,
                  "audio_count": len(audio), "audio_source": reference or item["file"],
                  "audio_source_sha256": sources[reference]["sha256"] if reference else None,
                  "audio_alignment": alignment,
                  "subtitle": subtitle["file"] if subtitle and subtitle_kind == "original_subtitle" else None,
                  "subtitle_kind": subtitle_kind, "original_cues": original_cues, "asr_cues": asr_cues,
                  "audio_processed": bool(audio), "audio_scope": audio_scope,
                  "subtitle_candidates": candidates, "notes": notes, "prepare_signature": signature,
                  "cues": cues, "speech_windows": rows}
        if recovery:
            record["recovery"] = recovery
        if "embedded" in item:
            record["embedded"] = item["embedded"]
        if reference and "embedded" in sources[reference]:
            record["audio_embedded"] = sources[reference]["embedded"]
        text = [f"# {number:02} {Path(item['file']).stem}"]
        if recovery:
            text.append(recovery["scope"])
        text.extend(f"[{stamp(cue['start'])}–{stamp(cue['end'])}] {cue['text']}" for cue in cues)
        write(project.work / f"{number:02} 转写.md", "\n\n".join(text) + "\n")
        put(destination, record)
        results.append(record)
        print(f"第 {number} 讲 · {method} · {len(cues)} 段", flush=True)
    summary = {
        "videos": [{k: row[k] for k in ("lesson", "source", "duration_s", "sha256", "recovery", "embedded") if k in row}
                   for row in results],
        "duration_s": sum(row["duration_s"] for row in results),
        "cue_total": sum(len(row["cues"]) for row in results),
    }
    if unreadable:
        summary["unreadable"] = unreadable
    put(project.ledger / "media-summary.json", summary)
    return summary
