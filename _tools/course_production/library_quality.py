"""Inventory reader-facing course materials and stage evidence-bound companion files."""
from argparse import ArgumentParser
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
from runpy import run_path
from sys import path as module_path
from urllib.parse import quote, unquote, urlsplit
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from project import copy_file, local_child, put, read, sha, write
from reader_review import markdown_segments


CHECKOUT = Path(__file__).resolve().parents[2]
WORKSPACE = CHECKOUT.parent.parent if CHECKOUT.parent.name == ".runtime" else CHECKOUT
PATH_AUTHORITY = WORKSPACE / "_ingest/wangzjk-to-pan/v4-review/workspace_paths.py"
REGISTRY = Path("整理结果/.course-dispatch/分类索引.json")
RUNTIME = Path(".runtime")
COMPANIONS = "配套资料"
MATERIALS = {".md", ".txt", ".doc", ".docx", ".xlsx", ".xls", ".csv", ".ppt", ".pptx",
             ".pdf", ".json", ".yaml", ".yml", ".js", ".py", ".ps1", ".sh", ".xmind"}
PICTURES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".avif"}
REUSE_TERMS = re.compile(r"提示词|规范|模板|脚本|分镜|配置")
AD_CANDIDATE = re.compile(r"重要的.{0,4}说明书|此处获取|群码")
INTERNAL_COPY = re.compile(r"验收范围[:：]|非核心缺件[:：]|原件证据[:：]|获取条件[:：]")
LINK = re.compile(r"(!?)\[[^\]\n]*\]\((<[^>\n]+>|[^)\n]+)\)")
WIKILINK = re.compile(r"!\[\[([^|\]\n]+)(?:\|[^\]\n]+)?\]\]")
COMMENT = re.compile(r"<!--[\s\S]*?-->")
DIGEST = re.compile(r"[a-f0-9]{64}")


def private_output(workspace, raw, *, history_version=None):
    target = Path(raw).absolute()
    root = workspace / RUNTIME
    if history_version is not None:
        if PATH_AUTHORITY.resolve(strict=True) != PATH_AUTHORITY or not PATH_AUTHORITY.is_file():
            raise ValueError("历史记录的保存位置无法核实")
        root = run_path(str(PATH_AUTHORITY))["job_target"]("history_backup", workspace=workspace,
                                                            version_id=history_version)
    if not target.is_relative_to(root) or target == root:
        raise ValueError("审核产物须写入本任务已核对的保存目录")
    if CHECKOUT != workspace and target.is_relative_to(CHECKOUT):
        raise ValueError("审核产物须与正式代码工作树分开")
    return local_child(workspace, target.relative_to(workspace))


def bound_path(workspace, raw, *, area):
    root = workspace / area
    absolute = Path(raw)
    return local_child(root, absolute.relative_to(root))


def registered_batches(workspace):
    registry = read(local_child(workspace, REGISTRY))
    if registry.get("schema") != 1 or Path(registry["root"]) != workspace / "分析目标下载地址":
        raise ValueError("分类索引不属于当前正式输入目录")
    for category, binding in sorted(registry["bindings"].items()):
        manifest = bound_path(workspace, binding["manifest"], area="整理结果")
        queue = read(manifest)
        source = bound_path(workspace, binding["source"], area="分析目标下载地址")
        vault = bound_path(workspace, binding["vault"], area="分析目标下载地址")
        if (binding["category"] != category
                or Path(queue["source"]) != source or Path(queue["vault"]) != vault):
            raise ValueError("分类的正式批次、原件目录和教材目录对应不上")
        yield category, manifest, queue, source, vault


