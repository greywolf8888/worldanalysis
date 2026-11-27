"""Prepare an existing single-video reading package, offline by default.

Default: native DOM draft + displayed pages in their stable _ingest directory.
--transcript-only: original passive subtitle response, no fabricated DOM layout.
--frames-manifest: actual remote-source frame set in its existing capture_root.
No original JSON/image is rewritten or image re-acquired/copied. Only --publish
opens Store; the importer keeps source, timing and coverage declared/unverified.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys

from PIL import Image

from .contracts import ContractError, check_id, digest, redact_text, reject_links, safe_path, scan_secrets
from . import platform_materials as materials
from .policy import GateError, Policy
from .store import Store

MAX_JSON_BYTES = 8 * 1024**2
_SHA = re.compile(r"[0-9a-f]{64}")
_TIME = re.compile(r"(\d{2,}):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?")


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _integer(value, name, *, minimum=0):
    _require(type(value) is int and value >= minimum, name + " must be a nonnegative integer")
    return value


def _json_bytes(value):
    scan_secrets(value)
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _load(root, supplied):
    path = Path(supplied)
    path = path if path.is_absolute() else root / path
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ContractError("input must be inside the workspace") from exc
    path = safe_path(root, relative)
    data = materials._read(path, MAX_JSON_BYTES)

    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(_):
        raise ContractError("nonfinite JSON value")

    try:
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=nonfinite)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ContractError("invalid evidence JSON") from exc
    _require(isinstance(value, dict), "evidence JSON must be an object")
    scan_secrets(value)
    return path, value, {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _source(value, expected=None):
    _require(isinstance(value, dict), "source declaration is required")
    check_id(value.get("file_id"))
    check_id(value.get("source_revision"))
    Policy().require_cloud_path(value.get("path"))
    if expected is not None:
        for key in ("file_id", "source_revision"):
            _require(value.get(key) == expected.get(key), "source " + key + " mismatch")
        if value.get("account_ref") is not None and expected.get("account_ref") is not None:
            _require(value["account_ref"] == expected["account_ref"], "source account_ref mismatch")
    return value


def _paragraphs(draft):
    _require(draft.get("schema") == "worldanalysis.browser-draft.v1", "unsupported draft schema")
    _require(draft.get("method") == "existing_platform_dom", "draft must be existing_platform_dom")
    count = _integer(draft.get("sentence_count"), "sentence_count", minimum=1)
    _require(count <= 100000, "draft sentence bound exceeded")
    paragraphs = draft.get("paragraphs")
    _require(isinstance(paragraphs, list) and 0 < len(paragraphs) <= count, "invalid draft paragraphs")
    next_sentence, last_start, result = 0, -1, []
    for index, item in enumerate(paragraphs):
        _require(isinstance(item, dict), "invalid draft paragraph")
        text = item.get("text")
        _require(isinstance(text, str) and text.strip(), "paragraph must contain literal text")
        _require(not any(ord(c) < 32 and c not in "\t\n\r" for c in text), "control content in paragraph")
        indices = item.get("sentence_indices")
        _require(isinstance(indices, list) and indices and all(type(i) is int for i in indices), "sentence indices required")
        _require(indices == list(range(next_sentence, next_sentence + len(indices))), "draft sentence indices must be contiguous")
        next_sentence += len(indices)
        start = item.get("start_ms")
        if start is not None:
            _integer(start, "paragraph start_ms")
            _require(start >= last_start, "paragraph start_ms moves backwards")
            last_start = start
        result.append({"paragraph_index": index, "sentence_indices": indices, "start_ms": start,
                       "end_ms": None, "speaker": None, "text": text})
    _require(next_sentence == count, "draft sentence_count mismatch")
    return result


def _timestamp(value):
    _require(isinstance(value, str), "page timestamp must be HH:MM:SS[.mmm]")
    match = _TIME.fullmatch(value)
    _require(match is not None, "page timestamp must be HH:MM:SS[.mmm]")
    hours, minutes, seconds, fraction = match.groups()
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int((fraction or "").ljust(3, "0"))


def _times(metadata, source, count):
    _require(metadata.get("schema") == "worldanalysis.rendered-courseware-observation.v1", "unsupported courseware metadata schema")
    _require(metadata.get("errno") == 0 and metadata.get("http_status") == 200, "courseware metadata was not a successful response")
    _require(metadata.get("source_file_id") == source["file_id"], "courseware metadata source_file_id mismatch")
    if "source_revision" in metadata:
        _require(metadata["source_revision"] == source["source_revision"], "courseware metadata source_revision mismatch")
    Policy().require_cloud_path(metadata.get("source_path"))
    _require(type(metadata.get("page_count")) is int and metadata["page_count"] == count, "courseware metadata page_count mismatch")
    containers = metadata.get("list")
    _require(isinstance(containers, list) and containers and len(containers) == metadata.get("file_count"), "courseware container count mismatch")
    next_page, last_start, last_end, result = 1, -1, -1, []
    for container_index, item in enumerate(containers):
        _require(isinstance(item, dict), "invalid courseware container")
        first = _integer(item.get("start_page"), "start_page", minimum=1)
        last = _integer(item.get("end_page"), "end_page", minimum=1)
        _require(first == next_page and first <= last <= count, "courseware pages must be contiguous")
        starts, ends = item.get("img_timestamp"), item.get("endtime_timestamp")
        _require(isinstance(starts, list) and isinstance(ends, list) and len(starts) == len(ends) == last - first + 1,
                 "courseware timestamp count mismatch")
        for offset, (start_text, end_text) in enumerate(zip(starts, ends)):
            start, end = _timestamp(start_text), _timestamp(end_text)
            _require(start <= end and start >= last_start and end >= last_end, "invalid/backwards page interval")
            result.append({"page": first + offset, "start_ms": start, "end_ms": end,
                           "metadata_container_index": container_index,
                           "img_timestamp": start_text, "endtime_timestamp": end_text})
            last_start, last_end = start, end
        next_page = last + 1
    _require(next_page == count + 1, "courseware page coverage mismatch")
    return result


def _pages(manifest, directory, source):
    _require(manifest.get("schema") == "worldanalysis.rendered-native-pages.v1", "unsupported rendered-page schema")
    pages = manifest.get("pages")
    _require(isinstance(pages, list) and 0 < len(pages) <= materials.MAX_IMAGES, "invalid rendered-page count")
    _require(type(source.get("page_count")) is int and source["page_count"] == len(pages), "draft/rendered page_count mismatch")
    total, paths, result = 0, set(), []
    for ordinal, item in enumerate(pages, 1):
        _require(isinstance(item, dict) and type(item.get("page")) is int and item["page"] == ordinal, "rendered pages must be ordered and contiguous")
        _require(item.get("schema") == "worldanalysis.browser-courseware-page.v1", "unsupported page schema")
        _source(item.get("source"), source)
        _require(type(item["source"].get("page_count")) is int and item["source"]["page_count"] == len(pages), "page source page_count mismatch")
        _require(item.get("representation") == "normally_displayed_platform_page" and item.get("not_native_container_bytes") is True,
                 "page must retain normally-displayed, non-native-container provenance")
        relative = item.get("relative_path")
        path = safe_path(directory, relative)
        _require(path.suffix.lower() in materials.IMAGE_SUFFIXES and relative not in paths, "invalid/duplicate image path")
        paths.add(relative)
        payload = materials._read(path, materials.DEFAULT_LIMITS["max_image_bytes"])
        total += len(payload)
        _require(total <= materials.DEFAULT_LIMITS["max_total_bytes"], "rendered-page byte bound exceeded")
        sha = hashlib.sha256(payload).hexdigest()
        _require(type(item.get("bytes")) is int and item["bytes"] == len(payload) and item.get("sha256") == sha,
                 "rendered-page bytes/sha256 mismatch")
        materials._check_bytes(payload, "image")
        readable, reason = materials._image_readable(payload, relative)
        _require(readable, "rendered image is unreadable: " + reason)
        with Image.open(io.BytesIO(payload)) as image:
            width, height, image_format = image.width, image.height, image.format.lower()
        _require(type(item.get("width")) is int and type(item.get("height")) is int
                 and (width, height) == (item["width"], item["height"]) and image_format == item.get("format"),
                 "rendered-page dimensions/format mismatch")
        _require(isinstance(item.get("pixel_sha256"), str) and _SHA.fullmatch(item["pixel_sha256"]), "invalid observed browser pixel hash")
        result.append({"page": ordinal, "relative_path": relative, "sha256": sha, "bytes": len(payload),
                       "width": width, "height": height, "format": image_format,
                       "browser_pixel_sha256_observed": item["pixel_sha256"],
                       "browser_pixel_equivalence_verified": False,
                       "source_kind": "claimed_video_frame", "video_frame_verified": False})
    return result


def _subtitle(root, supplied, source=None, paragraphs=None):
    path, response, evidence = _load(root, supplied)
    _require(response.get("schema") == "worldanalysis.native-subtitle-response.v1", "unsupported subtitle response schema")
    _require(response.get("http_status") == 200 and response.get("acquisition") == "passive_normal_page_response",
             "subtitle must come from a successful passive normal-page response")
    observed_source = {"file_id": response.get("source_file_id"), "path": response.get("source_path"),
                       "source_revision": response.get("source_revision")}
    _source(observed_source, source)
    Policy().require_cloud_path(response.get("source_path"))
    content = response.get("content")
    _require(isinstance(content, str) and content, "subtitle response content required")
    raw = content.encode("utf-8")
    _require(type(response.get("bytes")) is int and len(raw) == response["bytes"], "subtitle response byte count mismatch")
    materials._check_bytes(raw, "transcript")
    cues, metadata_cues, last_start, last_end = [], [], -1, -1
    groups = list(re.finditer(r"\S[^\r\n]*(?:\r?\n(?![ \t]*\r?\n)[^\r\n]*)*", content))
    for group in groups:
        body = group.group(0)
        timelines = list(materials.documents._TIMELINE.finditer(body))
        _require(len(timelines) == 1, "subtitle group must have one actual timestamp line")
        timeline = timelines[0]
        label = body[:timeline.start()].strip()
        _require(label.isdecimal(), "subtitle numeric source label required")
        start = materials.documents._ms(timeline.groups()[:4])
        end = materials.documents._ms(timeline.groups()[4:8])
        _require(start <= end and start >= last_start and end >= last_end, "invalid/backwards subtitle cue")
        last_start, last_end = start, end
        text = body[timeline.end():]
        text = text[2:] if text.startswith("\r\n") else text[1:] if text.startswith("\n") else text
        cue = {"source_label": label, "start_ms": start, "end_ms": end, "text": text, "speaker": None}
        if label == "0":
            _require(not cues and not metadata_cues and start == end == 0 and text == "此字幕由AI自动生成",
                     "only the original zero-duration AI disclaimer may be a metadata cue")
            metadata_cues.append(cue)
            continue
        _require(int(label) == len(cues) + 1 and text.strip(), "spoken subtitle labels must be contiguous")
        cue["sentence_index"] = len(cues)
        cues.append(cue)
    _require(0 < len(cues) <= 100000, "subtitle speech cue count outside bound")
    if paragraphs is not None:
        _require(len(cues) == sum(len(p["sentence_indices"]) for p in paragraphs), "subtitle/draft sentence count mismatch")
    for paragraph in paragraphs or []:
        sentences = [cues[i] for i in paragraph["sentence_indices"]]
        _require("".join(cue["text"] for cue in sentences) == paragraph["text"], "subtitle/draft literal text mismatch; no automatic rewriting")
        if paragraph["start_ms"] is not None:
            _require(paragraph["start_ms"] == sentences[0]["start_ms"], "subtitle/draft paragraph start mismatch")
        paragraph.update({"subtitle_start_ms": sentences[0]["start_ms"], "end_ms": sentences[-1]["end_ms"],
                          "end_source": "last_explicit_subtitle_cue", "sentences": sentences})
    return raw, response, evidence, metadata_cues, cues


def _clock(milliseconds):
    if milliseconds is None:
        return "未知"
    seconds, fraction = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{fraction:03d}"


def _existing_output(path, data):
    reject_links(path)
    if path.exists():
        _require(materials._read(path, max(MAX_JSON_BYTES, len(data))) == data,
                 "existing prepared output differs; preserve edits/evidence rather than overwrite: " + path.name)
        return True
    return False


def _create(path, data):
    if _existing_output(path, data):
        return False
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        _existing_output(path, data)
        return False
    return True


def prepare_delivery(root, project_id, draft, pages_manifest, courseware_metadata, *, title=None,
                     source_path=None, source_path_observed_at=None, subtitle_response=None):
    """Validate local evidence and create immutable reading files; never open Store.

    A different cloud location creates only a small new manifest/observation. The
    literal transcript and original path evidence remain byte-for-byte unchanged.
    Modified generated files are refused, never silently overwritten.
    """
    root = Path(os.path.abspath(root))
    reject_links(root)
    _require(root.is_dir(), "existing workspace required")
    project_id = check_id(project_id)
    draft_path, draft_value, draft_evidence = _load(root, draft)
    pages_path, pages_value, pages_evidence = _load(root, pages_manifest)
    metadata_path, metadata_value, metadata_evidence = _load(root, courseware_metadata)
    source = _source(draft_value.get("source"))
    directory = pages_path.parent
    directory_rel = directory.relative_to(root)
    _require(len(directory_rel.parts) >= 3 and directory_rel.parts[0] == "_ingest"
             and directory.name == source["file_id"] and directory.parent.name == project_id,
             "output must stay in the existing _ingest stable project/source ID directory")
    if source_path_observed_at is not None:
        _require(source_path is not None, "source_path_observed_at requires an explicit source_path")
        try:
            observed = datetime.fromisoformat(source_path_observed_at.replace("Z", "+00:00"))
            _require(observed.utcoffset() is not None, "source path observation needs an explicit time zone")
        except (TypeError, ValueError) as exc:
            raise ContractError("invalid source path observed_at") from exc
    title = title if title is not None else PurePosixPath(source["path"]).stem
    paragraphs = _paragraphs(draft_value)
    raw_subtitle, subtitle_value, subtitle_evidence, metadata_cues, cues = None, None, None, [], []
    if subtitle_response is not None:
        raw_subtitle, subtitle_value, subtitle_evidence, metadata_cues, cues = _subtitle(root, subtitle_response, source, paragraphs)
    observed_path = subtitle_value["source_path"] if subtitle_value is not None else source["path"]
    current_path = Policy().require_cloud_path(source_path if source_path is not None else observed_path)
    pages = _pages(pages_value, directory, source)
    intervals = _times(metadata_value, source, len(pages))
    for page, interval in zip(pages, intervals):
        page.update(interval)
        page["paragraph_starts_in_interval"] = [p["paragraph_index"] for p in paragraphs
                                                if p["start_ms"] is not None and page["start_ms"] <= p["start_ms"] <= page["end_ms"]]
    for paragraph in paragraphs:
        paragraph["pages_covering_start"] = [p["page"] for p in pages
                                              if paragraph["start_ms"] is not None and p["start_ms"] <= paragraph["start_ms"] <= p["end_ms"]]
    evidence = {"draft": draft_evidence, "rendered_pages": pages_evidence, "courseware_metadata": metadata_evidence}
    if subtitle_evidence is not None:
        evidence["subtitle_response"] = subtitle_evidence
    for cue in cues:
        cue["pages_overlapping_interval"] = [p["page"] for p in pages if p["start_ms"] <= cue["end_ms"] and cue["start_ms"] <= p["end_ms"]]
    associations = {"schema": "worldanalysis.course-page-times.v1", "source_file_id": source["file_id"],
                    "source_revision": source["source_revision"], "evidence": evidence,
                    "association_rule": "inclusive page interval contains known paragraph start; no inferred paragraph/sentence end",
                    "sentence_times": "native_subtitle_response" if raw_subtitle is not None else draft_value.get("sentence_times", "unknown"),
                    "paragraph_end_ms": "last_explicit_subtitle_cue" if raw_subtitle is not None else "unknown", "remote_timing_verified": False,
                    "metadata_cues_retained_in_raw_srt": metadata_cues,
                    "paragraphs": paragraphs, "pages": pages}
    transcript = ("\n\n".join(p["text"] for p in paragraphs) + "\n").encode("utf-8")
    metadata = {"schema": "worldanalysis.course-delivery-preparation.v1", "project_id": project_id,
                "source_file_id": source["file_id"], "source_revision": source["source_revision"], "evidence": evidence,
                "original_source_observations": {"draft": source, "rendered_pages": [p["source"] for p in pages_value["pages"]],
                                                 "courseware_metadata": {"file_id": metadata_value["source_file_id"],
                                                                         "path": metadata_value["source_path"],
                                                                         "observed_at": metadata_value.get("observed_at")}},
                "transcript": {"path": "transcript.txt", "sha256": hashlib.sha256(transcript).hexdigest(), "bytes": len(transcript),
                               "origin": "asr", "source_method": "existing_platform_dom", "literal_paragraph_text": True,
                               "sentence_count": draft_value["sentence_count"], "paragraph_count": len(paragraphs),
                               "extraction_scope": draft_value.get("extraction_scope", "unknown"),
                               "full_media_coverage": draft_value.get("full_media_coverage", "unknown"),
                               "sentence_times": draft_value.get("sentence_times", "unknown"),
                               "paragraph_start_ms": [p["start_ms"] for p in paragraphs],
                               "paragraph_end_ms": [p["end_ms"] for p in paragraphs] if raw_subtitle is not None else "unknown"},
                "images": {"count": len(pages), "bytes": sum(p["bytes"] for p in pages),
                           "local_hash_dimensions_and_decode_validated": True, "native_container_bytes": False,
                           "browser_pixel_equivalence_verified": False, "video_frame_verified": False},
                "quality": "not_assessed", "remote_identity_verified": False, "full_media_coverage_verified": False,
                "srt_generated": False, "raw_srt_retained": raw_subtitle is not None,
                "store_plain_transcript_has_no_cue_times": raw_subtitle is None,
                "sidecars_are_not_platform_ai_notes": True}
    if subtitle_value is not None:
        metadata["original_source_observations"]["subtitle_response"] = {key: subtitle_value.get(key) for key in ("source_file_id", "source_revision", "source_path", "observed_at", "acquisition", "transport")}
        metadata["subtitle"] = {"path": "native-subtitle.raw.srt", "bytes": len(raw_subtitle), "sha256": hashlib.sha256(raw_subtitle).hexdigest(),
                                "spoken_sentence_count": len(cues), "total_cue_count": len(cues) + len(metadata_cues),
                                "metadata_cues": metadata_cues, "normalization_performed": False,
                                "text_and_paragraph_starts_match_draft": True, "timing_source": "passive_normal_page_response"}
    lines = [title, "", "网盘 AI 自动转写，未逐字校对。以下正文逐段原样保留，不改写原话。",
             "此为当前 DOM 文稿与已保存页面的可读视图，不代表逐字准确、全片覆盖、质量充分或课程完成。",
             "来源 file_id：" + source["file_id"], "来源 revision：" + source["source_revision"],
             "原始取证路径（保留历史；不是当前位置）：" + source["path"],
             "来源方法：existing_platform_dom；origin：asr。",
             f"当前 DOM 连续句：{draft_value['sentence_count']}；段落：{len(paragraphs)}；页面：{len(pages)}。",
             ("逐句开始/结束来自正常页面原始 SRT 响应，严格匹配 DOM 原文与段首。说话人未知；未独立对照音视频。原样保留零时长 cue 0 元提示，不计入口述句数。"
              if raw_subtitle is not None else "逐句开始/结束时间未可靠取得；段落结束与说话人未知。不以相邻段落/页时间补造 end，不生成 SRT/VTT。"),
             "图片为平台正常显示页面，不是原生容器字节；claimed_video_frame 仅声明，未独立验证为原视频帧。",
             "页时间来自原始 metadata 的 img_timestamp/endtime_timestamp，不是句子/段落结束时间。",
             "当前位置另存 source-location 观察，路径移动不改原文或历史来源。", "", "来源证据（工作区相对路径 + SHA-256）"]
    for key, value in evidence.items():
        lines.append(f"{key}：{value['path']}；{value['sha256']}")
    lines += ["", "原样正文与已知段落起点"]
    for paragraph in paragraphs:
        indices = paragraph["sentence_indices"]
        lines += ["", f"[段落 {paragraph['paragraph_index'] + 1}；起点 {_clock(paragraph['start_ms'])}；"
                  f"start_ms={paragraph['start_ms'] if paragraph['start_ms'] is not None else '未知'}；句索引 {indices[0]}–{indices[-1]}；"
                  f"结束 {_clock(paragraph['end_ms'])}（{paragraph.get('end_source', '未知')}）；说话人未知]",
                  paragraph["text"]]
    if raw_subtitle is not None:
        lines += ["", "逐句时间与原文（原始 SRT 口述 cues；cue 0 元提示另见 raw 文件）"]
        for cue in cues:
            lines += [f"[句 {cue['source_label']}；{_clock(cue['start_ms'])}–{_clock(cue['end_ms'])}；说话人未知]", cue["text"]]
    lines += ["", "页面索引与时间（仅保留实际观察值）"]
    index = ["# " + title + " · 本地课件页面", "", "真实本地链接；非原生容器字节。未验证原视频帧身份、逐句结束或全片画面覆盖。", "",
             "页时间来源：" + metadata_evidence["path"], "", "| 页 | 起点 | 终点 | 本地图像 | 命中的段落起点（1起） |", "|---|---|---|---|---|"]
    for page in pages:
        hits = ", ".join(str(i + 1) for i in page["paragraph_starts_in_interval"]) or "无（不推定）"
        label = f"{_clock(page['start_ms'])}–{_clock(page['end_ms'])}"
        lines.append(f"页 {page['page']:03d}：{label}；{page['relative_path']}；命中的段落起点：{hits}")
        index.append(f"| {page['page']} | {_clock(page['start_ms'])} | {_clock(page['end_ms'])} | [图片](<{page['relative_path']}>) | {hits} |")
    index += ["", "只按已知段落起点落入页区间关联；无匹配不等于无画面。浏览器原像素哈希另存，仅校验本地文件哈希、解码及尺寸，不自动宣称像素/远端同一性。", ""]
    declared_source = {"provider": "baidu", "file_id": source["file_id"], "source_revision": source["source_revision"], "path": current_path}
    for field in ("account_ref", "duration_ms", "size_bytes"):
        if field in source:
            declared_source[field] = source[field]
    manifest = {"schema_version": "1.0", "material_id": "native-" + source["file_id"], "title": title, "source": declared_source,
                "transcript": {"path": "transcript.txt", "kind": "plain_transcript", "origin": "asr", "source_method": "existing_platform_dom"},
                "images": [{"path": p["relative_path"], "source_kind": "claimed_video_frame", "timestamp_ms": p["start_ms"]} for p in pages]}
    if raw_subtitle is not None:
        manifest["transcript"].update({"path": "native-subtitle.raw.srt", "kind": "timed_subtitle"})
    # Never import local prose as a provider_ai_note or fabricate subtitle cues.
    materials._validate_manifest(manifest, Policy())
    manifest_data = _json_bytes(manifest)
    _require(len(manifest_data) <= materials.MAX_MANIFEST_BYTES, "material manifest bound exceeded")
    import_text = raw_subtitle if raw_subtitle is not None else transcript
    _require(len(import_text) <= materials.DEFAULT_LIMITS["max_text_bytes"], "transcript byte bound exceeded")
    _require(len(manifest_data) + len(import_text) + metadata["images"]["bytes"] <= materials.DEFAULT_LIMITS["max_total_bytes"],
             "material package total byte bound exceeded")
    materials._json(manifest_data)
    materials._check_bytes(transcript, "transcript")
    materials._text_resources(transcript, "transcript.txt", {p["relative_path"] for p in pages})
    location = {"schema": "worldanalysis.course-source-location.v1", "project_id": project_id,
                "file_id": source["file_id"], "source_revision": source["source_revision"], "path": current_path,
                "observation_method": "explicit_cli_input" if source_path is not None else "native_subtitle_response" if subtitle_value is not None else "original_draft_observation",
                "observed_at": source_path_observed_at if source_path is not None else (subtitle_value or draft_value).get("observed_at"),
                "remote_identity_verified": False, "original_evidence": evidence}
    manifest_name = "material-manifest-" + digest(current_path)[:16] + ".json"
    location_name = "source-location-" + digest(location)[:16] + ".json"
    outputs = {"transcript.txt": transcript, "文案.txt": ("\n".join(lines) + "\n").encode("utf-8"),
               "课件索引.md": "\n".join(index).encode("utf-8"), "时间页关联.json": _json_bytes(associations),
               "preparation-metadata.json": _json_bytes(metadata), manifest_name: manifest_data, location_name: _json_bytes(location)}
    if raw_subtitle is not None:
        outputs["native-subtitle.raw.srt"] = raw_subtitle
    for name, data in outputs.items():
        _require(name not in {draft_path.name, pages_path.name, metadata_path.name}, "output collides with original evidence")
        _existing_output(directory / name, data)
    created, unchanged = [], []
    for name, data in outputs.items():
        (created if _create(directory / name, data) else unchanged).append(name)
    return {"status": "prepared", "published": False, "project_id": project_id, "source_file_id": source["file_id"],
            "directory": str(directory), "manifest_path": str(directory / manifest_name), "location_path": str(directory / location_name),
            "sentence_count": draft_value["sentence_count"], "paragraph_start_ms": [p["start_ms"] for p in paragraphs],
            "page_count": len(pages), "image_bytes": metadata["images"]["bytes"], "quality": "not_assessed",
            "full_media_coverage_verified": False, "srt_generated": False, "raw_srt_retained": raw_subtitle is not None,
            "subtitle_cue_count": len(cues) + len(metadata_cues), "created_files": created, "unchanged_files": unchanged}


def prepare_transcript_only(root, project_id, subtitle_response, *, title=None,
                            source_path=None, source_path_observed_at=None):
    """Prepare literal full-response cues without asserting DOM paragraphs or coverage."""
    root = Path(os.path.abspath(root))
    reject_links(root)
    _require(root.is_dir(), "existing workspace required")
    project_id = check_id(project_id)
    raw, response, evidence, metadata_cues, cues = _subtitle(root, subtitle_response)
    response_path = safe_path(root, evidence["path"])
    directory = response_path.parent
    relative = directory.relative_to(root)
    file_id = response["source_file_id"]
    _require(relative.parts[0] == "_ingest" and directory.name == file_id and directory.parent.name == project_id,
             "transcript-only output must stay in the existing _ingest stable project/source ID directory")
    current_path = Policy().require_cloud_path(source_path if source_path is not None else response["source_path"])
    if source_path_observed_at is not None:
        _require(source_path is not None, "source_path_observed_at requires an explicit source_path")
        try:
            observed = datetime.fromisoformat(source_path_observed_at.replace("Z", "+00:00"))
            _require(observed.utcoffset() is not None, "source path observation needs an explicit time zone")
        except (TypeError, ValueError) as exc:
            raise ContractError("invalid source path observed_at") from exc
    title = title if title is not None else PurePosixPath(response["source_path"]).stem
    source = {"provider": "baidu", "file_id": file_id, "path": current_path, "source_revision": response["source_revision"]}
    manifest = {"schema_version": "1.0", "material_id": "native-" + file_id, "title": title, "source": source,
                "transcript": {"path": "native-subtitle.raw.srt", "kind": "timed_subtitle", "origin": "asr",
                               "source_method": "passive_normal_page_response"}}
    materials._validate_manifest(manifest, Policy())
    manifest_data = _json_bytes(manifest)
    materials._json(manifest_data)
    _require(len(manifest_data) <= materials.MAX_MANIFEST_BYTES and len(raw) <= materials.DEFAULT_LIMITS["max_text_bytes"]
             and len(manifest_data) + len(raw) <= materials.DEFAULT_LIMITS["max_total_bytes"], "transcript-only package byte bound exceeded")
    # One literal cue body per line; these are not claimed DOM or author paragraphs.
    transcript = ("\n".join(c["text"] for c in cues) + "\n").encode("utf-8")
    lines = [title, "", "网盘 AI 自动转写，未逐字校对；按原始字幕 cue 序列逐句展示，不改写原话。",
             "来源：passive_normal_page_response（正常页面原始字幕响应）；origin：asr。",
             "没有读取/重构 DOM 段落；DOM 段落边界未知，说话人未知。cue 换行不是人为段落或说话人推断。",
             "逐句起止来自原响应；完整保存本次响应不等于已独立核验全片、字幕准确或质量充分。",
             "图片/课件未纳入本次 transcript-only 包，不把文稿准备成功算成视觉证据充分。",
             "原样保留 raw SRT（含 cue 0 平台提示）；cue 0 不计入口述句数，不规范化或补造时间。",
             "来源 file_id：" + file_id, "来源 revision：" + response["source_revision"],
             "原始响应取证路径（历史观察，移动不覆盖）：" + response["source_path"],
             "响应证据：" + evidence["path"] + "；SHA-256=" + evidence["sha256"],
             f"口述 cues：{len(cues)}；元提示 cues：{len(metadata_cues)}。", "", "原始口述 cue 序列"]
    for cue in cues:
        lines += [f"[句 {cue['source_label']}；{_clock(cue['start_ms'])}–{_clock(cue['end_ms'])}；说话人未知]", cue["text"]]
    metadata = {"schema": "worldanalysis.transcript-only-preparation.v1", "project_id": project_id,
                "source_file_id": file_id, "source_revision": response["source_revision"], "evidence": evidence,
                "source_observation": {"path": response["source_path"], "observed_at": response.get("observed_at"),
                                       "transport": response.get("transport"), "acquisition": response["acquisition"]},
                "transcript": {"origin": "asr", "source_method": "passive_normal_page_response", "path": "transcript.txt",
                               "sha256": hashlib.sha256(transcript).hexdigest(), "bytes": len(transcript),
                               "layout": "literal_spoken_cue_sequence_not_dom_paragraphs", "dom_paragraphs": None,
                               "spoken_cue_count": len(cues), "speaker": None},
                "raw_subtitle": {"path": "native-subtitle.raw.srt", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                                 "cue_count": len(cues) + len(metadata_cues), "metadata_cues": metadata_cues,
                                 "normalization_performed": False},
                "sentence_times": "original_native_subtitle_response", "quality": "not_assessed",
                "remote_identity_verified": False, "full_media_coverage_verified": False,
                "visual_materials_included": False, "srt_generated": False}
    times = {"schema": "worldanalysis.subtitle-cue-sequence.v1", "source_file_id": file_id,
             "source_revision": response["source_revision"], "evidence": evidence, "dom_paragraphs": None,
             "speaker": None, "metadata_cues": metadata_cues, "spoken_cues": cues, "timing_accuracy_verified": False}
    location = {"schema": "worldanalysis.course-source-location.v1", "project_id": project_id, "file_id": file_id,
                "source_revision": response["source_revision"], "path": current_path,
                "observation_method": "explicit_cli_input" if source_path is not None else "native_subtitle_response",
                "observed_at": source_path_observed_at if source_path is not None else response.get("observed_at"),
                "remote_identity_verified": False, "original_evidence": {"subtitle_response": evidence}}
    manifest_name = "material-manifest-" + digest(current_path)[:16] + ".json"
    location_name = "source-location-" + digest(location)[:16] + ".json"
    outputs = {"transcript.txt": transcript, "文案.txt": ("\n".join(lines) + "\n").encode("utf-8"),
               "字幕时间关联.json": _json_bytes(times), "transcript-only-metadata.json": _json_bytes(metadata),
               "native-subtitle.raw.srt": raw, manifest_name: manifest_data, location_name: _json_bytes(location)}
    for name, data in outputs.items():
        _require(name != response_path.name, "output collides with original subtitle response")
        _existing_output(directory / name, data)
    created, unchanged = [], []
    for name, data in outputs.items():
        (created if _create(directory / name, data) else unchanged).append(name)
    return {"status": "prepared", "mode": "transcript_only", "published": False, "project_id": project_id,
            "source_file_id": file_id, "directory": str(directory), "manifest_path": str(directory / manifest_name),
            "location_path": str(directory / location_name), "sentence_count": len(cues),
            "subtitle_cue_count": len(cues) + len(metadata_cues), "raw_subtitle_bytes": len(raw),
            "first_start_ms": cues[0]["start_ms"], "last_end_ms": cues[-1]["end_ms"], "dom_paragraphs": None,
            "quality": "not_assessed", "full_media_coverage_verified": False,
            "created_files": created, "unchanged_files": unchanged}


def prepare_source_frames(root, project_id, frames_manifest, *, subtitle_response=None, title=None,
                          source_path=None, source_path_observed_at=None):
    """Legacy offline readability-only entry; ordinary 64MP limit is unchanged."""
    return _prepare_source_frames(root, project_id, frames_manifest, subtitle_response=subtitle_response, title=title,
                                  source_path=source_path, source_path_observed_at=source_path_observed_at)


def prepare_source_frame_execution(store, project_id, job_id, quality_review_path, *, subtitle_response=None,
                                   title=None, source_path=None, source_path_observed_at=None):
    """Prepare the complete collection of an actually completed, reviewed job."""
    if store.connection.in_transaction:
        raise ContractError("source-frame prepare cannot initialize inside an outer transaction")
    from .platform_first import PlatformFirst
    from .platform_execution import SourceFrameExecution
    from .platform_products import inspect_source_frame_execution
    execution = SourceFrameExecution(store, PlatformFirst(store))
    job, _, _, _, receipt = execution._completion_evidence(job_id, quality_review_path)
    context = {"job_id": job_id, "input_revision": job["input_revision"],
               "quality_review_path": receipt["quality_review"]["relative_path"],
               "manifest_sha256": receipt["manifest"]["sha256"], "quality_review_sha256": receipt["quality_review"]["sha256"]}
    state = materials._source_frame_state(store, project_id, context)
    inspected = inspect_source_frame_execution(store, state["coordinator"], job_id, quality_review_path)
    if (inspected["dependencies"] != state["artifact"]["dependencies"]
            or any(inspected[k] != state["artifact"][k] for k in ("relative_path", "sha256", "size_bytes"))):
        raise ContractError("completed source-frame artifact changed during prepare")
    state = materials._source_frame_state(store, project_id, context)
    # Persist the stable Store admission, not a new PID/timing on every rerun.
    return _prepare_source_frames(store.root, project_id, receipt["manifest"]["relative_path"],
        subtitle_response=subtitle_response, title=title, source_path=source_path,
        source_path_observed_at=source_path_observed_at, inspected_artifact=state["artifact"], execution_context=context)


def _prepare_source_frames(root, project_id, frames_manifest, *, subtitle_response=None, title=None,
                           source_path=None, source_path_observed_at=None, inspected_artifact=None, execution_context=None):
    """Offline view of real remote-source frame files; never acquire/copy images.

    The shared product inspector accepts a root/limit reader, not a writable
    Store. Legacy import timestamps are floored to integer ms; exact rVFC
    media_time_ms values remain in the index and JSON association sidecar.
    """
    from types import SimpleNamespace
    from . import platform_products

    root = Path(os.path.abspath(root))
    reject_links(root)
    _require(root.is_dir(), "existing workspace required")
    project_id = check_id(project_id)
    path, value, evidence = _load(root, frames_manifest)
    _require(value.get("schema") == platform_products.SOURCE_FRAMES_SCHEMA, "source-video-frames.v1 manifest required")
    source = _source(value.get("source"))
    directory = path.parent
    parts = directory.relative_to(root).parts
    _require(parts[:2] == ("采集输入", "platform-source-frames") and len(parts) == 3,
             "source-frame output must stay in the existing capture_root: 采集输入/platform-source-frames/<job_id>")
    reader = SimpleNamespace(root=root, max_file_bytes=materials.DEFAULT_LIMITS["max_total_bytes"])
    inspected = inspected_artifact if execution_context is not None else platform_products.inspect_product(reader, "courseware", evidence["path"])
    _require(inspected.get("readable") is True, "source-frame set is not locally readable")
    _require((inspected["relative_path"], inspected["sha256"], inspected["size_bytes"]) == (evidence["path"], evidence["sha256"], evidence["bytes"]), "source-frame manifest changed during prepare")
    suffix = "-" + digest(execution_context)[:16] if execution_context is not None else ""
    index_name, prose_name = "源帧索引" + suffix + ".md", "源帧文案" + suffix + ".txt"
    times_name, preparation_name = "source-frame-times" + suffix + ".json", "source-frame-preparation" + suffix + ".json"
    subtitle_name = "native-subtitle" + suffix + ".raw.srt"
    raw, response, subtitle_evidence, metadata_cues, cues = None, None, None, [], []
    if subtitle_response is not None:
        raw, response, subtitle_evidence, metadata_cues, cues = _subtitle(root, subtitle_response, source)
    current_path = Policy().require_cloud_path(source_path if source_path is not None else source["path"])
    if source_path_observed_at is not None:
        _require(source_path is not None, "source_path_observed_at requires an explicit source_path")
        try:
            observed = datetime.fromisoformat(source_path_observed_at.replace("Z", "+00:00"))
            _require(observed.utcoffset() is not None, "source path observation needs an explicit time zone")
        except (TypeError, ValueError) as exc:
            raise ContractError("invalid source path observed_at") from exc
    label = "远程源帧，非原生PPT"
    title = title if title is not None else PurePosixPath(source["path"]).stem + " · " + label
    declared_source = {"provider": "baidu", "file_id": source["file_id"], "path": current_path, "source_revision": source["source_revision"]}
    for field in ("account_ref", "size_bytes"):
        if field in source:
            declared_source[field] = source[field]
    frames = [{**frame, "importer_timestamp_ms": int(frame["media_time_ms"]),
               "importer_time_conversion": "floor_to_integer_ms_exact_media_time_ms_retained",
               "spoken_cues_covering_media_time": [c["source_label"] for c in cues if c["start_ms"] <= frame["media_time_ms"] <= c["end_ms"]]}
              for frame in value["frames"]]
    manifest = {"schema_version": "1.0", "material_id": "source-frames-" + source["file_id"], "title": title,
                "source": declared_source,
                "images": [{"path": f["relative_path"], "source_kind": "claimed_video_frame", "timestamp_ms": f["importer_timestamp_ms"]} for f in frames]}
    if raw is not None:
        manifest["transcript"] = {"path": subtitle_name, "kind": "timed_subtitle", "origin": "asr", "source_method": "passive_normal_page_response"}
    if execution_context is not None:
        manifest["source_frame_execution"] = execution_context
    materials._validate_manifest(manifest, Policy())
    manifest_data = _json_bytes(manifest)
    materials._json(manifest_data)
    image_bytes = sum(f["size_bytes"] for f in frames)
    _require(len(manifest_data) <= materials.MAX_MANIFEST_BYTES and image_bytes + len(manifest_data) + len(raw or b"") <= materials.DEFAULT_LIMITS["max_total_bytes"],
             "source-frame import package byte bound exceeded")
    associations = {"schema": "worldanalysis.source-frame-times.v1", "project_id": project_id, "source": source,
                    "representation": "remote_source_frame_capture", "not_native_container_bytes": True,
                    "source_manifest": evidence, "subtitle_response": subtitle_evidence, "frames": frames,
                    "association_rule": "actual media_time_ms inside original speech-cue interval; no invented frame interval",
                    "quality": "not_assessed", "source_identity_verified": False, "timing_accuracy_verified": False,
                    "video_coverage_verified": False, "selection_verified": False}
    metadata = {"schema": "worldanalysis.source-frame-preparation.v1", "project_id": project_id,
                "project_binding": "explicit_cli_project_unverified", "representation": "remote_source_frame_capture",
                "not_native_container_bytes": True, "source_manifest": evidence, "subtitle_response": subtitle_evidence,
                "frame_count": len(frames), "image_bytes": image_bytes, "local_inspection": inspected,
                "quality": "not_assessed", "source_identity_verified": False, "timing_accuracy_verified": False,
                "video_coverage_verified": False, "selection_verified": False, "images_copied": False,
                "srt_normalized": False, "spoken_cue_count": len(cues), "metadata_cue_count": len(metadata_cues)}
    if execution_context is not None:
        associations["source_frame_execution"] = execution_context
        metadata.update({"source_frame_execution": execution_context, "project_binding": "completed_store_execution", "external_visual_review_bound": True})
    index = ["# " + title, "", "**" + label + "**。实际已保存图像的本地链接，不是平台原生课件页。",
             "只核验本地哈希/解码/文件依赖。来源身份、rVFC时间准确性、选择充分性、全片画面覆盖及质量均待独立审核。",
             "请求时刻和实际 media_time_ms 分别保留。旧导入器仅接受整数ms，manifest向下取整；此索引/JSON不改实际精度。",
             "选点依据（声明，未验证）：" + value["selection"]["basis"], "源manifest：" + evidence["path"], "",
             "| 帧 | requested_ms | 实际 media_time_ms | 导入定位ms | 本地图像 | 命中的口述cue |", "|---|---|---|---|---|---|"]
    for frame in frames:
        hits = ", ".join(frame["spoken_cues_covering_media_time"]) or "无（不推定）"
        index.append(f"| {frame['sequence']} | {frame['requested_ms']} | {frame['media_time_ms']} | {frame['importer_timestamp_ms']} | [源帧](<{frame['relative_path']}>) | {hits} |")
    if execution_context is not None:
        index[3] = "已绑定Store中完成的源帧任务及其当前正式独立画面审核；本准备器不因此宣称文稿/全课质量充分。"
    prose = [title, "", label, "来源/时间/覆盖质量未自动判充分；没有复制、重采已有图像。", "", "源帧索引：" + index_name]
    if raw is not None:
        prose += ["", "网盘 AI 自动转写，未逐字校对；passive_normal_page_response 原始口述cue序列。DOM段落与说话人未知。"]
        for cue in cues:
            prose += [f"[句 {cue['source_label']}；{_clock(cue['start_ms'])}–{_clock(cue['end_ms'])}；说话人未知]", cue["text"]]
    location = {"schema": "worldanalysis.course-source-location.v1", "project_id": project_id, "file_id": source["file_id"],
                "source_revision": source["source_revision"], "path": current_path,
                "observation_method": "explicit_cli_input" if source_path is not None else "source_frame_manifest_declaration",
                "observed_at": source_path_observed_at, "remote_identity_verified": False, "source_manifest": evidence}
    manifest_name = "source-frame-material-manifest-" + digest([current_path, execution_context] if execution_context is not None else current_path)[:16] + ".json"
    location_name = "source-frame-location-" + digest(location)[:16] + ".json"
    outputs = {index_name: ("\n".join(index) + "\n").encode("utf-8"), prose_name: ("\n".join(prose) + "\n").encode("utf-8"),
               times_name: _json_bytes(associations), preparation_name: _json_bytes(metadata),
               manifest_name: manifest_data, location_name: _json_bytes(location)}
    if raw is not None:
        outputs[subtitle_name] = raw  # Bounded small text only; images stay at their original relative paths.
    for name, data in outputs.items():
        _require(name != path.name, "output collides with source-frame manifest")
        _existing_output(directory / name, data)
    created, unchanged = [], []
    for name, data in outputs.items():
        (created if _create(directory / name, data) else unchanged).append(name)
    return {"status": "prepared", "mode": "source_frames", "published": False, "project_id": project_id,
            "source_file_id": source["file_id"], "directory": str(directory), "manifest_path": str(directory / manifest_name),
            "location_path": str(directory / location_name), "frame_count": len(frames), "image_bytes": image_bytes,
            "sentence_count": len(cues), "quality": "not_assessed", "video_coverage_verified": False,
            "created_files": created, "unchanged_files": unchanged}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--draft")
    parser.add_argument("--pages-manifest")
    parser.add_argument("--courseware-metadata")
    input_mode = parser.add_mutually_exclusive_group()
    input_mode.add_argument("--transcript-only", action="store_true", help="仅原始字幕响应：无需 draft/课件，不伪造 DOM 分段")
    input_mode.add_argument("--frames-manifest", help="实际source-video-frames.v1：远程源帧、非原生PPT；保留capture_root相对图像路径")
    parser.add_argument("--subtitle-response", help="正常页面原始 SRT 响应；有 draft 时严格逐字/段首对齐，无 draft 时保留真实 cue 序列")
    parser.add_argument("--title")
    parser.add_argument("--source-path", help="当前完整视频云路径：新观察；不覆盖旧取证路径、不参与内容身份")
    parser.add_argument("--source-path-observed-at", help="可选的含时区观察时刻；未知则留空，不自动编造")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-only", action="store_true", help="默认：仅本地材料准备，不打开 SQLite")
    mode.add_argument("--publish", action="store_true", help="显式授权后，通过原 Store/import_materials 导入现有项目")
    args = parser.parse_args(argv)
    try:
        if args.transcript_only:
            _require(args.subtitle_response is not None and not any((args.draft, args.pages_manifest, args.courseware_metadata)),
                     "--transcript-only requires --subtitle-response and no draft/pages/courseware inputs")
            result = prepare_transcript_only(args.root, args.project, args.subtitle_response, title=args.title,
                                             source_path=args.source_path, source_path_observed_at=args.source_path_observed_at)
        elif args.frames_manifest:
            _require(not any((args.draft, args.pages_manifest, args.courseware_metadata)), "source-frame mode does not accept native draft/pages/courseware inputs")
            result = prepare_source_frames(args.root, args.project, args.frames_manifest, subtitle_response=args.subtitle_response,
                                           title=args.title, source_path=args.source_path, source_path_observed_at=args.source_path_observed_at)
        else:
            _require(all((args.draft, args.pages_manifest, args.courseware_metadata)),
                     "native-page mode requires --draft, --pages-manifest and --courseware-metadata; or use --transcript-only")
            result = prepare_delivery(args.root, args.project, args.draft, args.pages_manifest, args.courseware_metadata,
                                      title=args.title, source_path=args.source_path, source_path_observed_at=args.source_path_observed_at,
                                      subtitle_response=args.subtitle_response)
        if args.publish:
            database = Path(args.root) / "整理结果" / "worldanalysis.sqlite3"
            reject_links(database)
            _require(database.is_file(), "--publish requires an existing Store database/project; prepare-only never creates it")
            with Store(args.root) as store:
                published = materials.import_materials(store, args.project, result["manifest_path"])
            result["publication"] = {key: published[key] for key in ("status", "reason", "project_id", "bundle_id", "publish_id") if key in published}
            result["published"] = published.get("publication", {}).get("state") == "published"
            result["status"] = "published" if result["published"] else published.get("status", "not_published")
    except (ContractError, GateError, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "failed", "reason": redact_text(str(exc))[0]}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"prepared", "published"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
