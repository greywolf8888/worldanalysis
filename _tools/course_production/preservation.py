"""Stage and verify a portable derivative of a signed course, without editing the source."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

from course_companions import COMPANIONS, image_outputs, verify_published_companions
from project import ENTRY, copy_file, local_child, put, read, sha, write
from reader_review import markdown_segments


MANIFEST = "独立成品清单.json"
SCHEMA = 1
APPENDIX = "> [!info]- 原课与配套资料"
REFERENCE = re.compile(
    r"^(?P<start>> - 第 \d+ 讲 · )\[(?:\\.|[^\]\\])+]\((?P<url>[^)]+)\) · (?P<clock>\d+:\d{2}(?::\d{2})?)$"
)
INLINE = re.compile(r"(?P<embed>!?)\[(?:\\.|[^\]\\\n])*]\((?P<url><[^>\n]+>|[^)\n]+)\)")
WIKI = re.compile(r"!?\[\[(?P<url>[^\]\n]+)]]")
PLAINTEXT = re.compile(r"[&<>\\\[\]*_\x60|~]")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def text_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def escaped(text):
    return PLAINTEXT.sub(lambda match: f"&#{ord(match[0])};", text)


def file_identity(path):
    file = path.lstat()
    if not stat.S_ISREG(file.st_mode) or path.is_symlink():
        raise ValueError(f"需要实际的常规文件 {path}")
    return {"bytes": file.st_size, "volume_id": str(file.st_dev),
            "file_id": str(file.st_ino), "hardlinks": file.st_nlink}


def proved_file(path, expected):
    identity = file_identity(path)
    digest = sha(path)
    if digest != expected or file_identity(path) != identity:
        raise ValueError(f"文件与登记指纹不一致 {path}")
    return {**identity, "sha256": digest}


def original_rel(url):
    decoded = unquote(url)
    if not decoded.startswith("../原始资料/"):
        raise ValueError(f"原片出处须指向本项目的相对原件 {url}")
    relative = decoded.removeprefix("../原始资料/")
    if Path(relative).as_posix() != relative or not relative or ".." in Path(relative).parts:
        raise ValueError(f"原件出处路径无效 {url}")
    return relative


def derive(parent, provenance, sources):
    lines = parent.splitlines(keepends=True)
    appendix = False
    changes = []
    for offset, line in enumerate(lines):
        text = line.removesuffix("\n")
        if text == APPENDIX:
            appendix = True
        elif not text.startswith(">"):
            appendix = False
        row = REFERENCE.fullmatch(text) if appendix else None
        if not row:
            continue
        name = original_rel(row["url"])
        if name not in provenance:
            continue
        if not provenance[name].strip():
            raise ValueError(f"缺少逐件独立成品判定 {name}")
        digest = sources[name]["sha256"]
        revised = (f"{row['start']}{escaped(name)} · 00:00–{row['clock']} · "
                   f"原片 SHA-256 {digest} · 本独立成品未附原片")
        revised += "\n" if line.endswith("\n") else ""
        changes.append({"line": offset + 1, "source": name, "original": line, "replacement": revised,
                        "dependency_evidence": provenance[name]})
        lines[offset] = revised
    if sorted(change["source"] for change in changes) != sorted(provenance):
        raise ValueError("出处白名单必须逐件、仅一次匹配折叠附录中的原片入口")
    return "".join(lines), changes


def restore_parent(text, changes):
    lines = text.splitlines(keepends=True)
    for change in changes:
        index = change["line"] - 1
        if index < 0 or index >= len(lines) or lines[index] != change["replacement"]:
            raise ValueError("派生改动已偏离登记的出处行")
        lines[index] = change["original"]
    return "".join(lines)


def plain_link(url):
    return unquote(url.removeprefix("<").removesuffix(">"))


def local_reference(course, note, url):
    if "\\" in url or "\x00" in url or url.startswith(("/", "~")):
        raise ValueError(f"禁止以宿主或绝对路径作为成品依赖 {url}")
    path = url.partition("#")[0]
    if not path:
        return None
    target = (note.parent / path).resolve()
    if not target.is_relative_to(course):
        raise ValueError(f"阅读链接越出成品课程 {url}")
    relative = target.relative_to(course).as_posix()
    return local_child(course, relative).relative_to(course).as_posix()


def links(course, note, body):
    clean = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    inline = list(INLINE.finditer(clean))
    wiki = list(WIKI.finditer(clean))
    residue = WIKI.sub("", INLINE.sub("", clean))
    if re.search(r"<\s*/?[A-Za-z]|\]\s*\(|\[\[|(?m:^\s*\[[^\]\n]+]:)", residue):
        raise ValueError("成品出现无法核对依赖的 HTML、引用式或非标准链接")
    references = {}
    for match in inline:
        url = plain_link(match["url"])
        scheme = urlsplit(url).scheme.lower()
        if scheme:
            if scheme not in ("http", "https") or match["embed"] or not urlsplit(url).hostname:
                raise ValueError(f"成品不能依赖宿主链接或远端嵌入 {url}")
            continue
        relative = local_reference(course, note, url)
        if relative:
            references[relative] = "figure" if match["embed"] else "attachment"
    for match in wiki:
        url = unquote(match["url"].split("|", 1)[0])
        if url.startswith("#") or url.partition("#")[0] == f"{course.name}/{note.relative_to(course).as_posix()}":
            continue
        if not url.startswith(f"{course.name}/"):
            raise ValueError(f"成品不能依赖别的笔记或跨项目素材 {url}")
        relative = local_reference(course, note, "../" + url.split("/", 1)[1])
        if relative is None:
            raise ValueError(f"无效的 Obsidian 链接 {url}")
        references[relative] = "figure" if match[0].startswith("!") else "attachment"
    return references


def companion_proofs(manifest):
    return {name: spec["sha256"] for name, spec in manifest["retained"].items() if spec["role"] == "companion"}


def retained_links(course, note, body, manifest):
    """Check both direct links and all nested reading dependencies of reviewed companions."""
    references = links(course, note, body)
    prefix = f"{COMPANIONS}/"
    direct = sorted(name for name in references if name.startswith(prefix))
    if direct != manifest.get("companion_links", []) or any(references[name] != "attachment" for name in direct):
        raise ValueError("正文的独立配套入口与保留清单不一致")
    proofs = companion_proofs(manifest)
    pending = list(direct)
    checked = set()
    while pending:
        name = pending.pop()
        if name in checked:
            continue
        if name not in proofs or not name.startswith(prefix):
            raise ValueError(f"配套资料依赖未签收或越出配套目录 {name}")
        checked.add(name)
        path = local_child(course, name)
        if path.suffix.casefold() == ".md":
            visible = "".join(chunk for fenced, chunk in markdown_segments(path.read_text("utf-8-sig")) if not fenced)
            pending.extend(links(course, path, visible))
    if checked != set(proofs):
        raise ValueError("保留清单含无从正文进入的配套资料")
    return {**references, **dict.fromkeys(checked, "companion")}


def reader_link_count(manifest):
    originals = sum(spec["role"] == "attachment" for spec in manifest["retained"].values())
    return originals + len(manifest.get("companion_links", []))


def verify_pdf(path):
    from pypdf import PdfReader

    with path.open("rb") as stream:
        pdf = PdfReader(stream, strict=True)
        if pdf.is_encrypted or pdf.attachments:
            raise ValueError(f"附件需要另行核对密码或内嵌文件 {path}")
        catalog = pdf.trailer["/Root"]
        names = catalog["/Names"] if "/Names" in catalog else {}
        if catalog.get("/OpenAction") or catalog.get("/AA") or names.get("/JavaScript"):
            raise ValueError(f"附件需要另行核对自动动作 {path}")
        pages = list(pdf.pages)
        for page in pages:
            if page.get("/AA") or page.get("/Annots"):
                raise ValueError(f"附件含链接或注释，需要另行确认全部依赖 {path}")
            contents = page.get_contents()
            if contents is not None:
                contents.get_data()
        if not pages:
            raise ValueError(f"附件无可读页面 {path}")
        return {"pages": len(pages), "embedded_files": 0, "page_annotations": 0}


def verify_figure(path):
    from PIL import Image

    with Image.open(path) as image:
        image.verify()
        return {"width": image.width, "height": image.height}


def verify_companion(path):
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        return verify_pdf(path)
    if suffix in (".md", ".txt"):
        raw = path.read_bytes()
        encoding = "utf-16" if suffix == ".txt" and raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        text = raw.decode(encoding)
        if not text.strip() or "\0" in text:
            raise ValueError(f"配套文件缺少完整可读正文 {path}")
        return {"encoding": encoding, "lines": len(text.splitlines())}
    raise ValueError(f"该配套格式需要另外核对阅读性与跨文件依赖 {path}")


def verify_stage(stage):
    stage = stage.resolve(strict=True)
    manifest = read(local_child(stage, MANIFEST))
    if manifest.get("schema_version") != SCHEMA or manifest.get("state") != "等待独立签收":
        raise ValueError("独立成品清单版本或状态无效")
    course = local_child(stage, manifest["course_dir"])
    note = local_child(course, manifest["note"])
    files = manifest["retained"]
    actual = set()
    for root, dirs, names in course.walk(follow_symlinks=False):
        for name in dirs + names:
            path = root / name
            if path.is_symlink() or path.is_junction():
                raise ValueError(f"隔离成品内不能保留链接或联接 {path}")
        actual.update((root / name).relative_to(course).as_posix() for name in names)
    if actual != set(files):
        raise ValueError(f"隔离目录与保留清单不同，缺少 {sorted(set(files) - actual)}，多出 {sorted(actual - set(files))}")
    body = note.read_bytes().decode("utf-8")
    if text_sha(body) != manifest["derived_body_sha256"]:
        raise ValueError("派生正文已变化")
    parent = restore_parent(body, manifest["changes"])
    if text_sha(parent) != manifest["parent_body_sha256"]:
        raise ValueError("还原的签收父版正文 SHA 不符")
    expected = {name: spec["role"] for name, spec in files.items() if spec["role"] != "note"}
    if retained_links(course, note, body, manifest) != expected:
        raise ValueError("正文实际依赖与成品素材白名单不符")
    inspections = {}
    for name, spec in files.items():
        path = local_child(course, name)
        file = proved_file(path, spec["sha256"])
        if file["bytes"] != spec["bytes"] or file["hardlinks"] != 1:
            raise ValueError(f"独立副本大小发生变化或与共享原件硬链接 {path}")
        if spec["role"] == "attachment":
            if path.suffix.lower() != ".pdf":
                raise ValueError(f"需要另行增加此附件的独立打开检查 {path}")
            inspections[name] = verify_pdf(path)
        elif spec["role"] == "figure":
            inspections[name] = verify_figure(path)
        elif spec["role"] == "companion":
            inspections[name] = verify_companion(path)
    return {"manifest_sha256": sha(stage / MANIFEST), "parent_body_sha256": text_sha(parent),
            "derived_body_sha256": text_sha(body), "retained_files": len(files),
            "figures": sum(spec["role"] == "figure" for spec in files.values()),
            "attachments": sum(spec["role"] == "attachment" for spec in files.values()),
            "companions": len(companion_proofs(manifest)),
            "inspections": inspections, "verified_at": utc_now()}


def workspace_path(workspace, raw):
    path = Path(raw)
    if not path.is_absolute() or not path.is_relative_to(workspace):
        raise ValueError(f"需要工作区内的绝对路径 {raw}")
    return local_child(workspace, path.relative_to(workspace))


def signed_parent(package, workspace, reviewer):
    ledger = local_child(package, ".course/ledger")
    delivery = read(local_child(ledger, "obsidian-delivery.json"))
    note = workspace_path(workspace, delivery["entry"])
    body = note.read_bytes().decode("utf-8")
    digest = text_sha(body)
    publication = local_child(ledger, f"publications/{digest}.json")
    record = read(publication)
    if (digest != delivery["sha256"] or body != record["body"]
            or record["outputs"][ENTRY.as_posix()] != digest):
        raise ValueError("正式教材与已签收的出版或日常交付存在差异")
    independent_proof = record["review"]["independent_fact_review"]
    review_path = workspace_path(workspace, independent_proof["receipt_path"])
    review = read(review_path)
    names = ("body_sha256", "scope_sha256", "facts_sha256", "reviewer", "decision")
    if (any(review[k] != independent_proof[k] for k in names)
            or review["body_sha256"] != digest
            or review["decision"] != "通过" or review["reviewer"]["thread_id"] != reviewer):
        raise ValueError("正式教材缺少具名的同版独立签收")
    render = local_child(package, f".course/work/日常库接口验收/{digest}.json")
    observation = delivery["observation"] or {}
    rendered = read(render)
    reader = rendered["checks"][0]
    companions = verify_published_companions(note.parent.parent, record)
    if companions and (independent_proof.get("companions") != companions or review.get("companions") != companions):
        raise ValueError("正式父版的独立事实回执未签收这些配套资料")
    if (sha(render) != observation["render_sha256"] or observation["sha256"] != digest
            or observation["entry"] != str(note) or reader["document_sha256"] != digest
            or not reader["reopened"] or not reader["reachedBottom"]
            or rendered.get("companions", {}) != companions):
        raise ValueError("正式教材的实际同版阅读回执不符")
    evidence = {name: {"path": str(path), "sha256": sha(path)}
                for name, path in (("publication", publication), ("independent_review", review_path),
                                   ("reader", render))}
    return note, body, record, evidence


def require_original_mp4_audio(package, name, digest):
    """Keep visual-only MP4s and originals whose own audio is unproved."""
    if Path(name).suffix.casefold() != ".mp4":
        return
    ledger = local_child(package, ".course/ledger")
    for path in ledger.glob("[0-9]*.json"):
        if not path.stem.isdecimal():
            continue
        record = read(local_child(ledger, path.name))
        if record.get("source") != name or record.get("sha256") != digest:
            continue
        if (record.get("audio_source") != name or record.get("audio_processed") is not True
                or record.get("audio_error") or record.get("recovery")):
            continue
        streams = (record.get("media") or {}).get("streams") or ()
        if not any(stream.get("codec_type") == "audio" for stream in streams):
            continue
        if any(isinstance(window.get("text"), str) and window["text"].strip()
               and not window.get("digitally_silent") for window in record.get("speech_windows") or ()):
            return
    raise ValueError(f"保留原 MP4，尚未确认原片自身有声 {name}")


def inspect_originals(original_input, project, course, sources, provenance, candidates):
    if not candidates or not set(candidates) <= set(provenance):
        raise ValueError("拟清理范围应为逐件核验的原片出处子集")
    results = {}
    for name, basis in provenance.items():
        digest = sources[name]["sha256"]
        copies = {role: local_child(root, name) for role, root in (
            ("manual_download", original_input),
            ("package_snapshot", project / "原始资料"),
            ("reading_vault", course / "原始资料"),
        )}
        candidate = name in candidates
        observed = {role: {"path": str(file), **(proved_file(file, digest) if candidate else file_identity(file))}
                    for role, file in copies.items()}
        separate = {(file["volume_id"], file["file_id"]) for file in observed.values()}
        if candidate and (observed["reading_vault"]["hardlinks"] != 1 or len(separate) != len(copies)):
            raise ValueError(f"日常库拟移除件存在共享硬链接或与留存件同身份 {name}")
        results[name] = {"state": "候选，等待独立签收及授权" if candidate else "已见在位，身份待另行核对",
                         "signed_intake_sha256": digest,
                         "provenance": basis, "copies": observed,
                         "proposed_removal": observed["reading_vault"]["path"] if candidate else None}
    return results


def prepare(plan_path, output):
    plan = read(plan_path)
    if plan.get("schema_version") != SCHEMA:
        raise ValueError("独立成品方案版本无效")
    workspace = Path(plan["workspace"]).resolve(strict=True)
    output = workspace_path(workspace, output)
    if output.exists() or not output.parent.is_dir():
        raise ValueError("成品需要尚未使用的独立输出目录，且上级目录必须存在")
    package = workspace_path(workspace, plan["package"])
    source = workspace_path(workspace, plan["manual_download"])
    note, parent, record, evidence = signed_parent(package, workspace, plan["reviewer"])
    course = note.parent.parent
    provenance = plan["provenance_only"]
    candidates = plan["candidates"]
    attachments = set(plan["attachments"])
    sources = {row["file"]: row for row in read(local_child(package, ".course/ledger/intake.json"))["sources"]}
    if not provenance or not (set(provenance) | attachments) <= sources.keys() or set(provenance) & attachments:
        raise ValueError("附件与原片出处须分别列出，且须见于正式实收清单")
    for name in provenance:
        require_original_mp4_audio(package, name, sources[name]["sha256"])
    derived, changes = derive(parent, provenance, sources)
    relative_note = note.relative_to(course).as_posix()
    images = image_outputs(record)
    companions = record["review"]["snapshot"].get("companions", {})
    if set(record["outputs"]) != {ENTRY.as_posix(), *images, *companions}:
        raise ValueError("正式出版有未登记为配图或配套的文件")
    retained = {relative_note: {"sha256": text_sha(derived), "bytes": len(derived.encode("utf-8")), "role": "note"}}
    retained.update((name, {"role": "figure", **proved_file(local_child(course, name), record["outputs"][name])})
                    for name in sorted(images))
    retained.update((name, {"role": "companion", **proved_file(local_child(course, name), digest)})
                    for name, digest in sorted(companions.items()))
    retained.update((f"原始资料/{name}", {"role": "attachment", **proved_file(
        local_child(course, f"原始资料/{name}"), sources[name]["sha256"])}) for name in sorted(attachments))
    originals = inspect_originals(source, package, course, sources, provenance, candidates)
    # Unlinked intake files remain outside the isolated copy and outside cleanup authorization.
    others = sorted(sources.keys() - set(provenance) - attachments)
    course_dir = f"vault/{course.name}"
    manifest = {
        "schema_version": SCHEMA, "record_type": "course_preservation", "state": "等待独立签收",
        "course_dir": course_dir, "note": relative_note, "formal_entry": str(note),
        "observed_at": utc_now(), "reviewer_thread_id": plan["reviewer"],
        "parent_body_sha256": text_sha(parent), "derived_body_sha256": text_sha(derived),
        "parent_evidence": evidence, "parent_intake_sha256": sha(local_child(package, ".course/ledger/intake.json")),
        "preserved_scope_status": record.get("scope_status"), "preserved_scope_gaps": record.get("scope_gaps", []),
        "changes": changes, "retained": retained, "source_videos": originals,
        "other_intake_files": {"state": "未进入本次清理范围", "paths": others},
        "deletion_authorization": None, "executed_removals": [],
    }
    if companions:
        manifest["companion_links"] = sorted(name for name in links(course, note, derived) if name in companions)
    if sha(note) != manifest["parent_body_sha256"]:
        raise ValueError("取证期间正式教材发生改动")
    with TemporaryDirectory(prefix=".preservation-", dir=output.parent) as temporary:
        stage = Path(temporary) / "edition"
        target_note = local_child(stage, f"{course_dir}/{relative_note}")
        write(target_note, derived)
        for name, spec in retained.items():
            if name != relative_note:
                copy_file(local_child(course, name), local_child(stage, f"{course_dir}/{name}"), spec["sha256"])
        put(stage / "vault/.obsidian/app.json", {"defaultViewMode": "preview", "readableLineLength": True})
        put(stage / MANIFEST, manifest)
        verify_stage(stage)
        stage.rename(output)
    return verify_stage(output)


def run_isolated_reader(course, manifest, receipt, executable):
    node = shutil.which("node")
    if not node:
        raise ValueError("需要可用的 Node.js")
    env = {**os.environ, "OBSIDIAN_EXE": str(executable.resolve(strict=True))}
    arguments = [node, str(Path(__file__).with_name("verify_obsidian.cjs")), "--installed",
                 str(course), manifest["note"], str(receipt)]
    companions = companion_proofs(manifest)
    if companions:
        with TemporaryDirectory(prefix="companion-reader-", dir=receipt.parent) as temporary:
            inputs = Path(temporary) / "配套读取参数.json"
            put(inputs, {"review": {"snapshot": {"companions": companions}}})
            subprocess.run([*arguments, str(inputs)], env=env, stdout=sys.stderr, check=True)
    else:
        subprocess.run(arguments, env=env, stdout=sys.stderr, check=True)


def read_in_obsidian(stage, executable):
    verified = verify_stage(stage)
    manifest = read(stage / MANIFEST)
    course = local_child(stage, manifest["course_dir"])
    receipt = stage / "Obsidian独立重开.json"
    if receipt.exists():
        raise ValueError("阅读回执已存在，请保留同版证据")
    run_isolated_reader(course, manifest, receipt, executable)
    verify_stage(stage)
    rendered = read(receipt)
    view = rendered["checks"][0]
    if (view["document_sha256"] != manifest["derived_body_sha256"] or not view["reopened"]
            or not view["reachedBottom"] or len(view["images"]) != verified["figures"]
            or view["rawLinks"] != reader_link_count(manifest) or view["missingRawLinks"]
            or rendered.get("companions", {}) != companion_proofs(manifest)):
        raise ValueError("Obsidian 阅读证据和派生正文或保留件不一致")
    return {**verified, "reader": {"path": str(receipt), "sha256": sha(receipt)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    build = commands.add_parser("prepare")
    build.add_argument("plan", type=Path)
    build.add_argument("output", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("stage", type=Path)
    reader = commands.add_parser("reader")
    reader.add_argument("stage", type=Path)
    reader.add_argument("--obsidian-exe", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            result = prepare(args.plan, args.output)
        elif args.action == "verify":
            result = verify_stage(args.stage)
        else:
            result = read_in_obsidian(args.stage.resolve(strict=True), args.obsidian_exe)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