def registered_publication(row, workspace, vault):
    if row["status"] == "同源复用":
        return None, None, "same_source_reuse"
    publication = row.get("publication") or {}
    if not publication.get("entry"):
        return None, None, "unpublished"
    try:
        entry = local_child(vault, Path(publication["entry"]).relative_to(vault))
        if not entry.is_file():
            return entry, None, "missing"
        observed = sha(entry)
        if observed == publication.get("sha256"):
            return entry, observed, "same_registered_bytes"
        package = bound_path(workspace, row["package"], area="整理结果")
        from vault_dates import same_edition

        edition = same_edition(package, vault, {entry.relative_to(vault).as_posix(): observed})
        if edition and edition.get("preservation"):
            return entry, observed, "verified_independent_derivative"
        return entry, observed, "different_bytes"
    except (OSError, ValueError, TypeError, KeyError):
        return None, None, "unreadable"


def local_note_links(entry, vault, text):
    prose = "\n".join(COMMENT.sub("", segment) for is_code, segment in markdown_segments(text) if not is_code)
    references = [(image == "!", raw, entry.parent) for image, raw in LINK.findall(prose)]
    references.extend((True, raw, vault) for raw in WIKILINK.findall(prose))
    for image, raw, parent in references:
        raw = raw.strip().removeprefix("<").removesuffix(">")
        url = urlsplit(raw.replace("\\", "/"))
        if not url.path or url.scheme or url.netloc:
            continue
        target = (parent / unquote(url.path)).resolve()
        if target.is_relative_to(entry.parent.parent):
            relative = target.relative_to(entry.parent.parent)
            top = relative.parts[0] if relative.parts else None
            state = ("source_dependency" if top == "原始资料"
                     else "companion" if top == COMPANIONS else "inside_package")
            yield relative.as_posix(), state if target.is_file() else "missing", image
        else:
            yield raw, "outside_package", image


def body_image_links(entry, vault, text):
    teaching = text.partition("\n> [!info]- 原课与配套资料")[0]
    visual_links = [(name, inline) for name, _, inline in local_note_links(entry, vault, teaching)
                    if Path(name).suffix.casefold() in PICTURES]
    inline = {name for name, embedded in visual_links if embedded}
    linked = {name for name, embedded in visual_links if not embedded}
    return {"embedded_images": len(inline), "linked_images_to_review": sorted(linked - inline)}


def audit_row(workspace, category, row, source, vault):
    entry, digest, edition = registered_publication(row, workspace, vault)
    source_dir = local_child(source, row["name"])
    intake = None
    if row.get("package"):
        path = bound_path(workspace, row["package"], area="整理结果")
        intake = local_child(path, ".course/ledger/intake.json").is_file()
    result = {"category": category, "project_id": row["id"], "sequence": row["sequence"],
              "local_project": row["name"], "batch_status": row["status"], "scope_status": row.get("scope_status"),
              "source_present": source_dir.is_dir(), "registered_file_count": len(row["metadata"]),
              "has_intake": intake, "edition": edition, "entry": str(entry) if entry else None,
              "observed_note_sha256": digest}
    if edition not in ("same_registered_bytes", "verified_independent_derivative"):
        return result
    text = entry.read_text("utf-8-sig")
    references = list(local_note_links(entry, vault, text))
    source_links = {link for link, state, _ in references if state == "source_dependency"}
    companion = {link for link, state, _ in references if state == "companion"}
    result.update(issues=[{"type": "image" if image else "link", "path": link, "state": state}
                          for link, state, image in references if state in ("missing", "outside_package")],
                  source_link_count=len(source_links), companion_link_count=len(companion),
                  visible_internal_label=bool(INTERNAL_COPY.search(COMMENT.sub("", text))),
                  **body_image_links(entry, vault, text))
    candidates = [file["file"] for file in row["metadata"] if Path(file["file"]).suffix.lower() in MATERIALS]
    result["retention_candidates"] = [
        {"path": name, "source_link_only": f"原始资料/{name}" in source_links,
         "contains_reuse_term": bool(REUSE_TERMS.search(name))}
        for name in candidates
    ]
    return result


