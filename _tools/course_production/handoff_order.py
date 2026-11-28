"""Validate V4 handoff evidence and select work without changing course identity."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import unicodedata


ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
SHA = re.compile(r"[a-f0-9]{64}")
LEGACY_SITES = {"wangzjk", "wangzjk.com", "site-wangzjk"}
MAX_RECORD_BYTES = 32 * 1024 * 1024
DSH_SESSION = re.compile(r"session-[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
CONTROL_DECISIONS = ("_exchange", "control", "decisions")
CONTROL_TASKS = ("_exchange", "control", "tasks")
DSH_AUTHORIZATIONS = ("_exchange", "dsh", "authorizations")
COMMON = {
    "schema_version", "record_type", "record_id", "producer", "revision",
    "observed_at", "state", "execution_allowed",
}
PLAN_FIELDS = {
    "batch_id", "confirmation_seq", "confirmed_at", "authorization_ref",
    "order_mode", "order_revision", "selection_sha256", "items",
}
ITEM_FIELDS = {
    "queue_item_id", "resource_version_id", "site_id", "article_id", "source_id",
    "source_revision", "range_manifest_ref", "item_rank", "transfer_task_id",
}
CONFIRMATION_FIELDS = {
    "confirmation_actor", "batch_id", "confirmation_seq", "confirmed_at", "order_mode",
    "order_revision", "selection_sha256", "ordered_items_sha256",
}
INTAKE_FIELDS = {
    "batch_id", "queue_item_id", "resource_version_id", "source_id", "source_revision",
    "accepted_order_revision", "local_project_id", "source_folder", "range_manifest_ref",
    "files", "download_complete", "analysis_ready",
}
DSH_RANGE_FIELDS = {
    "article_id", "site_id", "resource_version_id", "article_revision", "recursive_evidence",
    "roots", "directories", "share_sha256", "destination",
}


def require(test, reason):
    if not test:
        raise ValueError(reason)


def legacy_dsh_session():
    session = os.environ.get("WORLDANALYSIS_LEGACY_DSH_SESSION_ID", "")
    require(DSH_SESSION.fullmatch(session), "项目缺少原 DSH 会话绑定，请检查本机执行端配置")
    return session


def fields(value, expected, optional=()):
    require(isinstance(value, dict) and expected <= value.keys()
            and value.keys() <= expected | set(optional), "记录字段不符合契约")


def identifier(value):
    require(isinstance(value, str) and ID.fullmatch(value), "记录身份无效")


def positive(value):
    require(type(value) is int and value > 0, "记录顺位或修订无效")


def instant(value):
    require(isinstance(value, str), "交接时间无效")
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None, "交接时间缺少时区")
    return parsed


def identity_revision(value):
    if type(value) is int:
        positive(value)
    else:
        identifier(value)


def fingerprint(value):
    require(isinstance(value, str) and SHA.fullmatch(value), "交接摘要无效")


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return sha_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")).encode("utf-8"))


def priority_article_keys(item, manifest=None):
    version, site, article, revision = (item.get(field) for field in (
        "resource_version_id", "site_id", "article_id", "article_revision"))
    yield version, site, article, revision
    share = re.fullmatch(r"src_([a-f0-9]{64})", item.get("source_id") or "")
    if (site != "wangzjk.com" or not manifest or not manifest.get("roots") or not share
            or version != "rv_" + digest(["unverified_article_reference", [site, article, [share[1]]]])):
        return
    roots = sorted([root["source_id"], root["name"], root["is_dir"], root["size"]]
                   for root in manifest["roots"])
    canonical = "rv_" + digest([share[1], "selected_roots_only", roots])
    yield canonical, site, article, revision


def relative(value):
    require(isinstance(value, str) and value and "\\" not in value and ":" not in value
            and "\x00" not in value, "交接相对路径无效")
    path = PurePosixPath(value)
    require(not path.is_absolute() and all(part not in ("", ".", "..") for part in value.split("/")),
            "交接路径超出许可范围")
    return path


def workspace_file(workspace, name):
    path = relative(name)
    current = workspace
    for part in path.parts:
        current = current / part
        require(not current.is_symlink() and not current.is_junction(), "交接路径含联接")
    return current


def no_duplicate_keys(pairs):
    value = dict(pairs)
    require(len(value) == len(pairs), "交接记录重复字段")
    return value


def read_json(path, ref=None):
    require(path.is_file() and path.stat().st_size <= MAX_RECORD_BYTES, "交接文件缺失或超出读取范围")
    with path.open("rb") as stream:
        data = stream.read(MAX_RECORD_BYTES + 1)
    require(len(data) <= MAX_RECORD_BYTES, "交接文件超出读取范围")
    if ref:
        require(sha_bytes(data) == ref["sha256"], "交接清单摘要已变")
    return json.loads(data, object_pairs_hook=no_duplicate_keys)


def record(value, expected, kind, producer, state, optional=()):
    fields(value, COMMON | expected, {"demo", "event_id", *optional})
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["record_type"] == kind and value["producer"] == producer
            and value["state"] == state and value["execution_allowed"] is True
            and value.get("demo", False) is False, "交接记录未获生产接收")
    identifier(value["record_id"])
    if "event_id" in value:
        identifier(value["event_id"])
    positive(value["revision"])
    instant(value["observed_at"])


def reference(ref):
    fields(ref, {"path", "sha256"})
    path = relative(ref["path"])
    require(path.parts[:2] == ("_exchange", "dsh") and len(path.parts) > 3
            and path.suffix == ".json", "资料清单不在 DSH 记录目录")
    fingerprint(ref["sha256"])


def selection_sha(items):
    frozen = [{k: v for k, v in item.items() if k != "item_rank"}
              for item in sorted(items, key=lambda item: item["queue_item_id"])]
    return digest(frozen)


def ordered_items_sha(items):
    return digest([{"queue_item_id": item["queue_item_id"], "item_rank": item["item_rank"]}
                   for item in sorted(items, key=lambda item: item["item_rank"])])


def authorization_reference(ref):
    protocol_reference(ref, DSH_AUTHORIZATIONS, CONTROL_DECISIONS)


def protocol_reference(ref, *parents):
    fields(ref, {"path", "sha256"})
    path = relative(ref["path"])
    require(path.parts[:-1] in parents and path.suffix == ".json", "确认凭据不在指定交接目录")
    fingerprint(ref["sha256"])


def validate_plan(plan):
    record(plan, PLAN_FIELDS, "confirmed_plan", "dsh", "confirmed",
           ("batch_rank", "global_order_locked", "confirmation_task_ref"))
    positive(plan["order_revision"])
    require(plan["record_id"] == plan["batch_id"] and plan["revision"] == plan["order_revision"]
            and plan["order_mode"] in ("ready_ordered", "strict"), "计划身份、修订或顺序模式无效")
    authorization_reference(plan["authorization_ref"])
    positive(plan["confirmation_seq"])
    require(instant(plan["confirmed_at"]) <= instant(plan["observed_at"]), "确认晚于记录时间")
    if "batch_rank" in plan:
        positive(plan["batch_rank"])
    if "global_order_locked" in plan:
        require(type(plan["global_order_locked"]) is bool
                and plan["global_order_locked"] == ("batch_rank" in plan), "全局锁定状态与批次顺位不一致")
    items = plan["items"]
    require(isinstance(items, list) and items, "确认计划没有资料")
    for item in items:
        fields(item, ITEM_FIELDS, {"previous_cloud_root_id", "article_revision"})
        for key in ITEM_FIELDS - {"source_revision", "range_manifest_ref", "item_rank"}:
            identifier(item[key])
        if "previous_cloud_root_id" in item:
            identifier(item["previous_cloud_root_id"])
        if "article_revision" in item:
            identity_revision(item["article_revision"])
        identity_revision(item["source_revision"])
        positive(item["item_rank"])
        reference(item["range_manifest_ref"])
    require(sorted(item["item_rank"] for item in items) == list(range(1, len(items) + 1)),
            "确认计划项的顺位缺失或重复")
    for key in ("queue_item_id", "resource_version_id", "transfer_task_id"):
        require(len({item[key] for item in items}) == len(items), "确认计划存在重复资料或任务")
    require(plan["selection_sha256"] == selection_sha(items), "冻结选择范围的摘要不一致")
    if "confirmation_task_ref" in plan:
        protocol_reference(plan["confirmation_task_ref"], CONTROL_TASKS)


def control_proof(workspace, plan, confirmation):
    """Read through the standalone DSH receipt to the actual Codex user decision."""
    task_ref = plan.get("confirmation_task_ref")
    decision_ref = confirmation.get("control_decision_ref")
    require(task_ref is not None and decision_ref is not None
            and confirmation["producer"] == "dsh-ui"
            and confirmation.get("confirmation_channel") == "codex_control_record_of_actual_user_reply",
            "独立确认渠道缺少原始控制证据")
    protocol_reference(decision_ref, CONTROL_DECISIONS)
    task = read_json(workspace_file(workspace, task_ref["path"]), task_ref)
    decision = read_json(workspace_file(workspace, decision_ref["path"]), decision_ref)
    task_id = PurePosixPath(task_ref["path"]).stem
    expected_session = legacy_dsh_session()
    require(isinstance(task, dict) and task.get("schema_version") == 1
            and task.get("record_type") == "control_task" and task.get("producer") == "codex-control"
            and task.get("state") == "ready" and task.get("plan_version") == "V4"
            and task.get("target_session_id") == expected_session
            and task.get("task_id") == task_id and task.get("demo", False) is False
            and task.get("execution_allowed", True) is True
            and task.get("authorization_ref") == decision_ref
            and isinstance(task.get("inputs"), dict), "原 DSH 控制任务未绑定实际用户决定")
    require(isinstance(decision, dict) and decision.get("schema_version") == 1
            and decision.get("record_type") == "user_confirmation" and decision.get("producer") == "codex-control"
            and decision.get("state") == "confirmed" and decision.get("execution_allowed") is True
            and decision.get("demo", False) is False and decision.get("confirmation_actor") == "user"
            and decision.get("authorized_task_id") == task_id
            and decision.get("operation") == "one_bounded_normal_baidu_ui_transfer_batch"
            and isinstance(decision.get("user_message_reference"), str)
            and 1 <= len(decision["user_message_reference"].strip()) <= 250,
            "原始用户决定没有授权当前任务")
    keys = ("batch_id", "confirmation_seq", "confirmed_at", "order_mode", "order_revision",
            "selection_sha256", "ordered_items_sha256", "batch_rank", "global_order_locked")
    require(all(confirmation.get(key) == decision.get(key) for key in keys)
            and decision.get("revision") == plan["order_revision"]
            and decision.get("reacquire_queue_item_ids", []) == confirmation.get("reacquire_queue_item_ids", []),
            "独立确认回执与原始用户决定内容不同")
    selection_ref = task["inputs"].get("frozen_selection_ref")
    require(selection_ref == decision.get("frozen_selection_ref"), "任务与用户决定锁定的选择不同")
    protocol_reference(selection_ref, CONTROL_DECISIONS)
    selection = read_json(workspace_file(workspace, selection_ref["path"]), selection_ref)
    require(isinstance(selection, dict) and selection.get("schema_version") == 1
            and selection.get("record_type") == "frozen_transfer_selection"
            and selection.get("state") == "frozen" and selection.get("demo", False) is False
            and all(selection.get(key) == confirmation.get(key) for key in (
                "batch_id", "order_mode", "order_revision", "selection_sha256", "ordered_items_sha256",
                "batch_rank", "global_order_locked"))
            and selection.get("items") == plan["items"],
            "冻结选择的资料、版本或顺序已经变化")


def check_confirmation(workspace, plan):
    validate_plan(plan)
    ref = plan["authorization_ref"]
    confirmation = read_json(workspace_file(workspace, ref["path"]), ref)
    require(isinstance(confirmation, dict) and confirmation.get("producer") in ("dsh-ui", "codex-control"),
            "授权证据来源无效")
    record(confirmation, CONFIRMATION_FIELDS, "user_confirmation", confirmation["producer"],
           "confirmed", ("batch_rank", "global_order_locked", "reacquire_queue_item_ids",
                         "confirmation_channel", "control_decision_ref"))
    shared = CONFIRMATION_FIELDS - {"ordered_items_sha256", "confirmation_actor"}
    require(confirmation["confirmation_actor"] == "user"
            and all(confirmation[key] == plan[key] for key in shared)
            and confirmation.get("batch_rank") == plan.get("batch_rank")
            and ("global_order_locked" not in confirmation or
                 confirmation["global_order_locked"] is plan.get("global_order_locked"))
            and confirmation["revision"] == plan["order_revision"]
            and instant(plan["confirmed_at"]) <= instant(confirmation["observed_at"]) <= instant(plan["observed_at"])
            and confirmation["ordered_items_sha256"] == ordered_items_sha(plan["items"]),
            "实际用户确认与计划身份、范围或顺序不一致")
    reacquire = confirmation.get("reacquire_queue_item_ids", [])
    require(isinstance(reacquire, list) and all(isinstance(value, str) for value in reacquire)
            and len(set(reacquire)) == len(reacquire)
            and set(reacquire) <= {item["queue_item_id"] for item in plan["items"]},
            "重新获取授权超出本批选择项")
    if ("confirmation_task_ref" in plan or "confirmation_channel" in confirmation
            or "control_decision_ref" in confirmation):
        require(confirmation["record_id"] == PurePosixPath(ref["path"]).stem
                and PurePosixPath(ref["path"]).parts[:-1] == DSH_AUTHORIZATIONS,
                "独立确认回执身份与实际凭据文件不同")
        control_proof(workspace, plan, confirmation)
        for item in plan["items"]:
            load_range(workspace, item, require_dsh=True)
    return set(reacquire)


def files_by_path(rows, *, local=False):
    require(isinstance(rows, list), "文件清单不是列表")
    files = {}
    for row in rows:
        fields(row, {"file", "bytes", "mtime_ns"} if local else {"file", "bytes"})
        path = normalized_file(row["file"])
        require(path not in files and type(row["bytes"]) is int and row["bytes"] >= 0,
                "文件清单路径重复或长度无效")
        if local:
            require(type(row["mtime_ns"]) is int and row["mtime_ns"] >= 0, "本地文件时间无效")
        files[path] = row["bytes"]
    return files


def normalized_file(value):
    return unicodedata.normalize("NFC", relative(value).as_posix()).casefold()


def validate_intake(intake):
    record(intake, INTAKE_FIELDS, "local_intake", "codex-course", "matched")
    require(intake["record_id"] == intake["queue_item_id"], "本地收件身份无效")
    for key in ("batch_id", "resource_version_id", "local_project_id", "source_id"):
        identifier(intake[key])
    identity_revision(intake["source_revision"])
    positive(intake["accepted_order_revision"])
    reference(intake["range_manifest_ref"])
    relative(intake["source_folder"])
    require(all(type(intake[key]) is bool for key in ("download_complete", "analysis_ready")),
            "本地下载和就绪状态无效")
    files_by_path(intake["files"], local=True)


def validate_dsh_range(item, manifest):
    require(DSH_RANGE_FIELDS <= manifest.keys()
            and manifest["scope"] == "recursive" and manifest.get("complete_recursive") is True
            and manifest["recursive_evidence"] == "normal_ui_exact_directory_listing",
            "DSH 根及目录证据不足以绑定完整资料范围")
    keys = ("site_id", "article_id", "article_revision", "resource_version_id",
            "source_id", "source_revision")
    require(all(manifest[key] == item.get(key) for key in keys), "DSH 资料范围与冻结来源身份不一致")
    fingerprint(manifest["share_sha256"])
    require(isinstance(manifest["article_revision"], str)
            and re.fullmatch(r"arv_[0-9a-f]{64}", manifest["article_revision"]),
            "DSH 文章版本缺少实际证据摘要")
    destination = manifest["destination"]
    require(isinstance(destination, str) and destination.startswith("/"), "DSH 目标路径无效")
    relative(destination[1:])
    roots = manifest["roots"]
    require(isinstance(roots, list) and 1 <= len(roots) <= 20
            and type(manifest.get("root_count")) is int and manifest["root_count"] == len(roots),
            "DSH 原目录根数量不一致")
    root_names, root_ids = set(), set()
    for root in roots:
        fields(root, {"name", "source_id", "is_dir", "size"})
        name = relative(root["name"])
        identifier(root["source_id"])
        require(len(name.parts) == 1 and root["is_dir"] is True
                and type(root["size"]) is int and root["size"] == 0
                and root["name"] not in root_names and root["source_id"] not in root_ids,
                "DSH 原目录根身份不一致")
        root_names.add(root["name"])
        root_ids.add(root["source_id"])

    directories = manifest["directories"]
    files = manifest["files"]
    require(isinstance(directories, list) and len(roots) <= len(directories) <= 1200
            and 1 <= len(files) <= 10000, "DSH 目录及成员范围不完整")
    for directory in directories:
        relative(directory)
    local_names = [*directories, *(row["file"] for row in files)]
    collision_keys = {unicodedata.normalize("NFKC", path).casefold() for path in local_names}
    require(len(collision_keys) == len(local_names), "DSH 范围含相互重叠的目录或文件")
    directory_set = set(directories)
    require(root_names <= directory_set
            and all((value.rsplit("/", 1)[0] in directory_set if "/" in value else value in root_names)
                    for value in directories)
            and all("/" in row["file"] and row["file"].rsplit("/", 1)[0] in directory_set for row in files),
            "DSH 文件并不位于已证实的目录根内")
    require(manifest["source_revision"] == "sr_" + digest([
        sorted(roots, key=lambda root: root["source_id"]), sorted(directories),
        sorted(files, key=lambda row: row["file"]),
    ]), "DSH 原目录或文件已超出源版本摘要")
    require(0 < sum(row["bytes"] for row in files) < 2**53, "DSH 内容字节总量不能确认")
    return root_names


def load_range(workspace, item, *, local_shapes=False, require_dsh=False):
    ref = item["range_manifest_ref"]
    manifest = read_json(workspace_file(workspace, ref["path"]), ref)
    fields(manifest, {"schema_version", "record_type", "source_id", "source_revision", "scope", "files"},
           {"event_id", "demo", "execution_allowed", "root_count", "complete_recursive"} | DSH_RANGE_FIELDS)
    if "event_id" in manifest:
        identifier(manifest["event_id"])
    if "root_count" in manifest:
        positive(manifest["root_count"])
    if "complete_recursive" in manifest:
        require(type(manifest["complete_recursive"]) is bool
                and manifest["complete_recursive"] == (manifest["scope"] == "recursive"), "递归资料范围声明不一致")
    require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1
            and manifest["record_type"] == "range_manifest"
            and manifest.get("demo", False) is False and manifest.get("execution_allowed", True) is True
            and manifest["scope"] in ("recursive", "selected_roots_only")
            and (manifest["source_id"], manifest["source_revision"])
            == (item["source_id"], item["source_revision"]), "资料清单版本或身份不一致")
    files = files_by_path(manifest["files"])
    if require_dsh or DSH_RANGE_FIELDS & manifest.keys():
        root_names = validate_dsh_range(item, manifest)
        if local_shapes and len(root_names) == 1:
            # In a manual single-root download, the native project folder can be
            # that root itself. A grouped download keeps the original hierarchy.
            return manifest["scope"], (files, {path.split("/", 1)[1]: size for path, size in files.items()})
    return manifest["scope"], (files,) if local_shapes else files


def historical_files(record, article, root_id):
    for entry in record.get("records", ()):
        locator = entry["p"]
        if (str(locator.get("root_id")) == root_id
                and article in map(str, locator.get("articles", ())) and entry.get("matched") is True):
            return entry["files"]
    if (str(record.get("article_id")) == article
            and record.get("exact_relative_name_size_manifest_match") is True):
        return record["files"]
    for entry in record.get("projects", ()):
        if str(entry.get("id")) == root_id and str(entry.get("article")) == article:
            return entry["fresh_files"]
    raise ValueError("历史实收证据未绑定相同文章和网盘对象")


def historical_ref(workspace, ref, cache):
    raw_path = Path(ref["path"])
    path = raw_path.relative_to(workspace) if raw_path.is_absolute() else raw_path
    require(path.parts[:4] == ("_ingest", "wangzjk-to-pan", "state", "evidence"),
            "历史清理证据路径超出记录目录")
    key = path.as_posix(), ref["sha256"]
    if key not in cache:
        cache[key] = read_json(workspace_file(workspace, path.as_posix()), ref)
    return cache[key]


def historical_blocks(workspace, plans, approved):
    """Recognize retired Wangzjk versions by the recorded article/object pair."""
    candidates = [item for plan in plans.values() for item in plan["items"]
                  if item["site_id"] in LEGACY_SITES
                  and item["queue_item_id"] not in approved.get(plan["batch_id"], ())]
    if not candidates:
        return {}
    marker_path = workspace_file(workspace, "_ingest/wangzjk-to-pan/state/downloaded-project-markers.json")
    if not marker_path.is_file():
        return {}
    markers = read_json(marker_path)
    require(markers.get("schema") == "worldanalysis-baidu-downloaded-project-markers.v1",
            "历史清理记录版本不一致")
    withdrawn = {(str(mark["article_id"]), str(mark["cloud_root_id"])): mark
                 for mark in markers["entries"] if mark["cloud_cleanup_state"] == "recycled_verified_exact_id"}
    candidates = [item for item in candidates if any(key[0] == item["article_id"] for key in withdrawn)]
    if not candidates:
        return {}
    journal = read_json(workspace_file(workspace, "_ingest/wangzjk-to-pan/state/journal.json"))
    require(journal.get("schema") == "wangzjk-to-pan.journal.v1", "历史网盘操作版本不一致")
    targets = {(str(article), str(row["target_id"])) for op in journal["operations"].values()
               if op["state"] == "saved_root_verified" for row in op.get("verified_rows", ())
               for article in op["article_ids"]}
    cache, blocked = {}, {}
    for item in candidates:
        item_id = item["queue_item_id"]
        try:
            scope, shapes = load_range(workspace, item, local_shapes=True)
            for (article, root_id), mark in withdrawn.items():
                if article != item["article_id"]:
                    continue
                require((article, root_id) in targets, "历史网盘对象证据待对账")
                historical_ref(workspace, mark["cloud_cleanup_result_ref"], cache)
                exact_root = item.get("previous_cloud_root_id") == root_id
                if exact_root:
                    blocked[item_id] = "同版网盘自有副本已经回收，须明确授权重新获取"
                    break
                if scope != "recursive":
                    raise ValueError("有历史清理记录，须核对新版本的实质范围")
                evidence = historical_ref(workspace, mark["download_completion_evidence_ref"], cache)
                names_and_bytes = files_by_path([
                    {"file": "/".join(part) if isinstance(part, list) else part,
                     "bytes": row.get("size", row.get("bytes"))}
                    for row in historical_files(evidence, article, root_id) if not row.get("is_dir")
                    for part in (row.get("part", row.get("name")),)
                ])
                if names_and_bytes in shapes:
                    blocked[item_id] = "与已清理的文章和网盘对象实收范围相同，须明确授权重新获取"
                    break
        except (KeyError, OSError, TypeError, ValueError) as error:
            blocked[item_id] = str(error).splitlines()[0]
    return blocked


def exchange_records(workspace, section, validator, previous):
    directory = workspace_file(workspace, section)
    records = dict(previous)
    warnings = []
    if not directory.exists():
        return records, [{"record": key, "reason": "交接入口暂不可见，沿用最后验真的记录"} for key in previous]
    require(directory.is_dir(), "交接记录入口不是目录")
    current = set()
    for path in sorted(directory.glob("*.json")):
        current.add(path.stem)
        try:
            value = read_json(workspace_file(workspace, path.relative_to(workspace).as_posix()))
            validator(value)
            key = value["record_id"]
            require(path.stem == key, "交接文件名与记录身份不同")
            old = previous.get(key)
            if old and value != old:
                require(value["revision"] > old["revision"]
                        and instant(value["observed_at"]) >= instant(old["observed_at"]), "拒绝旧修订或同版冲突")
                if value["record_type"] == "confirmed_plan":
                    immutable = ("confirmation_seq", "confirmed_at", "selection_sha256")
                else:
                    immutable = ("batch_id", "resource_version_id", "source_id", "source_revision",
                                 "range_manifest_ref", "local_project_id", "source_folder")
                    require(value["accepted_order_revision"] >= old["accepted_order_revision"],
                            "收件记录回退至更早的计划版本")
                require(all(value[field] == old[field] for field in immutable), "已确认范围或本地身份变更")
            records[key] = value
        except (OSError, ValueError) as error:
            warnings.append({"record": path.stem, "reason": str(error).splitlines()[0]})
    warnings.extend({"record": key, "reason": "交接记录暂不可见，沿用最后验真的版本"}
                    for key in sorted(previous.keys() - current))
    return records, warnings


@dataclass(frozen=True)
class Planned:
    plan: dict
    item: dict
    binding: dict | None
    row: dict | None
    ready: bool
    blocker: str

    def summary(self):
        return {
            "batch_id": self.plan["batch_id"], "queue_item_id": self.item["queue_item_id"],
            "resource_version_id": self.item["resource_version_id"], "item_rank": self.item["item_rank"],
            "order_mode": self.plan["order_mode"], "order_revision": self.plan["order_revision"],
            "observed_at": self.plan["observed_at"], "blocker": self.blocker,
        }


@dataclass(frozen=True)
class Selection:
    pair: tuple | None
    waiting: dict | None = None
    order: dict | None = None
    plan_items: tuple = ()
    analysis_priority: dict | None = None
    analysis_submission: dict | None = None
    analysis_waiting: tuple = ()


def select(pairs, *, planned=(), held=(), eligible, review_priority, natural, active_state, analysis_priorities=None):
    """The shared selection rule operates on all rows, including strict plan placeholders."""
    active = [pair for pair in pairs if pair[1]["status"] == active_state]
    if len(active) > 1:
        raise ValueError(f"发现 {len(active)} 个正式在办项目，须对账后继续唯一生产")
    if active:
        return Selection(active[0])

    plan_items = tuple({**item.summary(), "analysis_ready": item.ready,
                        "local_project_id": item.row["id"] if item.row is not None else None}
                       for item in planned)
    ready = [pair for pair in pairs if eligible(pair[1]) and pair[1]["id"] not in held]
    reserved = {id(item.row) for item in planned if item.row is not None}
    plan_ready = {id(item.row) for item in planned if item.ready}
    analysis_priorities = analysis_priorities or {}
    user_ordered = [pair for pair in ready if pair[1]["id"] in analysis_priorities
                    and (id(pair[1]) not in reserved or id(pair[1]) in plan_ready)]
    if user_ordered:
        pair = min(user_ordered, key=lambda value: (analysis_priorities[value[1]["id"]]["rank"], value[1]["id"]))
        transfer = next((item.summary() for item in planned if item.ready and item.row is pair[1]), None)
        return Selection(pair, order=transfer, plan_items=plan_items,
                         analysis_priority=analysis_priorities[pair[1]["id"]])
    explicit = [pair for pair in ready if review_priority(pair[1]) is not None
                and (id(pair[1]) not in reserved or id(pair[1]) in plan_ready)]
    if explicit:
        pair = min(explicit, key=lambda pair: (
            review_priority(pair[1]), natural(pair[0]["category"]), pair[1]["id"]))
        order = next((item.summary() for item in planned if item.ready and item.row is pair[1]), None)
        return Selection(pair, order=order, plan_items=plan_items)

    def plan_key(value):
        plan = value.plan
        rank = (0, plan["batch_rank"]) if "batch_rank" in plan else (1, -plan["confirmation_seq"])
        return rank, plan["batch_id"], value.item["item_rank"], value.item["queue_item_id"]

    pending = sorted(planned, key=plan_key)
    waiting = None
    blocked_batches = set()
    for item in pending:
        batch_id = item.plan["batch_id"]
        if batch_id in blocked_batches:
            continue
        if item.ready and item.row["id"] not in held:
            return Selection((item.binding, item.row), waiting=waiting, order=item.summary(), plan_items=plan_items)
        if item.plan["order_mode"] == "strict":
            blocked_batches.add(batch_id)
            if waiting is None:
                waiting = item.summary()
                if item.ready:
                    waiting = {**waiting, "blocker": "所选分析版本尚待核对"}

    ready = [pair for pair in ready if id(pair[1]) not in reserved]
    arrivals = [pair for pair in ready if pair[1].get("arrival_at")]
    if arrivals:
        newest = max(instant(row["arrival_at"]) for _, row in arrivals)
        tied = [pair for pair in arrivals if instant(pair[1]["arrival_at"]) == newest]
        def arrival_key(pair):
            category, name = natural(pair[0]["category"]), natural(pair[1]["name"])
            return (name, category, pair[1]["id"]) if planned else (category, name, pair[1]["id"])
        return Selection(min(tied, key=arrival_key), waiting=waiting, plan_items=plan_items)
    pair = min(ready, key=lambda pair: (
        natural(pair[0]["category"]), natural(pair[1]["name"]), pair[1]["id"]), default=None)
    return Selection(pair, waiting=waiting, plan_items=plan_items)
