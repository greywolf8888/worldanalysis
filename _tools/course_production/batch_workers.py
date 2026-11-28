"""Bind analysis workers to real queue identities and independent packages."""

from contextlib import nullcontext
from pathlib import Path
import re
from uuid import uuid4

import batch
from project import CourseProject, file_lock, put, read


FIELD = "analysis_workers"
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
TOKEN = re.compile(r"[0-9a-f]{32}")
REGISTRY = Path("整理结果/.course-dispatch/分类索引.json")
PACKAGES = REGISTRY.parent / "worker-packages"


def worker_state(info):
    settings = info.get(FIELD, {"limit": 1, "assignments": {}})
    limit, assignments = settings.get("limit"), settings.get("assignments")
    if type(limit) is not int or not 1 <= limit <= 3 or not isinstance(assignments, dict):
        raise ValueError("分析工作分配记录无效")
    return settings


def pending_revision(row):
    return {"history_entries": len(row.get("history") or ()), "last_changed": row.get("last_changed"),
            "ready_at": row.get("ready_at")}


def current_assignment(row, job):
    if not job:
        return None
    pending = row["status"] == batch.PENDING and job.get("pending_revision") == pending_revision(row)
    active = row["status"] == batch.ACTIVE and row.get("package") == job["package"]
    if pending or active:
        return job


def worker_root(package):
    return next((parent for parent in Path(package).parents
                 if parent.name == PACKAGES.name and parent.parent.name == PACKAGES.parent.name
                 and parent.parent.parent.name == PACKAGES.parent.parent.name), None)


def check_formal_claim(info, row, package, ticket):
    job = current_assignment(row, worker_state(info)["assignments"].get(row["id"]))
    if not job:
        if ticket or (package is not None and worker_root(package) is not None):
            raise ValueError("该项目没有有效的分析交接")
        return
    # A completed formal claim can be reopened by the existing formal entry.
    if row["status"] == batch.ACTIVE and row.get("package") == str(package) and not ticket:
        return
    if job["state"] != "ready" or ticket != job["ticket"] or str(package) != job["package"]:
        raise ValueError("项目已分配分析。交接准备完成后使用当前领取凭据")
    return job


def read_registry(registry):
    from batch_dispatch import SCHEMA, load_bound, course_pairs

    info = read(registry)
    if info.get("schema") != SCHEMA or not isinstance(info.get("bindings"), dict):
        raise ValueError("请选择正式分类索引")
    root = batch.safe_path(info["root"])
    workspace = root.parent
    if root.name != "分析目标下载地址" or registry != workspace / REGISTRY:
        raise ValueError("分类索引与原件总目录不在同一项目内")
    bound = load_bound(info)
    pairs = {}
    for binding, row in course_pairs(bound):
        if row["id"] in pairs:
            raise ValueError("发现重复的正式项目身份，请先对账")
        pairs[row["id"]] = (binding, row)
    return info, workspace, bound, pairs


def publication_lock(package):
    workers = worker_root(package)
    if workers is None:
        return nullcontext()
    registry = batch.safe_path(workers.parent / REGISTRY.name)
    return file_lock(registry.with_suffix(registry.suffix + ".lock"))


def require_worker_publication(package, action, *, vault=None, folder=None):
    workers = worker_root(package)
    if workers is None:
        return
    relative = package.relative_to(workers)
    if len(relative.parts) != 3:
        raise ValueError("请从正式队列接手独立分析目录")
    project_id, ticket, _ = relative.parts
    info, _, _, pairs = read_registry(batch.safe_path(workers.parent / REGISTRY.name))
    if project_id not in pairs:
        raise ValueError("请从正式队列接手独立分析目录")
    binding, row = pairs[project_id]
    job = worker_state(info)["assignments"].get(project_id) or {}
    claimed = (row["status"] == batch.ACTIVE and job.get("state") == "ready"
               and job.get("ticket") == ticket and job.get("package") == str(package))
    published = row["status"] in batch.PUBLISHED and action in ("confirm-vault", "verify-reader")
    if row.get("package") != str(package) or not (claimed or published):
        raise ValueError("请从正式队列接手独立分析目录")
    if (vault is not None and batch.safe_path(vault) != batch.safe_path(binding["vault"])) or (
            folder is not None and folder != (row.get("course_folder") or row["name"])):
        raise ValueError("请选择该项目已登记的教材位置")


def present_assignments(settings, pairs):
    return {key: job for key, job in settings["assignments"].items()
            if key in pairs and current_assignment(pairs[key][1], job)}