def audit(workspace, *, offset=0, limit=None):
    result, failures = [], []
    ad_candidates = defaultdict(list)
    manifests = []
    seen = 0
    for category, manifest, queue, source, vault in registered_batches(workspace):
        manifests.append({"category": category, "path": str(manifest), "registered": len(queue["projects"])})
        for row in queue["projects"]:
            seen += 1
            if seen <= offset or limit is not None and len(result) + len(failures) >= limit:
                continue
            try:
                result.append(audit_row(workspace, category, row, source, vault))
            except (OSError, ValueError, KeyError, UnicodeError) as error:
                failures.append({"category": category, "sequence": row.get("sequence"), "reason": str(error)})
                continue
            for item in row["metadata"]:
                if not 0 < item["bytes"] <= 2_097_152 or not AD_CANDIDATE.search(item["file"]):
                    continue
                try:
                    path = local_child(source, Path(row["name"]) / item["file"])
                    if path.is_file() and path.stat().st_size == item["bytes"]:
                        ad_candidates[sha(path)].append({"project_id": row["id"], "sequence": row["sequence"],
                                                        "relative_path": item["file"]})
                except (OSError, ValueError):
                    result[-1].setdefault("unreadable_ad_candidates", []).append(item["file"])
    states = Counter(row["edition"] for row in result)
    return {"schema": 1, "observed_at": datetime.now(timezone.utc).isoformat(),
            "kind": "library_quality_observation", "coverage": {
                "registered_projects": seen, "offset": offset, "checked": len(result),
                "failed_rows": len(failures), "next_offset": offset + len(result) + len(failures)
                if offset + len(result) + len(failures) < seen else None, "edition_states": dict(states),
                "needs_companion_review": sum(bool(row.get("retention_candidates")) and not row["companion_link_count"]
                                              for row in result),
            }, "manifests": manifests, "rows": result, "failures": failures,
            "channel_repeat_candidates": [
                {"sha256": digest, "occurrences": entries}
                for digest, entries in sorted(ad_candidates.items())
                if len({item["project_id"] for item in entries}) > 1
            ]}


def safe_title(text):
    return re.sub(r"([\\\[\]()<>*_])", r"\\\1", text.replace("\n", " "))


def plain_text_preview(title, data):
    from worldanalysis.documents import _decode

    text, encoding = _decode(data)
    longest = max((len(run) for run in re.findall(r"\x60+", text)), default=0)
    fence = chr(96) * max(3, longest + 1)
    return f"# {safe_title(title)}\n\n{fence}text\n{text}{'' if text.endswith(chr(10)) else chr(10)}{fence}\n", {
        "representation": "complete_literal_text", "source_encoding": encoding, "source_lines": len(text.splitlines()),
    }


def text_run(value):
    value = re.sub(r"([\\\x60*_{}\[\]|])", r"\\\1", value)
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")


