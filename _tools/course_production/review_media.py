"""Scan the original audio and screen timeline of one local course."""
from bisect import bisect_left, bisect_right
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
from itertools import accumulate
import json
import re
import time

from PIL import Image, ImageChops, ImageDraw, ImageStat

from frames import get_native_frame
from prepare import MODEL, MODEL_SHA, captions
from project import media_source, original_media_path, put, read, sha
from _tools.course_no_subtitles import speech
from _tools.course_no_subtitles.speech import load_model
import av
import cv2
import numpy as np


RATE = 16000
VAD_FILE = MODEL / "silero_vad.onnx"
VAD_SHA = "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6"
REVIEW_REV = 5
VISUAL_METHOD = "source PTS sampling, local change, annotations including dark backgrounds"
ACTION_WORDS = re.compile(r"右键|点击|点开|按住|长按|打开|选中|切换|拖动|弹出|放大|缩小|按钮|菜单|勾选|圈出|画框|保存")
GAP_S = .45
PAD_S = .2


def align_video(silent, voiced, duration, origins=(0, 0)):
    evidence = []
    for ratio in (.011,.04,.098,.17,.243,.331,.408,.49,.585,.669,.764,.885,.989):
        expected = round(duration * ratio, 3)
        images = [get_native_frame(path, expected, origin) for path, origin in zip((silent, voiced), origins)]
        (left, lt), (right, rt) = images
        left = left.convert("RGB").resize((256, 144))
        right = right.convert("RGB").resize((256, 144))
        mean = sum(ImageStat.Stat(ImageChops.difference(left, right)).mean) / 3
        if abs(rt - lt) > .1 or mean > 9:
            raise ValueError(f"有声和交付原片时间轴不一致 {expected}: PTS={lt}/{rt}, 画面差={mean:.2f}")
        evidence.append({"at_s": expected, "delivered_pts": lt, "voiced_pts": rt, "mean_pixel_delta": round(mean,3)})
    return evidence


def merged_ranges(items, padding=0):
    result = []
    for l, r in sorted((max(0,a-padding), b+padding) for a,b in items if b>a):
        if result and l <= result[-1][1]:
            result[-1][1] = max(result[-1][1], r)
        else:
            result.append([l,r])
    return result


def missing_ranges(left, right, covered, ends, minimum=GAP_S):
    gaps = []
    for l, r in covered[bisect_right(ends, left):]:
        if l >= right:
            break
        if l - left >= minimum and l > left:
            gaps.append((left, l))
        left = max(left,r)
        if left >= right:
            break
    if right - left >= minimum and right > left:
        gaps.append((left, right))
    return gaps


def speech_detector():
    import sherpa_onnx

    cfg=sherpa_onnx.VadModelConfig()
    cfg.sample_rate=RATE
    cfg.num_threads=1
    silero=cfg.silero_vad
    silero.model=str(VAD_FILE)
    silero.window_size=512
    silero.threshold=.42
    silero.min_speech_duration=.22
    silero.min_silence_duration=.25
    silero.max_speech_duration=12
    if not cfg.validate():
        raise ValueError("VAD 配置不兼容")
    return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)


