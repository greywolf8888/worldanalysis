"""Read current path bindings and version the destinations of future work."""

from argparse import ArgumentParser
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from shutil import disk_usage
import stat
import sys
from tempfile import NamedTemporaryFile


BASE = Path(__file__).absolute().parents[3]
INGEST = Path("_ingest/wangzjk-to-pan")
REVIEW = INGEST / "v4-review"
RESULTS = Path("整理结果")
INPUT = Path("分析目标下载地址")
REGISTRY = RESULTS / ".course-dispatch/分类索引.json"
WORKTREE = Path(".runtime/course-production-worktree")
COURSE_TOOLS = WORKTREE / "_tools/course_production"
LEGACY_VAULT = INPUT / "02_短视频与自媒体/分析结果"
PATH_CONFIG = INGEST / "state/workspace-path-versions.json"
LEGACY_VERSION = "legacy"
CLOUD_DESTINATION = "/apps/教育分析学习/wangzjk.com"
READ_LIMIT = 32 * 1024 * 1024
PROPOSAL_ROLES = {
    "catalog_data": "未来采集数据目录",
    "intake": "未来收件目录",
    "analysis_work": "未来分析工作目录",
    "delivery_vault": "未来正式教材目录",
    "test_vault": "未来独立验收目录",
    "history_backup": "未来历史备份目录",
}
PATHS = (
    ("registered_input", "正式收件根", INPUT, "directory"),
    ("registry", "正式分类索引", REGISTRY, "file"),
    ("catalog_data", "采集与简介数据", REVIEW / "data", "directory"),
    ("catalog_index", "分类与版本索引", REVIEW / "data/pipeline-runs.sqlite3", "file"),
    ("analysis_order", "分析顺序", REVIEW / "data/analysis-priority.sqlite3", "file"),
    ("analysis_tools", "正式分析入口", COURSE_TOOLS / "course-agent.ps1", "file"),
    ("course_runtime", "项目运行目录", Path(".runtime"), "directory"),
    ("course_scratch", "分析临时目录", Path(".runtime/tmp"), "directory"),
    ("course_run_logs", "分析运行记录", Path(".runtime/agent-runs"), "directory"),
    ("course_reviews", "独立事实验收记录", Path(".runtime/independent-fact-reviews"), "directory"),
    ("results", "本地成果根", RESULTS, "directory"),
    ("production_log", "正式生产记录", RESULTS / "生产控制", "directory"),
    ("transfer_history", "转存履历", INGEST / "state/journal.json", "file"),
    ("download_history", "已下载资料记录", INGEST / "state/downloaded-project-markers.json", "file"),
    ("dsh_executor", "网盘执行入口", INGEST / "fast-agent.mjs", "file"),
    ("dsh_bridge", "网盘桥接安装资料", Path(".runtime/v4-dsh-bridge-install"), "directory"),
    ("dsh_tasks", "网盘确认任务", Path("_exchange/control/tasks"), "directory"),
    ("dsh_decisions", "用户确认记录", Path("_exchange/control/decisions"), "directory"),
    ("dsh_authorizations", "网盘授权回执", Path("_exchange/dsh/authorizations"), "directory"),
    ("dsh_plans", "网盘正式计划", Path("_exchange/dsh/plans"), "directory"),
    ("dsh_receipts", "网盘读回记录", Path("_exchange/dsh/receipts"), "directory"),
    ("intake_receipts", "本地实收记录", Path("_exchange/codex/intake"), "directory"),
)
PROTECTED = (
    INGEST, WORKTREE, Path("_tools"), Path("_exchange"), Path(".git"), Path(".agents"),
    Path(".runtime/cache"), Path(".runtime/tools"), Path(".runtime/v4-dsh-bridge-source"),
    RESULTS / ".course-dispatch",
)
CATEGORY_ROLES = {"analysis_work": "analysis_work", "delivery_vault": "vault", "test_vault": "test_vault"}
FORMAL_FIXED_ROLES = {
    "intake": "正式批次仍使用原收件根，请先核对并登记迁移范围。",
    "delivery_vault": "正式批次仍使用已签收的教材位置，请先核对并登记迁移范围。",
}
CATALOG_REQUIRED_FILES = ("codex-public.json", "public-details.json", "pipeline-runs.sqlite3")


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(raw).hexdigest()