def validate_session(session):
    if not isinstance(session, str) or not SESSION.fullmatch(session):
        raise ValueError("请提供可追溯的分析会话身份")


def new_assignment(workspace, binding, row, version, session, previous=None):
    ticket = uuid4().hex
    package = batch.safe_path(workspace / PACKAGES / row["id"] / ticket / row["name"])
    if package.exists() or not package.is_relative_to(workspace / PACKAGES):
        raise ValueError("无法准备独立分析目录")
    job = {"ticket": ticket, "session": session, "state": "working", "project_id": row["id"],
           "sequence": row["sequence"], "manifest": binding["manifest"],
           "source": str(Path(binding["source"]) / row["name"]), "package": str(package),
           "course_folder": row.get("course_folder") or row["name"], "source_version": version,
           "pending_revision": pending_revision(row),
           "assigned_at": batch.timestamp()}
    if previous:
        job["previous_package"] = previous["package"]
    return job


def run_workers(registry, action, *, limit=None, session=None, ticket=None):
    from batch_dispatch import dispatch_selection, local_priority_identity

    registry = batch.safe_path(registry)
    if not registry.is_file():
        raise ValueError("正式分类索引不存在")
    # Shared lock with dispatch and formal claim keeps the capacity authoritative.
    with file_lock(registry.with_suffix(registry.suffix + ".lock"), blocking=True):
        info, workspace, bound, pairs = read_registry(registry)
        settings = worker_state(info)
        jobs = present_assignments(settings, pairs)
        formal = {key for key, (_, row) in pairs.items() if row["status"] == batch.ACTIVE}
        capacity = settings["limit"]
        active_count = len(formal | jobs.keys())
        result = None
        if action == "status":
            return {"limit": capacity, "in_use": active_count, "formal_active": len(formal),
                    "assignments": [{k: v for k, v in job.items() if k != "ticket"} for job in jobs.values()]}
        if action == "configure":
            if type(limit) is not int or not 1 <= limit <= 3:
                raise ValueError("上限须为一至三")
            capacity = limit
            result = {"limit": capacity, "in_use": active_count}
        elif action == "reserve":
            validate_session(session)
            if len(formal) > 1:
                raise ValueError("发现多个正式在办项目，请先对账")
            if any(job["session"] == session for job in jobs.values()):
                raise ValueError("该会话已有项目，请接续或显式交接")
            if active_count >= capacity:
                raise ValueError("分析席位已满")
            # Work can proceed in separate packages while the one production
            # publisher retains the active row and the original acceptance gates.
            selected = dispatch_selection(bound, info, preparing=True).pair
            if not selected:
                raise ValueError("没有可分配的就绪项目")
            binding, row = selected
            version = local_priority_identity(workspace, binding, row)
            result = jobs[row["id"]] = new_assignment(workspace, binding, row, version, session)
        else:
            if not isinstance(ticket, str) or not TOKEN.fullmatch(ticket):
                raise ValueError("请提供当前分析领取凭据")
            job = next((job for job in jobs.values() if job["ticket"] == ticket), None)
            if job is None:
                raise ValueError("领取凭据已更换或失效")
            key = job["project_id"]
            if key in formal:
                raise ValueError("该项目已由正式队列接手，请从原正式入口接续")
            binding, row = pairs[key]
            if action == "release":
                jobs.pop(key)
                result = {"project_id": key, "state": "released"}
            elif action == "recover":
                validate_session(session)
                if any(other["session"] == session for other in jobs.values()):
                    raise ValueError("请使用一个尚未分配的接续会话")
                version = local_priority_identity(workspace, binding, row)
                if version != job["source_version"]:
                    raise ValueError("原件版本已有变化，请释放旧任务后重新核对")
                result = jobs[key] = new_assignment(workspace, binding, row, version, session, job)
            elif action == "ready":
                if local_priority_identity(workspace, binding, row) != job["source_version"]:
                    raise ValueError("原件版本已有变化，不能交接旧版工作")
                course = CourseProject.open(batch.safe_path(job["package"]))
                with course.locked():
                    queue = batch.open_queue(Path(binding["manifest"]))
                    source, files, _ = batch.identify(queue, row)
                    batch.verify_identity(course, source, files, row)
                result = jobs[key] = {**job, "state": "ready", "ready_at": job.get("ready_at") or batch.timestamp()}
            else:
                raise ValueError("未知分析分配操作")
        updated = {"limit": capacity, "assignments": jobs}
        if updated != settings:
            put(registry, {**info, FIELD: updated})
        return result