def word_preview(title, source):
    from worldanalysis.documents import DEFAULT_LIMITS, _Package, _Stop, _q, _word_string

    try:
        package = _Package(source, DEFAULT_LIMITS)
    except (BadZipFile, _Stop) as error:
        raise ValueError("Word 文件无法安全拆读") from error
    try:
        package.check()
        parts = ["word/document.xml", *sorted(name for name in package.by_name if re.fullmatch(
            r"word/(?:header[^/]*|footer[^/]*|comments|footnotes|endnotes)\.xml", name))]
        if any(re.search(r"vbaproject|^word/(media|embeddings)/", name, re.I) for name in package.by_name):
            raise ValueError("Word 含需原格式预览的内嵌对象")
        lines = [f"# {safe_title(title)}", ""]
        paragraphs = 0
        tables = 0
        page_fields = 0
        for part in parts:
            xml = package.xml(part)
            paragraph_tag, table_tag = _q("w", "p"), _q("w", "tbl")
            if any(node.tag.rsplit("}")[-1] in {
                "pict", "drawing", "txbxContent", "altChunk", "object", "sdt",
                "ins", "del", "vanish", "oMath", "oMathPara", "hyperlink", "gridSpan", "vMerge",
            } for node in xml.iter()):
                raise ValueError("Word 含暂不能按文字和表格完整呈现的结构")
            instructions = list(xml.iter(_q("w", "instrText")))
            simple_fields = list(xml.iter(_q("w", "fldSimple")))
            field_codes = [node.text or "" for node in instructions] + [
                node.get(_q("w", "instr")) or "" for node in simple_fields
            ]
            if instructions or simple_fields or any(xml.iter(_q("w", "fldChar"))):
                if (not re.fullmatch(r"word/(?:header|footer)[^/]*.xml", part) or not field_codes
                        or any(code.strip().upper() not in {"PAGE", "NUMPAGES"} for code in field_codes)):
                    raise ValueError("Word 含需由原格式解释的域")
                page_fields += len(field_codes)
                for node in instructions:
                    node.text = ""
            if part == parts[0]:
                parent = xml.find(_q("w", "body"))
                nodes = list(parent) if parent is not None else []
                if any(node.tag not in {paragraph_tag, table_tag, _q("w", "sectPr")} for node in nodes):
                    raise ValueError("Word 含暂不能按文字和表格呈现的正文节点")
            elif any(_word_string(node, local_paragraph=True).strip() for node in xml.iter(paragraph_tag)):
                nodes = list(xml.iter(paragraph_tag))
                lines.extend(["## " + {"header": "页眉", "footer": "页脚", "comments": "批注",
                                       "footnotes": "脚注", "endnotes": "尾注"}[
                    re.sub(r"[0-9]+$", "", Path(part).stem)], ""])
            else:
                continue
            for block in nodes:
                if block.tag == paragraph_tag:
                    value = _word_string(block, local_paragraph=True)
                    if value.strip():
                        marker = "- " if block.find(f"{_q('w','pPr')}/{_q('w','numPr')}") is not None else ""
                        lines.extend([marker + text_run(value), ""])
                        paragraphs += 1
                elif block.tag == table_tag:
                    if len(list(block.iter(table_tag))) != 1:
                        raise ValueError("Word 含需要原格式打开的嵌套表格")
                    rows = [["\n".join(_word_string(p, local_paragraph=True)
                                       for p in cell.iter(paragraph_tag)) for cell in row.findall(_q("w", "tc"))]
                            for row in block.findall(_q("w", "tr"))]
                    if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
                        raise ValueError("Word 表格不是完整矩形")
                    formatted = ["| " + " | ".join(text_run(cell) for cell in row) + " |" for row in rows]
                    lines.extend([formatted[0], "| " + " | ".join("---" for _ in rows[0]) + " |", *formatted[1:], ""])
                    tables += 1
        if not paragraphs and not tables:
            raise ValueError("Word 未发现可读取正文")
        return "\n".join(lines), {"representation": "word_text_tables", "tables": tables,
                                  "paragraphs": paragraphs, "parts": len(parts),
                                  "page_fields_with_stored_results": page_fields}
    except (BadZipFile, ParseError, _Stop) as error:
        raise ValueError("Word 内容无法安全拆读") from error
    finally:
        package.close()


