"""Retire authorized redundant copies while preserving the formal raw snapshot."""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote

from preservation import proved_file, require_original_mp4_audio, run_isolated_reader, utc_now, workspace_path
from preservation_delivery import (CLEANUP, FORMAL_READER, INSTALL, LEDGER, MANIFEST, check_formal_assets, check_parent_evidence,
                                   check_reader, checkout_entry, course_from_manifest, digest_json, identity_matches,
                                   installed_derivative, move_exact_windows_file, path_absent, remove_exact_windows_file,
                                   require_cleanup_coverage)
from project import file_lock, local_child, put, read, sha
from scope_review import publication_scope
from storage_inventory import cleanup_pressure, open_facts


METHOD = "staged_verified_original_copies"
RECEIPTS = "_exchange/codex/cleanup/retirements"
ROLES = ("manual_download", "reading_vault")
FIELDS = ("bytes", "file_id", "volume_id", "hardlinks")
LINK = re.compile(r"\]\((<[^<>\r\n]+>|[^)\r\n]+)\)|!?\[\[([^\]\r\n]+)\]\]|"
                  r"(?m:^\s*\[[^\]\r\n]+\]:\s*(<[^<>\r\n]+>|\S+))|\b(?:href|src)\s*=\s*[\"']([^\"'\r\n]+)[\"']")
COMMENT = re.compile(r"<!--[\s\S]*?-->")


def basis(workspace, selection):
    batch = workspace_path(workspace, selection["batch"])
    if not batch.is_relative_to(local_child(workspace, "整理结果")):
        raise ValueError("仅可按工作区正式批次选择冗余原件")
    queue = read(batch)
    rows = [row for row in queue["projects"] if row["sequence"] == selection["sequence"]]
    if len(rows) != 1 or rows[0]["status"] not in ("已验收", "已验收（配套待补）"):
        raise ValueError("当前项目尚未完成实收独立验收或正在续作")
    row = rows[0]
    package = workspace_path(workspace, row["package"])
    origin = workspace_path(workspace, read(local_child(package, ".course/project.json"))["origin"])
    state, vault, entry = checkout_entry(package)
    workspace_path(workspace, entry)
    parent, child = state["sha256"], sha(entry)
    publication = row["publication"]
    if (Path(queue["vault"]) != vault or Path(publication["entry"]) != entry or publication["sha256"] != parent
            or origin != local_child(workspace_path(workspace, queue["source"]), row["name"])):
        raise ValueError("批次、已签收正文和实收位置没有同一身份")
    if not installed_derivative(package, vault, entry, parent, child):
        raise ValueError("需先安装经独立签收且切离拟退场原件的同名教材")
    require_cleanup_coverage(package, parent)
    directory = local_child(package, f"{LEDGER}/{child}")
    legacy_cleanup = local_child(directory, CLEANUP)
    if legacy_cleanup.is_file() and read(legacy_cleanup).get("state") != "removed":
        raise ValueError("旧入口已有未核清的退场提交，请先对账")
    manifest = course_from_manifest(local_child(directory, MANIFEST))
    intake = local_child(package, ".course/ledger/intake.json")
    formal_reader = local_child(directory, FORMAL_READER)
    check_parent_evidence(manifest, workspace)
    check_reader(formal_reader, manifest)
    parent_publication = read(local_child(package, f".course/ledger/publications/{parent}.json"))
    identity = {"batch": str(batch), "sequence": row["sequence"], "project_id": row["id"], "package": str(package),
                "entry": str(entry), "vault": str(vault), "origin": str(origin), "parent_body_sha256": parent,
                "derived_body_sha256": child, "install_sha256": sha(local_child(directory, INSTALL)),
                "manifest_sha256": sha(local_child(directory, MANIFEST)), "intake_sha256": sha(intake),
                "formal_reader_sha256": sha(formal_reader)}
    return identity, manifest, row, read(intake), publication_scope(parent_publication)["scope_status"]


def scan_references(vault, paths):
    targets = {str(path).casefold() for path in paths}
    names = {path.name.casefold() for path in paths}
    for note in vault.rglob("*.md"):
        if not note.is_file() or note.resolve() != note:
            continue
        body = COMMENT.sub("", note.read_text("utf-8-sig"))
        for markdown, wiki, definition, html in LINK.findall(body):
            raw = unquote((markdown or wiki or definition or html).strip("<>").split("|")[0].split("#")[0])
            if not raw or re.match(r"^(?:https?|obsidian):", raw, re.I):
                continue
            path = Path(re.sub(r"^file:/+", "", raw, flags=re.I))
            for root in (note.parent, vault) if wiki else (note.parent,):
                resolved = os.path.normpath(root / path).casefold()
                if resolved in targets or (wiki and len(path.parts) == 1 and path.name.casefold() in names):
                    raise ValueError(f"阅读库仍引用该拟退场原件 {note}")