def speech_scan(source, metadata, offset, gain_db=0):
    v=speech_detector()
    cue_ranges=merged_ranges([(c["start"],c["end"]) for c in metadata["cues"]],PAD_S)
    ends=[x[1] for x in cue_ranges]
    gain=10**(gain_db/20)
    activity=[]
    candidates=[]
    below_threshold=[]
    decoded_end=0.0
    tail=np.empty(0,dtype=np.float32)

    def release():
        while not v.empty():
            piece=v.front
            waveform=np.asarray(piece.samples,dtype=np.float32)
            native=piece.start/RATE
            start=native+offset
            end=min(start+len(waveform)/RATE,decoded_end+offset)
            v.pop()
            if end <= start:
                continue
            activity.append((start,end))
            for left,right in missing_ranges(start,end,cue_ranges,ends,minimum=0):
                if right - left < GAP_S:
                    below_threshold.append({"start": round(left, 3), "end": round(right, 3)})
                    continue
                # Decode only the uncaptioned portion of a speech candidate, with a
                # short context margin for word boundaries. Existing cues stay intact.
                a=max(0,round((left-start-.15)*RATE))
                b=min(len(waveform),round((right-start+.15)*RATE))
                pcm=np.clip(waveform[a:b]*32768,-32768,32767).astype("<i2")
                load_model(metadata.get("language", "zh"))
                line=speech.recognize((start+a/RATE,pcm.tobytes()))
                samples=captions([line])
                candidates.append({
                    "start":round(left,3),"end":round(right,3),
                    "context_start":round(start+a/RATE,3),
                    "context_end":round(start+b/RATE,3),
                    "asr_text":line["text"],
                    "suggested_cues":samples,
                })
    for start,buf in speech.audio_windows(source,0,None,"fixed",metadata.get("audio_track", 0)):
        new=np.frombuffer(buf,dtype="<i2").astype(np.float32)/32768
        if gain_db:
            new=np.clip(new*gain,-1,1)
        decoded_end=start+len(new)/RATE
        chunk=np.concatenate((tail,new))
        length=len(chunk)//512*512
        for i in range(0,length,512):
            v.accept_waveform(chunk[i:i+512])
            release()
        tail=chunk[length:]
    if len(tail):
        v.accept_waveform(np.pad(tail,(0,512-len(tail))))
    v.flush()
    release()
    if not decoded_end:
        raise ValueError(f"有声源解码未产生 PCM {source}")
    speaking=merged_ranges(activity)
    return {"decoded_s":round(decoded_end,3),"offset_s":offset,"gain_db":gain_db,
            "original_cues":len(metadata["cues"]),
            "speech_ranges":len(activity),
            "speech_s":round(sum(b-a for a,b in speaking),2),
            "speech_windows":[[round(a,3),round(b,3)] for a,b in speaking],
            "candidate_min_s":GAP_S,"cue_padding_s":PAD_S,"below_threshold":below_threshold,
            "uncovered_candidates":candidates}


