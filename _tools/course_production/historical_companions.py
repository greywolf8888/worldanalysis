"""Publish a reviewed attachment-only revision of an unclassified historical edition."""

from copy import deepcopy
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path, PurePosixPath
import posixpath
import re
from tempfile import TemporaryDirectory
from urllib.parse import quote, unquote

from course_companions import PREVIEW_MANIFEST, companion_hashes, image_outputs
from independent_facts import configured_reviewer
from project import ENTRY, copy_file, local_child, put, read, sha
from scope_review import publication_scope
from source_coverage import validate_source_coverage


FIELD = "historical_attachment_revision"
ORIGINAL = "原始资料/"
COMPANION = "配套资料/"
LINK = re.compile(r"\[(?P<label>[^\[\]\n]+)\]\((?P<href>\.\./[^\s)]+)\)")


def path_inside(root, raw):
    path = Path(raw)
    if path.is_absolute():
        path = path.relative_to(root)
    return local_child(root, path)


def read_proof(root, reference):
    path = path_inside(root, reference["path"])
    if sha(path) != reference["sha256"]:
        raise ValueError(f"独立证据已变化 {path}")
    return path, read(path)


def hashes_in(value, root, expected):
    """Recheck independently recorded files belonging to the exact companion tree."""
    found = {}
    if isinstance(value, list):
        for item in value:
            found.update(hashes_in(item, root, expected))
    if isinstance(value, dict):
        if "path" in value and "sha256" in value and isinstance(value["path"], str):
            path = Path(value["path"])
            if path.is_absolute() and path.is_relative_to(root):
                name = path.relative_to(root).as_posix()
                if name in expected:
                    if value["sha256"] != expected[name] or sha(path) != expected[name]:
                        raise ValueError(f"独立配套证据不同版 {path}")
                    found[name] = expected[name]
        for item in value.values():
            found.update(hashes_in(item, root, expected))
    return found


def check_delta(parent, child, sources):
    """Require every changed byte to be an allowed reader link."""
    if not sources or len({row["source"] for row in sources}) != len(sources):
        raise ValueError("原附件匹配未形成唯一映射")
    restored = child
    for item in sources:
        original, preview, source = (item[k] for k in ("original", "preview", "source"))
        if (Path(source).suffix.casefold() != ".txt" or not original.startswith(COMPANION)
                or not preview.startswith(COMPANION) or Path(preview).name != "阅读版.md"):
            raise ValueError("旧范围原位补强只支持逐件已核的 TXT 与阅读版")
        def target(match):
            return unquote(match["href"]).removeprefix("../")
        direct = [link for link in LINK.finditer(restored) if target(link) == preview]
        raw = [link for link in LINK.finditer(restored) if target(link) == original]
        if (len(direct) != 1 or len(raw) != 1 or direct[0].start() == 0
                or restored[direct[0].start() - 1] != " "):
            raise ValueError(f"缺同版阅读入口或折叠原件 {source}")
        link = direct[0]
        restored = restored[:link.start() - 1] + restored[link.end():]
        old_url = "../" + quote(ORIGINAL + source, safe="/")
        current_url = raw[0]["href"]
        old_footer = f"> - [{raw[0]['label']}]({old_url})"
        new_footer = f"> - [{raw[0]['label']}]({current_url})"
        if restored.count(new_footer) != 1 or parent.count(old_footer) != 1:
            raise ValueError(f"文末原入口无法唯一还原 {source}")
        restored = restored.replace(new_footer, old_footer, 1)
    if restored != parent:
        raise ValueError("候选改动超出逐件附件入口，须沿新正文复核发布")


