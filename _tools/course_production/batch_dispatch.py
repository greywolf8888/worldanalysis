"""Discover category sources and coordinate their existing authoritative course batches."""
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import batch
from analysis_priority_order import resolved_priorities, submitted_order
import handoff_order as handoff
from project import file_lock, put, read


# The top-level directories are categories; only immediate children of a
# recognized input container can be course projects.
INPUT_NAMES = ("储存", "课程原件", "原件")
OUTPUT_NAMES = {"分析结果", "正在分析", "测试分析稿件", "测试分析结果", ".obsidian", "00 入口"}
CATEGORY_MARKER = ".worldanalysis-category.json"
PROJECT_MARKER = ".worldanalysis-project.json"
DIRECT_CORE_SUFFIXES = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mp3", ".wav", ".m4a", ".aac",
    ".flac", ".ogg", ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".rar", ".7z",
}
PARTIAL_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload", ".download", ".baiduyun.downloading",
                    ".aria2", ".!qb", ".xltd", ".filepart")
SCHEMA = 1


def is_plain_dir(path):
    return path.is_dir() and not path.is_symlink() and not path.is_junction()


def direct_course(path):
    if not is_plain_dir(path):
        return False
    marker = path / PROJECT_MARKER
    if marker.is_file() and not marker.is_symlink():
        try:
            if read(marker) == {"schema": 1, "kind": "course-project"}:
                return True
        except (OSError, ValueError):
            pass
    if (path / ".course" / "project.json").exists():
        return False
    try:
        return any(file.is_file() and not file.is_symlink() and file.suffix.casefold() in DIRECT_CORE_SUFFIXES
                   for file in path.iterdir())
    except OSError:
        return False


def candidates(source, layout=None, registered=()):
    if not is_plain_dir(source):
        return []
    return [item for item in batch.project_candidates(source)
            if item.name not in OUTPUT_NAMES and not item.name.startswith(".")
            and (layout != "direct-evidence" or item.name in registered or direct_course(item))]


def discover(root):
    categories, uncertain = [], []
    for folder in candidates(root):
        if folder.name in INPUT_NAMES:
            uncertain.append({"category": folder.name, "detail": "总目录直属输入容器缺分类层"})
            continue
        if not is_plain_dir(folder):
            uncertain.append({"category": folder.name, "detail": "分类不是普通目录"})
            continue
        matches = [folder / name for name in INPUT_NAMES if (folder / name).exists()]
        marker = folder / CATEGORY_MARKER
        direct = False
        if marker.exists():
            try:
                direct = (not marker.is_symlink() and not marker.is_junction() and marker.is_file()
                          and read(marker) == {"schema": 1, "layout": "direct-projects"})
            except (OSError, ValueError):
                pass
            if not direct or matches:
                uncertain.append({"category": folder.name, "detail": "直属课程标记无效或与输入容器冲突"})
                continue
        if direct:
            source = folder.resolve()
            layout = "direct-marked"
        elif len(matches) == 1 and is_plain_dir(matches[0]):
            source = matches[0].resolve()
            layout = "container"
        elif not matches and any(direct_course(path) for path in candidates(folder)):
            source = folder.resolve()
            layout = "direct-evidence"
            ambiguous = [path.name for path in candidates(folder)
                         if is_plain_dir(path) and not direct_course(path)]
            if ambiguous:
                uncertain.append({"category": folder.name, "detail": "部分直属目录缺少可识别课程原件或显式标记",
                                  "direct_candidates": ambiguous})
        else:
            uncertain.append({"category": folder.name,
                              "detail": "没有唯一明确的独立原件输入子目录",
                              "direct_candidates": [p.name for p in candidates(folder)
                                                    if p.name not in INPUT_NAMES and is_plain_dir(p)]})
            continue
        vault = batch.safe_path(folder / "分析结果")
        category = {"category": folder.name, "source": str(source), "vault": str(vault), "layout": layout}
        categories.append(category)
    return categories, uncertain


def shape(files):
    return [(file["file"], file["bytes"]) for file in files]