def stage_materials(workspace, manifest, sequence, names, output):
    target = private_output(workspace, output)
    matches = [(category, queue, vault) for category, path, queue, _, vault in registered_batches(workspace)
               if path == manifest]
    if len(matches) != 1:
        raise ValueError("请选择已注册的正式批次")
    category, queue, vault = matches[0]
    rows = [row for row in queue["projects"] if row["sequence"] == sequence]
    if len(rows) != 1:
        raise ValueError("项目序号未唯一登记")
    row = rows[0]
    entry, current, edition = registered_publication(row, workspace, vault)
    if edition not in ("same_registered_bytes", "verified_independent_derivative"):
        raise ValueError("缺少现存同版正式教材，不能准备历史配套修订")
    package = bound_path(workspace, row["package"], area="整理结果")
    intake = read(local_child(package, ".course/ledger/intake.json"))
    registered = {file["file"]: file for file in intake["sources"]}
    if len(set(names)) != len(names) or any(name not in registered for name in names):
        raise ValueError("需逐项指定当前原件快照中存在的文件")
    manifest_path = local_child(target, "配套资料清单.json")
    if target.exists() and any(target.iterdir()):
        previous = read(manifest_path) if manifest_path.is_file() else {}
        known = previous.get("materials", ())
        if (previous.get("kind") != "companion_stage" or previous.get("project_id") != row["id"]
                or previous.get("parent_note_sha256") != current or [item["source"] for item in known] != names):
            raise ValueError("候选目录已登记另一份材料或教材版次，请使用新的隔离目录")
        for item in known:
            outputs = [(item["original"], item["source_sha256"])]
            if item.get("preview"):
                outputs.append((item["preview"], item["preview_sha256"]))
            if any(sha(local_child(target, path)) != fingerprint for path, fingerprint in outputs):
                raise ValueError("已有候选副本发生变化，保留现场，请使用新的隔离目录")
    target.mkdir(parents=True, exist_ok=True)
    save = local_child(target, COMPANIONS)
    records = []
    prefixes = Counter(registered[name]["sha256"][:12] for name in names)
    for name in names:
        source = local_child(package, Path("原始资料") / name)
        info = registered[name]
        if not DIGEST.fullmatch(info["sha256"]) or not source.is_file() or sha(source) != info["sha256"]:
            raise ValueError("实际原件与原件快照不一致")
        short = info["sha256"][:12]
        prefix = "资料-" + short
        if prefixes[short] > 1:
            prefix += "-" + sha256(name.encode("utf-8")).hexdigest()[:12]
        original = local_child(save, Path(prefix) / source.name)
        copy_file(source, original, info["sha256"])
        record = {"source": name, "original": original.relative_to(target).as_posix(), "source_sha256": info["sha256"],
                  "bytes": original.stat().st_size, "dispositions": ["原格式保留"]}
        try:
            if source.suffix.lower() in (".txt", ".md"):
                preview, visual = plain_text_preview(source.name, original.read_bytes())
            elif source.suffix.lower() == ".docx":
                preview, visual = word_preview(source.name, original)
            else:
                raise ValueError("当前格式只保留原件，阅读预览需另核")
            preview_path = local_child(save, Path(prefix) / "阅读版.md")
            write(preview_path, preview)
            record.update(preview=preview_path.relative_to(target).as_posix(), preview_sha256=sha(preview_path),
                          preview_structure=visual, dispositions=["原格式保留", "完整文字阅读候选"])
        except (OSError, ValueError, KeyError, UnicodeError) as error:
            record.update(preview_pending=str(error))
        records.append(record)
    sections = ["# 配套资料", ""]
    for record in records:
        label = safe_title(Path(record["source"]).name)
        original = quote(Path(record["original"]).relative_to(COMPANIONS).as_posix(), safe="/")
        links = [f"[打开原格式]({original})"]
        if record.get("preview"):
            preview = quote(Path(record["preview"]).relative_to(COMPANIONS).as_posix(), safe="/")
            links.insert(0, f"[打开阅读版]({preview})")
        sections.extend([f"- **{label}** · " + " · ".join(links)])
    write(local_child(save, "00 配套资料.md"), "\n".join(sections) + "\n")
    report = {"schema": 1, "kind": "companion_stage", "project_id": row["id"], "sequence": sequence,
              "category": category, "parent_entry": str(entry), "parent_note_sha256": current,
              "source_complete": row.get("scope_status"), "requires_independent_review": True,
              "materials": records}
    put(manifest_path, report)
    return report


