"""Read-only duplicate inventory for one registered course and its exact files."""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

from preservation import utc_now, workspace_path
from preservation_delivery import (approved_decision, checkout_entry, cleanup_plan,
                                   installed_derivative)
from project import local_child, read, sha


SHA256 = re.compile(r"[a-f0-9]{64}")
MIB = 1024 * 1024
ROLES = ("manual_download", "package_original", "reading_vault")


def cleanup_pressure(path, *, minimum_free_percent=10, next_task_bytes=None, stage_complete=False):
    if not 0 <= minimum_free_percent <= 100:
        raise ValueError("可用空间阈值应在 0 到 100 之间")
    if next_task_bytes is not None and next_task_bytes < 0:
        raise ValueError("后续任务预计用量不能是负数")
    usage = shutil.disk_usage(path)
    free_percent = usage.free * 100 / usage.total
    reasons = []
    if stage_complete:
        reasons.append("当前阶段已完成")
    if next_task_bytes is not None and next_task_bytes > usage.free:
        reasons.append("剩余空间不足以处理后续任务")
    if free_percent < minimum_free_percent:
        reasons.append("可用空间低于阈值")
    return {"path": str(path), "total_bytes": usage.total, "free_bytes": usage.free,
            "free_percent": round(free_percent, 2), "minimum_free_percent": minimum_free_percent,
            "next_task_bytes": next_task_bytes, "stage_complete": stage_complete,
            "cleanup_preflight_needed": bool(reasons), "reasons": reasons, "read_only": True}


def allocated_bytes(file, facts):
    if os.name != "nt":
        return facts.st_blocks * 512 if hasattr(facts, "st_blocks") else None
    import ctypes
    import msvcrt

    class FileStandardInfo(ctypes.Structure):
        _fields_ = [("allocation", ctypes.c_longlong), ("length", ctypes.c_longlong),
                    ("links", ctypes.c_ulong), ("delete_pending", ctypes.c_ubyte),
                    ("directory", ctypes.c_ubyte)]

    measure = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    measure.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong)
    measure.restype = ctypes.c_int
    result = FileStandardInfo()
    storage_info_class = 1
    if not measure(msvcrt.get_osfhandle(file.fileno()), storage_info_class,
                   ctypes.byref(result), ctypes.sizeof(result)):
        return None
    if result.directory or result.delete_pending or result.length != facts.st_size or result.links != facts.st_nlink:
        return None
    return result.allocation


def identity(facts):
    return (str(facts.st_dev), str(facts.st_ino), facts.st_size, facts.st_nlink, facts.st_mtime_ns)


def open_facts(path):
    expected = path.lstat()
    if not stat.S_ISREG(expected.st_mode) or path.is_symlink():
        raise ValueError("非独立的普通文件路径")
    with path.open("rb") as stream:
        facts = os.fstat(stream.fileno())
        if identity(facts) != identity(expected):
            raise ValueError("实物在检查期间变化")
        return {"volume_id": str(facts.st_dev), "file_id": str(facts.st_ino), "bytes": facts.st_size,
                "hardlinks": facts.st_nlink, "mtime_ns": facts.st_mtime_ns,
                "allocated_bytes": allocated_bytes(stream, facts)}


def physical(file):
    return file["volume_id"], file["file_id"]


def exact_digest(file):
    path = Path(file["path"])
    expected = tuple(file[key] for key in ("volume_id", "file_id", "bytes", "hardlinks", "mtime_ns"))
    with path.open("rb") as stream:
        if identity(os.fstat(stream.fileno())) != expected:
            raise ValueError("重读前实物已变化")
        fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
        if identity(os.fstat(stream.fileno())) != expected or identity(path.lstat()) != expected:
            raise ValueError("哈希计算期间实物已变化")
    return fingerprint