def wait_for_originals(row, moment, settle_seconds):
    """Stability is merely an eligibility gate. claim still verifies the full source."""
    old_metadata = row["metadata"]
    signature = batch.digest(old_metadata)
    if signature != row.get("observed_metadata_sha256"):
        row["observed_metadata_sha256"] = signature
        row["stable_since"] = moment.isoformat()
        row.pop("ready_at", None)
    if not old_metadata:
        row["arrival_blocker"] = "未发现可用原件"
    elif any(file["file"].casefold().endswith(PARTIAL_SUFFIXES) for file in old_metadata):
        row["arrival_blocker"] = "发现传输或临时文件后缀"
    elif not any(file["bytes"] for file in old_metadata):
        row["arrival_blocker"] = "全部原件均为零字节"
    else:
        since = datetime.fromisoformat(row["stable_since"])
        if (moment - since).total_seconds() >= settle_seconds:
            row.setdefault("ready_at", moment.isoformat())
            row.pop("arrival_blocker", None)
            return
        row["arrival_blocker"] = "等待下一次稳定性复扫"
    row.pop("ready_at", None)


def refresh_queue(queue, *, moment=None):
    moment = moment or datetime.now(timezone.utc)
    settle_seconds = queue["dispatch_settle_seconds"]
    projects = queue["projects"]
    source = Path(queue["source"])
    rows_by_name = {row["name"]: row for row in projects}
    present = {p.name: p for p in candidates(source, queue.get("dispatch_layout"), rows_by_name)}
    missing = [row for row in projects if row["name"] not in present]
    identity_pending, unassigned = [], []
    for name, path in present.items():
        if not batch.is_project(path):
            unassigned.append(name)
            continue
        registered = rows_by_name.get(name)
        if registered is not None:
            # Historical and already bound rows retain their evidence. An
            # unclaimed new arrival may change while copying and must settle again.
            if (registered["status"] == batch.PENDING and registered.get("arrival_at")
                    and not registered.get("package") and not registered.get("source_sha256")):
                observed = batch.observe_folder(source, path, registered["sequence"])
                registered["metadata"] = observed["metadata"]
                wait_for_originals(registered, moment, settle_seconds)
                if observed.get("reason"):
                    registered["arrival_blocker"] = observed["reason"]
            continue

        row = batch.observe_folder(source, path, len(projects) + 1)
        # A missing registered path with the same file names/sizes is a possible
        # rename. Never create another identity until the operator verifies it.
        possible = [old["sequence"] for old in missing if row["metadata"]
                    and shape(row["metadata"]) == shape(old["metadata"])]
        if possible:
            identity_pending.append({"path": str(path), "possible_sequences": possible,
                                     "detail": "疑似改名或迁移：待核身份，不自动创建第二个项目"})
            continue
        row.update(status=batch.PENDING, arrival_at=moment.isoformat())
        observed_reason = row.pop("reason", None)
        wait_for_originals(row, moment, settle_seconds)
        if observed_reason:
            row["arrival_blocker"] = observed_reason
        projects.append(row)
    for row in missing:
        if (row["status"] == batch.PENDING and row.get("arrival_at")
                and not row.get("package") and not row.get("source_sha256")):
            row.pop("ready_at", None)
            row.pop("observed_metadata_sha256", None)
            row["arrival_blocker"] = "新来件目录暂不可见"
    queue["identity_pending"] = identity_pending
    queue["unassigned"] = unassigned


def registry_data(root, registry):
    if not registry.exists():
        return {"schema": SCHEMA, "root": str(root), "bindings": {}}
    info = read(registry)
    if info.get("schema") != SCHEMA or info.get("root") != str(root):
        raise ValueError(f"分类绑定索引不属于当前总目录 {registry}")
    return info