def check_reviewed_delta(parent, child, revision):
    """Squash exact reviewed companion and figure edits back to the accepted teaching prose."""
    if not revision.get("document_edition"):
        check_delta(parent, child, revision["materials"])
        return
    figure = revision.get("extra_figure")
    if figure:
        block = figure["inserted_block"]
        if child.count(block) != 1:
            raise ValueError("独立签收的新增图片没有唯一锚点")
        child = child.replace(block, "", 1)
    materials = revision["materials"]
    previews = {}
    for item in materials:
        def refs(target):
            return [m for m in LINK.finditer(child) if unquote(m["href"]) == "../" + target]
        original = refs(item["original"])
        direct = refs(item["preview"])
        if len(original) != 1 or not direct:
            raise ValueError("同版正文资料入口或文末原格式入口缺失")
        old_url = "../" + quote(ORIGINAL + item["source"], safe="/")
        raw = original[0]
        expected = f"> - [{raw['label']}]({old_url})"
        switched = f"> - {raw[0]}"
        if parent.count(expected) != 1 or child.count(switched) != 1:
            raise ValueError("旧版折叠原件来源无法还原")
        child = child.replace(switched, expected, 1)
        previews[item["preview"]] = old_url

    def restore_inline(line):
        converted = LINK.sub(lambda m: m[0].replace(m["href"], previews.get(
            unquote(m["href"]).removeprefix("../"), m["href"]), 1), line)
        return converted if converted in parent_lines else line

    parent_lines = parent.splitlines(keepends=True)
    current = [restore_inline(line) for line in child.splitlines(keepends=True)]
    diff = SequenceMatcher(a=parent_lines, b=current, autojunk=False)
    for kind, _, _, start, stop in diff.get_opcodes():
        if kind == "equal":
            continue
        if kind != "insert":
            raise ValueError("本版变更会替换原已签教学正文")
        for line in current[start:stop]:
            if not line.strip() or line.rstrip("\r\n") == revision.get("signed_exercise"):
                continue
            links = LINK.findall(line)
            if not links or any(unquote(href).removeprefix("../") not in previews for _, href in links):
                raise ValueError("新增文字超出经复核的配套导读")


def figure_hashes(project, revision, root):
    figure = revision.get("extra_figure")
    if not figure:
        return {}
    from PIL import Image, ImageChops

    name = figure["name"]
    if (not name.startswith("课件/原图/") or Path(name).suffix.casefold() != ".png"
            or not re.search(r"!\[[^\n]+\]\(原图/", unquote(figure["inserted_block"]))):
        raise ValueError("历史补图须使用有已收原件出处的 PNG")
    image = local_child(root, name)
    original = local_child(project.folder, ORIGINAL + figure["source"])
    if sha(image) != figure["sha256"] or sha(original) != figure["source_sha256"]:
        raise ValueError("补图、原件或同版指纹不一致")
    with Image.open(image) as actual, Image.open(original) as source:
        if actual.size != source.size or ImageChops.difference(actual.convert("RGB"), source.convert("RGB")).getbbox():
            raise ValueError("补图不是已收附件的完整原画")
    return {name: figure["sha256"]}


def child_review(parent, digest, assets, added_figures=None):
    review = deepcopy(parent["review"])
    review["snapshot"].update(text_sha256=digest, companions=assets)
    if added_figures:
        images = review["snapshot"]["images"]
        if not isinstance(images, dict):
            raise ValueError("历史图件快照无法合并")
        images.update({Path(name).relative_to(ENTRY.parent).as_posix(): digest for name, digest in added_figures.items()})
    return review