def frame_scan(source, duration, cues, origin=0, speech_windows=None):
    cv2.setNumThreads(1)
    targets = [duration*x for x in (.012,.18,.39,.6,.81,.973)]
    overview = []
    strongest = strongest_quiet = None
    previous = previous_marks = previous_t = first = None
    previous_dark = False
    largest_gap = 0.0
    sample_count = decoded = encoded_keys = dark_count = 0
    events = []
    pending = None
    next_sample = next_anchor = 0.0
    last_change = -1.0
    columns, rows = 10, 8
    cue_starts = [c["start"] for c in cues]
    cue_ends = list(accumulate((c["end"] for c in cues), max))
    action_cues = [bool(ACTION_WORDS.search(c["text"])) for c in cues]
    voice_starts = [left for left, _ in speech_windows or []]
    voice_ends = [right for _, right in speech_windows or []]
    with av.open(str(source), options={"protocol_whitelist": "file,pipe"}) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        stream.codec_context.thread_count = 2
        ratio = stream.width / stream.height
        sample_rate = 6 if ratio < .95 or not cues else 4
        if ratio >= 1:
            width, height = 320, max(32, round(320 / ratio / rows) * rows)
        else:
            width, height = max(40, round(320 * ratio / columns) * columns), 320
        scale = min(208 / width, 118 / height)
        preview_size = (round(width * scale), round(height * scale))
        for frame in container.decode(stream):
            decoded += 1
            encoded_keys += bool(frame.key_frame)
            raw_t = None if frame.time is None else frame.time - origin
            if raw_t is None or raw_t + 1e-6 < next_sample:
                continue
            next_sample = (int((raw_t + 1e-6) * sample_rate) + 1) / sample_rate
            t = round(raw_t, 5)
            sample_count += 1
            rgb = frame.reformat(width=width, height=height, format="rgb24").to_ndarray()
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            inner = gray[height//10:height*8//10, width//10:width*9//10]
            dark = cv2.countNonZero(cv2.inRange(inner, 0, 12)) >= inner.size * .87
            red = rgb[:, :, 0]
            marks = (red >= 130) & (cv2.subtract(red, cv2.max(rgb[:, :, 1], rgb[:, :, 2])) > 48)
            dark_count += dark
            cue = bisect_left(cue_ends, t-.35)
            outside_cues = cue >= len(cue_starts) or cue_starts[cue] > t+.35
            voice = bisect_left(voice_ends, t-.2)
            quiet = outside_cues if speech_windows is None else (
                voice >= len(voice_starts) or voice_starts[voice] > t+.2
            )
            action = not outside_cues and action_cues[cue]
            voice_flags = {"outside_cues":outside_cues, "outside_speech":quiet}
            local = overall = ink = 0.0
            tile = None
            if previous_t is None:
                first = t
            else:
                largest_gap = max(largest_gap, t - previous_t)
                delta = cv2.absdiff(gray, previous)
                cell_shape = (rows, height//rows, columns, width//columns)
                cells = delta.reshape(cell_shape).mean(axis=(1, 3))
                iy, ix = np.unravel_index(cells.argmax(), cells.shape)
                tile = [int(ix), int(iy)]
                local = float(cells[iy, ix])
                overall = float(cv2.mean(delta)[0])
                ink = float(np.logical_xor(marks, previous_marks).reshape(cell_shape).mean(axis=(1, 3)).max())

            if pending is not None:
                pending["after_pts"] = t
                pending = None
            change = local >= (3.0 if action else 5.0) or overall >= 2.4 or ink >= .017
            if change and t - last_change >= .58:
                pending = {"kind":"change", "before_pts":previous_t, "pts":t, "after_pts":t,
                           "change":round(local,3), "whole":round(overall,3), "ink":round(ink,4),
                           "tile":tile, **voice_flags, "near_spoken_action":action,
                           "dark_background":dark and previous_dark}
                events.append(pending)
                last_change = t
            if t >= next_anchor:
                next_anchor = (int(t / 18) + 1) * 18
                if pending is None:
                    pending = {"kind":"interval", "before_pts":previous_t, "pts":t, "after_pts":t,
                               "change":0.0, **voice_flags}
                    events.append(pending)
            need_overview = len(overview) < len(targets) and t >= targets[len(overview)]
            peak = change and (strongest is None or local > strongest[1])
            quiet_peak = change and quiet and (strongest_quiet is None or local > strongest_quiet[1])
            if need_overview or peak or quiet_peak:
                image = Image.fromarray(rgb).resize(preview_size, Image.Resampling.BILINEAR)
                item = (t, local, image)
                while len(overview) < len(targets) and t >= targets[len(overview)]:
                    overview.append(item)
                if peak:
                    strongest = item
                if quiet_peak:
                    strongest_quiet = item
            previous, previous_marks, previous_t, previous_dark = gray, marks, t, dark
    if not sample_count:
        raise ValueError(f"原视频无可解码帧 {source}")
    selected = []
    used = set()
    overview_labels = ["head","early","middle-1","middle-2","late","tail"]
    all_points = [*zip(overview_labels, overview), ("change", strongest), ("quiet-change", strongest_quiet)]
    for label, item in all_points:
        if item is None or item[0] in used:
            continue
        used.add(item[0])
        selected.append((label,*item))
    return {
        "decoded_frames":decoded,"decoded_keyframes":encoded_keys,"sample_rate":sample_rate,
        "sample_count":sample_count,"first_pts":first,"last_pts":previous_t,
        "largest_gap_s":round(largest_gap,3),"mostly_dark_frames":dark_count,
        "tiles":[columns,rows],"scene_probes":events,
        "contact_points":[{"role":label,"pts":t,"change":round(delta,2)} for label,t,delta,pict in selected],
    }, selected


def write_strip(storage,lesson,pictures):
    tile_w,tile_h=216,143
    board=Image.new("RGB",(tile_w*8,tile_h),"#f4f4f2")
    draw=ImageDraw.Draw(board)
    for i,(label,t,delta,pict) in enumerate(pictures):
        x=i*tile_w
        board.paste(pict,(x+2,23))
        t0=int(t)
        draw.text((x+4,5),f"L{lesson:02} {t0//3600:02}:{t0//60%60:02}:{t0%60:02} {label}",fill="#121212")
    board.save(storage/f"L{lesson:02}.jpg",quality=88)


def scan_video(course, ledger, work, lesson):
    name = course.name
    metadata=read(ledger/f"{lesson:02}.json")
    raw = course / "原始资料"
    source = media_source(raw, work, metadata)
    audio_file = metadata.get("audio_source", metadata["source"])
    sound = (source if audio_file == metadata["source"] else
             original_media_path(raw, work, audio_file, metadata.get("audio_embedded")))
    audio_count = metadata["audio_count"]
    sound_sha = metadata.get("audio_source_sha256")
    offset = metadata["audio_offset_s"]
    storage=work/"媒体补查"
    storage.mkdir(parents=True,exist_ok=True)
    output=storage/f"L{lesson:02}.json"
    signature={
        "revision":REVIEW_REV,"raw_sha256":metadata["sha256"],
        "raw_mtime_ns":source.stat().st_mtime_ns,"audio_sha256":sound_sha,
        "audio_mtime_ns":sound.stat().st_mtime_ns,"vad_sha256":VAD_SHA,
        "cues_sha256":hashlib.sha256(json.dumps(metadata["cues"],ensure_ascii=False).encode("utf-8")).hexdigest(),
    }
    signature["prepare"] = metadata["prepare_signature"]
    signature["audio_method"] = {"revision": 3, "min_s": GAP_S, "cue_padding_s": PAD_S} if audio_count else None
    audio_error = metadata.get("audio_error")
    if audio_error:
        signature["audio_error"] = audio_error
    previous = read(output) if output.is_file() else None
    sheet = storage/f"L{lesson:02}.jpg"
    audio_only = metadata.get("kind") == "audio"
    if previous and (audio_only or sheet.is_file()) and previous["signature"] == signature:
        return previous
    started=time.perf_counter()
    same_audio = (previous and previous["signature"].get("audio_error") == audio_error
                  and all(previous["signature"].get(key) == value for key, value in signature.items()
                          if key != "revision"))
    if same_audio:
        sound_info = previous["audio"]
    elif audio_error:
        sound_info = {"decode_error": audio_error, "uncovered_candidates": [], "speech_windows": []}
    else:
        sound_info=speech_scan(sound,metadata,offset) if audio_count else {
            "no_audio":True, "uncovered_candidates":[], "speech_windows":[],
        }
    if audio_only:
        display = {"no_video": True, "sample_count": 0, "scene_probes": []}
    else:
        display,pictures=frame_scan(source,metadata["duration_s"],metadata["cues"],
                                    metadata.get("timeline_origin_s", 0), sound_info.get("speech_windows") if not audio_error else None)
        write_strip(storage,lesson,pictures)
    receipt={"course":name,"lesson":lesson,"source":metadata["source"],
             "duration":metadata["duration_s"],"signature":signature,
             "audio":sound_info,"picture":display,"elapsed_s":round(time.perf_counter()-started,2)}
    put(output,receipt)
    return receipt


def scan_project(project, videos, workers):
    if not videos:
        return []
    has_audio = any(read(project.ledger / f"{row['lesson']:02}.json").get("audio_processed") for row in videos)
    if has_audio and (sha(VAD_FILE) != VAD_SHA or sha(MODEL / "model.int8.onnx") != MODEL_SHA):
        raise ValueError("补查使用的本地模型哈希不符")
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(scan_video, project.folder, project.ledger, project.work, row["lesson"])
                   for row in sorted(videos, key=lambda row: -row["duration_s"])]
        reports = []
        for future in as_completed(futures):
            item = future.result()
            reports.append(item)
            print(f"第 {item['lesson']} 讲 · {item['picture']['sample_count']} 帧抽样 · "
                  f"{len(item['picture']['scene_probes'])} 处视觉候选 · "
                  f"{len(item['audio']['uncovered_candidates'])} 处语音候选", flush=True)
    reports.sort(key=lambda item: item["lesson"])
    put(project.work / "媒体补查" / "扫描汇总.json", {"lessons": reports})
    return reports
