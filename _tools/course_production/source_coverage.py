"""Source-first scope and bidirectional review for a new textbook publication."""

import hashlib
from html import unescape
import json
from pathlib import Path
import re

from project import VIDEO_EXTENSIONS, local_child, read, sha
from reader_review import IMAGE, markdown_segments


WINDOW_SECONDS = 120
CHANNEL_SECONDS = 30
ANCHOR_EPSILON = .05
AUTHOR_KINDS = {"作者渠道", "资料署名", "分发渠道", "归属待核", "非渠道"}
OUTCOMES = {"正文已覆盖", "无独立教学", "原件缺口"}
NON_TEACHING = {"过场", "重复", "静默"}
EMPTY_NOTES = {"已看", "已阅", "已核", "已查", "已检查", "同上", "略", "无", "无内容", "无独立教学", "完成",
               "ok", "reviewed", "viewed", "pass"}
CONTACT = re.compile(
    r"联系方式|联系(?:我|作者|讲师)|加(?:我的|我|老师的)?微信|微信(?:号|群|[:：])|"
    r"(?:抖音|快手|小红书|公众号)(?:号|账号|[:：])|"
    r"(?:找|联系|交给)(?:一下)?(?:你的|你们的|我的|我们的|原课程)?助教(?:老师)?|"
    r"\b(?:WeChat|QQ)\s*(?:ID|群|号|[:：])|作者(?:主页|频道|微信)|讲师(?:主页|频道|微信)|"
    r"资料(?:领取|下载)链接|(?:作者|讲师|技术)支持", re.IGNORECASE,
)
CONTACT_TOKEN = re.compile(r"https?://\S+|(?:\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b)|[a-z0-9_-]{5,}", re.IGNORECASE)
DIRECT_CONTACT = re.compile(r"https?://[^\s<>）】\]]+|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", re.IGNORECASE)
CONTACT_ID = re.compile(r"^\s*(?:是|为)?\s*[:：]?\s*([a-z0-9_][a-z0-9_.-]{4,63})", re.IGNORECASE)
COMMENTS = re.compile(r"<!--([\s\S]*?)-->")
PARAGRAPH_SOURCE = re.compile(r"<!-- 段落出处 ([^\n]+?) -->")
VIDEO_LABEL = re.compile(r"第 (\d+) 讲 ([0-9:.]+)(?:–([0-9:.]+))?(?=$| · )")
PAGE_LABEL = re.compile(r"(?:第\s*)?(\d+)\s*(?:(?:[–—~～-]|至|到)\s*(\d+)\s*)?(?:页|張|张|张幻灯片)")
CELL_LABEL = re.compile(r"(?<![A-Z0-9_])\$?([A-Z]{1,3})\$?([1-9][0-9]*)(?![A-Z0-9_])", re.IGNORECASE)
COLLAPSED = re.compile(
    r"^ {0,3}>[ \t]*\[![^\]\n]+\]-[^\n]*(?:\n[ \t]*>[^\n]*)*"
    r"|<details\b[^>]*>[\s\S]*?</details\s*>", re.MULTILINE | re.IGNORECASE,
)