def verify_revision(project, record, digest):
    """Reapply the parent and independent-evidence gates when verifying a published child."""
    from acceptance import check_render

    revision = record[FIELD]
    parent_digest = revision["parent_body_sha256"]
    if not re.fullmatch(r"[a-f0-9]{64}", parent_digest) or parent_digest == digest:
        raise ValueError("历史配套版缺有效父正文")
    previous = local_child(project.ledger, f"publications/{parent_digest}.json")
    if sha(previous) != revision["parent_record_sha256"]:
        raise ValueError("历史父版回执已变化")
    parent = read(previous)
    if (parent.get(FIELD) or parent["outputs"][ENTRY.as_posix()] != parent_digest
            or publication_scope(parent)["scope_status"] is not None
            or parent["review"]["schema"] != 2 or parent["review"]["source_coverage"].get("claims_version") is not None
            or sha256(parent["body"].encode("utf-8")).hexdigest() != parent_digest):
        raise ValueError("历史配套版须从有双向核查记录的旧签收父版接入")
    assets = record["review"]["snapshot"]["companions"]
    added = figure_hashes(project, revision, project.folder)
    if (record["review"] != child_review(parent, digest, assets, added)
            or record["outputs"] != {**parent["outputs"], ENTRY.as_posix(): digest, **assets, **added}
            or record.get("analysis_completed_at") != parent.get("analysis_completed_at")
            or publication_scope(record)["scope_status"] is not None):
        raise ValueError("历史配套版原范围、配图、来源或分析日期已变化")
    check_reviewed_delta(parent["body"], record["body"], revision)
    approved, reader = archived_review(project, digest, revision)
    # Older revisions froze the approved receipt hash without a separate reviewer field.
    expected_reviewer = revision.get("reviewer_thread_id", approved["reviewer"]["thread_id"])
    check_independent(approved, reader, revision, digest, assets, image_outputs(record),
                      project.folder.name, expected_reviewer)
    validate_source_coverage(parent["body"], parent["review"]["source_scope"], parent["review"], allow_legacy=True)
    check_render(record["obsidian"], digest, companions=assets)


def archived_review(project, digest, revision):
    path = local_child(project.ledger, f"historical-attachment-reviews/{digest}")
    values = []
    for name in ("signed", "reader"):
        file = local_child(path, f"{name}.json")
        if sha(file) != revision[f"{name}_sha256"]:
            raise ValueError("独立同版签收或重开回执内容已变")
        values.append(read(file))
    return values


def check_independent(approval, render, revision, digest, assets, figures, folder, expected_reviewer):
    from datetime import datetime

    reviewer = approval["reviewer"]
    if (not isinstance(expected_reviewer, str) or not expected_reviewer.strip()
            or approval.get("decision") != "通过" or reviewer.get("role") != "测试agent"
            or reviewer.get("thread_id") != expected_reviewer
            or datetime.fromisoformat(reviewer["reviewed_at"]).utcoffset() is None):
        raise ValueError("缺实际具名独立签收")
    signed = next(row for row in review_items(approval) if row["sequence"] == revision["sequence"])
    if revision.get("extra_figure") and (signed.get("extra_figure") != revision["extra_figure"]
                                           or signed.get("signed_exercise") != revision.get("signed_exercise")):
        raise ValueError("补图及练习文字须绑定当版具名签收")
    note = f"{folder}/课件/{folder}.md"
    main = next(row for row in render["checks"] if row["file"] == note)
    if (signed.get("decision") != "通过" or signed["parent_note_sha256"] != revision["parent_body_sha256"]
            or signed["candidate_note_sha256"] != digest or signed["reader"]["sha256"] != revision["reader_sha256"]
            or main.get("document_sha256") != digest or main.get("reopened") is not True
            or main.get("reachedBottom") is not True or main.get("horizontalOverflow") is not False
            or main.get("visibleCitations") != [] or main.get("missingRawLinks") != []
            or {img["name"] for img in main["images"] if img.get("fit") is True} != {Path(name).name for name in figures}):
        raise ValueError("具名实际重开与要发布的正文或配图不一致")
    for relative, fingerprint in assets.items():
        target = f"{folder}/{relative}"
        if relative.endswith(".md"):
            if not any(row["file"] == target and row.get("document_sha256") == fingerprint
                       and row.get("reopened") and row.get("reachedBottom") for row in render["checks"]):
                raise ValueError(f"阅读版未按同一实体打开 {relative}")
            if not any(row["source"] == note and row.get("target") == target for row in render["navigation"]):
                raise ValueError(f"未实测由主教材进入阅读版 {relative}")
        else:
            locations = [row for row in render["navigation"] if row.get("sha256") == fingerprint
                         and (row.get("target") == target or posixpath.normpath(
                             str(PurePosixPath(row["source"]).parent / row["link"])) == target)]
            if relative.endswith(".pdf"):
                locations = [row for row in locations if row.get("mode") == "pdf" and row.get("mounted") is True]
            if not locations:
                raise ValueError(f"原格式或 PDF 阅读版未按同一实体读取 {relative}")