def normalize_path(workspace, value):
    if not isinstance(value, (str, Path)):
        raise ValueError("路径须为文字")
    text = str(value)
    if not text or len(text) > 1024 or any(ord(char) < 32 for char in text):
        raise ValueError("路径含有无效字符")
    path = Path(text)
    if path.anchor and not path.is_absolute():
        raise ValueError("请填写项目内的绝对路径或相对路径")
    if not path.is_absolute():
        path = workspace / path
    if path == workspace or ".." in path.parts or not path.is_relative_to(workspace):
        raise ValueError("路径须位于 G 盘 Worldanalysis 项目目录内")
    relative = path.relative_to(workspace)
    is_reserved = getattr(os.path, "isreserved", None)
    if any(":" in part or part in (".", "..") or (is_reserved and is_reserved(part)) for part in relative.parts):
        raise ValueError("路径含有系统保留名称或无效字符")
    return path


def inspect_path(path, kind):
    """Use lstat on every parent, including the nearest existing directory."""
    parent = None
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            return "missing", parent
        reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if reparse or stat.S_ISLNK(info.st_mode):
            raise ValueError("路径经过链接或重解析点")
        directory = stat.S_ISDIR(info.st_mode)
        if item != path or kind == "directory":
            if not directory:
                raise ValueError("路径与现有文件重叠")
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError("配置文件不是普通文件")
        parent = item if directory else parent
    return "observed", parent


def observed(workspace, value, kind):
    try:
        path = normalize_path(workspace, value)
    except ValueError:
        return None, "unverified"
    try:
        return path, inspect_path(path, kind)[0]
    except (OSError, ValueError):
        return path, "unverified"


def read_object(path):
    with path.open("rb") as stream:
        raw = stream.read(READ_LIMIT + 1)
    if len(raw) > READ_LIMIT:
        raise ValueError("正式登记表超出读取范围")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("正式登记格式无效")
    return value