def bind_categories(info, registry, categories, legacy_manifests):
    discovered = {cat["category"]: cat for cat in categories}
    bindings = info["bindings"]
    assigned = {binding["manifest"] for binding in bindings.values()}
    for path in legacy_manifests:
        path = batch.safe_path(path)
        queue = batch.open_queue(path)
        matching = [cat for cat in categories
                    if (cat["source"], cat["vault"]) == (queue["source"], queue["vault"])]
        if len(matching) != 1:
            raise ValueError(f"既有批次的来源/阅读库不是当前已识别的分类 {path}")
        category = matching[0]["category"]
        if (category in bindings and bindings[category]["manifest"] != str(path)) or (
                str(path) in assigned and bindings.get(category, {}).get("manifest") != str(path)):
            raise ValueError(f"分类已有其他权威批次清单 {category}")
        bindings[category] = {**matching[0], "manifest": str(path)}
        assigned.add(str(path))
    for name, cat in discovered.items():
        if name in bindings:
            binding = bindings[name]
            if (binding["source"], binding["vault"]) != (cat["source"], cat["vault"]):
                raise ValueError(f"分类输入或阅读库路径变化，需先核对原批次身份 {name}")
            binding["layout"] = cat["layout"]
            continue
        manifest = registry.parent / "category-batches" / f"{batch.digest([cat['source']])[:24]}.json"
        # Existing reader-visible projects signal that a historical ledger may
        # already exist. Never silently start another authoritative list.
        vault = Path(cat["vault"])
        if vault.is_dir() and any(is_plain_dir(item) and item.name not in OUTPUT_NAMES
                                  and not item.name.startswith(".") for item in vault.iterdir()):
            raise ValueError(f"分类已有正式阅读项目，请提供既有权威批次 --legacy-manifest {name}")
        bindings[name] = {**cat, "manifest": str(manifest)}


def load_bound(info):
    return [(binding, batch.open_queue(Path(binding["manifest"])))
            for binding in info["bindings"].values() if Path(binding["manifest"]).is_file()]


def course_pairs(bound):
    return [(binding, row) for binding, queue in bound for row in queue["projects"]]


def refresh_handoff(info, bound):
    workspace = Path(info["root"]).parent
    previous = info.get("v4_handoff", {})
    previous_plans = previous.get("plans", {})
    confirmed = {}

    def validate_confirmation(plan):
        approved = handoff.check_confirmation(workspace, plan)
        confirmed[(plan["batch_id"], plan["revision"], plan["authorization_ref"]["sha256"])] = sorted(approved)

    try:
        plans, plan_warnings = handoff.exchange_records(
            workspace, "_exchange/dsh/plans", validate_confirmation, previous_plans)
        intakes, intake_warnings = handoff.exchange_records(
            workspace, "_exchange/codex/intake", handoff.validate_intake, previous.get("intakes", {}))
    except (OSError, ValueError) as error:
        info["v4_handoff"] = {**previous, "warnings": [{"reason": str(error)}]}
        return

    pairs = course_pairs(bound)
    by_local = {row["id"]: row for _, row in pairs}
    finalized = batch.PUBLISHED | {batch.ACTIVE, batch.REUSED, batch.ARCHIVED}
    for key, plan in previous_plans.items():
        revised = plans[key]
        if revised == plan:
            continue
        old_ranks = {item["queue_item_id"]: item["item_rank"] for item in plan["items"]}
        for item in revised["items"]:
            item_id = item["queue_item_id"]
            local = by_local.get((previous.get("intakes", {}).get(item_id) or {}).get("local_project_id"))
            if local and local["status"] in finalized and old_ranks[item_id] != item["item_rank"]:
                plans[key] = plan
                plan_warnings.append({"record": key, "reason": "已在办或已闭合项目不得改动确认顺位"})
                break
    held = set()
    approved = {}
    blocked = {}
    if plans:
        approved = {key: confirmed.get((key, plan["revision"], plan["authorization_ref"]["sha256"]),
                                       previous.get("approved_reacquisition", {}).get(key, []))
                    for key, plan in plans.items()}
        try:
            blocked = handoff.historical_blocks(workspace, plans, approved)
        except (KeyError, OSError, TypeError, ValueError) as error:
            blocked = {item["queue_item_id"]: "历史清理账待核"
                       for plan in plans.values() for item in plan["items"]
                       if item["site_id"] in handoff.LEGACY_SITES
                       and item["queue_item_id"] not in approved.get(plan["batch_id"], ())}
            plan_warnings.append({"reason": str(error).splitlines()[0]})
        earlier_blocks = previous.get("blocked", {})
        for key, plan in plans.items():
            for item in plan["items"]:
                item_id = item["queue_item_id"]
                old_reason = earlier_blocks.get(item_id, "")
                if old_reason.startswith(("同版网盘自有副本已经回收", "与已清理的文章")) and item_id not in approved[key]:
                    blocked[item_id] = old_reason
        match_warnings, held = match_intakes(workspace, plans, intakes, pairs, blocked)
        intake_warnings += match_warnings
    if plans or intakes or plan_warnings or intake_warnings or previous:
        info["v4_handoff"] = {"plans": plans, "intakes": intakes, "held": sorted(held),
                              "approved_reacquisition": approved, "blocked": blocked,
                              "warnings": plan_warnings + intake_warnings}


