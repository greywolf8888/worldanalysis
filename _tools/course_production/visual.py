"""Open time-ordered original video observations and an optional OCR cue track."""
import hashlib
import json
import unicodedata

from frames import get_native_frame
from project import media_source, put, read, sha
from transcript import valid_span


def text_cues(rows, end):
    def key(value):
        return "".join(unicodedata.normalize("NFKC", value).split()).casefold()

    cues = []
    active = None
    for row in rows:
        when, text = row["pts"], row["text"]
        if active and key(active["text"]) != key(text):
            active["end"] = when
            active = None
        if text and active is None:
            active = {"start": when, "end": end, "text": text, "first_image": row["image"]}
            cues.append(active)
    return [row for row in cues if row["end"] > row["start"]]


def observe(project, lesson, start, end, step, crop, ocr):
    import av
    import numpy as np
    from worldanalysis.ocr import PROVIDER_VERSION, ocr_image

    meta = read(project.ledger / f"{lesson:02}.json")
    valid_span([start, end], meta["duration_s"])
    if not .04 <= step <= 30:
        raise ValueError("帧观察步长应在 0.04–30 秒之间")
    source = media_source(project.raw, project.work, meta, verify_original=True)
    plan = {"revision": 2, "source": meta["source"], "source_sha256": meta["sha256"],
            "range_s": [start, end], "step": step, "crop": crop,
            "ocr": PROVIDER_VERSION if ocr else None}
    if recovery := meta.get("recovery"):
        plan["recovery_sha256"] = recovery["file_sha256"]
    job = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:18]
    folder = project.work / "视觉核查" / f"{lesson:02}" / job
    result_file = folder / "observations.json"
    if result_file.is_file():
        cached = read(result_file)
        if cached["plan"] == plan and all(
            (folder / row["image"]).is_file() and sha(folder / row["image"]) == row["sha256"]
            and (not ocr or row.get("ocr_status") == "extracted") for row in cached["frames"]
        ):
            return result_file
    folder.mkdir(parents=True, exist_ok=True)
    rows = []

    def crop_frame(native):
        if crop:
            x, y, right, bottom = crop
            if not (0 <= x < right <= native.width and 0 <= y < bottom <= native.height):
                raise ValueError("观察区域超出视频原图")
            return native.crop(crop)
        return native

    def keep_frame(pts, image, dark):
        out = folder / f"frame-{round(pts * 1_000_000):013}.png"
        image.save(out, format="PNG")
        row = {"pts": pts, "image": out.name, "sha256": sha(out), "dark": bool(dark), "text": ""}
        if ocr:
            result = ocr_image(out, options={"allowed_root": project.work})
            row.update(text=result["text"], boxes=result["boxes"], ocr_status=result["processing_status"],
                       ocr_reason=result["reason"])
        rows.append(row)
    previous = None
    last_probe = float("-inf")
    target = start
    sampled = 0
    origin = meta.get("timeline_origin_s", 0)
    with av.open(str(source), options={"protocol_whitelist": "file,pipe"}) as container:
        stream = container.streams.video[0]
        container.seek(int((start + origin) / stream.time_base), stream=stream, backward=True)
        for frame in container.decode(stream):
            if frame.time is None:
                continue
            t = frame.time - origin
            if t >= end:
                break
            if t < target - 1e-6:
                continue
            sampled += 1
            while target <= t + 1e-6:
                target += step
            native = crop_frame(frame.to_image())
            gray = np.asarray(native.convert("L").resize((192, 108)), dtype=np.int16)
            changed = previous is None or (np.abs(gray - previous) > 18).mean() >= .002
            if not ocr or changed or t - last_probe >= max(3 * step, 4):
                keep_frame(round(t, 6), native, gray.mean() < 35)
                last_probe = t
                previous = gray
    if not rows:
        native, pts = get_native_frame(source, start, origin)
        if not start - 1e-6 <= pts < end:
            raise ValueError("此观察窗没有可定位的原帧")
        native = crop_frame(native)
        keep_frame(pts, native, np.asarray(native.convert("L")).mean() < 35)
    put(result_file, {"plan": plan, "sampled_frames": sampled, "frames": rows,
                      "text_cues": text_cues(rows, end) if ocr else [],
                      "ocr_unreadable_frames": [row["pts"] for row in rows
                                                if ocr and row.get("ocr_status") != "extracted"],
                      "requires_visual_review": True})
    return result_file