def overlaps(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


def read_path_config(workspace):
    file = workspace / PATH_CONFIG
    if inspect_path(file, "file")[0] == "missing":
        return {"schema_version": 1, "workspace": workspace.as_posix(), "versions": {}, "switches": []}
    config = read_object(file)
    if (config.get("schema_version") != 1 or config.get("workspace") != workspace.as_posix()
            or not isinstance(config.get("versions"), dict) or not isinstance(config.get("switches"), list)):
        raise ValueError("保存位置版本记录无法核实")
    versions = config["versions"]
    known = {LEGACY_VERSION}
    for version_id, version in versions.items():
        if (not isinstance(version, dict) or version_id != digest(version)
                or version.get("base_version_id") not in known or not isinstance(version.get("created_at"), str)
                or not isinstance(version.get("overrides"), dict) or not version["overrides"]
                or set(version["overrides"]) - PROPOSAL_ROLES.keys()
                or "minimum_free_bytes" not in version or not valid_capacity(version["minimum_free_bytes"])):
            raise ValueError("保存位置版本记录无法核实")
        for value in version["overrides"].values():
            if not isinstance(value, str) or normalize_path(workspace, value).as_posix() != value:
                raise ValueError("保存位置版本记录含有无效路径")
        known.add(version_id)
    active, previous = LEGACY_VERSION, None
    for change in config["switches"]:
        if (not isinstance(change, dict) or change.get("from_version_id") != active
                or not isinstance(change.get("changed_at"), str) or not isinstance(change.get("version_id"), str)):
            raise ValueError("保存位置切换记录无法核实")
        target = change.get("version_id")
        if change.get("action") == "activate" and target in versions and versions[target]["base_version_id"] == active:
            pass
        elif change.get("action") != "rollback" or previous is None or target != previous:
            raise ValueError("保存位置切换记录无法核实")
        active, previous = target, active
    return config


def active_versions(config):
    if not config["switches"]:
        return LEGACY_VERSION, None
    last = config["switches"][-1]
    return last["version_id"], last["from_version_id"]


@contextmanager
def path_config_lock(workspace):
    path = workspace / PATH_CONFIG.with_suffix(".lock")
    inspect_path(path, "file")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        try:
            if sys.platform == "win32":
                import msvcrt
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("保存位置正在更新，请刷新后重试") from error
        yield


def write_path_config(workspace, config):
    path = workspace / PATH_CONFIG
    raw = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(raw) > READ_LIMIT:
        raise ValueError("保存位置版本记录超过可读取范围")
    temp = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix="workspace-path-", suffix=".next", delete=False) as stream:
            temp = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def read_bindings(workspace):
    registry = workspace / REGISTRY
    state, _ = inspect_path(registry, "file")
    if state == "missing":
        return state, []
    index = read_object(registry)
    if index.get("schema") != 1 or Path(index["root"]) != workspace / INPUT or not isinstance(index["bindings"], dict):
        raise ValueError("正式分类索引与当前项目不一致")
    categories = []
    for category, entry in sorted(index["bindings"].items()):
        if not isinstance(entry, dict) or entry.get("category") != category:
            raise ValueError("分类映射缺少一致的登记身份")
        source, source_state = observed(workspace, entry.get("source"), "directory")
        vault, vault_state = observed(workspace, entry.get("vault"), "directory")
        manifest, manifest_state = observed(workspace, entry.get("manifest"), "file")
        if (source is None or vault is None or manifest is None
                or not source.is_relative_to(workspace / INPUT) or not vault.is_relative_to(workspace / INPUT)
                or not manifest.is_relative_to(workspace / RESULTS) or overlaps(source, vault)):
            raise ValueError("分类路径超出正式登记或相互重叠")
        if manifest_state == "observed":
            try:
                batch = read_object(manifest)
                same_binding = (Path(batch.get("source", "")), Path(batch.get("vault", "")),
                                Path(batch.get("dispatch_registry", ""))) == (source, vault, registry)
            except (OSError, ValueError, TypeError):
                same_binding = False
            if not same_binding:
                manifest_state = "unverified"
        testing, testing_state = observed(workspace, vault.parent / "测试分析稿件", "directory")
        work, work_state = observed(workspace, vault.parent / "正在分析", "directory")
        overview, overview_state = observed(workspace, vault / "00 入口/00 项目总览.md", "file")
        checks = (source_state, vault_state, manifest_state)
        combined = "unverified" if "unverified" in checks else "missing" if "missing" in checks else "observed"
        categories.append({
            "category": category, "source": source.as_posix(), "vault": vault.as_posix(),
            "manifest": manifest.as_posix(), "test_vault": testing.as_posix(), "analysis_work": work.as_posix(),
            "overview": overview.as_posix(), "state": combined, "source_state": source_state,
            "vault_state": vault_state, "manifest_state": manifest_state, "test_vault_state": testing_state,
            "analysis_work_state": work_state, "overview_state": overview_state,
        })
    return state, categories


def configured_reader_root(workspace):
    try:
        config = read_path_config(workspace)
        version = config["versions"].get(active_versions(config)[0], {})
        value = version.get("overrides", {}).get("delivery_vault")
        path, status = observed(workspace, value, "directory")
        return path if status == "observed" else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def reader_roots(workspace, projected_roots=()):
    """Use the current registered reading libraries, with a fallback for old workspaces."""
    workspace = Path(workspace).absolute()
    try:
        state, categories = read_bindings(workspace)
    except (OSError, ValueError, TypeError, KeyError):
        return ()
    if state == "observed":
        roots = [Path(category["vault"]) for category in categories
                 if category["vault_state"] == category["manifest_state"] == "observed"]
    else:
        legacy = [observed(workspace, value, "directory") for value in (*projected_roots, LEGACY_VAULT)]
        roots = [path for path, status in legacy if status == "observed"]
    if configured := configured_reader_root(workspace):
        roots.append(configured)
    return tuple(dict.fromkeys(roots))