def match_intakes(workspace, plans, intakes, pairs, blocked):
    """Project an unambiguous complete file-set match, using no title heuristic."""
    cached = defaultdict(list)
    for binding, row in pairs:
        if not batch.eligible_pending(row) or not row["metadata"]:
            continue
        shape = handoff.files_by_path(row["metadata"], local=True)
        cached[tuple(sorted(shape.items()))].append((binding, row))

    prospective = []
    warnings = []
    held = set()
    for plan in plans.values():
        for item in plan["items"]:
            item_id = item["queue_item_id"]
            intake = intakes.get(item_id)
            if intake and intake["accepted_order_revision"] == plan["order_revision"]:
                continue
            try:
                scope, remote_shapes = handoff.load_range(workspace, item, local_shapes=True)
                if scope != "recursive" or not remote_shapes[0]:
                    continue
                by_match_id = {}
                for remote in remote_shapes:
                    for pair in cached.get(tuple(sorted(remote.items())), ()):
                        by_match_id[pair[1]["id"]] = pair
                matches = list(by_match_id.values())
                if intake:
                    matches = [pair for pair in matches if pair[1]["id"] == intake["local_project_id"]]
                if item_id in blocked:
                    held.update(row["id"] for _, row in matches)
                    continue
                if len(matches) > 1:
                    held.update(row["id"] for _, row in matches)
                    warnings.append({"record": item_id, "reason": "多个本地目录符合相同范围，待核准确身份"})
                    continue
                if len(matches) == 1:
                    prospective.append((matches[0], plan, item, intake, remote_shapes))
            except (OSError, ValueError) as error:
                warnings.append({"record": item_id, "reason": str(error).splitlines()[0]})
    claimed = {intake["local_project_id"]: item_id for item_id, intake in intakes.items()}
    competing = Counter(pair[1]["id"] for pair, *_ in prospective)
    for (binding, row), plan, item, prior, remote_shapes in prospective:
        item_id = item["queue_item_id"]
        if competing[row["id"]] > 1 or claimed.get(row["id"], item_id) != item_id:
            held.add(row["id"])
            warnings.append({"record": item_id, "reason": "同一实收目录对应多个确认项，待核资料身份"})
            continue
        intake = {
            **{key: item[key] for key in ("queue_item_id", "resource_version_id", "source_id",
                                         "source_revision", "range_manifest_ref")},
            "schema_version": 1, "record_type": "local_intake", "record_id": item_id,
            "producer": "codex-course", "revision": prior["revision"] + 1 if prior else 1,
            "observed_at": datetime.now(timezone.utc).isoformat(), "state": "matched",
            "execution_allowed": True, "batch_id": plan["batch_id"],
            "accepted_order_revision": plan["order_revision"], "local_project_id": row["id"],
            "source_folder": (Path(binding["source"]) / row["name"]).relative_to(workspace).as_posix(),
            "files": row["metadata"], "download_complete": True, "analysis_ready": True,
        }
        try:
            handoff.validate_intake(intake)
            if not actual_intake(workspace, binding, row, intake, remote_shapes):
                held.add(row["id"])
                continue
            output = handoff.workspace_file(workspace, f"_exchange/codex/intake/{item_id}.json")
            put(output, intake)
            intakes[item_id] = intake
        except (OSError, ValueError) as error:
            held.add(row["id"])
            warnings.append({"record": item_id, "reason": str(error).splitlines()[0]})
    return warnings, held


def actual_intake(workspace, binding, row, intake, remote_shapes):
    source = handoff.workspace_file(workspace, intake["source_folder"])
    handoff.require(source == Path(binding["source"]) / row["name"], "本地目录与正式项目身份不一致")
    actual = batch.metadata(source)
    handoff.require(actual == row["metadata"] == intake["files"], "本地实收文件与登记清单不一致")
    received = handoff.files_by_path(actual, local=True)
    matches = [remote for remote in remote_shapes if received
               and all(remote.get(name) == size for name, size in received.items())]
    handoff.require(len(matches) == 1, "实收路径与确认资料范围不一致或根位置有歧义")
    handoff.require(intake["download_complete"] == (received == matches[0]), "下载齐全标记与实际范围不一致")
    if not intake["analysis_ready"] or not batch.eligible_pending(row):
        return False
    if any(size == 0 or name.endswith(PARTIAL_SUFFIXES) for name, size in received.items()):
        return False
    material = [member for member in actual
                if Path(member["file"]).suffix.casefold() in DIRECT_CORE_SUFFIXES]
    if not material:
        return False
    for member in material:
        path = handoff.workspace_file(workspace, (source / member["file"]).relative_to(workspace).as_posix())
        with path.open("rb") as stream:
            if not stream.read(1):
                return False
    return True


