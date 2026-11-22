"""Validate the editor's source review and publish the approved reading copy."""
import hashlib
from html import unescape
import json
from pathlib import Path
import re
from urllib.parse import unquote

from embedded_media import DIRECTORY as EMBEDDED_MEDIA_DIR
from project import ENTRY, RECOVERY_DIR, copy_file, local_child, put, read, recovery_files, sha, write
from reader_review import IMAGE, ITEMS, chapters, markdown_segments, snapshot, validate_cases
from publish import timecode
from source_coverage import NON_TEACHING, build_source_manifest, coverage_review_template, fingerprint as source_scope_digest
from source_coverage import validate_source_coverage
from transcript import timeline_paths, update_timeline, validate_proof


PARAGRAPH_SOURCE = re.compile(r"<!-- 段落出处 ([^\n]+?) -->")
VIDEO_RANGE = re.compile(r"第 (\d+) 讲 ([\d:.]+)(?:–([\d:.]+))?")


REVIEW = "验收记录.json"
TEMPLATE = "验收填写模板.json"
SUPPLEMENTS = "补充内容复核.json"
COMMENT = re.compile(r"<!--[\s\S]*?-->")
COLLAPSED = re.compile(
    r"^ {0,3}>[ \t]*\[![^\]\n]+\]-[^\n]*(?:\n[ \t]*>[^\n]*)*"
    r"|<details\b[^>]*>[\s\S]*?</details\s*>", re.MULTILINE | re.IGNORECASE
)
SPANS = {"教学", "原例", "说明"} | NON_TEACHING
POINTS = {"概念", "输入", "操作", "参数", "条件", "原例", "纠错", "结果", "画面", "说明"}
AUDIO_OUTCOMES = {"已补正文", "正文已涵盖", "非教学声音", "听辨缺口"}
DOCUMENT_USES = {"引用", "用作转写", "参考", "重复", "原样存档", "提取缺口"}
VISUAL_BASES = {"原片", "附件原图", "复用同一原画", "无可教学画面"}
CHANNEL_CHECKS = ("opening", "closing", "screen", "finding")


def figure_names(text):
    return {Path(unquote(link)).name for link in IMAGE.findall(text)}


def source_ranges(section, display_number):
    return [[timecode(left), timecode(right)] for comment in PARAGRAPH_SOURCE.findall(section)
            for number, left, right in VIDEO_RANGE.findall(comment)
            if int(number) == display_number and right and timecode(left) < timecode(right)]