def review_items(approval):
    return approval["remaining_material_preservation"]["items"] if "remaining_material_preservation" in approval else approval["items"]


def prepare(project, workspace, sequence, signed_path):
    from acceptance import verify_publication
    from vault_delivery import delivery, delivery_status

    workspace = workspace.resolve(strict=True)
    signed_path = path_inside(workspace, signed_path)
    approval = read(signed_path)
    signed = next(row for row in review_items(approval) if row["sequence"] == sequence)
    document_edition = "candidate_manifest" in signed
    if document_edition:
        candidate_path, candidate = read_proof(workspace, signed["candidate_manifest"])
        material_file, registry = read_proof(workspace, signed["materials_manifest"])
        if candidate.get("materials_manifest_sha256") != sha(material_file):
            raise ValueError("具名候选配套清单已变化")
        report_reference = signed["independent_evidence"][0]
    else:
        index_path, index = read_proof(workspace, approval["index"])
        candidate = next(row for row in index["projects"] if row["sequence"] == sequence)
        material_file = local_child(index_path.parent, candidate["materials_manifest"])
        registry = read(material_file)
        report_reference = signed["material_facts"]
    reader_path, reader = read_proof(workspace, signed["reader"])
    _, report = read_proof(workspace, report_reference)
    if report["sequence"] != sequence:
        raise ValueError("独立附件核验记录串课")
    body_ref = report.get("candidate") or report["body"]
    note = path_inside(workspace, body_ref["path"])
    digest = sha(note)
    body = note.read_bytes().decode("utf-8")
    if digest != body_ref["sha256"] or digest != candidate["candidate_note_sha256"]:
        raise ValueError("隔离候选已变或从未签收")
    candidate_course = note.parent.parent
    sources = registry["materials"]
    expected = {}
    for row in sources:
        expected[row["original"]] = row["source_sha256"]
        expected[row["preview"]] = row["preview_sha256"]
    assets = companion_hashes(candidate_course, body)
    if expected != assets or {**hashes_in(report, candidate_course, expected),
                              **hashes_in(report, material_file.parent, expected)} != assets:
        raise ValueError("可达配套、原附件与已签指纹未完整一致")
    current = verify_publication(project)
    current_digest = sha(project.entry)
    if current_digest == digest:
        if current.get(FIELD, {}).get("sequence") != sequence:
            raise ValueError("当前已是候选正文，但无相同接入记录")
        return current, None, None
    if (current.get(FIELD) or publication_scope(current)["scope_status"] is not None
            or current["review"]["schema"] != 2 or current["review"]["source_coverage"].get("claims_version") is not None):
        raise ValueError("只接入有双向核查记录、尚未作新版范围验收的历史教材")
    if current_digest != candidate["parent_note_sha256"]:
        raise ValueError("正式父版发生变化，须按当前版本重新核对")
    state = delivery(project)
    if not state or sha(Path(state["entry"])) != current_digest:
        raise ValueError("正式库已有用户编辑或缺入口")
    delivery_status(project, current, required=True)
    parent_record = local_child(project.ledger, f"publications/{current_digest}.json")
    expected_reviewer = configured_reviewer(workspace=workspace)
    revision = {"sequence": sequence, "parent_body_sha256": current_digest, "parent_record_sha256": sha(parent_record),
                "signed_sha256": sha(signed_path), "reader_sha256": sha(reader_path), "materials": sources,
                "reviewer_thread_id": expected_reviewer}
    if document_edition:
        revision["document_edition"] = True
        if candidate.get("extra_figure"):
            revision["extra_figure"] = candidate["extra_figure"]
            revision["signed_exercise"] = candidate.get("signed_exercise")
    added = figure_hashes(project, revision, candidate_course)
    if set(added) & set(current["outputs"]):
        raise ValueError("补图不得覆盖旧已签图片")
    check_reviewed_delta(current["body"], body, revision)
    check_independent(approval, reader, revision, digest, assets, set(image_outputs(current)) | set(added),
                      project.folder.name, expected_reviewer)
    for row in sources:
        raw = local_child(project.folder, ORIGINAL + row["source"])
        reader_raw = local_child(Path(state["entry"]).parent.parent, ORIGINAL + row["source"])
        if sha(raw) != row["source_sha256"] or sha(reader_raw) != row["source_sha256"]:
            raise ValueError(f"正式包或阅读库原附件不同版 {row['source']}")
    return {**current, "body": body, "outputs": {**current["outputs"], ENTRY.as_posix(): digest, **assets, **added},
            "review": child_review(current, digest, assets, added), FIELD: revision}, (signed_path, reader_path), candidate_course