def planned_work(info, pairs):
    workspace = Path(info["root"]).parent
    saved = info.get("v4_handoff", {})
    intakes = saved.get("intakes", {})
    by_local = {row["id"]: (binding, row) for binding, row in pairs}
    result = []
    for plan in saved.get("plans", {}).values():
        for item in plan["items"]:
            item_id = item["queue_item_id"]
            intake = intakes.get(item_id)
            binding, row = by_local.get((intake or {}).get("local_project_id"), (None, None))
            blocker = saved.get("blocked", {}).get(item_id)
            if blocker:
                result.append(handoff.Planned(plan, item, binding, row, False, blocker))
                continue
            if not intake:
                result.append(handoff.Planned(plan, item, None, None, False, "等待实收匹配"))
                continue
            try:
                shared = ("queue_item_id", "resource_version_id", "source_id", "source_revision",
                          "range_manifest_ref")
                handoff.require(all(item[field] == intake[field] for field in shared)
                                and intake["batch_id"] == plan["batch_id"], "收件资料身份与确认计划冲突")
                handoff.require(row is not None, "本地项目尚未登记在正式批次")
                # A previously signed edition retains its existing local identity
                # even after its original has been explicitly retired.
                if row["status"] in batch.PUBLISHED | {batch.REUSED, batch.ARCHIVED}:
                    source = handoff.workspace_file(workspace, intake["source_folder"])
                    handoff.require(source == Path(binding["source"]) / row["name"]
                                    and intake["files"] == row["metadata"], "历史收件身份不一致")
                    continue
                handoff.require(intake["accepted_order_revision"] == plan["order_revision"],
                                "等待本地接收最新顺序修订")
                scope, remote_shapes = handoff.load_range(workspace, item, local_shapes=True)
                handoff.require(scope == "recursive", "只有根级网盘读回，待实质文件清单")
                ready = actual_intake(workspace, binding, row, intake, remote_shapes)
                blocker = "" if ready else "等待稳定且可分析的实收范围"
            except (OSError, ValueError) as error:
                ready, blocker = False, str(error).splitlines()[0]
            result.append(handoff.Planned(plan, item, binding, row, ready, blocker))
    counts = Counter(value.row["id"] for value in result if value.row is not None)
    held = set(saved.get("held", ()))
    return [handoff.Planned(value.plan, value.item, value.binding, value.row, False, "计划项存在本地身份冲突")
            if value.row is not None and (counts[value.row["id"]] > 1 or value.row["id"] in held)
            else value for value in result]


def local_priority_ready(workspace, binding, row):
    """A local priority never certifies original material merely from a directory name."""
    if not batch.eligible_pending(row):
        return False
    try:
        raw = Path(binding["source"]) / row["name"]
        source = handoff.workspace_file(workspace, raw.relative_to(workspace).as_posix())
        actual = batch.metadata(source)
        if not actual or actual != row["metadata"] or any(
                member["bytes"] <= 0 or member["file"].casefold().endswith(PARTIAL_SUFFIXES) for member in actual):
            return False
        for member in actual:
            if Path(member["file"]).suffix.casefold() in DIRECT_CORE_SUFFIXES:
                path = handoff.workspace_file(workspace, (source / member["file"]).relative_to(workspace).as_posix())
                with path.open("rb") as stream:
                    if stream.read(1):
                        return True
    except (OSError, ValueError):
        pass
    return False


def local_priority_identity(workspace, binding, row):
    """Hash only the selected stable project, with no change to its ledger."""
    if not local_priority_ready(workspace, binding, row):
        raise ValueError("本地原件尚未就绪，请核对实收范围后再安排分析")
    _, _, identity = batch.identify({"source": binding["source"]}, row)
    return {"source_sha256": identity["source_sha256"], "metadata_sha256": batch.digest(row["metadata"])}