def prepare_review(project, body, ordered, receipt):
    sections = chapters(body)
    if not sections:
        raise ValueError("教材需要二级标题，以便定位原课和跟做案例")
    manifest = read(project.ledger / "documents-summary.json")["records"]
    media = {row["lesson"]: row for row in read(project.ledger / "media-summary.json")["videos"]}
    evidence = {}

    def evidence_from(path):
        evidence[path.relative_to(project.state).as_posix()] = sha(path)
        return read(path)

    for name in ("intake.json", "inventory.json", "documents-summary.json", "media-summary.json"):
        evidence_from(project.ledger / name)
    evidence_from(project.work / "讲次顺序.json")
    episodes = []
    for display_number, lesson in enumerate(ordered, 1):
        video = media[lesson]
        meta = evidence_from(project.ledger / f"{lesson:02}.json")
        adopted = update_timeline(project, lesson)
        evidence_from(timeline_paths(project, lesson)[0])
        proofs = {proof["path"] for cue in adopted["cues"] for proof in cue.get("proofs", [])}
        for relative in sorted(proofs):
            file = local_child(project.work, relative)
            evidence[file.relative_to(project.state).as_posix()] = sha(file)
        scan = evidence_from(project.work / "媒体补查" / f"L{lesson:02}.json")
        signature = scan["signature"]
        cue_sha = hashlib.sha256(json.dumps(meta["cues"], ensure_ascii=False).encode("utf-8")).hexdigest()
        if not (video["sha256"] == meta["sha256"] == signature["raw_sha256"] and
                video["source"] == meta["source"] == scan["source"] and
                signature["cues_sha256"] == cue_sha and signature["prepare"] == meta["prepare_signature"] and
                video.get("recovery") == meta.get("recovery") and video.get("embedded") == meta.get("embedded")):
            raise ValueError(f"第 {display_number} 讲的画面或语音扫描过期，请再运行 prepare")
        extra = project.work / "媒体补查" / f"L{lesson:02}-复识.json"
        if extra.is_file() and read(extra)["signature"]["scan_sha256"] == sha(
            project.work / "媒体补查" / f"L{lesson:02}.json"
        ):
            evidence_from(extra)
        points_path = project.work / "逐段要点" / f"{lesson:02}.json"
        source_points = evidence_from(points_path) if points_path.is_file() else []
        candidates = [title for title, text in sections.items() if source_ranges(text, display_number)]
        named = [title for title in candidates if re.match(rf"^第\s*{display_number}\s*讲", title)]
        chapter = named[0] if len(named) == 1 else candidates[0] if len(candidates) == 1 else ""
        episodes.append({"lesson": lesson, "display_number": display_number,
                         "source": video["source"], "source_sha256": video["sha256"],
                         "duration_s": video["duration_s"], "chapter": chapter,
                         "watched": [], "key_steps": [], "visual_basis": "", "visual_note": "",
                         "channel_review": dict.fromkeys(CHANNEL_CHECKS, ""),
                         "source_points": [dict(row, anchor="", source_limit="") for row in source_points],
                         "audio_checks": [{"start": row["start"], "end": row["end"], "outcome": "", "note": ""}
                                          for row in scan["audio"]["uncovered_candidates"]]})
        if embedded := video.get("embedded"):
            episodes[-1]["embedded"] = embedded
            evidence[f"work/{embedded['work_file']}"] = video["sha256"]
        if video.get("kind") == "audio":
            episodes[-1]["kind"] = "audio"
        if recovery := video.get("recovery"):
            recovered, recovery_manifest, spec = recovery_files(
                project.work, recovery["manifest"], video["sha256"], recovery["manifest_sha256"])
            episodes[-1]["recovery"] = recovery
            evidence[recovered.relative_to(project.state).as_posix()] = spec["sha256"]
            evidence[recovery_manifest.relative_to(project.state).as_posix()] = recovery["manifest_sha256"]
    supplement_path = project.work / SUPPLEMENTS
    supplements = evidence_from(supplement_path) if supplement_path.is_file() else []
    source_scope, coverage_evidence = build_source_manifest(project)
    for name, digest in coverage_evidence.items():
        if name in evidence and evidence[name] != digest:
            raise ValueError(f"生成来源复核清单时证据已变动 {name}")
    evidence.update(coverage_evidence)
    return {
        "schema": 2, "snapshot": {**snapshot(project.preview, body), "evidence": evidence,
                                  "source_scope_sha256": source_scope_digest(source_scope)},
        "source_scope": source_scope,
        "source_coverage": coverage_review_template(source_scope, body),
        "episodes": episodes,
        "documents": [{"id": row["id"], "source": row["file"], "sha256": row["sha256"],
                       "use": "引用" if row["id"] in receipt["document_refs"] else "",
                       "inspected_at": "", "note": ""}
                      for row in manifest],
        "supplements": supplements,
        "follow_along": [{"goal": "", "chapter": "", "checks": dict.fromkeys(ITEMS, ""),
                          "sources": [], "figures": [], "visual_note": ""}],
    }


def clip_range(raw, duration):
    if not isinstance(raw, list) or len(raw) != 2 or any(type(t) not in (int, float) for t in raw):
        raise ValueError("原片区间使用起止秒数")
    start, end = raw
    if not 0 <= start < end <= duration + .05:
        raise ValueError(f"原片区间超出时间轴 {raw}")
    return start, end


def nearby_figures(text, anchor, *, source=None):
    text = COMMENT.sub(lambda match: match[0].replace("\n", " "), text)
    blocks = re.split(r"\n[ \t]*\n", text)
    visible = [COMMENT.sub("", block) for block in blocks]
    matches = [index for index, block in enumerate(visible) if anchor in block]
    if not matches:
        raise ValueError(f"关键步骤无法定位到教材的可见文字 {anchor}")
    index = matches[0]
    if source:
        display_number, start, end = source
        index = next((candidate for candidate in matches if any(
            left <= start + .05 and end <= right + .05
            for left, right in source_ranges(blocks[candidate], display_number)
        )), index)
    found = figure_names(visible[index])
    for following in visible[index + 1:]:
        if following.strip() and re.sub(IMAGE, "", following).strip():
            break
        found.update(figure_names(following))
    return found