def stage_reader_copy(workspace, observation):
    """Stage only the exact legacy summary of a currently signed, readable edition."""
    from scope_review import legacy_scope_summary, publication_scope, scope_summary, validate_visible_summary
    from vault_dates import same_edition

    if observation.get("schema") != 1 or observation.get("kind") != "library_quality_observation":
        raise ValueError("请选择本项目的教材质量盘点")
    flagged = {item["project_id"]: item["observed_note_sha256"] for item in observation["rows"]
               if item.get("visible_internal_label")}
    candidates, issues = [], []
    found = set()
    for category, manifest, queue, _, vault in registered_batches(workspace):
        for row in queue["projects"]:
            key = row["id"]
            if key not in flagged:
                continue
            found.add(key)
            identity = {"category": category, "manifest": str(manifest), "sequence": row["sequence"], "project_id": key}
            try:
                entry, digest, state = registered_publication(row, workspace, vault)
                if state != "same_registered_bytes" or digest != flagged[key]:
                    raise ValueError("当前正文与盘点时的正式版不一致")
                package = bound_path(workspace, row["package"], area="整理结果")
                edition = same_edition(package, vault, {entry.relative_to(vault).as_posix(): digest})
                if not edition or edition["sha256"] != digest:
                    raise ValueError("缺少同版发布与阅读依据")
                publication = edition["publication"]
                scope = publication_scope(publication)
                if scope["scope_status"] is None:
                    raise ValueError("该版没有可复用的实收范围签收")
                body = publication["body"]
                before = legacy_scope_summary(publication["review"])
                after = scope_summary(publication["review"])
                if body.count(before) != 1 or INTERNAL_COPY.search(COMMENT.sub("", body.replace(before, "", 1))):
                    raise ValueError("须单独检查正文与旧资料说明的边界")
                start = body.index(before)
                candidate = body[:start] + after + body[start + len(before):]
                validate_visible_summary(candidate, publication["review"])
                if sha(entry) != digest:
                    raise ValueError("准备候选期间正式正文有变化")
                candidates.append({**identity, "entry": str(entry), "parent_sha256": digest,
                                   "candidate_sha256": sha256(candidate.encode("utf-8")).hexdigest(),
                                   "summary_before": before, "summary_after": after,
                                   "publication_receipt": str(edition["publication_path"]),
                                   "reader_receipt": str(edition["delivery_path"]),
                                   "scope_status": scope["scope_status"], "requires_independent_review": True})
            except (OSError, ValueError, KeyError, TypeError) as error:
                issues.append({**identity, "reason": str(error)})
    issues.extend({"project_id": key, "reason": "当前未找到该正式项目"} for key in sorted(flagged.keys() - found))
    return {"schema": 1, "kind": "legacy_reader_copy_candidates", "flagged": len(flagged),
            "candidates": candidates, "issues": issues}


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    inventory = commands.add_parser("audit", help="只读盘点全库及附件现状")
    inventory.add_argument("--output", type=Path, required=True)
    inventory.add_argument("--path-version", help="使用任务启动时绑定的历史备份位置")
    inventory.add_argument("--offset", type=int, default=0)
    inventory.add_argument("--limit", type=int)
    stage = commands.add_parser("stage", help="把指定正式项目实际原件制为隔离配套候选")
    stage.add_argument("manifest", type=Path)
    stage.add_argument("sequence", type=int)
    stage.add_argument("--file", action="append", required=True)
    stage.add_argument("--output", type=Path, required=True)
    copy = commands.add_parser("stage-reader-copy", help="准备现有教材的资料说明修订")
    copy.add_argument("--audit", type=Path, required=True)
    copy.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "audit":
            if args.offset < 0 or args.limit is not None and args.limit < 1:
                raise ValueError("分页范围无效")
            if args.output.suffix.lower() != ".json":
                raise ValueError("全库盘点请使用 JSON 证据文件")
            output = private_output(WORKSPACE, args.output, history_version=args.path_version)
            report = audit(WORKSPACE, offset=args.offset, limit=args.limit)
            put(output, report)
            print(report["coverage"])
        elif args.action == "stage":
            module_path.insert(0, str(CHECKOUT))
            result = stage_materials(WORKSPACE, args.manifest.resolve(strict=True), args.sequence, args.file, args.output)
            print({"sequence": args.sequence, "staged": len(result["materials"]),
                   "with_preview": sum("preview" in item for item in result["materials"])})
        else:
            source = private_output(WORKSPACE, args.audit)
            output = private_output(WORKSPACE, args.output)
            if source == output or output.suffix.lower() != ".json":
                raise ValueError("请选择独立的 JSON 候选记录位置")
            report = stage_reader_copy(WORKSPACE, read(source))
            put(output, report)
            print({"flagged": report["flagged"], "staged": len(report["candidates"]), "issues": len(report["issues"])})
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