def cleanup_status(workspace, package, state, vault, entry, approval):
    current = installed_derivative(package, vault, entry, state["sha256"], sha(entry))
    if not current:
        return {"state": "no_verified_preservation", "candidate": None}
    try:
        scope = cleanup_plan(package)
    except ValueError:
        return {"state": "pending_formal_reader", "candidate": None}
    phase = current["source_storage"]
    selected = scope["candidate"]
    result = {"candidate": selected, "scope_sha256": scope["scope_sha256"], "observed": phase}
    if phase.startswith("指定原件副本已主动清理"):
        return {**result, "state": ("removed_pending_reopen" if "待删后重开" in phase else "removed_and_reopened")}
    if phase != "指定原件副本在位":
        return {**result, "state": "reconciliation_required"}
    if approval is None:
        return {**result, "state": "candidate_owner_approval_not_supplied"}
    try:
        _, digest = approved_decision(workspace, approval, scope)
    except (OSError, ValueError, TypeError, KeyError) as error:
        return {**result, "state": "approval_invalid", "detail": str(error).splitlines()[0]}
    return {**result, "state": "owner_approved_pending_removal", "approval_sha256": digest}


def plan_groups(rows, focus, limit, cleanup, reading):
    by_name = {row["file"]: row for row in rows}
    if len(by_name) != len(rows):
        raise ValueError("原件台账存在重复相对路径")
    groups = defaultdict(list)
    for row in rows:
        digest = row["sha256"]
        if type(row["bytes"]) is not int or row["bytes"] <= 0 or not isinstance(digest, str) or not SHA256.fullmatch(digest):
            continue
        groups[(digest, row["bytes"])].append(row["file"])
    selected = cleanup.get("candidate")
    preferred = Path(selected["path"]).relative_to(reading).as_posix() if selected else None
    if focus:
        unknown = set(focus) - by_name.keys()
        if unknown:
            raise ValueError("指定文件未登记在该课程原件清单")
        selected_groups = {(by_name[name]["sha256"], by_name[name]["bytes"]) for name in focus}
        return [(key, members) for key, members in groups.items() if key in selected_groups]
    return sorted(groups.items(), key=lambda pair: (preferred not in pair[1], -pair[0][1], pair[1][0]))[:limit]


def group_observations(roots, names, size):
    observed = []
    for name in names:
        for role, root in roots.items():
            file = {"relative": name, "role": role}
            try:
                path = local_child(root, name)
                file["path"] = str(path)
                facts = open_facts(path)
                file.update(facts)
                state = "size_matches" if facts["bytes"] == size else "size_changed"
            except FileNotFoundError:
                state = "absent"
            except (OSError, ValueError):
                state = "unreadable"
            observed.append({**file, "state": state})
    return observed


def classify_group(fingerprint, size, related, remaining):
    hypotheses = defaultdict(list)
    for file in related:
        if file["state"] == "size_matches":
            hypotheses[physical(file)].append(file)
    enough = len(hypotheses) >= 2 and len(hypotheses) * size <= remaining
    if not enough:
        return ("hash_budget_limited" if len(hypotheses) >= 2 else "one_physical_copy_or_missing"), 0, []
    verified = []
    read_budget = 0
    for paths in hypotheses.values():
        canonical = paths[0]
        read_budget += size
        try:
            matched = exact_digest(canonical) == fingerprint
        except (OSError, ValueError):
            matched = False
        for file in paths:
            # A second directory entry is useful only while it still resolves to the same file.
            try:
                observed = {**file, **open_facts(Path(file["path"]))}
                intact = physical(file) == physical(observed) and all(
                    observed[field] == file[field] for field in ("bytes", "mtime_ns", "hardlinks"))
            except (OSError, ValueError):
                intact = False
            file["state"] = "exact_sha256" if matched and intact else "changed_or_unreadable"
        good = [file for file in paths if file["state"] == "exact_sha256"]
        if good:
            verified.append(good)
    return ("exact_duplicate" if len(verified) >= 2 else "no_exact_duplicate"), read_budget, verified