def validate_points(actual, expected, text):
    display_number = expected["display_number"]
    points = actual.get("source_points", [])
    expected_points = expected["source_points"]
    if len(points) != len(expected_points):
        raise ValueError(f"第 {display_number} 讲需从原课逐段登记要点后逐项对应正文")
    spans = []
    for point, source in zip(points, expected_points):
        start, end = clip_range(point.get("range_s"), expected["duration_s"])
        if any(point.get(k) != source.get(k) for k in ("range_s", "kind", "source_note")) or (
            point.get("kind") not in POINTS or not point.get("source_note", "").strip()
        ):
            raise ValueError(f"第 {display_number} 讲要点与原课清单不符 {start:g}–{end:g}")
        anchor = point.get("anchor", "").strip()
        limit = point.get("source_limit", "").strip()
        if bool(anchor) == bool(limit):
            raise ValueError(f"第 {display_number} 讲要点需对应正文或具体原件缺口 {start:g}–{end:g}")
        if anchor and not any(anchor in COMMENT.sub("", block) and any(
            left <= start + .05 and end <= right + .05 for left, right in source_ranges(block, display_number)
        ) for block in re.split(r"\n[ \t]*\n", text)):
            raise ValueError(f"第 {display_number} 讲要点未在对应正文块找到隐蔽原片出处 {anchor}")
        spans.append((start, end))
    for part in actual["watched"]:
        left, right = part["range_s"]
        if part["kind"] not in NON_TEACHING and not any(start < right and end > left for start, end in spans):
            raise ValueError(f"第 {display_number} 讲在 {left:g}–{right:g} 秒缺来源要点")


