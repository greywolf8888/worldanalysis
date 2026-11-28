"""Explicit publication scope; absent materials never suppress a received-source check."""

from pathlib import Path
import re

from project import ReceivedCoverageError, local_child, read
from source_coverage import (CLAIMS_VERSION, SCOPE_BLOCK, SCOPE_END, SCOPE_START, citation_syntax, cite_tokens,
                             claim_blocks, document_locator_valid, intersects, substantive_note,
                             unit_matches, video_covers)


DECLARATION = "范围验收.json"
SCHEMA = 1
STATES = {"完整验收", "配套待补", "核心待补"}
GAP_FIELDS = {"item", "kind", "source", "impact", "obtain"}
TEACHING_FIELDS = {"source", "anchor", "observation"}
RESOLUTION_FIELDS = {"item", "source", "resolution", "note"}
RESOLUTIONS = {"已取得并核实", "经复核原缺件不成立", "经复核改为非核心"}
SCOPE_FIELDS = ("scope_review_schema", "scope_status", "scope_gaps", "scope_teaching", "scope_resolutions",
                "scope_document_candidates", "scope_prior")
POINTER_SUFFIXES = {".url", ".lnk", ".webloc", ".website"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
URL = re.compile(r"https?://\S+|(?:www\.|(?:pan|yun)\.baidu\.|[\w-]+\.feishu\.)\S+", re.I)
POINTER_WORDS = re.compile(r"提取码|访问码|分享码|密码|网盘|飞书|链接|网址|入口|请访问|请下载|账号登录|扫码|复制到浏览器|复制到客户端")
POINTER_GUIDANCE = re.compile(r"^(?:课程|资源|资料|下载|分享|网盘|飞书)?(?:链接|网址|入口|提取码|访问码|分享码|密码)\s*[:：]|请访问|请下载|扫码|复制到浏览器|复制到客户端|账号登录|提取码\s*[:：]|访问码\s*[:：]|分享码\s*[:：]")
TEACHING_WORDS = re.compile(r"步骤|设置|参数|示例|案例|操作|选择|输入|输出|验证|检查|保存|计算|导出|练习|配置")


def single_line(value):
    return substantive_note(value) and not any(char in value for char in "\r\n<>\x00")


def validate_declaration(declaration):
    if not isinstance(declaration, dict) or declaration.get("scope_status") not in STATES:
        raise ValueError("范围验收须明确为完整验收、配套待补或核心待补")
    gaps = declaration.get("scope_gaps")
    if not isinstance(gaps, list) or any(
        not isinstance(gap, dict) or set(gap) != GAP_FIELDS or gap.get("kind") not in {"核心", "非核心"}
        or any(not single_line(gap.get(field)) for field in GAP_FIELDS - {"kind"})
        for gap in gaps
    ):
        raise ValueError("未到手缺件须逐项登记名称、核心归属、原件定位、影响与取得条件")
    if len({gap["item"].strip().casefold() for gap in gaps}) != len(gaps):
        raise ValueError("范围验收缺件重复，请合并为真实未收到的独立条目")
    expected = "核心待补" if any(gap["kind"] == "核心" for gap in gaps) else "配套待补" if gaps else "完整验收"
    if declaration["scope_status"] != expected:
        raise ValueError(f"范围验收状态与实际缺件归属冲突，应为 {expected}")
    resolutions = declaration.get("scope_resolutions", [])
    if not isinstance(resolutions, list) or any(
        not isinstance(row, dict) or set(row) != RESOLUTION_FIELDS or row.get("resolution") not in RESOLUTIONS
        or any(not single_line(row.get(field)) for field in RESOLUTION_FIELDS - {"resolution"}) for row in resolutions
    ) or len({row["item"].strip().casefold() for row in resolutions}) != len(resolutions):
        raise ValueError("此前缺件的处置须逐项登记名称、当前原件位置、处置类别和可核说明")
    remaining = {row["item"].strip().casefold(): row["kind"] for row in gaps}
    for row in resolutions:
        kind = remaining.get(row["item"].strip().casefold())
        if (row["resolution"] == "经复核改为非核心" and kind != "非核心"
                or row["resolution"] != "经复核改为非核心" and kind is not None):
            raise ValueError("旧缺件处置与此次实际未收到缺件状态冲突")
    teaching = declaration.get("scope_teaching")
    if not isinstance(teaching, list) or not teaching or any(
        not isinstance(row, dict) or set(row) != TEACHING_FIELDS
        or any(not single_line(row.get(field)) for field in TEACHING_FIELDS)
        for row in teaching
    ):
        raise ValueError("必须登记已收到且确有教学内容的真实原件位置、正文要点及实际观察，单独指针不能出版")


def declaration_from_project(project):
    path = project.work / DECLARATION
    if not path.is_file():
        return None
    declaration = read(path)
    validate_declaration(declaration)
    return declaration


def md_text(text):
    # Render literal labels without treating a filename or quoted value as Markdown syntax.
    return re.sub(r"([\\`*_\[\]{}|])", r"\\\1", text)


def reader_source(source):
    """A reader-facing locator, distinct from the private paragraph citation syntax."""
    if source.startswith("第 ") and " 讲 " in source:
        lesson, time = source.split(" 讲 ", 1)
        return md_text(f"{lesson} 讲（{time.replace('–', ' 至 ')}）")
    return md_text(source.replace(" · ", "；定位："))


def legacy_scope_summary(declaration):
    status = declaration["scope_status"]
    lines = [SCOPE_START, f"> **验收范围：{status}**"]
    if declaration["scope_gaps"]:
        lines.append("> 本篇已经核对当前收到的实质教学材料，以下未收到的内容不在本版验收范围。")
        for gap in declaration["scope_gaps"]:
            lines.extend([f"> - {gap['kind']}缺件：{md_text(gap['item'])}",
                          f">   - 原件证据：{reader_source(gap['source'])}",
                          f">   - 影响：{md_text(gap['impact'])}",
                          f">   - 获取条件：{md_text(gap['obtain'])}"])
    else:
        lines.append("> 当前已核明的课程原件已完成范围验收，无已知未收到的材料。")
    for row in declaration.get("scope_resolutions", []):
        lines.extend([f"> - 此前缺件处置：{md_text(row['item'])} · {row['resolution']}",
                      f">   - 当前原件证据：{reader_source(row['source'])}",
                      f">   - 复核说明：{md_text(row['note'])}"])
    lines.append(SCOPE_END)
    return "\n".join(lines)


def scope_summary(declaration):
    validate_declaration(declaration)
    lines = [SCOPE_START]
    if declaration["scope_gaps"]:
        lines.append("> 可先学习正文中已收录的部分。以下资料目前暂缺：")
        for gap in declaration["scope_gaps"]:
            lines.append(f"> - **{md_text(gap['item'])}**。{md_text(gap['impact'])}")
    lines.append(SCOPE_END)
    return "\n".join(lines)


def add_scope_summary(body, declaration):
    if SCOPE_START in body or SCOPE_END in body:
        raise ValueError("验收范围说明由出版程序生成，工作稿不得自带范围控制标记")
    if declaration is None:
        return body
    heading, _, rest = body.partition("\n")
    return heading + "\n\n" + scope_summary(declaration) + "\n\n" + rest.lstrip("\n")


def validate_visible_summary(body, declaration):
    summaries = SCOPE_BLOCK.findall(body)
    if (body.count(SCOPE_START) != 1 or body.count(SCOPE_END) != 1
            or len(summaries) != 1
            or summaries[0] not in (scope_summary(declaration), legacy_scope_summary(declaration))):
        raise ValueError("正文缺少同版范围状态或明示缺件边界，须重新预览")


def has_instructional_text(blocks):
    lines = [line.strip() for block in blocks for line in str(block.get("text") or "").splitlines() if line.strip()]
    has_pointer = any(URL.search(line) or POINTER_WORDS.search(line) for line in lines)
    for line in lines:
        text = URL.sub("", line).strip("[]()：: -|#*\t")
        if not text or POINTER_GUIDANCE.search(text) and not TEACHING_WORDS.search(text):
            continue
        # A genuine abstract/theoretical document can also contain references. A URL is
        # therefore not a requirement for operational verbs in its substantive sentences.
        if (not has_pointer or TEACHING_WORDS.search(text)
                or len(re.findall(r"[\u3400-\u9fff]", text)) >= 10
                or len(re.findall(r"\b[A-Za-z]+\b", text)) >= 6):
            return True
    return False


def document_teaching_candidates(project):
    """Recognize local material candidates, rejecting common pointer-only documents mechanically."""
    candidates = []
    for row in read(project.ledger / "documents-summary.json")["records"]:
        suffix = Path(row["file"]).suffix.lower()
        if suffix in POINTER_SUFFIXES or not row.get("result"):
            continue
        result = read(local_child(project.work, row["result"]))
        blocks = result.get("blocks", [])
        metadata = result.get("metadata", {})
        visual = (suffix in IMAGE_SUFFIXES or bool(metadata.get("word_pictures"))
                  or any(block.get("kind") in {"image", "image_occurrence"} for block in blocks))
        if has_instructional_text(blocks) or visual:
            candidates.append(row["id"])
    return candidates


def source_token(scope, source):
    tokens = list(cite_tokens(source, citation_syntax(scope["documents"])))
    if len(tokens) != 1:
        raise ValueError(f"范围说明需绑定单个真实原件位置 {source}")
    kind, identity, location = tokens[0]
    if kind == "video":
        match = next((r for r in scope["videos"] if r["display_number"] == identity), None)
        if match and 0 <= location[0] <= location[1] <= match["duration_s"] + .01:
            return kind, match, location
    elif kind == "document":
        match = next((r for r in scope["documents"] if r["source"] == identity), None)
        if match and document_locator_valid(match, location):
            return kind, match, location
    raise ValueError(f"范围说明没有可核已收到原件位置 {source}")


def validate_scope_review(body, review):
    if review.get("scope_review_schema") != SCHEMA or any(field not in review for field in SCOPE_FIELDS):
        raise ValueError(f"新出版须先填写 {DECLARATION}，明确已收到范围和未收到缺件")
    validate_declaration(review)
    validate_visible_summary(body, review)
    scope = review["source_scope"]
    received = {str(row["source"]).replace("\\", "/").casefold() for row in [*scope["videos"], *scope["documents"]]}
    received.update(Path(name).name for name in list(received))
    for document in scope["documents"]:
        members = [str(unit["value"]).replace("\\", "/").casefold()
                   for unit in document["units"] if unit["kind"] == "member"]
        received.update(members)
        received.update(Path(name).name for name in members)
    for gap in review["scope_gaps"]:
        item = gap["item"].replace("\\", "/").casefold()
        if item in received:
            raise ReceivedCoverageError("received_reported_missing", gap["item"],
                                        f"已收到的原件不能重报为外部未收到缺件 {gap['item']}")
        source_token(scope, gap["source"])
    current = {row["item"].strip().casefold(): row for row in review["scope_gaps"]}
    resolutions = {row["item"].strip().casefold(): row for row in review.get("scope_resolutions", [])}
    for row in resolutions.values():
        source_token(scope, row["source"])
    prior = review.get("scope_prior")
    if prior is not None:
        if (not isinstance(prior, dict) or not isinstance(prior.get("publication_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", prior["publication_sha256"])
                or not isinstance(prior.get("gaps"), list) or any(not isinstance(row, dict)
                    or set(row) != GAP_FIELDS or row.get("kind") not in {"核心", "非核心"}
                    or any(not single_line(row.get(field)) for field in GAP_FIELDS - {"kind"})
                    for row in prior["gaps"])):
            raise ValueError("此前正式版已签收缺件范围快照不完整")
        for row in prior["gaps"]:
            key = row["item"].strip().casefold()
            if key in current and not (row["kind"] == "核心" and current[key]["kind"] == "非核心"):
                continue
            if key not in resolutions:
                raise ValueError(f"此前明确缺件不得静默删除或降级，请登记处置 {row['item']}")
    coverage = review["source_coverage"]
    doc_candidates = review.get("scope_document_candidates")
    if not isinstance(doc_candidates, list):
        raise ValueError("必须从原始附件取材结果判定实际课程内容，不得仅使用链接指针")
    syntax = citation_syntax(scope["documents"])
    blocks = [(text, [token for note in notes for token in cite_tokens(note, syntax)])
              for text, notes in claim_blocks(body)]
    episodes = {row["lesson"]: row for row in review["episodes"]}
    document_reviews = {row["id"]: row for row in coverage["documents"]}
    for proof in review["scope_teaching"]:
        kind, source, location = source_token(scope, proof["source"])
        if kind == "video":
            episode = episodes[source["lesson"]]
            number = source["display_number"]
            points = [point for point in episode.get("source_points", []) if point.get("anchor") and
                      point.get("kind") != "说明" and intersects(point["range_s"], location)]
            good = any(proof["anchor"] in text and video_covers(cites, number, location)
                       and any(point["anchor"] in text for point in points) for text, cites in blocks)
        else:
            checked = document_reviews[source["id"]]["units"]
            units = {row["key"]: row for row in source["units"]}
            good = source["id"] in doc_candidates and any(
                row.get("outcome") == "正文已覆盖" and unit_matches(units[row["key"]], location)
                and any(proof["anchor"] in text and row.get("anchor") and row["anchor"] in text
                        and ("document", source["source"], location) in cites for text, cites in blocks)
                for row in checked)
        if not good:
            raise ValueError(f"当前范围未证明该项实际教学原件和可见正文的对应关系（单独指针不得出版） {proof['source']}")


def publication_scope(record):
    """Read new explicit scope or return an unclassified historical edition."""
    review = record.get("review", {})
    if not isinstance(review, dict):
        raise ValueError("出版回执缺少可核范围复审结构")
    if not any(field in record or field in review for field in ("scope_status", "scope_gaps", "scope_review_schema", "scope_resolutions")):
        return {"scope_status": None, "scope_gaps": [], "scope_resolutions": []}
    validate_declaration(review)
    validate_visible_summary(record["body"], review)
    if (review.get("schema") != 2 or review.get("scope_review_schema") != SCHEMA
            or not isinstance(review.get("source_coverage"), dict)
            or review["source_coverage"].get("claims_version") != CLAIMS_VERSION
            or any(field not in review for field in SCOPE_FIELDS)
            or any(field not in review or record.get(field) != review[field] for field in ("scope_status", "scope_gaps", "scope_resolutions"))):
        raise ValueError("出版回执中的范围状态或未到手缺件与同版验收不一致")
    from independent_facts import validate_independent_receipt

    validate_independent_receipt(record["body"], review, review.get("independent_fact_review"))
    return {field: record[field] for field in ("scope_status", "scope_gaps", "scope_resolutions")}
