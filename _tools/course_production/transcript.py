"""Versioned course transcript and continuous teaching-source reads."""
from difflib import SequenceMatcher
from math import isfinite
import re
import unicodedata

from project import local_child, put, read, sha, write


def normalize(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def compare_cues(original, recognized, duration, window=30):
    """Find disagreements inside covered time as well as intervals with only one source."""
    differences = []
    for bucket in range(0, int(duration) + 1, window):
        if bucket >= duration:
            break
        end = min(duration, bucket + window)
        left, right = (span_text(cues, bucket, end) for cues in (original, recognized))
        a, b = normalize(left), normalize(right)
        sensitive = r"\d+(?:\.\d+)?|不[要能可用会是]|[未无]开|开启|关闭|点击|勾选|退货率|毛利率"
        key_terms_differ = set(re.findall(sensitive, left)) != set(re.findall(sensitive, right))
        if a == b and not key_terms_differ:
            continue
        ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
        differences.append({"start": bucket, "end": end, "original": left, "recognized": right,
                            "kind": "both" if a and b else "one_source", "similarity": round(ratio, 3),
                            "critical_words_differ": key_terms_differ})
    return differences


def span_text(cues, start, end):
    return "\n".join(cue["text"] for cue in cues if cue["start"] < end and cue["end"] > start)


def stamp(seconds):
    hours, rest = divmod(round(seconds * 100), 360000)
    minutes, hundredths = divmod(rest, 6000)
    return f"{hours:02}:{minutes:02}:{hundredths / 100:05.2f}"


def valid_span(span, duration):
    if (not isinstance(span, list) or len(span) != 2 or
            any(type(s) not in (int, float) or not isfinite(s) for s in span) or
            not 0 <= span[0] < span[1] <= duration + .001):
        raise ValueError(f"无效的原课区间 {span}")
    return span


def validate_proof(work, proof):
    file = local_child(work, proof["path"])
    if not file.is_file() or sha(file) != proof["sha256"]:
        raise ValueError(f"采用依据字节已变动 {proof['path']}")


def timeline_paths(project, lesson):
    directory = project.work / "采用转写"
    return directory / f"{lesson:02}.json", directory / f"{lesson:02}-采用决定.json"


def update_timeline(project, lesson, reviewed=None):
    path, decisions = timeline_paths(project, lesson)
    meta_file = project.ledger / f"{lesson:02}.json"
    meta = read(meta_file)
    digest = sha(meta_file)
    cues = [{**row, "origin": meta["extraction"]} for row in meta["cues"]]
    duration = meta["duration_s"]
    spec = reviewed if reviewed is not None else read(decisions) if decisions.is_file() else None
    if reviewed is None and spec is not None and not spec["windows"]:
        spec = None
    if spec is not None:
        if spec.get("metadata_sha256") != digest:
            raise ValueError(f"第 {lesson} 讲的采用决定已过期，请保留旧文件并对照新底稿")
        last_end = 0.
        for row in spec["windows"]:
            start, end = valid_span(row["range_s"], duration)
            if start < last_end or not row["reason"].strip() or not row["proofs"]:
                raise ValueError("采用区间须有序、互不重叠且具有实际取证说明")
            for proof in row["proofs"]:
                validate_proof(project.work, proof)
            overlap = [cue for cue in cues if cue["start"] < end and cue["end"] > start]
            if any(cue["start"] < start or cue["end"] > end for cue in overlap):
                raise ValueError("改动不可从中间切断底稿的一句。请扩大到完整字幕边界。")
            revised = []
            previous = start
            for cue in row["cues"]:
                left, right = valid_span([cue["start"], cue["end"]], duration)
                if not previous <= left < right <= end or not cue["text"].strip():
                    raise ValueError("采用后的字幕须在确认区间内有序且非空")
                revised.append({**cue, "origin": "reviewed", "reason": row["reason"],
                                "proofs": row["proofs"]})
                previous = right
            cues = sorted([cue for cue in cues if cue not in overlap] + revised,
                          key=lambda row: (row["start"], row["end"]))
            last_end = end
    differences = compare_cues(meta.get("original_cues", []), meta.get("asr_cues", []), duration
                               ) if meta.get("original_cues") and meta.get("audio_processed") else []
    result = {
        "lesson": lesson, "source": meta["source"], "source_sha256": meta["sha256"],
        "duration_s": duration, "metadata_sha256": digest,
        "audio_scope": meta.get("audio_scope", []), "cues": cues, "differences": differences,
        "confirmed_windows": [] if spec is None else [row["range_s"] for row in spec["windows"]],
    }
    if recovery := meta.get("recovery"):
        result["recovery"] = recovery
    if reviewed is not None:
        put(decisions, reviewed)
    put(path, result)
    write(path.with_suffix(".md"), f"# 第 {lesson} 讲采用稿\n\n" + "\n\n".join(
        f"[{stamp(cue['start'])}–{stamp(cue['end'])}] {cue['text']}" for cue in cues
    ) + "\n")
    return result


def reading_packet(project, lesson, start, end=None, observations=()):
    material = update_timeline(project, lesson)
    duration = material["duration_s"]
    end = min(duration, start + 480) if end is None else end
    valid_span([start, end], duration)
    path, _ = timeline_paths(project, lesson)
    folder = project.work / "连续阅读" / f"{lesson:02}"
    uid = f"{round(start * 1000):09}-{round(end * 1000):09}"
    rows = [row for row in material["cues"] if row["start"] < end and row["end"] > start]
    scan_path = project.work / "媒体补查" / f"L{lesson:02}.json"
    scene_probes = read(scan_path)["picture"]["scene_probes"] if scan_path.is_file() else []
    visual = []
    for name in observations:
        observation = local_child(project.work, name)
        record = read(observation)
        plan = record["plan"]
        if (plan["source_sha256"] != material["source_sha256"] or plan["source"] != material["source"]):
            raise ValueError(f"视觉记录与本集原件不一致 {name}")
        if plan.get("recovery_sha256") != material.get("recovery", {}).get("file_sha256"):
            raise ValueError(f"视觉记录与本集采用的恢复副本不一致 {name}")
        visual.append({"path": name, "sha256": sha(observation), "range_s": plan["range_s"],
                       "text_cues": [row for row in record["text_cues"]
                                     if row["start"] < end and row["end"] > start],
                       "ocr_unreadable_frames": record.get("ocr_unreadable_frames", [])})
    packet = {"lesson": lesson, "range_s": [start, end], "duration_s": duration,
              "source_sha256": material["source_sha256"], "transcript_sha256": sha(path),
              "diffs": [row for row in material["differences"] if row["start"] < end and row["end"] > start],
              "visual_observations": visual,
              "cues": rows, "scene_probes": [row for row in scene_probes
                                             if start <= row.get("pts", -1) < end]}
    put(folder / f"{uid}.json", packet)
    return packet