def validate_episode(actual, expected, text, figures):
    source_lesson = expected["lesson"]
    display_number = expected["display_number"]
    duration = expected["duration_s"]
    for key in ("lesson", "display_number", "source", "source_sha256", "duration_s"):
        if actual.get(key) != expected[key]:
            raise ValueError(f"第 {display_number} 讲的原件身份或时长不一致")
    if actual.get("embedded") != expected.get("embedded"):
        raise ValueError(f"第 {display_number} 讲的内嵌附件和页码不一致")
    if actual.get("kind", "video") != expected.get("kind", "video"):
        raise ValueError(f"第 {display_number} 讲的原件媒体类型不一致")
    if recovery := expected.get("recovery"):
        visible = COMMENT.sub("", COLLAPSED.sub("", text))
        if actual.get("recovery") != recovery or not any(
            recovery["scope"] in part for is_code, part in markdown_segments(visible) if not is_code
        ):
            raise ValueError(f"第 {display_number} 讲需在正文交代原片恢复范围")
    heading = re.match(r"^第\s*(\d+)\s*讲", actual["chapter"])
    if heading and int(heading[1]) != display_number:
        raise ValueError(f"第 {display_number} 讲的标题讲次与阅读顺序不一致")
    citations = source_ranges(text, display_number)
    if not citations:
        raise ValueError(f"第 {display_number} 讲的对应章节缺少本集原片时间段")

    cursor = 0.0
    previous_start = 0.0
    for part in actual.get("watched", []):
        start, end = clip_range(part.get("range_s"), duration)
        if start < previous_start or start > cursor + .05 or part.get("kind") not in SPANS or not part.get("note", "").strip():
            raise ValueError(f"第 {display_number} 讲在 {cursor:g} 秒后缺少连续看片记录或本段观察")
        previous_start = start
        cursor = max(cursor, end)
    if cursor < duration - .05 or not actual.get("watched"):
        raise ValueError(f"第 {display_number} 讲的看片记录需覆盖首尾")
    if "channel_review" in expected:
        audit = actual.get("channel_review", {})
        if not isinstance(audit, dict) or not all(
            isinstance(audit.get(key), str) and audit[key].strip() for key in CHANNEL_CHECKS
        ):
            raise ValueError(f"第 {display_number} 讲需要记录片头、片尾、画面署名及作者渠道核查")
    if any(row["kind"] == "教学" for row in actual["watched"]) and not actual.get("key_steps"):
        raise ValueError(f"第 {display_number} 讲需要记录教学动作、对应文字和配图选择")
    validate_points(actual, expected, text)

    present = figure_names(text)
    basis = actual.get("visual_basis")
    if basis not in VISUAL_BASES or not actual.get("visual_note", "").strip():
        raise ValueError(f"第 {display_number} 讲需要确认原片、复用原画、附件原图或无可教学画面的依据")
    if expected.get("kind") == "audio" and basis == "原片":
        raise ValueError(f"第 {display_number} 讲为独立音频，配图须使用相应附件原图或注明来源的示意图")
    if basis == "原片" and not any(figures[name].get("lesson") == source_lesson for name in present):
        raise ValueError(f"第 {display_number} 讲的正文缺本集原片配图")
    if basis == "附件原图" and not any("document" in figures[name] for name in present):
        raise ValueError(f"第 {display_number} 讲的正文缺声明的附件配图")

    native_steps = False
    for step in actual.get("key_steps", []):
        start, end = clip_range(step.get("range_s"), duration)
        if not any(left <= start + .05 and right + .05 >= end for left, right in citations):
            raise ValueError(f"第 {display_number} 讲在 {start:g}–{end:g} 秒的关键步骤需要对应正文出处")
        anchor = step.get("anchor", "").strip()
        if not anchor:
            raise ValueError(f"第 {display_number} 讲缺关键步骤正文定位")
        chosen = step.get("figures", [])
        nearby = nearby_figures(text, anchor, source=(display_number, start, end))
        if any(name not in nearby for name in chosen) or (not chosen and not step.get("visual_note", "").strip()):
            raise ValueError(f"第 {display_number} 讲的关键图要紧接对应步骤；无图时记录依据 {anchor}")
        for name in chosen:
            asset = figures[name]
            diagram = asset.get("diagram")
            if diagram:
                left, right = diagram["range_s"]
                if diagram["lesson"] != source_lesson or left >= end or right <= start:
                    raise ValueError(f"第 {display_number} 讲整理示意图与该步骤的原课时间不重合 {name}")
            if "lesson" in asset:
                if asset["lesson"] != source_lesson or not start - .05 <= asset["pts"] <= end + .05:
                    raise ValueError(f"第 {display_number} 讲的步骤图片时间与原片区间不一致 {name}")
                native_steps = True
    if basis == "原片" and actual.get("key_steps") and not native_steps:
        raise ValueError(f"第 {display_number} 讲的原片图应放在对应的教学步骤旁")

    checks = actual.get("audio_checks", [])
    if [(row.get("start"), row.get("end")) for row in checks] != [
        (row["start"], row["end"]) for row in expected["audio_checks"]
    ] or any(row.get("outcome") not in AUDIO_OUTCOMES or not row.get("note", "").strip() for row in checks):
        raise ValueError(f"第 {display_number} 讲的语音候选需要逐项核实并记录处理")


def validate_review_proofs(project, review):
    for episode in review.get("episodes", []):
        for proof in episode.get("checked_sources", []):
            validate_proof(project.work, proof)