def fingerprint(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def expected_windows(duration):
    starts = range(0, int(duration) + 1, WINDOW_SECONDS)
    return [[start, min(start + WINDOW_SECONDS, duration)] for start in starts if start < duration]


def unit(key, value):
    return {"key": f"{key}:{value}", "kind": key, "value": value}


def locator_unit(locator):
    if not isinstance(locator, dict):
        return None
    for field, kind in (("member_path", "member"), ("page_number", "page"),
                        ("slide_number", "slide"), ("sheet_name", "sheet"), ("sheet", "sheet")):
        if locator.get(field) is not None:
            return unit(kind, locator[field])
    return None


def document_units(result):
    meta = result.get("metadata", {})
    units = {}
    observed_cells = {}

    def add(item):
        if item:
            units.setdefault(item["key"], {}).update(item)

    for number in range(1, meta.get("page_count", 0) + 1):
        add(unit("page", number))
    for record in [*meta.get("slides", []), *meta.get("sheets", [])]:
        item = locator_unit(record)
        if item and record.get("dimension"):
            item["dimension"] = record["dimension"]
        add(item)
    for member in result.get("archive_members", meta.get("archive_members", [])):
        if not (member.get("is_directory") or member.get("status") == "directory"):
            name = member.get("path") or member.get("name")
            if name:
                add(unit("member", name))
    for block in result.get("blocks", []):
        locator = block.get("locator", {})
        # Archive inventories already distinguish file members from directories.
        if locator.get("member_path") and (result.get("archive_members") or meta.get("archive_members")):
            continue
        item = locator_unit(locator)
        add(item)
        if item and item["kind"] == "sheet" and locator.get("cell_range"):
            observed_cells.setdefault(item["key"], []).extend(cell_coordinates(locator["cell_range"]))
    for key, cells in observed_cells.items():
        if not cells:
            continue
        observed = cell_bounds(cells)
        declared = cell_bounds(list(cell_coordinates(units[key].get("dimension", ""))))
        # A stale worksheet dimension can exclude cells that the extractor actually read.
        if not declared or not inside_cells(observed[:2], declared) or not inside_cells(observed[2:], declared):
            units[key]["observed_cell_bounds"] = observed
    gaps = {}
    blocks = result.get("blocks", [])
    for gap in result.get("gaps", []):
        located = locator_unit(gap.get("locator"))
        indexes = gap.get("block_indexes", [])
        if not located and indexes and all(type(index) is int and 0 <= index < len(blocks) for index in indexes):
            located = locator_unit(blocks[indexes[0]].get("locator"))
        key = located["key"] if located and located["key"] in units else "file"
        gaps.setdefault(key, []).append(gap.get("reason", "原件提取缺口"))
    if not units or "file" in gaps:
        units["file"] = {"key": "file", "kind": "file", "value": "整份原件"}
    for key, item in units.items():
        if key in gaps:
            item["extraction_notes"] = list(dict.fromkeys(gaps[key]))
    return list(units.values())


def archive_unit_index(classified):
    """Include static and nested archive members even when their extractor deferred them."""
    index = {}
    for archive in classified.get("archives", []):
        members = archive.get("members", [])
        if archive.get("member_count", 0) != len(members):
            raise ValueError(f"压缩包未登记完整成员索引，请重新盘点 {archive['source']}")
        units = index.setdefault(archive["document"], {})
        for member in members:
            if member["status"] == "directory":
                continue
            location = "!".join([*archive["member_chain"], member["path"]])
            source_unit = unit("member", location)
            units[source_unit["key"]] = {**source_unit, "archive_status": member["status"]}
    return index


def document_candidates(identifier, name, result, known_units):
    candidates = {}
    for block in result.get("blocks", []):
        text = block.get("text") or ""
        locator = block.get("locator", {})
        location = locator_unit(locator)
        key = location["key"] if location and location["key"] in known_units else "file"
        explicit_link = (Path(name).suffix.lower() == ".url" and locator.get("field") == "URL")
        match = CONTACT.search(text)
        if not explicit_link and not match:
            continue
        start = max(0, match.start() - 24) if match else 0
        cue = text[start:start + 150].strip()
        if not explicit_link and not CONTACT_TOKEN.search(cue):
            continue
        cand_id = f"{identifier}-{fingerprint([key, cue])[:12]}"
        candidates[cand_id] = {"id": cand_id, "unit": key, "cue": cue,
                               "value": contact_value(cue)}
    return list(candidates.values())


def contact_value(cue):
    if direct := DIRECT_CONTACT.search(cue):
        return direct[0].rstrip(".,;，。；）)")
    if hint := CONTACT.search(cue):
        if account := CONTACT_ID.match(cue[hint.end():]):
            return account[1]
    return ""


def video_candidates(lesson, duration, sources):
    candidates = {}
    for row in sources:
        text = row.get("text", "")
        match = CONTACT.search(text)
        if not match:
            continue
        start, end = row["start"], min(row["end"], duration)
        if not 0 <= start < end:
            continue
        cue = text[max(0, match.start() - 24):match.end() + 100].strip()
        cand_id = f"L{lesson:02}-{fingerprint([start, end, cue])[:12]}"
        candidates[cand_id] = {"id": cand_id, "range_s": [start, end], "cue": cue,
                               "value": contact_value(cue)}
    return list(candidates.values())


def build_source_manifest(project):
    """Derive review scope from the intake, timed media and attachment extraction records."""
    evidence = {}

    def evidence_from(path):
        relative = path.relative_to(project.state).as_posix()
        if relative not in evidence:
            evidence[relative] = sha(path)
        return read(path)

    intake = evidence_from(project.intake)["sources"]
    manifest = evidence_from(project.ledger / "inventory.json")["sources"]
    if [(row["file"], row["sha256"]) for row in intake] != [(row["file"], row["sha256"]) for row in manifest]:
        raise ValueError("取材清单与完整原件范围不一致，请重新运行 prepare")
    media = evidence_from(project.ledger / "media-summary.json")
    classified_path = project.ledger / "media-inventory.json"
    classified = evidence_from(classified_path) if classified_path.is_file() else {}
    archives = archive_unit_index(classified)
    files = evidence_from(project.ledger / "documents-summary.json")["records"]
    timed = {row["source"]: row["sha256"] for row in [*media["videos"], *media.get("unreadable", [])]
             if not row.get("embedded")}
    attachments = {row["file"]: row["sha256"] for row in files}
    originals = {row["file"]: row["sha256"] for row in intake}
    if timed.keys() & attachments.keys() or (timed | attachments) != originals:
        raise ValueError("原件未全部进入媒体或附件取材范围，请重新运行 prepare")
    if not classified and any(row["extension"] in VIDEO_EXTENSIONS and row["file"] not in timed for row in intake):
        raise ValueError("原视频未进入逐集取材范围，请重新运行 prepare")
    videos = []
    order = evidence_from(project.work / "讲次顺序.json")
    positions = {number: index for index, number in enumerate(order, 1)}
    for row in media["videos"]:
        lesson, duration = row["lesson"], row["duration_s"]
        if lesson not in positions or not duration > 0:
            raise ValueError(f"媒体讲次或时长未核实 {row['source']}")
        meta = evidence_from(project.ledger / f"{lesson:02}.json")
        adopted_path = project.work / "采用转写" / f"{lesson:02}.json"
        adopted = evidence_from(adopted_path) if adopted_path.is_file() else meta
        cues = [*meta.get("cues", []), *meta.get("original_cues", []), *adopted.get("cues", [])]
        zones = [
            {"zone": "opening", "range_s": [0, min(duration, CHANNEL_SECONDS)]},
            {"zone": "closing", "range_s": [max(0, duration - CHANNEL_SECONDS), duration]},
            {"zone": "screen", "range_s": [0, duration]},
        ]
        videos.append({"lesson": lesson, "display_number": positions[lesson], "source": row["source"],
                       "sha256": row["sha256"], "kind": row.get("kind", "video"),
                       "duration_s": duration, "windows": expected_windows(duration), "zones": zones,
                       "candidates": video_candidates(lesson, duration, cues)})

    documents = []
    for row in files:
        result = evidence_from(local_child(project.work, row["result"])) if row.get("result") else {
            "gaps": row.get("gaps_detail", []),
        }
        units = {unit["key"]: unit for unit in document_units(result)}
        for key, item in archives.get(row["file"], {}).items():
            units.setdefault(key, {}).update(item)
        candidates = document_candidates(row["id"], row["file"], result, units)
        if any(candidate["unit"] == "file" for candidate in candidates) and "file" not in units:
            units["file"] = {"key": "file", "kind": "file", "value": "整份原件"}
        documents.append({"id": row["id"], "source": row["file"], "sha256": row["sha256"],
                          "status": row["status"], "units": list(units.values()), "candidates": candidates})
    scope = {"schema": 1, "intake_sha256": evidence["ledger/intake.json"],
             "videos": sorted(videos, key=lambda row: row["display_number"]), "documents": documents,
             "unreadable": [row["source"] for row in media.get("unreadable", [])]}
    return scope, evidence


def claim_blocks(body):
    for is_code, part in markdown_segments(COLLAPSED.sub("", body)):
        if is_code:
            continue
        for block in re.split(r"\n[ \t]*\n", part):
            prose = IMAGE.sub("", COMMENTS.sub("", block))
            prose = " ".join(line.strip() for line in prose.splitlines()
                             if line.strip() and not line.lstrip().startswith("#"))
            if prose:
                yield prose, PARAGRAPH_SOURCE.findall(block)


def coverage_review_template(scope, body=""):
    def findings(row):
        return [{"id": finding["id"], "kind": "", "note": "", "anchor": ""}
                for finding in row["candidates"]]

    return {
        "videos": [{"lesson": row["lesson"],
                    "windows": [{"range_s": window, "outcome": "", "note": ""} for window in row["windows"]],
                    "channels": [{**zone, "outcome": "", "note": "", "findings": []} for zone in row["zones"]],
                    "candidates": findings(row)} for row in scope["videos"]],
        "documents": [{"id": row["id"],
                       "units": [{"key": unit["key"], "outcome": "", "note": "", "anchor": "", "channels": []}
                                 for unit in row["units"]],
                       "candidates": findings(row)} for row in scope["documents"]],
        "claims": [{"id": f"P{index:04}", "text": prose[:120], "sources": sources, "outcome": "", "note": ""}
                   for index, (prose, sources) in enumerate(claim_blocks(body), 1)],
    }


def populated(value):
    return isinstance(value, str) and bool(value.strip())


def substantive_note(value):
    return populated(value) and value.strip(" \t\r\n.!。！").casefold() not in EMPTY_NOTES


def matching_rows(actual, expected, identity, description):
    if not isinstance(actual, list) or any(not isinstance(row, dict) for row in actual) or (
        [row.get(identity) for row in actual] != [row[identity] for row in expected]
    ):
        raise ValueError(f"缺失、重复或变更{description}，请按当前来源复核模板填写")
    return zip(actual, expected)


def time_value(label):
    seconds = 0
    for field in label.split(":"):
        seconds = seconds * 60 + float(field)
    return seconds


def citation_syntax(documents):
    names = [row["source"] for row in documents]
    doc_labels = [re.escape(name) + r" · " for name in sorted(names, key=len, reverse=True)]
    return re.compile(r"(?:^| · )(" + "|".join([VIDEO_LABEL.pattern, *doc_labels]) + r")")


def cite_tokens(note, syntax):
    note = unescape(note)
    hits = list(syntax.finditer(note))
    if not hits or hits[0].start() != 0:
        yield "unknown", note, None
    for index, hit in enumerate(hits):
        segment = note[hit.start(1):hits[index + 1].start() if index + 1 < len(hits) else len(note)]
        if video := VIDEO_LABEL.fullmatch(segment):
            first = time_value(video[2])
            yield ("video", int(video[1]), [first, time_value(video[3]) if video[3] else first])
        else:
            yield ("document", segment[:len(hit[1]) - 3], segment[len(hit[1]):])


def unit_matches(unit, label):
    kind, value = unit["kind"], unit["value"]
    if kind in {"page", "slide"}:
        return any(int(first) <= value <= int(last or first) for first, last in PAGE_LABEL.findall(label))
    if kind == "sheet":
        return bool(re.search(r"(?<![a-z0-9_])" + re.escape(str(value)) + r"(?![a-z0-9_])", label, re.IGNORECASE))
    if kind == "member":
        path = re.escape(str(value).replace("\\", "/"))
        return bool(re.search(r"(?<![\w./!-])" + path + r"(?![\w./!-])", label.replace("\\", "/")))
    return populated(label)


def document_locator_valid(document, label):
    units = document["units"]
    pages = {unit["value"] for unit in units if unit["kind"] in {"page", "slide"}}
    if pages:
        ranges = [(int(first), int(last or first)) for first, last in PAGE_LABEL.findall(label)]
        return bool(ranges) and all(0 < left <= right and right - left < len(pages)
                                    and all(page in pages for page in range(left, right + 1)) for left, right in ranges)
    structured = [unit for unit in units if unit["kind"] != "file"]
    for unit in structured or units:
        if not unit_matches(unit, label):
            continue
        if unit["kind"] != "sheet" or sheet_cells_valid(unit, label):
            return True
    return False


def cell_coordinates(text):
    for column, row in CELL_LABEL.findall(text):
        number = 0
        for char in column.upper():
            number = number * 26 + ord(char) - ord("A") + 1
        yield number, int(row)


def cell_bounds(cells):
    if not cells:
        return None
    columns, rows = zip(*cells)
    return [min(columns), min(rows), max(columns), max(rows)]


def inside_cells(cell, bounds):
    column, row = cell
    first_column, first_row, last_column, last_row = bounds
    return first_column <= column <= last_column and first_row <= row <= last_row


def sheet_cells_valid(unit, label):
    bounds = unit.get("observed_cell_bounds") or cell_bounds(list(cell_coordinates(unit.get("dimension", ""))))
    if not bounds:
        return True
    unlabelled = re.sub(re.escape(str(unit["value"])), "", label, flags=re.IGNORECASE)
    return all(inside_cells(cell, bounds) for cell in cell_coordinates(unlabelled))


def intersects(left, right):
    return min(left[1], right[1]) - max(left[0], right[0]) > ANCHOR_EPSILON


def first_uncovered(location, accounted):
    cursor, end = location
    for left, right in sorted(accounted):
        if right < cursor:
            continue
        if left > cursor + ANCHOR_EPSILON:
            return [cursor, min(left, end)]
        cursor = max(cursor, right)
        if cursor + ANCHOR_EPSILON >= end:
            return None
    return [cursor, end] if cursor + ANCHOR_EPSILON < end else None


def video_covers(citations, number, location):
    start, end = location
    return any(kind == "video" and identity == number and cited[0] <= start + ANCHOR_EPSILON
               and cited[1] + ANCHOR_EPSILON >= end for kind, identity, cited in citations)


def document_covers(citations, source, unit):
    return any(kind == "document" and identity == source and unit_matches(unit, label)
               for kind, identity, label in citations)


def validate_finding(finding, location_matches, blocks, label, *, value=""):
    if not isinstance(finding, dict) or finding.get("kind") not in AUTHOR_KINDS or not substantive_note(finding.get("note")):
        raise ValueError(f"作者渠道线索须说明真实归属和核对结果 {label}")
    if finding["kind"] == "作者渠道":
        anchor = finding.get("anchor")
        if not populated(anchor) or not any(
            anchor in text and (not value or value.casefold() in text.casefold()) and location_matches(cites)
            for text, cites in blocks
        ):
            raise ValueError(f"已确认的作者渠道缺同段可见正文及原件出处 {label}")


def validate_outcome(row, label):
    if row.get("outcome") not in OUTCOMES or not substantive_note(row.get("note")):
        raise ValueError(f"需要按实际检查写明来源取材和归属 {label}")
    if row["outcome"] == "原件缺口":
        raise ValueError(f"原件仍有待核内容 {label}：{row['note']}")


def validate_source_coverage(body, scope, review):
    coverage = review.get("source_coverage")
    if not isinstance(coverage, dict):
        raise ValueError("本轮发布需填来源覆盖复核，请使用当前预览模板")
    if scope["unreadable"]:
        raise ValueError(f"原片仍有待恢复取材 {scope['unreadable'][0]}")
    syntax = citation_syntax(scope["documents"])
    blocks = [(text, [token for note in sources for token in cite_tokens(note, syntax)])
              for text, sources in claim_blocks(body)]
    episodes = {episode["lesson"]: episode for episode in review["episodes"]}
    for row, expected in matching_rows(coverage.get("videos"), scope["videos"], "lesson", "原视频来源复核"):
        number = expected["display_number"]
        episode = episodes[row["lesson"]]
        points = [point for point in episode.get("source_points", []) if point.get("anchor")]
        for point in points:
            if not any(point["anchor"] in text and video_covers(cites, number, point["range_s"]) for text, cites in blocks):
                raise ValueError(f"第 {number} 讲的原片要点需对应可见正文和同段原片区间 {point['anchor']}")
        mapped = [point["range_s"] for point in points]
        excluded = []
        for part in episode["watched"]:
            if part["kind"] in NON_TEACHING:
                if not substantive_note(part["note"]) or any(intersects(part["range_s"], span) for span in mapped):
                    raise ValueError(f"第 {number} 讲非独立教学时段和来源要点冲突，或缺少具体依据 {part['range_s']}")
                excluded.append(part["range_s"])
        accounted = mapped + excluded
        windows = [{"range_s": window} for window in expected["windows"]]
        for window, base in matching_rows(row.get("windows"), windows, "range_s", f"第 {number} 讲原片时窗"):
            start, end = base["range_s"]
            label = f"第 {number} 讲 {start:g}–{end:g} 秒"
            validate_outcome(window, label)
            has_content = any(intersects(base["range_s"], span) for span in mapped)
            if has_content != (window["outcome"] == "正文已覆盖"):
                raise ValueError(f"原片时窗用途与实际来源要点不一致 {label}")
            if missing := first_uncovered(base["range_s"], accounted):
                raise ValueError(f"第 {number} 讲 {missing[0]:g}–{missing[1]:g} 秒未对应正文或具体非教学观察")
        teaching = [part["range_s"] for part in episode["watched"] if part["kind"] not in NON_TEACHING]
        if any(intersects(part, boundary) for part in teaching for boundary in excluded):
            raise ValueError(f"第 {number} 讲同一时段被同时标为教学和非教学")
        earliest = {}
        for point in points:
            moment = point["range_s"][0]
            original = earliest.setdefault(point["anchor"], moment)
            if abs(moment - original) >= WINDOW_SECONDS or point["range_s"][1] - moment > WINDOW_SECONDS + ANCHOR_EPSILON:
                raise ValueError(f"第 {number} 讲过长时段须核对并分列实际要点，不能复用单句 {point['anchor']}")
        for zone, base in matching_rows(row.get("channels"), expected["zones"], "zone", f"第 {number} 讲作者渠道区域"):
            label = f"第 {number} 讲 {zone['zone']}"
            valid_outcome = "不适用" if zone["zone"] == "screen" and expected["kind"] == "audio" else "已查"
            if (zone.get("range_s") != base["range_s"] or zone.get("outcome") != valid_outcome or
                    not substantive_note(zone.get("note")) or not isinstance(zone.get("findings"), list)):
                raise ValueError(f"需核对作者渠道区域并写明结果 {label}")
            for finding in zone["findings"]:
                location = finding.get("range_s") if isinstance(finding, dict) else None
                if (not isinstance(location, list) or len(location) != 2 or any(type(value) not in {int, float} for value in location)
                        or not base["range_s"][0] <= location[0] < location[1] <= base["range_s"][1]):
                    raise ValueError(f"作者渠道定位超出所查原片区域 {label}")
                validate_finding(finding, lambda cites: video_covers(cites, number, location), blocks, label)
        for finding, detection in matching_rows(row.get("candidates"), expected["candidates"], "id", f"第 {number} 讲作者候选"):
            location = detection["range_s"]
            validate_finding(finding, lambda cites: video_covers(cites, number, location), blocks,
                             finding["id"], value=detection.get("value", ""))

    documents = {row["source"]: row for row in scope["documents"]}
    for row, expected in matching_rows(coverage.get("documents"), scope["documents"], "id", "附件来源复核"):
        name = expected["source"]
        units = {unit["key"]: unit for unit in expected["units"]}
        for checked, unit in matching_rows(row.get("units"), expected["units"], "key", f"附件 {name} 页、表或档内成员"):
            label = f"{name} · {unit['key']}"
            validate_outcome(checked, label)
            match = lambda cites: document_covers(cites, name, unit)
            if checked["outcome"] == "正文已覆盖":
                anchor = checked.get("anchor")
                if not populated(anchor) or not any(anchor in text and match(cites) for text, cites in blocks):
                    raise ValueError(f"附件独立内容缺可见正文及同段原件出处 {label}")
            if not isinstance(checked.get("channels"), list):
                raise ValueError(f"附件需登记实际查到的作者渠道 {label}")
            for finding in checked["channels"]:
                validate_finding(finding, match, blocks, label)
        for finding, detection in matching_rows(row.get("candidates"), expected["candidates"], "id", f"附件 {name} 作者候选"):
            unit = units[detection["unit"]]
            validate_finding(finding, lambda cites: document_covers(cites, name, unit), blocks,
                             finding["id"], value=detection.get("value", ""))

    timings = {row["display_number"]: row["duration_s"] for row in scope["videos"]}
    claims = coverage_review_template({"videos": [], "documents": []}, body)["claims"]
    for actual, expected in matching_rows(coverage.get("claims"), claims, "id", "教材正文反查记录"):
        if any(actual.get(key) != expected[key] for key in ("text", "sources")) or not substantive_note(actual.get("note")):
            raise ValueError(f"需交代每段正文实际反查结论 {expected['id']}")
        text, cites = blocks[int(expected["id"][1:]) - 1]
        outcome = actual.get("outcome")
        if outcome == "原件已核":
            if not cites:
                raise ValueError(f"原件事实缺少段落出处 {expected['id']}")
        elif outcome == "编辑衔接" and not expected["sources"]:
            continue
        elif outcome == "整理示例" and any(word in text for word in ("演算示例", "练习", "假设", "示意")):
            pass
        elif outcome == "外部核验" and any(word in text for word in ("外部核验", "另行核实", "补充核验")):
            pass
        else:
            raise ValueError(f"正文反查须标明原件事实、衔接、示例或外部核验 {expected['id']}")
        for kind, identity, location in cites:
            if kind == "video":
                if identity not in timings or not 0 <= location[0] <= location[1] <= timings[identity] + .01:
                    raise ValueError(f"正文原片时间定位超出核实范围 {expected['id']}")
            elif kind == "unknown" or identity not in documents or not document_locator_valid(documents[identity], location):
                raise ValueError(f"正文附件定位无法在原页、工作表或档内成员反查 {identity} · {location}")
