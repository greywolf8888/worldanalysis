"""Discover category sources and coordinate their existing authoritative course batches."""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import batch
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


def choose_next(bound):
    all_candidates = []
    active = []
    for binding, queue in bound:
        active.extend((binding, row) for row in queue["projects"] if row["status"] == batch.ACTIVE)
        row = batch.next_row(queue)
        if row:
            all_candidates.append((binding, row))
    if len(active) > 1:
        raise ValueError(f"发现 {len(active)} 个正式在办项目，须对账后继续唯一生产")
    if active:
        return active[0]
    priority = [pair for pair in all_candidates if batch.review_priority(pair[1]) is not None]
    if priority:
        return min(priority, key=lambda pair: (batch.review_priority(pair[1]), batch.natural(pair[0]["category"])))
    arrivals = [pair for pair in all_candidates if pair[1].get("arrival_at")]
    if arrivals:
        newest = max(row["arrival_at"] for _, row in arrivals)
        all_candidates = [pair for pair in arrivals if pair[1]["arrival_at"] == newest]
    return min(all_candidates,
               key=lambda pair: (batch.natural(pair[0]["category"]), batch.natural(pair[1]["name"])),
               default=None)


def brief_next(selection):
    if not selection:
        return None
    binding, row = selection
    out = {field: row[field] for field in ("sequence", "id", "name", "status", "arrival_at",
                                          "ready_at", "package", "course_folder", "re_review") if field in row}
    return {**out, "category": binding["category"], "manifest": binding["manifest"],
            "source_folder": str(Path(binding["source"]) / row["name"]), "vault": binding["vault"]}


def require_global_turn(registry, manifest, row, user_order_reason=None):
    info = read(registry)
    bound = load_bound(info)
    expected = choose_next(bound)
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
        return
    if expected is None or (Path(expected[0]["manifest"]), expected[1]["id"]) != (manifest, row["id"]):
        current = brief_next(expected)
        label = (f"{current['category']} 第 {current['sequence']} 项"
                 if current else "无已就绪待处理项目")
        raise ValueError(f"全局当前不可领取此项目；应接续 {label}")


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
    selected = choose_next(bound)
    selected_brief = brief_next(selected)
    return {"read_only": not refresh, "scope": info["root"], "categories": entries,
            "unrecognized_categories": uncertain, "next": selected_brief,
            "registered_projects": sum(row["registered"] for row in entries),
            "present_projects": sum(row["present"] for row in entries)}


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
        result = summarize(info, categories, uncertain, refresh=True)
    for manifest, queue in changed:
        batch.refresh_registered_catalog(queue, manifest)
    return result