def dispatch_paths(workspace=BASE):
    workspace = Path(workspace).absolute()
    entry = workspace / COURSE_TOOLS / "course.ps1"
    input_root = workspace / INPUT
    if inspect_path(entry, "file")[0] != "observed":
        raise ValueError("找不到正式课程运行入口")
    if inspect_path(input_root, "directory")[0] != "observed":
        raise ValueError("找不到正式收件目录")
    state, categories = read_bindings(workspace)
    if any(category["state"] == "unverified" for category in categories):
        raise ValueError("正式分类索引与收件或教材登记不一致")
    return {"entry": entry.as_posix(), "input": input_root.as_posix(),
            "registry": (workspace / REGISTRY).as_posix(), "registry_state": state}


def legacy_paths(workspace, categories, category=None):
    defaults = dict.fromkeys(PROPOSAL_ROLES)
    defaults["catalog_data"] = (workspace / REVIEW / "data").as_posix()
    defaults["intake"] = (workspace / INPUT).as_posix()
    available = [row for row in categories if row["state"] == "observed" and
                 (category is None or category == row["category"])]
    if len(available) == 1:
        for role, name in CATEGORY_ROLES.items():
            defaults[role] = available[0][name]
    return defaults


def version_paths(config, version_id, defaults):
    if version_id == LEGACY_VERSION:
        return defaults
    if version_id not in config["versions"]:
        raise ValueError("找不到所选保存位置版本")
    return {**defaults, **config["versions"][version_id]["overrides"]}


def path_bindings(*, workspace=BASE, version_id=None, category=None):
    """Resolve destinations once at job start and persist this result with the job."""
    workspace = Path(workspace).absolute()
    config = read_path_config(workspace)
    chosen = active_versions(config)[0] if version_id is None else version_id
    if chosen != LEGACY_VERSION and not any(change["version_id"] == chosen for change in config["switches"]):
        raise ValueError("所选保存位置版本尚未启用")
    try:
        _, categories = read_bindings(workspace)
    except (OSError, ValueError, TypeError, KeyError):
        categories = []
    paths = version_paths(config, chosen, legacy_paths(workspace, categories, category))
    unavailable = []
    for role in config["versions"].get(chosen, {}).get("overrides", {}):
        _, state = observed(workspace, paths[role], "directory")
        if state != "observed":
            unavailable.append(role)
            paths[role] = None
    return {"version_id": chosen, "paths": paths, "unavailable_roles": unavailable}


def job_target(role, *, workspace=BASE, version_id=None, category=None, name=None):
    workspace = Path(workspace).absolute()
    if role not in PROPOSAL_ROLES:
        raise ValueError("请选择支持的任务位置")
    binding = path_bindings(workspace=workspace, version_id=version_id, category=category)
    selected = binding["paths"][role]
    if selected is None:
        raise ValueError("当前任务位置尚不可用，请先核对保存位置")
    root = normalize_path(workspace, selected)
    if inspect_path(root, "directory")[0] != "observed" or root.resolve(strict=True) != root:
        raise ValueError("当前任务位置无法核实")
    if name is None:
        return root
    if not isinstance(name, str) or Path(name).name != name or role not in ("analysis_work", "history_backup"):
        raise ValueError("请使用该任务位置下的单一文件或项目名")
    target = normalize_path(workspace, root / name)
    if target.parent != root:
        raise ValueError("请使用该任务位置下的单一文件或项目名")
    inspect_path(target, "file" if role == "history_backup" else "directory")
    return target


