"""Run the single-course workflow against a durable, ordered batch of source folders."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

from project import CourseProject, file_lock, inventory_files, local_child, named_entry, put, read, sha


SCHEMA = 1
PENDING = "待处理"
ACTIVE = "处理中"
DEFERRED = "待补原件"
COMPLETE = "已验收"
HISTORICAL = "历史验收待复验"
LEGACY_QUEUE = "原件队列.json"
LEGACY_CURRENT = "当前续作.json"


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(rows):
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def natural(name):
    parts = tuple((0, int(part)) if part.isdecimal() else (1, part.casefold())
                  for part in re.split(r"(\d+)", name))
    return parts, name.casefold(), name


def safe_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError(f"批次路径包含链接或目录联接 {part}")
    return path.resolve()


def metadata(root):
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise ValueError(f"项目原件目录不存在或不是普通文件夹 {root}")

    files = []

    def visit(folder):
        for path in folder.iterdir():
            if path.is_symlink() or path.is_junction():
                raise ValueError(f"原件含链接或目录联接 {path.relative_to(root)}")
            if path.is_dir():
                visit(path)
            elif path.is_file():
                info = path.stat()
                files.append({"file": path.relative_to(root).as_posix(),
                              "bytes": info.st_size, "mtime_ns": info.st_mtime_ns})
            else:
                raise ValueError(f"原件含非普通文件 {path.relative_to(root)}")

    visit(root)
    return sorted(files, key=lambda item: (item["file"].casefold(), item["file"]))


def source_path(queue, row):
    return local_child(Path(queue["source"]), row["name"])


def observe_folder(source, path, sequence):
    row = {"sequence": sequence, "id": digest([str(source), path.name]),
           "name": path.name, "metadata": [], "status": PENDING}
    try:
        row["metadata"] = metadata(path)
        if not row["metadata"]:
            raise ValueError("未发现原件文件")
    except (OSError, ValueError) as error:
        row.update(status=DEFERRED, reason=str(error))
    return row


def project_candidates(source):
    return sorted(source.iterdir(), key=lambda item: natural(item.name))


def is_project(path):
    return path.is_dir() or path.is_symlink() or path.is_junction()


def scan_batch(source, vault=None):
    source = safe_path(source)
    if not source.is_dir():
        raise ValueError(f"找不到批次目录 {source}")
    if vault is not None:
        vault = safe_path(vault)
        if source.is_relative_to(vault) or vault.is_relative_to(source):
            raise ValueError("批次原件和日常阅读库需要处于两个独立目录")
        if not local_child(vault, ".obsidian").is_dir():
            raise ValueError(f"请选择日常使用的 Obsidian 库 {vault}")

    projects, unassigned = [], []
    for path in project_candidates(source):
        if not is_project(path):
            unassigned.append(path.name)
            continue
        projects.append(observe_folder(source, path, len(projects) + 1))
    if not projects:
        raise ValueError(f"批次目录没有项目子目录 {source}")
    return {"schema": SCHEMA, "source": str(source), "vault": str(vault) if vault else None,
            "projects": projects, "unassigned": unassigned}


def row_at(queue, sequence):
    rows = queue["projects"]
    if type(sequence) is not int or not 1 <= sequence <= len(rows):
        raise ValueError(f"没有批次项目顺位 {sequence}")
    return rows[sequence - 1]


def check_current_metadata(queue, row):
    source = source_path(queue, row)
    if metadata(source) != row["metadata"]:
        raise ValueError(f"批次原件已经变化。核完差异后重新登记第 {row['sequence']} 项 {source}")
    return source


def source_fingerprint(files):
    return digest([(row["file"], row["bytes"], row["sha256"]) for row in files])


def content_fingerprint(files):
    return digest(sorted((row["bytes"], row["sha256"]) for row in files))


def identify(queue, row):
    source = check_current_metadata(queue, row)
    files = inventory_files(source)
    if not files:
        raise ValueError(f"没有可登记的原件 {source}")
    if metadata(source) != row["metadata"]:
        raise ValueError(f"盘点期间原件已经变化 {source}")
    identity = {"source_sha256": source_fingerprint(files), "content_sha256": content_fingerprint(files)}
    if row.get("source_sha256") and identity["source_sha256"] != row["source_sha256"]:
        raise ValueError(f"原件指纹和当前批次登记不一致 {source}")
    return source, files, identity


def size_key(row):
    return tuple(sorted(file["bytes"] for file in row["metadata"]))


def find_duplicates(queue, row, identity):
    matches = []
    sizes = size_key(row)
    for other in queue["projects"]:
        if other["id"] == row["id"] or size_key(other) != sizes:
            continue
        try:
            _, _, current = identify(queue, other)
        except (OSError, ValueError):
            # A changed or unreadable row stays in the queue for its own review.
            continue
        if current["content_sha256"] == identity["content_sha256"]:
            matches.append(other["sequence"])
    return matches


def check_course_folder(name):
    if (not isinstance(name, str) or not name.strip() or name in (".", "..")
            or name.startswith(".") or any(ch in name for ch in '<>:"/\\|?*')
            or name.endswith((" ", "."))):
        raise ValueError(f"日常库项目名须为普通的一级目录名 {name}")
    return name


def check_target(queue, row, package, folder):
    if row.get("package") and Path(row["package"]) != package:
        raise ValueError(f"第 {row['sequence']} 项已绑定课程包 {row['package']}")
    if row.get("course_folder") and row["course_folder"] != folder:
        raise ValueError(f"第 {row['sequence']} 项已绑定日常库目录 {row['course_folder']}")
    if row.get("package_ready") and not package.is_dir():
        raise ValueError(f"已登记课程包不在原路径，请恢复后再续作 {package}")
    if package.exists() and not package.is_dir():
        raise ValueError(f"课程包位置不是目录 {package}")
    source = Path(queue["source"])
    if package.is_relative_to(source) or source.is_relative_to(package):
        raise ValueError(f"课程包不能和原件批次共用目录 {package}")
    conflicts = [other["sequence"] for other in queue["projects"] if other["id"] != row["id"]
                 and ((other.get("package") and Path(other["package"]) == package)
                      or (other.get("course_folder") or "").casefold() == folder.casefold())]
    if conflicts:
        raise ValueError(f"课程包或日常库项目名已经绑定批次第 {conflicts} 项")


def project_origin(project):
    return Path(read(project.state / "project.json")["origin"])


def verify_identity(project, source, originals, row):
    origin = project_origin(project)
    supplemented = row.get("supplemented_origin")
    if origin != (Path(supplemented) if supplemented else source):
        raise ValueError(f"课程包绑定的是另一批次原件 {origin}")
    package_originals = project.validate_raw()
    if supplemented:
        by_name = {item["file"]: item for item in package_originals}
        matches = all(by_name.get(item["file"]) == item for item in originals)
        expected = row.get("supplemented_sha256")
        if matches and (not expected or expected == source_fingerprint(package_originals)):
            return package_originals
    elif package_originals == originals:
        return package_originals
    raise ValueError(f"课程包原件与当前来源目录的原件指纹不一致 {source}")


def current_delivery(queue, project, folder, legacy_sha256=None):
    from acceptance import verify_publication
    from vault_delivery import delivery, delivery_status

    publication = verify_publication(project, raw_verified=True)
    state = delivery(project)
    vault = Path(queue["vault"])
    expected = named_entry(local_child(vault, folder))
    if (not state or Path(state["vault"]) != vault
            or state["folder"] != folder or Path(state["entry"]) != expected):
        raise ValueError(f"此课程需在它绑定的日常阅读库入口验收 {expected}")
    if publication["review"].get("schema") != 2 and state["sha256"] != legacy_sha256:
        raise ValueError("新增批次交付须通过当前来源覆盖验收，请按最新版流程复核")
    observed = state.get("observation")
    if (not observed or any(observed.get(k) != state[k] for k in ("entry", "sha256", "installed_at"))
            or observed.get("mode") != "preview"
            or not all(observed.get(k) is True for k in ("reopened", "reached_bottom", "references_collapsed"))
            or observed.get("visible_citations") is not False):
        raise ValueError(f"日常库当前版本缺同入口重开记录 {expected}")
    verified = delivery_status(project, publication, required=True)
    return {key: verified[key] for key in ("entry", "uri", "sha256", "method")}


def next_row(queue):
    rows = queue["projects"]
    return next((row for row in rows if row["status"] == ACTIVE), None) or min(
        (row for row in rows if row["status"] == PENDING), key=lambda row: natural(row["name"]), default=None)


def require_turn(queue, row):
    current = next_row(queue)
    if current is None or current["id"] != row["id"]:
        suffix = f"。当前应处理第 {current['sequence']} 项" if current else ""
        raise ValueError(f"第 {row['sequence']} 项当前不可办理{suffix}")


def preserve(row):
    prior = {k: row[k] for k in ("status", "reason", "package", "course_folder", "source_sha256",
                               "content_sha256", "publication", "package_ready", "last_changed") if k in row}
    row.setdefault("history", []).append({**prior, "metadata_sha256": digest(row["metadata"])})


def change(row, status, **fields):
    if row["status"] == status and all(row.get(key) == value for key, value in fields.items()):
        return
    if row.get("last_changed"):
        preserve(row)
    row.update(status=status, last_changed=timestamp(), **fields)


def claim(queue, row, package, folder=None, separate=None):
    require_turn(queue, row)
    package = safe_path(package)
    folder = check_course_folder(folder or row.get("course_folder") or package.name)
    check_target(queue, row, package, folder)
    source, originals, identity = identify(queue, row)
    duplicates = find_duplicates(queue, row, identity)
    decision = (separate or row.get("duplicate_decision") or "").strip()
    if duplicates and (not decision or not separate and not set(duplicates).issubset(row.get("duplicates", []))):
        raise ValueError(f"原件集合与第 {duplicates} 项完全一致。核实后提供单独保留项目的依据，或登记待复查")
    ready = package.exists()
    if ready:
        project = CourseProject.open(package)
        with project.locked():
            verify_identity(project, source, originals, row)
    change(row, ACTIVE, package=str(package), package_ready=ready, course_folder=folder, **identity,
           duplicates=duplicates, duplicate_decision=decision, reason=None)


def defer(queue, row, reason):
    if not reason.strip():
        raise ValueError("请登记缺少的原件或具体阻塞")
    if row["status"] != DEFERRED:
        require_turn(queue, row)
    change(row, DEFERRED, reason=reason)


def reopen(queue, row, accept_changes=None):
    if row["status"] == ACTIVE:
        raise ValueError("项目正在处理。请接续已有课程包，或记录具体阻塞后回到队列")
    current = metadata(source_path(queue, row))
    if not current:
        raise ValueError("待补项目仍没有原件")
    if current != row["metadata"] and not accept_changes:
        raise ValueError("原件范围有变化。请核实新增、移除或替换的证据后登记原因")
    if row["status"] in (COMPLETE, HISTORICAL) and not accept_changes:
        raise ValueError("项目已有验收。如需重做，请登记调整原因")
    if accept_changes is not None:
        if not accept_changes.strip():
            raise ValueError("请登记同一项目新增、移除或更换原件的依据")
        if row["status"] != PENDING or current != row["metadata"] or row.get("reason") != accept_changes:
            preserve(row)
            for key in ("source_sha256", "content_sha256", "package", "package_ready", "duplicates", "duplicate_decision",
                        "publication", "supplemented_origin", "supplemented_sha256", "supplemented_confirmed"):
                row.pop(key, None)
            row.update(status=PENDING, metadata=current, reason=accept_changes, last_changed=timestamp())
        return
    if row["status"] == DEFERRED:
        change(row, PENDING, reason=None)


def finish(queue, row, package):
    if row["status"] not in (ACTIVE, COMPLETE, HISTORICAL):
        raise ValueError("请先领取该项目并完成课程制作")
    package = safe_path(package)
    folder = row["course_folder"]
    check_target(queue, row, package, folder)
    source, originals, identity = identify(queue, row)
    duplicates = find_duplicates(queue, row, identity)
    if duplicates and (not row.get("duplicate_decision") or not set(duplicates).issubset(row.get("duplicates", []))):
        raise ValueError(f"原件集合与第 {duplicates} 项完全一致。请补记单独保留项目的依据")
    project = CourseProject.open(package)
    with project.locked():
        snapshot = verify_identity(project, source, originals, row)
        legacy = row.get("publication", {}).get("sha256") if row.get("legacy_record") else None
        publication = current_delivery(queue, project, folder, legacy)
        check_current_metadata(queue, row)
        if row.get("supplemented_origin"):
            identity["supplemented_sha256"] = source_fingerprint(snapshot)
        change(row, COMPLETE, **identity, package_ready=True, publication=publication, duplicates=duplicates, reason=None)


def legacy_snapshot(queue, directory):
    directory = safe_path(directory)
    old = read(directory / LEGACY_QUEUE)
    rows = old["项目"]
    if (Path(old["入库来源"]) != Path(queue["source"]) or len(rows) != len(queue["projects"])
            or (queue["vault"] and Path(old["日常库"]) != Path(queue["vault"]))):
        raise ValueError("历史队列的原件范围或日常库与本次批次不一致")
    for i, previous in enumerate(rows, 1):
        row = row_at(queue, i)
        recorded = [{"file": name["路径"].replace("\\", "/"), "bytes": name["大小"],
                     "mtime_ns": name["最后修改_ns"]} for name in previous["文件"]]
        recorded.sort(key=lambda item: (item["file"].casefold(), item["file"]))
        if (previous["序号"] != i or previous["目录名"] != row["name"]
                or Path(previous["原件目录"]) != Path(queue["source"]) / row["name"]
                or recorded != row["metadata"]):
            raise ValueError(f"历史队列与第 {i} 项原件现状不一致，请先核对范围")
    return directory


def historical_records(queue, directory):
    directory = legacy_snapshot(queue, directory)
    outcomes = {}
    for pattern, state in (("*-已验收项目.json", COMPLETE), ("*-原始教程待提供.json", DEFERRED)):
        for path in sorted(directory.glob(pattern)):
            info = read(path)
            index = int(path.name.split("-", 1)[0])
            row_at(queue, index)
            if index in outcomes or info.get("次序", info.get("序号", index)) != index:
                raise ValueError(f"历史项目记录顺位冲突 {path}")
            outcomes[index] = (path, info, state)
    current_path = directory / LEGACY_CURRENT
    if current_path.is_file():
        current = read(current_path)
        index = current["次序"]
        row = row_at(queue, index)
        if Path(current["原件"]) != Path(queue["source"]) / row["name"] or index in outcomes:
            raise ValueError(f"历史续作点与项目队列不一致 {current_path}")
        if index != next((i for i in range(1, len(queue["projects"]) + 1) if i not in outcomes), None):
            raise ValueError(f"历史续作点前还有未登记项目 {current_path}")
        outcomes[index] = (current_path, current, ACTIVE)
    return outcomes


def legacy_evidence(queue, row, path, info, state, *, verify, supplemented):
    if state == DEFERRED:
        reason = info["状态"]
        deferred = {"status": state, "reason": reason, "legacy_record": str(path)}
        if info.get("原始资料"):
            package = safe_path(Path(info["原始资料"]).parent)
            project = CourseProject.open(package)
            if project_origin(project) != source_path(queue, row):
                raise ValueError(f"待补项目的课程包与批次原件目录不一致 {path}")
            folder = check_course_folder(info["项目"])
            check_target(queue, row, package, folder)
            deferred.update(package=str(package), package_ready=True, course_folder=folder)
        return deferred
    if state == ACTIVE:
        package = safe_path(info["课程包"])
        folder = check_course_folder(info["目标项目"])
    else:
        expected = safe_path(info["入口"])
        recorded = safe_path(info["交付回执"])
        if not recorded.is_relative_to(path.parent):
            raise ValueError(f"历史课程回执超出批次记录目录 {recorded}")
        report = read(recorded)
        if not report.get("verified") or not report.get("package_verified"):
            raise ValueError(f"历史项目没有有效验收回执 {path}")
        package = safe_path(Path(report["entry"]).parent.parent)
        folder = check_course_folder(expected.parent.parent.name)
        vault = Path(queue["vault"]) if queue["vault"] else expected.parent.parent.parent
        if (expected != named_entry(local_child(vault, folder)) or sha(expected) != info["版本"]
                or report["vault_delivery"]["sha256"] != info["版本"]
                or Path(report["vault_delivery"]["entry"]) != expected):
            raise ValueError(f"历史记录与日常库教材版本不一致 {path}")
    check_target(queue, row, package, folder)
    project = CourseProject.open(package)
    mapped = {"status": state, "package": str(package), "package_ready": True,
              "course_folder": folder, "legacy_record": str(path)}
    if state == ACTIVE:
        mapped["resume_note"] = info.get("进度")
    else:
        mapped["publication"] = {key: report["vault_delivery"].get(key)
                                 for key in ("entry", "uri", "sha256", "method")}
        if info.get("原件缺口"):
            mapped["source_gap"] = info["原件缺口"]
    origin = project_origin(project)
    if origin != source_path(queue, row):
        if state != COMPLETE or not origin.is_relative_to(path.parent):
            raise ValueError(f"历史课程包与批次原件目录不一致 {path}")
        package_names = {item["file"]: item["bytes"] for item in read(project.intake)["sources"]}
        if any(package_names.get(item["file"]) != item["bytes"] for item in row["metadata"]):
            raise ValueError(f"历史补料快照未保留本项目全部原件路径和大小 {path}")
        mapped["supplemented_origin"] = str(origin)
        if verify and row["sequence"] not in supplemented:
            raise ValueError(f"第 {row['sequence']} 项使用合并的补料快照，先用只读 plan 核对，再明确登记该顺位")
    if not verify:
        return mapped
    source, originals, identity = identify(queue, row)
    with project.locked():
        snapshot = verify_identity(project, source, originals, mapped)
        if "supplemented_origin" in mapped:
            mapped["supplemented_sha256"] = source_fingerprint(snapshot)
        if state == COMPLETE:
            publication = current_delivery(queue, project, folder, info["版本"])
            if publication["sha256"] != info["版本"]:
                raise ValueError(f"现行日常库验收和原历史回执版本不一致 {path}")
            mapped["publication"] = publication
    return {**mapped, **identity}


def import_legacy(queue, directory, *, verify, supplemented=(), defer_history=False):
    records = historical_records(queue, directory)
    for index, (path, info, state) in sorted(records.items()):
        row = row_at(queue, index)
        rehash = verify and not (defer_history and state == COMPLETE)
        if rehash and state != DEFERRED:
            print(f"核验历史顺位 {index} · {row['name']}", file=sys.stderr, flush=True)
        row.update(legacy_evidence(queue, row, path, info, state, verify=rehash, supplemented=supplemented))
        if defer_history and state == COMPLETE:
            if row.get("supplemented_origin") and index not in supplemented:
                raise ValueError(f"第 {index} 项使用合并的补料快照，先用只读 plan 核对，再明确登记该顺位")
            row["status"] = HISTORICAL
        if row.get("supplemented_origin") and index in supplemented:
            row["supplemented_confirmed"] = True
        row["last_changed"] = info.get("日期时间") or info.get("最近更新时间") or info.get("日期") or timestamp()
    key = "supplemented_origin" if defer_history else "supplemented_sha256"
    if set(supplemented) - {row["sequence"] for row in queue["projects"] if row.get(key)}:
        raise ValueError("补料快照登记包含未使用补料的顺位")


def refresh(queue):
    source = Path(queue["source"])
    projects = queue["projects"]
    registered = {row["name"] for row in projects}
    unassigned = []
    for path in project_candidates(source):
        if path.name in registered:
            continue
        if is_project(path):
            projects.append(observe_folder(source, path, len(projects) + 1))
        else:
            unassigned.append(path.name)
    queue["unassigned"] = unassigned


def describe_row(queue, row):
    return {**{key: value for key, value in row.items() if key not in ("metadata", "history")},
            "original_folder": str(Path(queue["source"]) / row["name"])}


def status(queue, *, scan=False):
    current = next_row(queue)
    rows = queue["projects"]
    totals = Counter(row["status"] for row in rows)
    result = {"schema": SCHEMA, "source": queue["source"], "vault": queue["vault"],
              "projects": len(rows), "files": sum(len(row["metadata"]) for row in rows),
              "bytes": sum(item["bytes"] for row in rows for item in row["metadata"]),
              "counts": dict(totals), "next": current["sequence"] if current else None,
              "hash_verified": sum("source_sha256" in row for row in rows),
              "unassigned": queue["unassigned"],
              "entries": [describe_row(queue, row) for row in rows]}
    if scan:
        source = Path(queue["source"])
        registered = {row["name"] for row in rows}
        present = {path.name for path in source.iterdir()} if source.is_dir() else set()
        result["new"] = sorted(present - registered - set(queue["unassigned"]), key=natural)
        drift = []
        for row in rows:
            try:
                check_current_metadata(queue, row)
            except (OSError, ValueError) as error:
                drift.append({"sequence": row["sequence"], "detail": str(error)})
        result["metadata_drift"] = drift
        result["receipt_note_drift"] = [
            row["sequence"] for row in rows if row.get("publication")
            and not current_note_matches(row["publication"])]
        result["package_drift"] = [
            row["sequence"] for row in rows if row.get("package_ready")
            and not (Path(row["package"]) / ".course" / "project.json").is_file()]
    same_sizes = {}
    for row in rows:
        if row["metadata"]:
            same_sizes.setdefault(size_key(row), []).append(row["sequence"])
    result["possible_duplicate_groups_by_size"] = [group for group in same_sizes.values() if len(group) > 1]
    result["historical_supplements_pending_confirmation"] = [
        row["sequence"] for row in rows if "supplemented_origin" in row and not row.get("supplemented_confirmed")]
    return result


def current_note_matches(publication):
    try:
        return sha(Path(publication["entry"])) == publication["sha256"]
    except OSError:
        return False


def open_queue(path):
    queue = read(path)
    if queue.get("schema") != SCHEMA or not queue.get("projects"):
        raise ValueError(f"不是受支持的本地课程批次清单 {path}")
    return queue


def initialize(args):
    queue = scan_batch(args.source, args.vault)
    manifest = safe_path(args.manifest)
    source, vault = map(Path, (queue["source"], queue["vault"]))
    if manifest.is_relative_to(source) or manifest.is_relative_to(vault):
        raise ValueError("批次记录请放在原件和日常库之外")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(manifest.with_suffix(manifest.suffix + ".lock")):
        if manifest.exists():
            saved = open_queue(manifest)
            if (saved["source"], saved["vault"]) != (queue["source"], queue["vault"]):
                raise ValueError(f"已有批次清单属于不同原件或日常库 {manifest}")
            return status(saved, scan=True)
        if args.legacy_directory:
            import_legacy(queue, args.legacy_directory, verify=True, supplemented=args.supplemented,
                          defer_history=args.defer_history)
        elif args.supplemented or args.defer_history:
            raise ValueError("历史验收选项仅适用于迁移已有批次")
        queue["created_at"] = timestamp()
        put(manifest, queue)
    return status(queue)


def add_arguments(commands):
    batch = commands.add_parser("batch", help="盘点并按顺位追踪整批本地课程")
    sub = batch.add_subparsers(dest="batch_action", required=True)
    plan = sub.add_parser("plan", help="只读盘点原件和历史续作点")
    plan.add_argument("source", type=Path)
    plan.add_argument("--vault", type=Path)
    plan.add_argument("--legacy-directory", type=Path)
    init = sub.add_parser("init", help="建立可续作的本地批次清单")
    init.add_argument("source", type=Path)
    init.add_argument("--manifest", type=Path, required=True)
    init.add_argument("--vault", type=Path, required=True)
    init.add_argument("--legacy-directory", type=Path)
    init.add_argument("--supplemented", type=int, action="append", default=[], metavar="SEQUENCE",
                      help="同意逐项验证已有课程包内合并的补料快照，可重复传入")
    init.add_argument("--defer-history", action="store_true",
                      help="核实当前续作原件，历史验收项目保留回执和版次，稍后逐项复验")
    sub.add_parser("status", help="查看项目进度和原件范围变化").add_argument("manifest", type=Path)
    sub.add_parser("refresh", help="登记新增项目并保留原有项目编号").add_argument("manifest", type=Path)
    for name in ("claim", "defer", "reopen", "finish"):
        action = sub.add_parser(name)
        action.add_argument("manifest", type=Path)
        action.add_argument("sequence", type=int)
        if name in ("claim", "finish"):
            action.add_argument("package", type=Path)
        if name == "claim":
            action.add_argument("--course-folder")
            action.add_argument("--separate", metavar="REASON")
        if name == "defer":
            action.add_argument("reason")
        if name == "reopen":
            action.add_argument("--accept-changes", metavar="REASON")


def run(args):
    try:
        action = args.batch_action
        if action == "plan":
            queue = scan_batch(args.source, args.vault)
            if args.legacy_directory:
                import_legacy(queue, args.legacy_directory, verify=False)
            result = {**status(queue), "read_only_plan": True}
        elif action == "init":
            result = initialize(args)
        else:
            path = safe_path(args.manifest)
            if action == "status":
                result = status(open_queue(path), scan=True)
            else:
                with file_lock(path.with_suffix(path.suffix + ".lock")):
                    queue = open_queue(path)
                    if action == "refresh":
                        refresh(queue)
                        result = status(queue, scan=True)
                    else:
                        row = row_at(queue, args.sequence)
                        if action == "claim":
                            claim(queue, row, args.package, args.course_folder, args.separate)
                        elif action == "defer":
                            defer(queue, row, args.reason)
                        elif action == "reopen":
                            reopen(queue, row, args.accept_changes)
                        else:
                            finish(queue, row, args.package)
                        result = describe_row(queue, row)
                    put(path, queue)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2
