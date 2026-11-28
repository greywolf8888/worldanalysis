"""Install an independently reviewed portable edition at its existing reader entry."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from course_companions import verify_published_companions
from independent_facts import configured_reviewer
from preservation import (MANIFEST, companion_proofs, file_identity, proved_file, reader_link_count,
                          require_original_mp4_audio, restore_parent, retained_links, run_isolated_reader,
                          utc_now, verify_stage, workspace_path)
from project import copy_file, file_lock, local_child, named_entry, put, read, sha


LEDGER = ".course/ledger/preservation"
FORMAL_READER = "formal-reader.json"
POST_CLEANUP_READER = "post-cleanup-reader.json"
REVIEW = "independent-review.json"
REVIEWER_READER = "independent-reader.json"
INSTALL = "install.json"
CLEANUP = "local-cleanup.json"
SHA256 = re.compile(r"[a-f0-9]{64}")
CONFIRMED = ("only_provenance_changed", "all_figures", "all_retained_attachments",
             "no_source_media_dependency", "candidate_identity_checked")


def digest_json(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def checkout_entry(package):
    state = read(local_child(package, ".course/ledger/obsidian-delivery.json"))
    vault = Path(state["vault"]).resolve(strict=True)
    entry = named_entry(local_child(vault, state["folder"]))
    if str(entry) != state["entry"] or not entry.is_file():
        raise ValueError("独立成品只允许沿原正式同名日常入口安装")
    return state, vault, entry


def check_reader(reader, manifest):
    rendered = read(reader)
    checked = rendered["checks"]
    if len(checked) != 1:
        raise ValueError("阅读回执必须只有对应的一篇同名教材")
    view = checked[0]
    figures = {Path(name).name for name, spec in manifest["retained"].items() if spec["role"] == "figure"}
    expected = f"{Path(manifest['course_dir']).name}/{manifest['note']}"
    if (view.get("document_sha256") != manifest["derived_body_sha256"] or view.get("file") != expected
            or view.get("mode") != "preview" or view.get("reopened") is not True
            or view.get("reachedBottom") is not True or view.get("rawLinks") != reader_link_count(manifest)
            or view.get("missingRawLinks") != [] or view.get("visibleCitations") != []
            or view.get("horizontalOverflow") is not False
            or rendered.get("companions", {}) != companion_proofs(manifest)
            or {image["name"] for image in view["images"] if image.get("fit") is True} != figures):
        raise ValueError("同版真实重开回执与正文、图片或附件白名单不一致")


def check_signed_review(review_path, reader, manifest, manifest_sha, expected_reviewer):
    evidence = read(review_path)
    reviewer = evidence.get("reviewer") or {}
    stamp = datetime.fromisoformat(reviewer.get("reviewed_at", ""))
    confirmations = CONFIRMED + (("all_companions_readable",) if companion_proofs(manifest) else ())
    if (not isinstance(expected_reviewer, str) or not expected_reviewer.strip()
            or evidence.get("schema_version") != 1 or evidence.get("record_type") != "course_preservation_review"
            or reviewer.get("thread_id") != expected_reviewer or reviewer.get("role") != "测试agent" or not stamp.tzinfo
            or manifest.get("reviewer_thread_id") != expected_reviewer or evidence.get("decision") != "通过"
            or evidence.get("parent_body_sha256") != manifest["parent_body_sha256"]
            or evidence.get("derived_body_sha256") != manifest["derived_body_sha256"]
            or evidence.get("manifest_sha256") != manifest_sha
            or evidence.get("reader", {}).get("sha256") != sha(reader)
            or any((evidence.get("confirmed") or {}).get(key) is not True for key in confirmations)):
        raise ValueError("指定测试 Agent 的独立成品回执缺失、不同版或未通过")
    check_reader(reader, manifest)
    return evidence


def course_from_manifest(manifest_path):
    manifest = read(manifest_path)
    parent, child = (manifest[key] for key in ("parent_body_sha256", "derived_body_sha256"))
    if (manifest.get("schema_version") != 1 or manifest.get("state") != "等待独立签收"
            or parent == child or not all(SHA256.fullmatch(digest) for digest in (parent, child))):
        raise ValueError("成品清单未绑定两个有效的正文 SHA")
    return manifest


def check_formal_assets(entry, manifest, body_path):
    course = entry.parent.parent
    body = body_path.read_bytes().decode("utf-8")
    parent = restore_parent(body, manifest["changes"])
    if hashlib.sha256(parent.encode("utf-8")).hexdigest() != manifest["parent_body_sha256"]:
        raise ValueError("派生正文无法还原为旧已签收正文")
    if sha(body_path) != manifest["derived_body_sha256"]:
        raise ValueError("独立成品正文已发生变化")
    retained = {name: spec for name, spec in manifest["retained"].items() if spec["role"] != "note"}
    if retained_links(course, entry, body, manifest) != {name: spec["role"] for name, spec in retained.items()}:
        raise ValueError("实际正文依赖与签收白名单不一致")
    for name, spec in retained.items():
        proved = proved_file(local_child(course, name), spec["sha256"])
        if proved["bytes"] != spec["bytes"]:
            raise ValueError(f"正式阅读库保留素材已变化 {name}")


def candidate_specs(manifest, target):
    candidates = []
    for name, source in manifest["source_videos"].items():
        if not source.get("proposed_removal"):
            continue
        candidate = source["copies"]["reading_vault"]
        if (Path(candidate["path"]) != local_child(target, f"原始资料/{name}")
                or candidate["path"] != source["proposed_removal"]
                or candidate["hardlinks"] != 1
                or candidate["sha256"] != source["signed_intake_sha256"]
                or f"原始资料/{name}" in manifest["retained"]):
            raise ValueError("待核清理对象与签收白名单或磁盘身份冲突")
        candidates.append(candidate)
    if len(candidates) != 1:
        raise ValueError("当前接入只接受一件已物理定位的冗余副本")
    return candidates


def check_parent_evidence(manifest, workspace):
    for evidence in manifest["parent_evidence"].values():
        if sha(workspace_path(workspace, evidence["path"])) != evidence["sha256"]:
            raise ValueError("父版签收依据发生变化")


def check_parent_companions(package, entry, manifest):
    companions = companion_proofs(manifest)
    if companions:
        parent = manifest["parent_body_sha256"]
        publication = local_child(package, f".course/ledger/publications/{parent}.json")
        record = read(publication)
        if (sha(publication) != manifest["parent_evidence"]["publication"]["sha256"]
                or verify_published_companions(entry.parent.parent, record) != companions
                or record["review"]["independent_fact_review"].get("companions") != companions):
            raise ValueError("独立保全配套与正式签收父版不一致")


def identity_matches(candidate, require_hash=False):
    path = Path(candidate["path"])
    try:
        visible = proved_file(path, candidate["sha256"]) if require_hash else file_identity(path)
    except (OSError, ValueError):
        return False
    return all(candidate.get(field) == value for field, value in visible.items())


def prepare_plan(package, stage, review_path, workspace):
    state, vault, entry = checkout_entry(package)
    manifest_path = stage / MANIFEST
    manifest = course_from_manifest(manifest_path)
    actual = verify_stage(stage)
    if (str(entry) != manifest["formal_entry"] or state["sha256"] != manifest["parent_body_sha256"]
            or manifest["note"] != entry.relative_to(entry.parent.parent).as_posix()):
        raise ValueError("成品与课程包和同名日常入口绑定不一致")
    if sha(local_child(package, ".course/ledger/intake.json")) != manifest["parent_intake_sha256"]:
        raise ValueError("原件台账已变化，需要重核当前范围")
    check_parent_evidence(manifest, workspace)
    check_parent_companions(package, entry, manifest)
    current = sha(entry)
    if current not in (manifest["parent_body_sha256"], manifest["derived_body_sha256"]):
        raise ValueError("正式正文已有用户编辑或新内容，清理版计划等待人工对照")
    review = read(review_path)
    independent_reader = workspace_path(workspace, review.get("reader", {}).get("path", ""))
    if independent_reader == stage / "Obsidian独立重开.json":
        raise ValueError("独立测试 Agent 须提供独立重开证据")
    check_signed_review(review_path, independent_reader, manifest, actual["manifest_sha256"],
                        configured_reviewer(workspace=workspace))
    note = local_child(stage, f"{manifest['course_dir']}/{manifest['note']}")
    check_formal_assets(entry, manifest, note)
    candidates = candidate_specs(manifest, entry.parent.parent)
    if not all(identity_matches(file, require_hash=True) for file in candidates):
        raise ValueError("日常库待核对象已变化或不在位")
    install = {
        "schema_version": 1, "record_type": "course_preservation_install",
        "vault": str(vault), "entry": str(entry), "parent_body_sha256": state["sha256"],
        "derived_body_sha256": manifest["derived_body_sha256"],
        "manifest_sha256": actual["manifest_sha256"], "review_sha256": sha(review_path),
        "reader_sha256": sha(independent_reader), "candidates": candidates,
    }
    return install, manifest_path, review_path, independent_reader, note


def reviewed_install(package, stage, review, workspace):
    state, vault, entry = checkout_entry(package)
    with file_lock(local_child(package, ".course/lock"), blocking=True):
        with file_lock(local_child(vault, ".obsidian/worldanalysis-install.lock"), blocking=True):
            record, manifest, review, reader, note = prepare_plan(package, stage, review, workspace)
            before = sha(entry)
            child = record["derived_body_sha256"]
            ledger = local_child(package, f"{LEDGER}/{child}")
            if before == child:
                if not (ledger / INSTALL).is_file() or read(ledger / INSTALL) != record:
                    raise ValueError("正式正文已是派生 SHA，但缺本次安装记录")
                return record
            if before != state["sha256"] or read(local_child(package, ".course/ledger/obsidian-delivery.json")) != state:
                raise ValueError("并发期间正式签收或用户正文变动")
            for src, dst, fingerprint in (
                (manifest, ledger / MANIFEST, record["manifest_sha256"]),
                (review, ledger / REVIEW, record["review_sha256"]),
                (reader, ledger / REVIEWER_READER, record["reader_sha256"]),
            ):
                if dst.exists() and sha(dst) != fingerprint:
                    raise ValueError(f"派生版证据与旧记录冲突 {dst}")
                copy_file(src, dst, fingerprint)
            if (ledger / INSTALL).is_file() and read(ledger / INSTALL) != record:
                raise ValueError("同一派生正文已有不同待执行记录")
            # Record the recoverable decision before the atomic reader-note replacement.
            put(ledger / INSTALL, record)
            if sha(entry) != before:
                raise ValueError("安装前用户正文再次变化")
            copy_file(note, entry, child)
            if sha(entry) != child:
                raise ValueError("替换后正式正文读回不一致")
    from vault_catalog import refresh_catalog

    refresh_catalog(vault, packages=(package,))
    return record


def installed_derivative(package, vault, entry, parent, child):
    """Describe current storage only if the exact installed edition and evidence still agree."""
    if not SHA256.fullmatch(child):
        return None
    directory = local_child(package, f"{LEDGER}/{child}")
    try:
        install = read(local_child(directory, INSTALL))
        manifest_path = local_child(directory, MANIFEST)
        manifest = course_from_manifest(manifest_path)
        if (install.get("schema_version") != 1 or path_mismatch(install, manifest, vault, entry, parent, child)
                or sha(manifest_path) != install["manifest_sha256"]):
            return None
        review = local_child(directory, REVIEW)
        reader = local_child(directory, REVIEWER_READER)
        if sha(review) != install["review_sha256"] or sha(reader) != install["reader_sha256"]:
            return None
        check_signed_review(review, reader, manifest, install["manifest_sha256"], manifest.get("reviewer_thread_id"))
        check_formal_assets(entry, manifest, entry)
        check_parent_companions(package, entry, manifest)
        if sha(local_child(package, ".course/ledger/intake.json")) != manifest["parent_intake_sha256"]:
            return None
        candidate = candidate_specs(manifest, entry.parent.parent)[0]
        if install.get("candidates") != [candidate]:
            return None
        formal_reader = local_child(directory, FORMAL_READER)
        if formal_reader.is_file():
            check_reader(formal_reader, manifest)
        after_removal = local_child(directory, POST_CLEANUP_READER)
        if after_removal.is_file():
            check_reader(after_removal, manifest)
        status = original_storage(directory, install, candidate, formal_reader, after_removal)
        return {"status": "独立成品已重开" if formal_reader.is_file() else "独立成品已接入，待原入口重开",
                "source_storage": status, "parent_body_sha256": parent, "derived_body_sha256": child}
    except (OSError, ValueError, TypeError, KeyError, StopIteration):
        return None


def path_mismatch(install, manifest, vault, entry, parent, child):
    return (install.get("record_type") != "course_preservation_install"
            or Path(install["vault"]) != vault or Path(install["entry"]) != entry
            or Path(manifest["formal_entry"]) != entry
            or (install["parent_body_sha256"], install["derived_body_sha256"]) != (parent, child)
            or (manifest["parent_body_sha256"], manifest["derived_body_sha256"]) != (parent, child))


def original_storage(directory, install, candidate, formal_reader, after_removal):
    path = Path(candidate["path"])
    receipt = local_child(directory, CLEANUP)
    if receipt.is_file():
        plan = read(receipt)
        if (plan.get("state") == "removed" and plan.get("candidate") == candidate
                and plan.get("install_sha256") == digest_json(install)
                and formal_reader.is_file() and plan.get("formal_reader_sha256") == sha(formal_reader)
                and isinstance(plan.get("authorization_ref"), str) and plan["authorization_ref"].strip()
                and plan.get("scope_sha256") == digest_json(cleanup_scope(install, candidate, formal_reader))
                and path_absent(path)):
            return "指定原件副本已主动清理" + ("" if after_removal.is_file() else "，待删后重开")
        return "指定原件副本清理待对账"
    if path_absent(path):
        return "指定原件副本缺失待核"
    return "指定原件副本在位" if identity_matches(candidate) else "指定原件副本身份待核"


def path_absent(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def cleanup_scope(install, candidate, reader):
    return {"record_type": "course_local_cleanup_scope", "method": "permanent_single_file",
            "entry": install["entry"], "install_sha256": digest_json(install), "candidate": candidate,
            "formal_reader_sha256": sha(reader)}


def require_cleanup_coverage(package, parent_sha):
    from scope_review import publication_scope

    if not isinstance(parent_sha, str) or not SHA256.fullmatch(parent_sha):
        raise ValueError("缺少当前教材的正式验收记录")
    publication = read(local_child(package, f".course/ledger/publications/{parent_sha}.json"))
    body = publication.get("body")
    if not isinstance(body, str) or hashlib.sha256(body.encode("utf-8")).hexdigest() != parent_sha:
        raise ValueError("教材与正式验收记录版本不一致")
    if publication_scope(publication)["scope_status"] not in ("完整验收", "配套待补"):
        raise ValueError("实收资料范围尚未通过验收或仍有核心资料待补，保留原件")


def cleanup_plan(package):
    state, vault, entry = checkout_entry(package)
    child = sha(entry)
    if not installed_derivative(package, vault, entry, state["sha256"], child):
        raise ValueError("需要在位的已独立签收派生版")
    directory = local_child(package, f"{LEDGER}/{child}")
    install = read(local_child(directory, INSTALL))
    reader = local_child(directory, FORMAL_READER)
    if not reader.is_file():
        raise ValueError("先完成正式同名日常入口重开")
    candidate = install["candidates"][0]
    if not local_child(directory, CLEANUP).is_file():
        require_cleanup_coverage(package, state["sha256"])
        name = Path(candidate["path"]).relative_to(entry.parent.parent / "原始资料").as_posix()
        require_original_mp4_audio(package, name, candidate["sha256"])
    scope = cleanup_scope(install, candidate, reader)
    return {**scope, "scope_sha256": digest_json(scope)}


def approved_decision(workspace, decision, scope):
    directory = local_child(workspace, "_exchange/control/decisions")
    if decision.parent != directory or decision.suffix != ".json":
        raise ValueError("授权须来自总控已登记的独立具体决定")
    raw = decision.read_bytes()
    evidence = json.loads(raw.decode("utf-8-sig"))
    event = evidence.get("human_message_ref")
    if (evidence.get("schema_version") != 1 or evidence.get("record_type") != "course_local_cleanup_authorization"
            or evidence.get("actor") != "user" or evidence.get("decision") != "approve"
            or evidence.get("method") != scope["method"] or evidence.get("scope_sha256") != scope["scope_sha256"]
            or evidence.get("candidate") != scope["candidate"]
            or not isinstance(event, str) or not event.strip()):
        raise ValueError("授权没有对应实际用户确认及当前单件范围")
    return event, hashlib.sha256(raw).hexdigest()


@contextmanager
def locked_windows_candidate(candidate):
    """Pin identity with a Windows handle that excludes writes and renames."""
    if os.name != "nt":
        raise ValueError("此清理执行器只支持 Windows 文件身份")
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close_handle = kernel.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    set_info = kernel.SetFileInformationByHandle
    set_info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    set_info.restype = wintypes.BOOL
    path = candidate["path"]
    generic_read, delete = 0x80000000, 0x00010000
    share_read, open_existing, open_reparse_point = 0x1, 3, 0x00200000
    handle = create(path, generic_read | delete, share_read, None, open_existing, open_reparse_point, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        close_handle(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        locked = os.fstat(stream.fileno())
        identity = (str(locked.st_dev), str(locked.st_ino), locked.st_nlink, locked.st_size)
        expected = tuple(candidate[key] for key in ("volume_id", "file_id", "hardlinks", "bytes"))
        if identity != expected or hashlib.file_digest(stream, "sha256").hexdigest() != candidate["sha256"]:
            raise ValueError("锁定后的文件身份或指纹与已授权对象不符")
        yield kernel, handle


def remove_exact_windows_file(candidate):
    import ctypes

    with locked_windows_candidate(candidate) as (kernel, handle):
        delete_file = ctypes.c_ubyte(1)
        if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(delete_file), ctypes.sizeof(delete_file)):
            raise ctypes.WinError(ctypes.get_last_error())


def move_exact_windows_file(candidate, destination):
    import ctypes
    from ctypes import wintypes

    target = str(destination).encode("utf-16-le")

    class RenameInfo(ctypes.Structure):
        _fields_ = [("flags", wintypes.DWORD), ("root", wintypes.HANDLE),
                    ("length", wintypes.DWORD), ("name", ctypes.c_char * (len(target) + 2))]

    rename = RenameInfo(length=len(target))
    ctypes.memmove(ctypes.addressof(rename) + RenameInfo.name.offset, target, len(target))
    with locked_windows_candidate(candidate) as (kernel, handle):
        if not kernel.SetFileInformationByHandle(handle, 3, ctypes.byref(rename), ctypes.sizeof(rename)):
            raise ctypes.WinError(ctypes.get_last_error())


def execute_cleanup(package, workspace, decision):
    state, vault, entry = checkout_entry(package)
    lock = local_child(package, ".course/lock")
    with file_lock(lock, blocking=True):
        with file_lock(local_child(vault, ".obsidian/worldanalysis-install.lock"), blocking=True):
            scope = cleanup_plan(package)
            authorization, decision_sha = approved_decision(workspace, decision, scope)
            child = sha(entry)
            directory = local_child(package, f"{LEDGER}/{child}")
            receipt = local_child(directory, CLEANUP)
            if receipt.is_file():
                prior = read(receipt)
                if (prior.get("state") == "removed" and prior.get("scope_sha256") == scope["scope_sha256"]
                        and path_absent(Path(scope["candidate"]["path"]))):
                    return prior
                raise ValueError("清理已有提交记录，需要先对账，禁止重发")
            candidate = scope["candidate"]
            manifest = course_from_manifest(local_child(directory, MANIFEST))
            check_parent_evidence(manifest, workspace)
            sources = next(source["copies"] for source in manifest["source_videos"].values()
                           if source.get("proposed_removal") == candidate["path"])
            retained_copies = [sources[role] for role in ("manual_download", "package_snapshot")]
            if not all(identity_matches(file, require_hash=True) for file in [candidate, *retained_copies]):
                raise ValueError("清理件或仍须留存的物理副本发生变化")
            check_formal_assets(entry, manifest, entry)
            if sha(decision) != decision_sha:
                raise ValueError("清理前具体授权文件被修改")
            record = {**scope, "state": "submitted", "submitted_at": utc_now(),
                      "authorization_ref": authorization, "decision_sha256": decision_sha}
            put(receipt, record)
            remove_exact_windows_file(candidate)
            if not path_absent(Path(candidate["path"])):
                raise ValueError("删除调用后的目录读回未确认移除，需先对账")
            record = {**record, "state": "removed", "readback_at": utc_now()}
            put(receipt, record)
    from vault_catalog import refresh_catalog

    refresh_catalog(vault, packages=(package,))
    return record


def reconcile_cleanup(package):
    state, vault, entry = checkout_entry(package)
    child = sha(entry)
    edition = installed_derivative(package, vault, entry, state["sha256"], child)
    if not edition:
        raise ValueError("当前正式教材或保留件未通过独立成品检查")
    directory = local_child(package, f"{LEDGER}/{child}")
    attempt = local_child(directory, CLEANUP)
    install = read(local_child(directory, INSTALL))
    candidate = install["candidates"][0]
    path = Path(candidate["path"])
    present = not path_absent(path)
    return {"entry": str(entry), "source_storage": edition["source_storage"],
            "path_present": present, "current_fingerprint_matches": identity_matches(candidate, True) if present else None,
            "attempt": read(attempt) if attempt.is_file() else None,
            "checked_at": utc_now()}


def verify_installed_reader(package, executable):
    state, vault, entry = checkout_entry(package)
    child = sha(entry)
    if not installed_derivative(package, vault, entry, state["sha256"], child):
        raise ValueError("尚未安装当前已指定独立签收的成品")
    directory = local_child(package, f"{LEDGER}/{child}")
    manifest = course_from_manifest(local_child(directory, MANIFEST))
    with file_lock(local_child(package, ".course/lock"), blocking=True):
        current = installed_derivative(package, vault, entry, state["sha256"], child)
        if not current:
            raise ValueError("正式教材或清理状态并发变化，请重新读取")
        receipt_name = POST_CLEANUP_READER if current["source_storage"].startswith("指定原件副本已主动清理") else FORMAL_READER
        receipt = local_child(directory, receipt_name)
        if not receipt.is_file():
            run_isolated_reader(entry.parent.parent, manifest, receipt, executable)
            check_reader(receipt, manifest)
            if sha(entry) != child:
                raise ValueError("Obsidian 重开期间正式教材内容改变")
    from vault_catalog import refresh_catalog

    refresh_catalog(vault, packages=(package,))
    return installed_derivative(package, vault, entry, state["sha256"], child)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("plan", "reviewed-install"):
        sub = commands.add_parser(name)
        for arg in ("workspace", "package", "stage", "review"):
            sub.add_argument(arg, type=Path)
    reopen = commands.add_parser("verify-installed-reader")
    reopen.add_argument("workspace", type=Path)
    reopen.add_argument("package", type=Path)
    reopen.add_argument("--obsidian-exe", type=Path, required=True)
    for name in ("cleanup-plan", "execute-cleanup", "reconcile-cleanup"):
        sub = commands.add_parser(name)
        sub.add_argument("workspace", type=Path)
        sub.add_argument("package", type=Path)
        if name == "execute-cleanup":
            sub.add_argument("decision", type=Path)
    args = parser.parse_args()
    try:
        workspace = args.workspace.resolve(strict=True)
        package = workspace_path(workspace, args.package)
        if args.action == "verify-installed-reader":
            result = verify_installed_reader(package, args.obsidian_exe)
        elif args.action == "cleanup-plan":
            result = cleanup_plan(package)
        elif args.action == "execute-cleanup":
            result = execute_cleanup(package, workspace, workspace_path(workspace, args.decision))
        elif args.action == "reconcile-cleanup":
            result = reconcile_cleanup(package)
        else:
            stage = workspace_path(workspace, args.stage)
            review = workspace_path(workspace, args.review)
            if args.action == "plan":
                install, *_ = prepare_plan(package, stage, review, workspace)
                result = {**install, "install_sha256": digest_json(install), "state": "待正式安装",
                          "changed": False, "deleted": False}
            else:
                result = reviewed_install(package, stage, review, workspace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