def collection_files_ready(workspace, directory):
    try:
        directory = normalize_path(workspace, directory)
        for filename in CATALOG_REQUIRED_FILES:
            path = directory / filename
            if inspect_path(path, "file")[0] != "observed":
                return False
            with path.open("rb") as stream:
                header = stream.read(4096)
            prefix = b"SQLite format 3\x00" if path.suffix == ".sqlite3" else b"{"
            if not header.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(prefix):
                return False
    except (OSError, ValueError):
        return False
    return True


def formal_path_blocks(workspace, version):
    if workspace != BASE:
        return {}
    overrides = version["overrides"]
    blocked = {role: message for role, message in FORMAL_FIXED_ROLES.items() if role in overrides}
    if "catalog_data" in overrides and not collection_files_ready(workspace, overrides["catalog_data"]):
        blocked["catalog_data"] = "请先准备采集目录、简介和分类索引，再启用采集位置。"
    return blocked


def with_path_config(workspace, snapshot, config):
    defaults = legacy_paths(workspace, snapshot["categories"])
    active, previous = active_versions(config)
    versions = [{"version_id": version_id, "created_at": version["created_at"],
                 "base_version_id": version["base_version_id"], "paths": version_paths(config, version_id, defaults),
                 "blocked_roles": formal_path_blocks(workspace, version)}
                for version_id, version in reversed(config["versions"].items())]
    details = {**snapshot, "path_config": {
        "active_version_id": active, "previous_version_id": previous, "versions": versions,
        "paths": version_paths(config, active, defaults), "switches": config["switches"],
    }}
    details.pop("revision", None)
    return {**details, "revision": digest(details)}


def _snapshot(workspace, config):
    if inspect_path(workspace, "directory")[0] != "observed":
        raise ValueError("Worldanalysis 项目目录不存在")
    rows = []
    for key, label, relative, kind in PATHS:
        path, state = observed(workspace, relative, kind)
        rows.append({"key": key, "label": label, "path": path.as_posix() if path else None, "state": state})
    try:
        registry_state, categories = read_bindings(workspace)
    except (OSError, ValueError, TypeError, KeyError):
        registry_state, categories = "unverified", []
    details = {
        "schema_version": 1, "workspace": workspace.as_posix(), "paths": rows,
        "categories": categories, "registry_state": registry_state,
        "cloud_destination": {"path": CLOUD_DESTINATION, "state": "configured"}, "activation": "versioned",
        "proposal_roles": [{"key": key, "label": label} for key, label in PROPOSAL_ROLES.items()],
    }
    return with_path_config(workspace, details, config)


def snapshot(workspace=BASE):
    workspace = Path(workspace).absolute()
    return _snapshot(workspace, read_path_config(workspace))


def require_revision(current, revision):
    if revision != current["revision"]:
        raise ValueError("当前路径登记已变化，请刷新后重试")


def valid_capacity(value):
    return value is None or type(value) is int and 0 <= value < 2**63