def dispatch_selection(bound, info=None, *, preparing=False):
    from batch_workers import present_assignments, worker_state

    pairs = course_pairs(bound)
    running = {row["id"] for _, row in pairs if row["status"] == batch.ACTIVE}
    active = bool(running) and not preparing
    planned = planned_work(info, pairs) if info and info.get("v4_handoff") and not active else ()
    held = set((info or {}).get("v4_handoff", {}).get("held", ()))
    settings = worker_state(info or {})
    assignments = present_assignments(settings, {row["id"]: (binding, row) for binding, row in pairs})
    held.update(key for key, job in assignments.items() if preparing or job["state"] != "ready")
    if preparing:
        workspace = Path(info["root"]).parent
        in_preparation = running | assignments.keys()
        planned = [item for item in planned if item.row is None or item.row["id"] not in in_preparation]
        pairs = [(binding, row) for binding, row in pairs if row["status"] != batch.ACTIVE]
        held.update(row["id"] for binding, row in pairs if row["id"] not in held
                    and not local_priority_ready(workspace, binding, row))
    if not preparing and len(assignments) >= settings["limit"]:
        held.update(row["id"] for _, row in pairs if row["id"] not in assignments)
    priorities = {}
    rejected = {}
    submitted = None
    if not active and (info or {}).get("root"):
        workspace = Path(info["root"]).parent
        submitted = submitted_order(workspace)
        reserved = {item.row["id"] for item in planned if item.row is not None and not item.ready}

        def ready(binding, row):
            return row["id"] not in held and row["id"] not in reserved and local_priority_ready(workspace, binding, row)

        def matched(binding, row, anchor):
            if row["status"] != batch.PENDING:
                return None  # An already closed project has no pending ordering issue.
            if (row["id"] in held or row["id"] in reserved
                    or batch.digest(row["metadata"]) != anchor["metadata_sha256"]):
                return False
            try:
                return local_priority_identity(workspace, binding, row) == anchor
            except (OSError, ValueError):
                return False

        priorities = resolved_priorities(submitted, pairs, planned,
                                         ready, workspace, local_identity=matched, rejected_local=rejected,
                                         first_only=True)
    selection = handoff.select(pairs, planned=planned, held=held | set(rejected), eligible=batch.eligible_pending,
                               review_priority=batch.review_priority, natural=batch.natural,
                               active_state=batch.ACTIVE, analysis_priorities=priorities)
    if submitted:
        submission = {key: submitted[key] for key in ("event_id", "revision")}
        return replace(selection, analysis_submission=submission, analysis_waiting=tuple(rejected.values()))
    return selection


def choose_next(bound, info=None):
    return dispatch_selection(bound, info).pair


def brief_next(selection):
    if not selection:
        return None
    binding, row = selection
    out = {field: row[field] for field in ("sequence", "id", "name", "status", "arrival_at",
                                          "ready_at", "package", "course_folder", "re_review",
                                          "v4_order", "analysis_priority") if field in row}
    return {**out, "category": binding["category"], "manifest": binding["manifest"],
            "source_folder": str(Path(binding["source"]) / row["name"]), "vault": binding["vault"]}


def require_global_turn(registry, manifest, row, user_order_reason=None):
    info = read(registry)
    bound = load_bound(info)
    selection = dispatch_selection(bound, info)
    expected = selection.pair
    if user_order_reason is not None:
        if expected and expected[1]["status"] == batch.ACTIVE and (
                Path(expected[0]["manifest"]), expected[1]["id"]) != (manifest, row["id"]):
            current = brief_next(expected)
            raise ValueError(f"类别 {current['category']} 第 {current['sequence']} 项仍在处理，不可提前领取")
        target = next((queue for binding, queue in bound if Path(binding["manifest"]) == manifest), None)
        if target is None:
            raise ValueError("用户指定项目的批次未绑定到当前全局调度")
        target_row = next((item for item in target["projects"] if item["id"] == row["id"]), None)
        if target_row is None:
            raise ValueError("用户指定项目不存在于绑定批次")
        batch.require_turn(target, target_row, user_order_reason)
        if target_row["status"] == batch.ACTIVE:
            return None
        if target_row["id"] in info.get("v4_handoff", {}).get("held", ()):
            raise ValueError("用户指定项与确认计划存在身份冲突，需先核清实收范围")
        from batch_workers import present_assignments, worker_state
        settings = worker_state(info)
        pending_assignments = present_assignments(settings, {item["id"]: (binding, item)
            for binding, item in course_pairs(bound) if item["status"] == batch.PENDING})
        if target_row["id"] not in pending_assignments and len(pending_assignments) >= settings["limit"]:
            raise ValueError("分析席位已满，请先接续已分配项目")
        matched = [value for value in planned_work(info, course_pairs(bound)) if value.row is target_row]
        if matched and not matched[0].ready:
            raise ValueError(f"用户指定项的已确认资料须先完成实收匹配：{matched[0].blocker}")
        return matched[0].summary() if matched else None
    if expected is None or (Path(expected[0]["manifest"]), expected[1]["id"]) != (manifest, row["id"]):
        current = brief_next(expected)
        label = (f"{current['category']} 第 {current['sequence']} 项" if current else
                 (selection.waiting or {}).get("blocker", "无已就绪待处理项目"))
        raise ValueError(f"全局当前不可领取此项目；应接续 {label}")
    if selection.analysis_priority:
        return {**(selection.order or {}), "analysis_priority": selection.analysis_priority}
    return selection.order