def validate_shared_visual(episode, text, figures, earlier_chapters):
    shared = episode.get("shared_visual")
    if episode["visual_basis"] != "复用同一原画":
        if shared is not None:
            raise ValueError("只有复用同一原画时填写 shared_visual")
        return
    display = episode["display_number"]
    if episode.get("kind") == "audio" or not isinstance(shared, dict):
        raise ValueError(f"第 {display} 讲复用原画须登记本集原片对照和前一处读者配图")
    chapter, name, proof = (shared.get(field) for field in ("chapter", "figure", "proof"))
    if not isinstance(chapter, str) or not isinstance(name, str):
        raise ValueError(f"第 {display} 讲复用原画需指定已经验收的章节和原图")
    earlier = earlier_chapters.get(chapter)
    if (earlier is None or name not in earlier["figures"] or
            figures.get(name, {}).get("lesson") != earlier["lesson"]):
        raise ValueError(f"第 {display} 讲复用原画须指向前文已插入的本课原图")
    if (not isinstance(proof, dict) or not isinstance(proof.get("path"), str) or not proof["path"].strip() or
            not isinstance(proof.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", proof["sha256"]) or
            proof not in episode.get("checked_sources", [])):
        raise ValueError(f"第 {display} 讲复用原画缺本集指纹化的原片对照")
    visible = COMMENT.sub("", COLLAPSED.sub("", text))
    target = r"\[\[#"+re.escape(chapter)+r"(?:\|[^\]\n]+)?\]\]"
    if not any(re.search(target, part) for is_code, part in markdown_segments(visible) if not is_code):
        raise ValueError(f"第 {display} 讲正文缺前文复用原画的读者定位")


def validate_supplements(body, registered, reviewed):
    """A supplemental item must be readable in the same block as its hidden evidence."""
    if not isinstance(registered, list) or reviewed != registered:
        raise ValueError("补充内容验收需与当前登记一致")
    if not registered:
        return
    blocks = [
        (COMMENT.sub("", block), [f" · {unescape(note)} · " for note in PARAGRAPH_SOURCE.findall(block)])
        for is_code, part in markdown_segments(body) if not is_code
        for block in re.split(r"\n[ \t]*\n", COLLAPSED.sub("", part))
    ]
    for item in registered:
        if not isinstance(item, dict):
            raise ValueError("补充内容需登记可见短句及原件位置")
        anchor, sources = item.get("anchor"), item.get("sources")
        if (not isinstance(anchor, str) or not anchor.strip() or not isinstance(sources, list)
                or not sources or any(not isinstance(s, str) or not s.strip() for s in sources)):
            raise ValueError("补充内容需登记可见短句及原件位置")
        if not any(anchor in visible and all(any(f" · {source} · " in note for note in notes) for source in sources)
                   for visible, notes in blocks):
            raise ValueError(f"补充内容缺可见正文或相邻隐藏出处 {anchor}")


def validate_review(body, figures, review, template):
    if review.get("schema") != template.get("schema", 1) or review.get("snapshot") != template["snapshot"]:
        raise ValueError("验收记录与当前预览、配图或证据版本不一致")
    sections = chapters(body)
    files = {Path(info["name"]).name: info for info in figures}
    episodes = review.get("episodes", [])
    if [row.get("lesson") for row in episodes] != [row["lesson"] for row in template["episodes"]]:
        raise ValueError("每个原视频都需要按教学顺序逐集验收")
    headings = [row.get("chapter") for row in episodes]
    if len(set(headings)) != len(headings) or headings != [title for title in sections if title in headings]:
        raise ValueError("逐集验收须使用对应的正文二级标题，并保持原课顺序")
    earlier_chapters = {}
    for actual, expected in zip(episodes, template["episodes"]):
        chapter = actual["chapter"]
        section = sections[chapter]
        validate_episode(actual, expected, section, files)
        validate_shared_visual(actual, section, files, earlier_chapters)
        earlier_chapters[chapter] = {"lesson": actual["lesson"], "figures": figure_names(section)}
    expected_docs = {row["id"]: row for row in template["documents"]}
    documents = review.get("documents", [])
    if len(documents) != len(expected_docs) or {row.get("id") for row in documents} != set(expected_docs):
        raise ValueError("附件须逐件登记核对结果和用途")
    for row in documents:
        expected = expected_docs[row["id"]]
        if "inspected_at" in expected and not (
            isinstance(row.get("inspected_at"), str) and row["inspected_at"].strip()
        ):
            raise ValueError(f"附件须登记实际检查的页、表、档内成员或原件部位 {expected['source']}")
        if any(row.get(key) != expected[key] for key in ("source", "sha256")) or (
            row.get("use") not in DOCUMENT_USES or not row.get("note", "").strip()
            or (row["use"] == "引用") != (expected["use"] == "引用")
        ):
            raise ValueError(f"附件出处、用途或核对说明不符 {expected['source']}")

    validate_supplements(body, template.get("supplements", []), review.get("supplements", []))

    cases = review.get("follow_along")
    validate_cases(body, cases)
    for case in cases:
        text = sections[case["chapter"]]
        visible = COMMENT.sub("", text)
        if any(case["checks"][key] not in visible for key in ITEMS):
            raise ValueError(f"跟做内容需要写在可见正文 {case['goal']}")
        hidden = " ".join(map(unescape, PARAGRAPH_SOURCE.findall(text)))
        if any(source not in hidden for source in case["sources"]):
            raise ValueError(f"跟做案例的出处需要定位到同节的隐藏注释 {case['goal']}")
    if "source_scope" in template:
        if review.get("source_scope") != template["source_scope"]:
            raise ValueError("验收记录的来源范围与当前原件清单不一致，请从预览模板更新")
        validate_source_coverage(body, template["source_scope"], review)


def check_render(render, digest):
    checks = render.get("checks", [])
    if len(checks) != 1:
        raise ValueError("需要这一篇教材的 Obsidian 阅读回执")
    view = checks[0]
    if not (view["document_sha256"] == digest and view["mode"] == "preview" and view["reachedBottom"]
            and not view["horizontalOverflow"] and not view["visibleCitations"]
            and "原课与配套资料" in view["closedAppendices"]):
        raise ValueError("Obsidian 回执与当前教材或阅读要求不一致")


def publication_receipt(project):
    digest = sha(project.entry)
    path = local_child(project.ledger, f"publications/{digest}.json")
    if not path.is_file():
        raise ValueError("现有正文没有本流程的发布回执，请先保留正文编辑并对照工作稿")
    record = read(path)
    if record["outputs"].get(ENTRY.as_posix()) != digest:
        raise ValueError("发布回执与教材版本不一致")
    return record


def verify_publication(project, *, raw_verified=False):
    from media_inventory import verify_course_media

    sources = read(project.intake)["sources"] if raw_verified else project.validate_raw()
    record = publication_receipt(project)
    review = record["review"]
    inputs = review["snapshot"]
    verify_course_media(project, sources, review["episodes"], require_current=False,
                        manifest_sha256=inputs["evidence"].get("ledger/media-inventory.json"))
    entry = project.entry
    for name, digest in record["outputs"].items():
        file = entry if name == ENTRY.as_posix() else local_child(project.folder, name)
        if sha(file) != digest:
            raise ValueError(f"发布件和验收回执不一致 {name}")
    if set(entry.parent.rglob("*.md")) != {entry}:
        raise ValueError("课件目录需要且仅保留一篇 Markdown")
    digest = record["outputs"][ENTRY.as_posix()]
    if (inputs["text_sha256"] != digest or hashlib.sha256(record["body"].encode("utf-8")).hexdigest() != digest
            or inputs["evidence"]["ledger/intake.json"] != sha(project.intake)
            or snapshot(project.folder, entry.read_text(encoding="utf-8"))["images"] != inputs["images"]):
        raise ValueError("原件、配图或正文和已审核版本不一致")
    for relative, fingerprint in inputs["evidence"].items():
        if relative.startswith((f"work/{RECOVERY_DIR}/", f"work/{EMBEDDED_MEDIA_DIR}/")):
            if sha(local_child(project.state, relative)) != fingerprint:
                raise ValueError(f"验收使用的媒体资料已变动 {relative}")
    if review.get("schema") == 2:
        scope = review.get("source_scope")
        if (not scope or inputs.get("source_scope_sha256") != source_scope_digest(scope)
                or scope.get("intake_sha256") != inputs["evidence"]["ledger/intake.json"]):
            raise ValueError("已发布版本的来源覆盖清单与验收回执不一致")
        validate_source_coverage(record["body"], scope, review)
    check_render(record["obsidian"], digest)
    return record


def commit(project, body, figures, review, render):
    if review.get("schema") != 2 or not review.get("source_scope") or not review.get("source_coverage"):
        raise ValueError("新发布版本需要当前来源覆盖复核")
    digest = sha(project.preview / ENTRY)
    check_render(render, digest)
    if project.entry.exists():
        publication_receipt(project)
    if set(project.entry.parent.rglob("*.md")) - {project.entry}:
        raise ValueError("课件目录中有未登记的其他 Markdown，请保留并检查它们")
    files = {info["name"]: sha(local_child(project.preview, info["name"])) for info in figures}
    for relative, image_sha in files.items():
        target = local_child(project.folder, relative)
        if target.exists() and sha(target) != image_sha:
            raise ValueError(f"已有发布原图同名但不同，请用新图名 {relative}")
    for relative, image_sha in files.items():
        copy_file(local_child(project.preview, relative), local_child(project.folder, relative), image_sha)
    record = {"schema": 1, "body": body, "outputs": {ENTRY.as_posix(): digest, **files},
              "review": review, "obsidian": render}
    put(local_child(project.ledger, f"publications/{digest}.json"), record)
    # Replacing the note is the commit point. Earlier notes keep their images and receipts.
    write(project.entry, body)