def validated_paths(proposal, workspace, current, config, minimum_free_bytes):
    if current["registry_state"] != "observed" or any(cat["state"] == "unverified" for cat in current["categories"]):
        raise ValueError("正式分类路径尚待核实，暂不能预检改动")
    if not isinstance(proposal, dict) or not proposal or set(proposal) - PROPOSAL_ROLES.keys():
        raise ValueError("请选择受支持的未来业务目录")
    if not valid_capacity(minimum_free_bytes):
        raise ValueError("所需空间须填写有效字节数")
    protected = {workspace / relative for relative in PROTECTED}
    protected.update(Path(row["path"]) for row in current["paths"]
                     if row["path"] and row["key"] not in ("registered_input", "results", "course_runtime"))
    for category in current["categories"]:
        protected.update(Path(category[key]) for key in ("source", "vault", "manifest", "test_vault", "analysis_work"))
    used_versions = {change["version_id"] for change in config["switches"]} - {LEGACY_VERSION}
    used_paths = {(role, Path(path)) for version in used_versions
                  for role, path in config["versions"][version]["overrides"].items()}
    rows = []
    seen = set()
    for key, value in sorted(proposal.items()):
        label = PROPOSAL_ROLES[key]
        try:
            path = normalize_path(workspace, value)
            if (any(overlaps(path, other) for other in protected | seen) or any(
                    overlaps(path, other) for other_role, other in used_paths if (key, path) != (other_role, other))):
                raise ValueError("与现有正式资料或其他拟用目录重叠")
            state, parent = inspect_path(path, "directory")
            if parent is None or parent.resolve(strict=True) != parent:
                raise ValueError("请使用现有上级目录的实际路径")
            if not os.access(parent, os.R_OK | os.W_OK):
                raise ValueError("操作系统未报告上级目录的读写权限")
            try:
                free = disk_usage(parent).free
            except OSError:
                free = None
            if minimum_free_bytes is not None and (free is None or free < minimum_free_bytes):
                raise ValueError("尚无法确认满足所需空间")
        except (OSError, ValueError) as error:
            detail = str(error) if isinstance(error, ValueError) else "暂无法读取目录状态"
            raise ValueError(f"{label}预检未通过，{detail}") from error
        seen.add(path)
        rows.append({"key": key, "label": label, "path": path.as_posix(), "state": state, "free_bytes": free})
    return rows


def _preflight(proposal, workspace, current, config, minimum_free_bytes):
    active = active_versions(config)[0]
    active_overrides = config["versions"].get(active, {}).get("overrides", {})
    if not isinstance(proposal, dict) or not proposal:
        raise ValueError("请选择受支持的未来业务目录")
    combined = {**active_overrides, **proposal}
    rows = [row for row in validated_paths(combined, workspace, current, config, minimum_free_bytes)
            if row["key"] in proposal]
    identity = {"revision": current["revision"], "paths": {row["key"]: row["path"] for row in rows},
                "minimum_free_bytes": minimum_free_bytes}
    return {"schema_version": 1, "revision": current["revision"], "workspace": workspace.as_posix(),
            "proposal_id": digest(identity), "activation": "proposal_only", "applied": False,
            "paths": rows, "message": "路径核对通过，可以保存为新版本。"}


def preflight(proposal, *, workspace=BASE, revision, minimum_free_bytes=None):
    workspace = Path(workspace).absolute()
    config = read_path_config(workspace)
    current = _snapshot(workspace, config)
    require_revision(current, revision)
    return _preflight(proposal, workspace, current, config, minimum_free_bytes)


def save_version(proposal, *, revision, proposal_id, minimum_free_bytes=None, workspace=BASE):
    workspace = Path(workspace).absolute()
    proof = preflight(proposal, workspace=workspace, revision=revision, minimum_free_bytes=minimum_free_bytes)
    if proof["proposal_id"] != proposal_id:
        raise ValueError("拟用目录与预检结果不一致，请重新核对")
    with path_config_lock(workspace):
        config = read_path_config(workspace)
        current = _snapshot(workspace, config)
        require_revision(current, revision)
        preview = _preflight(proposal, workspace, current, config, minimum_free_bytes)
        if preview["proposal_id"] != proposal_id:
            raise ValueError("拟用目录与预检结果不一致，请重新核对")
        active = active_versions(config)[0]
        inherited = config["versions"].get(active, {}).get("overrides", {})
        overrides = {**inherited, **{row["key"]: row["path"] for row in preview["paths"]}}
        if overrides == inherited:
            raise ValueError("拟用目录与当前设置相同")
        version = {"base_version_id": active, "created_at": datetime.now(timezone.utc).isoformat(),
                   "overrides": overrides, "minimum_free_bytes": minimum_free_bytes}
        version_id = digest(version)
        config["versions"][version_id] = version
        result = with_path_config(workspace, current, config)
        write_path_config(workspace, config)
        return {**result, "saved_version_id": version_id}