def register(info, registry, *, settle_seconds, moment):
    changed = []
    for binding in info["bindings"].values():
        manifest = batch.safe_path(binding["manifest"])
        source = Path(binding["source"])
        if not manifest.exists() and not any(batch.is_project(path) for path in candidates(source, binding.get("layout"))):
            continue
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(manifest.with_suffix(manifest.suffix + ".lock")):
            if manifest.exists():
                queue = batch.open_queue(manifest)
                before = batch.digest(queue)
                if (queue["source"], queue["vault"]) != (binding["source"], binding["vault"]):
                    raise ValueError(f"绑定批次来源或阅读库不一致 {manifest}")
                if queue.get("dispatch_registry") not in (None, str(registry)):
                    raise ValueError(f"批次已绑定其他全局调度 {manifest}")
            else:
                # There is no historical ledger in this category. Every course
                # is newly observed, and must pass the same stability gate.
                vault = Path(binding["vault"])
                (vault / ".obsidian").mkdir(parents=True, exist_ok=True)
                queue = {"schema": batch.SCHEMA, "source": str(source), "vault": str(vault),
                         "projects": [], "unassigned": [], "created_at": moment.isoformat()}
                before = None
            queue["dispatch_registry"] = str(registry)
            queue["dispatch_settle_seconds"] = settle_seconds
            queue["dispatch_layout"] = binding.get("layout")
            refresh_queue(queue, moment=moment)
            if queue["projects"] and batch.digest(queue) != before:
                put(manifest, queue)
                changed.append((manifest, queue))
    return changed


def changed_waiting_sources(queue):
    """Expose actual additions/replacements without approving a new edition."""
    changed = []
    waiting_states = {batch.DEFERRED, batch.SUPPLEMENT_PENDING, batch.CORE_PENDING, batch.REUSED}
    for row in queue["projects"]:
        if row["status"] not in waiting_states and not (
                row["status"] == batch.PENDING and row.get("re_review")):
            continue
        try:
            actual = batch.metadata(batch.source_path(queue, row))
        except (OSError, ValueError) as error:
            changed.append({"sequence": row["sequence"], "detail": str(error)})
            continue
        before = {item["file"]: item for item in row["metadata"]}
        after = {item["file"]: item for item in actual}
        additions = sorted(after.keys() - before.keys())
        removals = sorted(before.keys() - after.keys())
        replacements = sorted(key for key in before.keys() & after.keys() if before[key] != after[key])
        if additions or removals or replacements:
            changed.append({"sequence": row["sequence"], "status": row["status"],
                            "added": additions, "removed": removals, "changed": replacements,
                            "same_source_primary": row.get("same_source_primary")})
    return changed