def plan(workspace, selection, *, minimum_free_percent=10, next_task_bytes=None, stage_complete=False):
    identity, manifest, row, intake, status = basis(workspace, selection)
    package, entry, origin = (Path(identity[key]) for key in ("package", "entry", "origin"))
    source_map = {part["file"]: part["sha256"] for part in intake["sources"]}
    chosen = selection.get("copies", ())
    if not isinstance(chosen, list) or not chosen:
        raise ValueError("请逐件指定原件相对路径及要退场的副本位置")
    candidates, retained, seen = [], {}, set()
    volume = str(workspace.stat().st_dev)
    for item in sorted(chosen, key=lambda item: (item["file"], item["role"])):
        name, role = item["file"], item["role"]
        if (role not in ROLES or (name, role) in seen or Path(name).suffix.casefold() != ".mp4"
                or role == "manual_download" and (status != "完整验收" or row["status"] != "已验收")):
            raise ValueError("范围重复、存在受保护原件，或拟退场实收副本尚无完整验收")
        seen.add((name, role))
        proof = manifest["source_videos"].get(name)
        digest = source_map.get(name)
        if (not proof or not digest or proof["signed_intake_sha256"] != digest
                or f"原始资料/{name}" in manifest["retained"]):
            raise ValueError(f"原件仍用于阅读，或缺少同版独立保全 {name}")
        require_original_mp4_audio(package, name, digest)
        canonical = local_child(package, f"原始资料/{name}")
        if name not in retained:
            exact = proved_file(canonical, digest)
            signed = proof["copies"]["package_snapshot"]
            if Path(signed["path"]) != canonical or any(exact[field] != signed[field] for field in FIELDS):
                raise ValueError(f"正式原件快照已变化 {name}")
            retained[name] = {"file": name, "path": str(canonical), **exact}
        path = local_child(origin if role == "manual_download" else entry.parent.parent / "原始资料", name)
        facts = open_facts(path)
        signed = proof["copies"][role]
        if (Path(signed["path"]) != path or facts["hardlinks"] != 1 or facts["volume_id"] != volume
                or any(facts[field] != signed[field] for field in FIELDS)
                or facts["file_id"] == retained[name]["file_id"] and facts["volume_id"] == retained[name]["volume_id"]
                or proved_file(path, digest)["file_id"] != facts["file_id"]):
            raise ValueError(f"不是已锁定的磁盘独立副本 {name} · {role}")
        candidates.append({"file": name, "role": role, "path": str(path), "sha256": digest, **facts})
    if len({(f["volume_id"], f["file_id"]) for f in candidates}) != len(candidates):
        raise ValueError("候选含同一物理文件的重复身份")
    scan_references(Path(identity["vault"]), [Path(item["path"]) for item in candidates])
    scope = {"record_type": "course_local_cleanup_scope", "method": METHOD, "basis": identity,
             "candidates": candidates, "retained_originals": list(retained.values())}
    return {"scope": {**scope, "scope_sha256": digest_json(scope)}, "read_only": True,
            "pressure": cleanup_pressure(workspace, minimum_free_percent=minimum_free_percent,
                                         next_task_bytes=next_task_bytes, stage_complete=stage_complete),
            "estimated_allocated_bytes": sum(item["allocated_bytes"] or 0 for item in candidates),
            "unknown_allocations": sum(item["allocated_bytes"] is None for item in candidates)}


def approval(workspace, decision, scope):
    decision = workspace_path(workspace, decision)
    if decision.parent != local_child(workspace, "_exchange/control/decisions") or decision.suffix != ".json":
        raise ValueError("需要当次具体范围的用户授权文件")
    digest = sha(decision)
    info = read(decision)
    if (info.get("schema_version") != 1 or info.get("record_type") != "course_local_cleanup_authorization"
            or info.get("actor") != "user" or info.get("decision") != "approve" or info.get("method") != METHOD
            or info.get("scope_sha256") != scope["scope_sha256"] or info.get("candidates") != scope["candidates"]
            or not isinstance(info.get("human_message_ref"), str) or not info["human_message_ref"].strip()
            or sha(decision) != digest):
        raise ValueError("授权未覆盖当前副本、暂存及正式退场范围")
    return {"path": str(decision), "sha256": digest, "human_message_ref": info["human_message_ref"]}


def staged_file(directory, index, candidate):
    return {**candidate, "path": str(local_child(directory, f"objects/{index:05}.mp4"))}


def locations(directory, scope):
    for index, candidate in enumerate(scope["candidates"]):
        staged = staged_file(directory, index, candidate)
        original_exists, staged_exists = (not path_absent(Path(item["path"])) for item in (candidate, staged))
        if original_exists and not staged_exists and identity_matches(candidate, True):
            yield "original"
        elif staged_exists and not original_exists and identity_matches(staged, True):
            yield "staged"
        elif not original_exists and not staged_exists:
            yield "absent"
        else:
            yield "conflict"