def _switch_version(workspace, revision, action, version_id=None):
    workspace = Path(workspace).absolute()
    require_revision(snapshot(workspace), revision)
    with path_config_lock(workspace):
        config = read_path_config(workspace)
        current = _snapshot(workspace, config)
        require_revision(current, revision)
        active, previous = active_versions(config)
        if action == "activate":
            version = config["versions"].get(version_id)
            if version is None or version["base_version_id"] != active:
                raise ValueError("该版本已无法从当前设置启用，请重新核对并保存")
        else:
            if previous is None:
                raise ValueError("没有可恢复的上一套保存位置")
            version_id = previous
            version = config["versions"].get(version_id)
        if version is not None:
            if blocked := formal_path_blocks(workspace, version):
                raise ValueError("".join(blocked.values()))
            rows = validated_paths(version["overrides"], workspace, current, config, version["minimum_free_bytes"])
            if any(row["state"] != "observed" for row in rows):
                raise ValueError("请先准备并核对全部拟用目录，再切换保存位置")
        elif current["registry_state"] != "observed" or any(row["state"] != "observed" for row in current["categories"]):
            raise ValueError("原正式目录尚未核实，暂不能恢复")
        config["switches"].append({"action": action, "from_version_id": active, "version_id": version_id,
                                   "changed_at": datetime.now(timezone.utc).isoformat()})
        result = with_path_config(workspace, current, config)
        write_path_config(workspace, config)
        return result


def activate_version(version_id, *, revision, workspace=BASE):
    if not isinstance(version_id, str):
        raise ValueError("请选择要启用的保存位置版本")
    return _switch_version(workspace, revision, "activate", version_id)


def rollback_version(*, revision, workspace=BASE):
    return _switch_version(workspace, revision, "rollback")


def main(argv=None):
    parser = ArgumentParser(description="核对 Worldanalysis 保存位置，或恢复上一套路径设置")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--propose", action="append", nargs=2, metavar=("用途", "路径"))
    modes.add_argument("--dispatch-paths", action="store_true", help="读取现有正式调度使用的路径")
    modes.add_argument("--bindings", action="store_true", help="读取新任务当前可用的保存位置")
    modes.add_argument("--target", choices=PROPOSAL_ROLES, help="读取该任务可直接使用的位置")
    modes.add_argument("--rollback", action="store_true", help="按当前核对版本恢复上一套路径设置")
    parser.add_argument("--revision", help="当前路径登记的核对版本")
    parser.add_argument("--min-free-bytes", type=int, help="要求的最低可用空间")
    parser.add_argument("--path-version", help="按已启用的旧版本读回任务绑定")
    parser.add_argument("--category", help="按正式分类读回任务绑定")
    parser.add_argument("--name", help="分析项目名或历史记录文件名")
    args = parser.parse_args(argv)
    try:
        if (args.path_version or args.category) and not (args.bindings or args.target):
            raise ValueError("请同时选择读取任务绑定")
        if args.name is not None and not args.target:
            raise ValueError("请同时选择任务位置")
        if args.propose:
            proposal = dict(args.propose)
            if len(proposal) != len(args.propose):
                raise ValueError("同一用途只能提议一个目录")
            result = preflight(proposal, revision=args.revision, minimum_free_bytes=args.min_free_bytes)
        elif args.rollback:
            if not args.revision or args.min_free_bytes is not None:
                raise ValueError("请先读取设置，再用当前核对版本恢复")
            result = rollback_version(revision=args.revision)
        else:
            if args.revision or args.min_free_bytes is not None:
                raise ValueError("请同时给出拟用目录")
            if args.dispatch_paths:
                result = dispatch_paths()
            elif args.bindings:
                result = path_bindings(version_id=args.path_version, category=args.category)
            elif args.target:
                print(job_target(args.target, version_id=args.path_version, category=args.category, name=args.name))
                return 0
            else:
                result = snapshot()
        print(json.dumps(result, ensure_ascii=args.dispatch_paths or args.bindings, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