def inventory(workspace, package, *, focus=(), limit=12, maximum_hash_bytes=512 * MIB, approval=None):
    if not 1 <= limit <= 30 or not 1 <= maximum_hash_bytes <= 4 * 1024 * MIB:
        raise ValueError("盘点范围或哈希读取预算超出上限")
    workspace = Path(workspace).resolve(strict=True)
    package = workspace_path(workspace, package)
    approval = workspace_path(workspace, approval) if approval is not None else None
    origin = workspace_path(workspace, read(local_child(package, ".course/project.json"))["origin"])
    intake_path = local_child(package, ".course/ledger/intake.json")
    intake_digest = sha(intake_path)
    source_files = read(intake_path)["sources"]
    state, vault, entry = checkout_entry(package)
    entry = workspace_path(workspace, entry)
    reading = local_child(entry.parent.parent, "原始资料")
    roots = dict(zip(ROLES, (origin, local_child(package, "原始资料"), reading)))
    cleanup = cleanup_status(workspace, package, state, vault, entry, approval)
    groups = plan_groups(source_files, focus, limit, cleanup, reading)
    remaining = maximum_hash_bytes
    items = []
    for (fingerprint, size), names in groups:
        observed = group_observations(roots, names, size)
        group_state, used, verified = classify_group(fingerprint, size, observed, remaining)
        remaining -= used
        candidates = []
        retained_identity = {physical(files[0]) for files in verified
                             if any(file["role"] == "package_original" for file in files)}
        if retained_identity and group_state == "exact_duplicate":
            for files in verified:
                first = files[0]
                if (len(files) == 1 and first["role"] == "reading_vault" and first["hardlinks"] == 1
                        and physical(first) not in retained_identity and first["allocated_bytes"] is not None):
                    candidates.append(first["allocated_bytes"])
        items.append({"sha256_in_intake": fingerprint, "file_bytes": size, "relative_names": names,
                      "state": group_state, "locations": observed, "verified_physical_copies": len(verified),
                      "same_content_reading_vault_allocated_bytes": sum(candidates)})
    items.sort(key=lambda group: (-group["same_content_reading_vault_allocated_bytes"],
                                  group["state"] != "exact_duplicate", -group["file_bytes"]))
    if sha(intake_path) != intake_digest:
        raise ValueError("盘点期间课程台账发生变化")
    return {"schema_version": 1, "record_type": "local_duplicate_inventory", "observed_at": utc_now(),
            "package": str(package), "entry": str(entry), "read_only": True,
            "intake_sha256": intake_digest, "registered_files": len(source_files), "inspected_groups": len(items),
            "hash_bytes_read_at_most": maximum_hash_bytes - remaining,
            "possible_duplicate_reading_vault_allocated_bytes": sum(
                row["same_content_reading_vault_allocated_bytes"] for row in items),
            "actual_volume_released_bytes": None, "cleanup_approval": cleanup, "items": items}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("package", type=Path, nargs="?")
    parser.add_argument("--pressure-only", action="store_true", help="只读取指定目录所在磁盘的清理预检条件")
    parser.add_argument("--minimum-free-percent", type=float, default=10)
    parser.add_argument("--next-task-bytes", type=int)
    parser.add_argument("--stage-complete", action="store_true")
    parser.add_argument("--file", action="append", default=[], help="原件清单中的精确相对路径，可重复指定")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--hash-mib", type=int, default=512)
    parser.add_argument("--owner-decision", type=Path)
    args = parser.parse_args()
    try:
        if args.pressure_only:
            if args.package or args.file or args.owner_decision:
                raise ValueError("只读空间检查无需提供课程包、文件或授权")
            result = cleanup_pressure(args.workspace, minimum_free_percent=args.minimum_free_percent,
                                      next_task_bytes=args.next_task_bytes, stage_complete=args.stage_complete)
        else:
            if args.package is None:
                raise ValueError("课程空间盘点需要课程包")
            result = inventory(args.workspace, args.package, focus=args.file, limit=args.limit,
                               maximum_hash_bytes=args.hash_mib * MIB, approval=args.owner_decision)
            result["cleanup_pressure"] = cleanup_pressure(Path(result["entry"]).parent,
                                                          minimum_free_percent=args.minimum_free_percent,
                                                          next_task_bytes=args.next_task_bytes,
                                                          stage_complete=args.stage_complete)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