def summarize(info, categories, uncertain, *, refresh):
    bound = load_bound(info)
    by_category = {binding["category"]: queue for binding, queue in bound}
    discovered_names = {cat["category"] for cat in categories}
    entries = []
    for name, binding in info["bindings"].items():
        source = Path(binding["source"])
        queue = by_category.get(name)
        rows = queue["projects"] if queue else []
        registered = {row["name"] for row in rows}
        present = {p.name for p in candidates(source, binding.get("layout"), registered) if batch.is_project(p)}
        counts = dict(Counter(row["status"] for row in rows))
        entries.append({**binding, "source_available": name in discovered_names,
                        "registered": len(rows), "present": len(present), "counts": counts,
                        "new_not_registered": sorted(present - registered, key=batch.natural),
                        "new_waiting": [{"sequence": row["sequence"], "name": row["name"],
                                         "detail": row.get("arrival_blocker")}
                                        for row in rows if row.get("arrival_at")
                                        and row["status"] == batch.PENDING and not row.get("ready_at")],
                        "missing_registered": [{"sequence": row["sequence"], "name": row["name"]}
                                               for row in rows if row["name"] not in present],
                        "identity_pending": queue.get("identity_pending", []) if queue else [],
                        "re_review_pending": [row["sequence"] for row in rows
                                              if row.get("re_review", {}).get("state") == "待复审"
                                              and row["status"] in (batch.PENDING, batch.ACTIVE)],
                        "alias_followups": [{"sequence": row["sequence"], "primary": row["same_source_primary"],
                                             "detail": row.get("alias_recheck")}
                                            for row in rows if row.get("same_source_primary")
                                            and row.get("requires_alias_resolution")],
                        "source_change_candidates": changed_waiting_sources(queue) if queue else []})
    selection = dispatch_selection(bound, info)
    selected_brief = brief_next(selection.pair)
    if selected_brief and selection.order:
        selected_brief["v4_order"] = selection.order
    if selected_brief and selection.analysis_priority:
        selected_brief["analysis_priority"] = selection.analysis_priority
    result = {"read_only": not refresh, "scope": info["root"], "categories": entries,
            "unrecognized_categories": uncertain, "next": selected_brief,
            "registered_projects": sum(row["registered"] for row in entries),
            "present_projects": sum(row["present"] for row in entries)}
    from storage_inventory import cleanup_pressure

    next_bytes = None
    if selection.pair and selection.pair[1]["status"] == batch.PENDING:
        next_bytes = sum(file["bytes"] for file in selection.pair[1]["metadata"])
    try:
        result["cleanup_pressure"] = cleanup_pressure(Path(info["root"]), next_task_bytes=next_bytes)
    except OSError:
        result["cleanup_pressure"] = {"read_only": True, "state": "storage_unavailable"}
    if info.get("v4_handoff"):
        result["v4_order"] = {"waiting": selection.waiting, "items": selection.plan_items,
                              "warnings": info["v4_handoff"].get("warnings", [])}
    if selection.analysis_waiting:
        result["analysis_waiting"] = list(selection.analysis_waiting)
    if selection.analysis_submission:
        result["analysis_submission"] = selection.analysis_submission
    return result


def dispatch(root, registry, legacy_manifests=(), *, refresh=False, settle_seconds=30):
    root, registry = map(batch.safe_path, (root, registry))
    if not root.is_dir() or registry.is_relative_to(root):
        raise ValueError("调度需使用真实总目录，绑定索引必须放在输入总目录之外")
    if settle_seconds < 1:
        raise ValueError("新增项目稳定时间至少一秒，需两次盘点")
    if not refresh:
        categories, uncertain = discover(root)
        info = registry_data(root, registry)
        bind_categories(info, registry, categories, legacy_manifests)
        return summarize(info, categories, uncertain, refresh=False)
    registry.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(registry.with_suffix(registry.suffix + ".lock")):
        categories, uncertain = discover(root)
        info = registry_data(root, registry)
        bind_categories(info, registry, categories, legacy_manifests)
        # Persist bindings first so a restart can recover without creating another
        # identity; all authoritative course states remain in the category ledgers.
        put(registry, info)
        changed = register(info, registry, settle_seconds=settle_seconds, moment=datetime.now(timezone.utc))
        refresh_handoff(info, load_bound(info))
        result = summarize(info, categories, uncertain, refresh=True)
        previous_readback = info.pop("analysis_readback", None)
        if submission := result.get("analysis_submission"):
            info["analysis_readback"] = {"schema_version": 1, **submission,
                                         "observed_at": datetime.now(timezone.utc).isoformat(),
                                         "waiting": result.get("analysis_waiting", [])}
        if info.get("v4_handoff") or previous_readback != info.get("analysis_readback"):
            put(registry, info)
    for manifest, queue in changed:
        batch.refresh_registered_catalog(queue, manifest)
    return result