def accept(project, workspace, sequence, signed_path, executable, run_obsidian):
    from acceptance import check_render, verify_publication
    from vault_delivery import delivery, install

    record, proofs, candidate = prepare(project, workspace, sequence, signed_path)
    digest = record["outputs"][ENTRY.as_posix()]
    if proofs is None:
        return {**install(project, record), "historical_edition": True}
    revision = record[FIELD]
    assets = record["review"]["snapshot"]["companions"]
    figures = {name: record["outputs"][name] for name in image_outputs(record)}
    new_files = {**assets, **figure_hashes(project, revision, candidate)}
    with TemporaryDirectory(prefix="historical-attachments-", dir=project.work) as temp:
        stage = Path(temp)
        for name, fingerprint in {**assets, **figures}.items():
            source = local_child(candidate if name in new_files else project.folder, name)
            copy_file(source, local_child(stage, name), fingerprint)
        from project import write

        write(local_child(stage, ENTRY), record["body"])
        put(stage / PREVIEW_MANIFEST, {"schema": 1, "companions": assets})
        run_obsidian(executable, "--project", project.folder, stage)
        record["obsidian"] = read(stage / "obsidian-render.json")
        check_render(record["obsidian"], digest, companions=assets)
        if (sha(local_child(stage, ENTRY)) != digest
                or sha(project.entry) != revision["parent_body_sha256"]
                or sha(Path(delivery(project)["entry"])) != revision["parent_body_sha256"]):
            raise ValueError("正式父版或候选在阅读期间发生变化")
        for name, fingerprint in new_files.items():
            target = local_child(project.folder, name)
            if target.exists() and sha(target) != fingerprint:
                raise ValueError(f"配套目标已有不同文件 {name}")
            reader_target = local_child(Path(delivery(project)["entry"]).parent.parent, name)
            if reader_target.exists() and sha(reader_target) != fingerprint:
                raise ValueError(f"日常库配套目标已有不同文件 {name}")
        for name, fingerprint in new_files.items():
            copy_file(local_child(stage, name), local_child(project.folder, name), fingerprint)
        archive = local_child(project.ledger, f"historical-attachment-reviews/{digest}")
        for key, source in zip(("signed", "reader"), proofs, strict=True):
            copy_file(source, local_child(archive, f"{key}.json"), revision[f"{key}_sha256"])
        verify_revision(project, record, digest)
        put(local_child(project.ledger, f"publications/{digest}.json"), record)
        copy_file(local_child(stage, ENTRY), project.entry, digest)
    return {**install(project, verify_publication(project)), "historical_edition": True}