def reader(entry, manifest, directory, name, executable):
    output = local_child(directory, name)
    if not output.is_file():
        run_isolated_reader(entry.parent.parent, manifest, output, executable)
    check_reader(output, manifest)
    check_formal_assets(entry, manifest, entry)
    return {"path": str(output), "sha256": sha(output)}


def operate(workspace, scope, action, decision=None, executable=None):
    digest = scope.get("scope_sha256")
    if scope.get("method") != METHOD or digest_json({k: v for k, v in scope.items() if k != "scope_sha256"}) != digest:
        raise ValueError("范围摘要不符，请从预检重做")
    directory = local_child(workspace, f"{RECEIPTS}/{digest}")
    receipt = local_child(directory, "receipt.json")
    for item in [*scope["candidates"], *scope["retained_originals"]]:
        if workspace_path(workspace, item["path"]) != Path(item["path"]):
            raise ValueError("退场与保留路径均须是工作区里的规范路径")
    current = read(receipt) if receipt.is_file() else None
    if action == "status":
        return {"state": current["state"] if current else "unsubmitted", "locations": list(locations(directory, scope)),
                "receipt": current, "read_only": True}
    if action == "handoff":
        if (not current or current["scope"] != scope or current["state"] != "verified"
                or any(location != "absent" for location in locations(directory, scope))):
            raise ValueError("还没有当前范围的本地退场及重开回读")
        identity, manifest, *_ = basis(workspace, scope["basis"])
        reader_proof = local_child(directory, "reader-after-removal.json")
        if (identity != scope["basis"] or current["post_cleanup_reader"] != {
                "path": str(reader_proof), "sha256": sha(reader_proof)}
                or not all(identity_matches(file, True) for file in scope["retained_originals"])):
            raise ValueError("本地教材、保全或删后回读已变化，暂不能交接 DSH")
        check_reader(reader_proof, manifest)
        scan_references(Path(identity["vault"]), [Path(item["path"]) for item in scope["candidates"]])
        return {"schema_version": 1, "record_type": "course_local_retirement_verified", "execution_allowed": False,
                "local_project_id": identity["project_id"], "local_batch_sequence": identity["sequence"],
                "scope_sha256": digest, "local_retirement_receipt": {"path": str(receipt), "sha256": sha(receipt)},
                "current_body_sha256": identity["derived_body_sha256"],
                "next": "专用 DSH 核对同版网盘账号、实物范围并另行取得当次授权"}
    author = approval(workspace, decision, scope) if action != "restore" else None
    identity = scope["basis"]
    batch, package, vault, entry = (workspace_path(workspace, identity[k]) for k in ("batch", "package", "vault", "entry"))
    directory.mkdir(parents=True, exist_ok=True)
    local_child(directory, "objects").mkdir(exist_ok=True)
    with ExitStack() as locks:
        for lock in (batch.with_suffix(batch.suffix + ".lock"), local_child(package, ".course/lock"),
                     local_child(vault, ".obsidian/worldanalysis-install.lock"), local_child(directory, "lock")):
            locks.enter_context(file_lock(lock, blocking=True))
        current = read(receipt) if receipt.is_file() else None
        if current and (current["scope"] != scope or (author and current["authorization"] != author)):
            raise ValueError("已有不同范围或授权的提交记录")
        candidates = [Path(item["path"]) for item in scope["candidates"]]
        if action != "restore":
            actual, manifest, *_ = basis(workspace, identity)
            if actual != identity or not all(identity_matches(file, True) for file in scope["retained_originals"]):
                raise ValueError("教材、签收、实收或保留原件已变化，先核对当前范围")
            scan_references(vault, candidates)
        present = list(locations(directory, scope))
        if action == "stage" and not current:
            if any(item != "original" for item in present):
                raise ValueError("原始路径发生变化，禁止提交暂存")
            selection = {**identity, "copies": [{k: item[k] for k in ("file", "role")} for item in scope["candidates"]]}
            if plan(workspace, selection)["scope"] != scope:
                raise ValueError("预检对象已变化，重新冻结范围")
            current = {"state": "staging", "scope": scope, "authorization": author, "submitted_at": utc_now(),
                       "removed": [], "volume_free_before_bytes": shutil.disk_usage(workspace).free}
            put(receipt, current)
        if current is None:
            raise ValueError("暂存范围尚未提交")
        if current["state"] == "verified":
            if any(item != "absent" for item in present):
                raise ValueError("已退场路径出现变化，需人工对账")
            return current
        if action == "restore":
            if current["removed"] or current.get("delete_submitted") is not None or "absent" in present or "conflict" in present:
                raise ValueError("已进入移除或实物待对账，不能自动恢复")
            for index, (candidate, location) in enumerate(zip(scope["candidates"], present)):
                if location == "staged":
                    move_exact_windows_file(staged_file(directory, index, candidate), Path(candidate["path"]))
            if any(result != "original" for result in locations(directory, scope)):
                raise ValueError("恢复后的目录回读不一致")
            current.update(state="restored", restored_at=utc_now())
        elif action == "stage":
            if current["state"] not in ("staging", "staged", "restored") or any(item not in ("original", "staged") for item in present):
                raise ValueError("已有待对账或已执行记录，禁止重新提交")
            for index, (candidate, location) in enumerate(zip(scope["candidates"], present)):
                if location == "original":
                    if sha(Path(author["path"])) != author["sha256"]:
                        raise ValueError("暂存前具体授权已变化")
                    move_exact_windows_file(candidate, Path(staged_file(directory, index, candidate)["path"]))
            if any(result != "staged" for result in locations(directory, scope)):
                raise ValueError("暂存后实际目录读回不一致")
            current.update(state="staged", staged_at=utc_now())
        elif action == "finalize":
            expected = ["absent" if index in current["removed"] else "staged" for index in range(len(present))]
            retained = {item["file"]: item for item in scope["retained_originals"]}
            if (current["state"] not in ("staged", "removing", "removed") or present != expected
                    or current.get("delete_submitted") is not None):
                raise ValueError("存在未确认的移除提交或路径变化，先对账，禁止重复提交")
            if not current["removed"]:
                current["detached_reader"] = reader(entry, manifest, directory, "reader-while-detached.json", executable)
                scan_references(vault, candidates)
            for index, candidate in enumerate(scope["candidates"]):
                if index in current["removed"]:
                    continue
                if (sha(Path(author["path"])) != author["sha256"] or not path_absent(Path(candidate["path"]))
                        or not identity_matches(retained[candidate["file"]], True)):
                    raise ValueError("移除前授权、来源路径或保留原件发生变化")
                current.update(state="removing", delete_submitted=index)
                put(receipt, current)
                staged = staged_file(directory, index, candidate)
                remove_exact_windows_file(staged)
                if not path_absent(Path(staged["path"])):
                    raise ValueError("移除后的文件状态不明，先对账")
                current["removed"].append(index)
                current.update(delete_submitted=None)
                put(receipt, current)
            free_after = shutil.disk_usage(workspace).free
            current.update(state="removed", volume_free_after_bytes=free_after,
                           observed_volume_delta_bytes=free_after - current["volume_free_before_bytes"])
            put(receipt, current)
            current["post_cleanup_reader"] = reader(entry, manifest, directory, "reader-after-removal.json", executable)
            if (any(result != "absent" for result in locations(directory, scope))
                    or not all(identity_matches(file, True) for file in scope["retained_originals"])):
                raise ValueError("移除后路径或保留原件读回不符，暂不进入后续清理")
            current.update(state="verified", verified_at=utc_now(),
                           verified_candidate_allocated_bytes=sum(item["allocated_bytes"] or 0 for item in scope["candidates"]),
                           unknown_allocations=sum(item["allocated_bytes"] is None for item in scope["candidates"]),
                           downstream={"state": "waiting_exact_cloud_scope", "executor": "dedicated_dsh", "execution_allowed": False})
        put(receipt, current)
        return current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("trigger", "plan", "stage", "status", "finalize", "restore", "handoff"))
    parser.add_argument("workspace", type=Path)
    parser.add_argument("input", type=Path)
    parser.add_argument("--decision", type=Path)
    parser.add_argument("--obsidian-exe", type=Path)
    parser.add_argument("--minimum-free-percent", type=float, default=10)
    parser.add_argument("--next-task-bytes", type=int)
    parser.add_argument("--stage-complete", action="store_true")
    args = parser.parse_args()
    try:
        workspace = args.workspace.resolve(strict=True)
        payload = read(workspace_path(workspace, args.input))
        if args.action in ("trigger", "plan"):
            triggers = {"minimum_free_percent": args.minimum_free_percent, "next_task_bytes": args.next_task_bytes,
                        "stage_complete": args.stage_complete}
            pressure = cleanup_pressure(workspace, **triggers)
            result = pressure if args.action == "trigger" and not pressure["cleanup_preflight_needed"] else plan(workspace, payload, **triggers)
        else:
            if args.action in ("stage", "finalize") and args.decision is None:
                raise ValueError("请提供当前范围的具体授权")
            if args.action == "finalize" and args.obsidian_exe is None:
                raise ValueError("正式退场需指定 Obsidian，以便实际重开和读回")
            result = operate(workspace, payload.get("scope", payload), args.action, args.decision, args.obsidian_exe)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (KeyError, OSError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
