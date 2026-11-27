"""Run the single-course workflow against a durable, ordered batch of source folders."""
from collections import Counter
from contextlib import nullcontext
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
SUPPLEMENT_PENDING = "已验收（配套待补）"
CORE_PENDING = "已分析（核心待补）"
REUSED = "同源复用"
ARCHIVED = "留档"
HISTORICAL = "历史验收待复验"
PUBLISHED = {COMPLETE, SUPPLEMENT_PENDING, CORE_PENDING}
SCOPE_STATE = {"完整验收": COMPLETE, "配套待补": SUPPLEMENT_PENDING, "核心待补": CORE_PENDING}
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
        if (other["id"] == row["id"] or other.get("same_source_primary") == row["sequence"]
                or size_key(other) != sizes):
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
    from scope_review import publication_scope
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
    result = {key: verified[key] for key in ("entry", "uri", "sha256", "method")}
    scope = publication_scope(publication)
    if scope["scope_status"] is None:
        if state["sha256"] != legacy_sha256:
            raise ValueError("新验收版次缺明确范围签收状态，须按当前流程复核")
        result["legacy_scope_unclassified"] = True
        return result
    if scope["scope_status"] not in SCOPE_STATE:
        raise ValueError("发布回执中存在未支持的验收范围状态")
    return {**result, **scope}


def eligible_pending(row):
    return (row["status"] == PENDING and not row.get("same_source_primary")
            and (not row.get("arrival_at") or bool(row.get("ready_at"))))


def review_priority(row):
    review = row.get("re_review") or {}
    return review.get("priority_rank") if review.get("state") == "待复审" else None


def next_row(queue):
    rows = queue["projects"]
    active = next((row for row in rows if row["status"] == ACTIVE), None)
    if active is not None:
        return active
    pending = [row for row in rows if eligible_pending(row)]
    priority = [row for row in pending if review_priority(row) is not None]
    if priority:
        return min(priority, key=review_priority)
    arrivals = [row for row in pending if row.get("arrival_at")]
    if arrivals:
        newest = max(row["arrival_at"] for row in arrivals)
        return min((row for row in arrivals if row["arrival_at"] == newest),
                   key=lambda row: natural(row["name"]))
    return min(pending, key=lambda row: natural(row["name"]), default=None)


def require_turn(queue, row, user_order_reason=None):
    if user_order_reason is not None:
        if not user_order_reason.strip():
            raise ValueError("指定顺序须提供用户明确要求的具体依据")
        active = next((item for item in queue["projects"] if item["status"] == ACTIVE
                       and item["id"] != row["id"]), None)
        if active:
            raise ValueError(f"第 {active['sequence']} 项仍在处理，不可按用户指定顺序提前领取")
        if row["status"] != ACTIVE and not eligible_pending(row):
            raise ValueError(f"第 {row['sequence']} 项尚未就绪或不是待处理项目")
        return
    current = next_row(queue)
    if current is None or current["id"] != row["id"]:
        suffix = f"。当前应处理第 {current['sequence']} 项" if current else ""
        raise ValueError(f"第 {row['sequence']} 项当前不可办理{suffix}")


def preserve(row):
    prior = {k: row[k] for k in ("status", "reason", "package", "course_folder", "source_sha256",
                               "content_sha256", "publication", "package_ready", "last_changed",
                               "supplemented_origin", "supplemented_sha256", "supplement_reason",
                               "user_order_reason", "user_order_at", "scope_status", "scope_gaps", "scope_resolutions",
                               "re_review", "reuse_target", "same_source_primary", "archive_evidence",
                               "alias_recheck", "requires_alias_resolution", "alias_previous_publication_sha256",
                               "disposition_evidence") if k in row}
    row.setdefault("history", []).append({**prior, "metadata_sha256": digest(row["metadata"])})


def change(row, status, **fields):
    if row["status"] == status and all(row.get(key) == value for key, value in fields.items()):
        return
    if row.get("last_changed"):
        preserve(row)
    row.update(status=status, last_changed=timestamp(), **fields)


def close_re_review(row, outcome):
    if row.get("re_review", {}).get("state") in ("待复审", "后续补件待复审"):
        return {"re_review": {**row["re_review"], "state": outcome, "reviewed_at": timestamp()}}
    return {}


def synchronize_aliases(queue, row):
    """A confirmed same-source dependent shares its primary edition, never writes a second note."""
    for alias in queue["projects"]:
        if alias.get("same_source_primary") != row["sequence"]:
            continue
        if alias.get("requires_alias_resolution"):
            continue
        if row["status"] not in PUBLISHED:
            if alias.get("re_review", {}).get("state") == "关联待复审":
                change(alias, DEFERRED, re_review={**alias["re_review"], "state": "共同待补",
                                                    "reviewed_at": timestamp()})
            continue
        try:
            _, _, identity = identify(queue, alias)
        except (OSError, ValueError) as error:
            alias["alias_recheck"] = str(error)
            alias["requires_alias_resolution"] = True
            alias["alias_previous_publication_sha256"] = (alias.get("publication") or {}).get("sha256")
            continue
        publication = row["publication"]
        review = alias.get("re_review", {})
        fields = {"re_review": {**review, "state": "同源已复审", "reviewed_at": timestamp()}} if (
            review.get("state") == "关联待复审") else {}
        alias.pop("alias_recheck", None)
        change(alias, REUSED, **identity, publication=publication,
               reuse_target={"sequence": row["sequence"], "entry": publication["entry"],
                             "sha256": publication["sha256"]},
               scope_status=publication.get("scope_status"), scope_gaps=publication.get("scope_gaps", []),
               scope_resolutions=publication.get("scope_resolutions", []),
               reason="已核同源，沿主项目既有正式同版入口复用", **fields)


def claim(queue, row, package, folder=None, separate=None, *, user_order_reason=None):
    require_turn(queue, row, user_order_reason)
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
    order = {}
    if user_order_reason is not None:
        reason = user_order_reason.strip()
        order["user_order_reason"] = reason
        order["user_order_at"] = (row.get("user_order_at", timestamp())
                                  if row["status"] == ACTIVE and row.get("user_order_reason") == reason
                                  else timestamp())
    change(row, ACTIVE, package=str(package), package_ready=ready, course_folder=folder, **identity,
           duplicates=duplicates, duplicate_decision=decision, reason=None, **order)


def supplement(queue, row, package, reason):
    require_turn(queue, row)
    reason = reason.strip()
    if row["status"] != ACTIVE or not row.get("package") or not reason:
        raise ValueError("请先领取项目，并给出同一课程补料的核对依据")
    previous_path = safe_path(row["package"])
    package = safe_path(package)
    check_target(queue, dict(row, package=None, package_ready=False), package, row["course_folder"])
    source, originals, identity = identify(queue, row)
    project = CourseProject.open(package)
    if package == previous_path:
        if row.get("supplement_reason") != reason:
            raise ValueError("补料需要另一份新入库的课程包")
        with project.locked():
            verify_identity(project, source, originals, row)
        return

    origin = safe_path(project_origin(project))
    archive = Path(queue["source"])
    vault = Path(queue["vault"]) if queue["vault"] else None
    if (origin.is_relative_to(archive) or archive.is_relative_to(origin)
            or (vault and (origin.is_relative_to(vault) or vault.is_relative_to(origin)))):
        raise ValueError("请在批次原件和日常库之外准备补料快照")
    previous = CourseProject.open(previous_path)
    with previous.locked(), project.locked():
        retained = verify_identity(previous, source, originals, row)
        combined = project.validate_raw()
        by_name = {item["file"]: item for item in combined}
        if len(combined) <= len(retained) or any(by_name.get(item["file"]) != item for item in retained):
            raise ValueError("补料包须逐件保留原快照的路径和指纹，并包含新增原件")
        if inventory_files(origin) != combined:
            raise ValueError("补料快照和新课程包的原件不一致")
        check_current_metadata(queue, row)
        change(row, ACTIVE, **identity, package=str(package), package_ready=True,
               supplemented_origin=str(origin), supplemented_sha256=source_fingerprint(combined),
               supplemented_confirmed=True, supplement_reason=reason)


def defer(queue, row, reason):
    if not reason.strip():
        raise ValueError("请登记缺少的原件或具体阻塞")
    if row["status"] != DEFERRED:
        require_turn(queue, row)
    publication = row.get("publication") or {}
    previous_scope = publication.get("scope_status")
    state = (SCOPE_STATE.get(previous_scope, COMPLETE)
             if publication.get("sha256") and current_note_matches(publication) else DEFERRED)
    change(row, state, reason=reason, **close_re_review(row, "仍待原件"))
    synchronize_aliases(queue, row)


def reopen(queue, row, accept_changes=None):
    if row.get("same_source_primary"):
        raise ValueError(f"本项目是第 {row['same_source_primary']} 项的确认同源来件，请用 alias-review 核实新变化")
    if row["status"] == ACTIVE:
        raise ValueError("项目正在处理。请接续已有课程包，或记录具体阻塞后回到队列")
    if row.get("re_review", {}).get("state") == "仍待原件" and not accept_changes:
        raise ValueError("本项已完成一次性复审，请登记新到原件、授权或版本证据后再开启")
    current = metadata(source_path(queue, row))
    if not current:
        raise ValueError("待补项目仍没有原件")
    if current != row["metadata"] and not accept_changes:
        raise ValueError("原件范围有变化。请核实新增、移除或替换的证据后登记原因")
    if row["status"] in PUBLISHED | {HISTORICAL, REUSED, ARCHIVED} and not accept_changes:
        raise ValueError("项目已有验收。如需重做，请登记调整原因")
    if accept_changes is not None:
        if not accept_changes.strip():
            raise ValueError("请登记同一项目新增、移除或更换原件的依据")
        if row["status"] != PENDING or current != row["metadata"] or row.get("reason") != accept_changes:
            preserve(row)
            if row["status"] == REUSED:
                for key in ("publication", "reuse_target", "scope_status", "scope_gaps", "scope_resolutions"):
                    row.pop(key, None)
            for key in ("source_sha256", "content_sha256", "package", "package_ready", "duplicates", "duplicate_decision",
                        "supplemented_origin", "supplemented_sha256", "supplemented_confirmed",
                        "supplement_reason"):
                row.pop(key, None)
            row.update(status=PENDING, metadata=current, reason=accept_changes, last_changed=timestamp())
            if row.get("re_review", {}).get("state") in ("仍待原件", "同源已复审"):
                row["re_review"] = {**row["re_review"], "state": "后续补件待复审", "priority_rank": None}
        return
    if row["status"] == DEFERRED:
        change(row, PENDING, reason=None)


def finish(queue, row, package):
    if row["status"] not in PUBLISHED | {ACTIVE, HISTORICAL}:
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
        legacy = (row.get("publication") or {}).get("sha256")
        publication = current_delivery(queue, project, folder, legacy)
        require_gap_disposition(row, publication)
        check_current_metadata(queue, row)
        if row.get("supplemented_origin"):
            identity["supplemented_sha256"] = source_fingerprint(snapshot)
        scope = publication.get("scope_status")
        state = SCOPE_STATE[scope] if scope is not None else COMPLETE
        scope_fields = ({"scope_status": scope, "scope_gaps": publication["scope_gaps"],
                         "scope_resolutions": publication.get("scope_resolutions", [])}
                        if scope is not None else {})
        change(row, state, **identity, package_ready=True, publication=publication, duplicates=duplicates,
               reason=None, **scope_fields, **close_re_review(row, state))
        synchronize_aliases(queue, row)


def require_gap_disposition(row, publication):
    if not publication.get("scope_status"):
        return  # Existing same-version legacy sign-off only; current_delivery enforces this.
    gaps = publication["scope_gaps"]
    resolutions = publication.get("scope_resolutions", [])
    old = row.get("scope_gaps") or (row.get("publication") or {}).get("scope_gaps", [])
    known_old_gap = bool(old or row.get("source_gap") or
                         row.get("re_review", {}).get("gaps_before_review"))
    if known_old_gap and publication["scope_status"] == "完整验收" and not resolutions:
        raise ValueError("旧项目已登记缺件，改为完整验收须有同版独立复核过的旧缺件处置证据")
    carried = {item["item"].strip().casefold() for item in [*gaps, *resolutions]}
    missing = [item["item"] for item in old if item["item"].strip().casefold() not in carried]
    if missing:
        raise ValueError("上一版缺件既未继续登记也未独立核实处置：" + "、".join(missing))


def disposition_proof(row, identity, evidence, kind):
    evidence = safe_path(evidence)
    proof = read(evidence)
    if (proof.get("schema") != 1 or proof.get("kind") != kind or proof.get("source_id") != row["id"]
            or proof.get("source_sha256") != identity["source_sha256"]
            or not isinstance(proof.get("basis"), str) or not proof["basis"].strip()):
        raise ValueError("处置证据未绑定本项目现有原件身份、结论和核查依据")
    return proof, {"path": str(evidence), "sha256": sha(evidence)}


def reuse(queue, row, related, evidence, reason, *, canonical_package=None):
    require_turn(queue, row)
    _, _, identity = identify(queue, row)
    proof, receipt = disposition_proof(row, identity, evidence, "same_source")
    if not reason.strip():
        raise ValueError("同源复用须记录具体核对依据")
    link = {}
    target_fields = {"sequence": related}
    if related is not None:
        target = row_at(queue, related)
        if target is row or target["status"] not in PUBLISHED or not target.get("publication"):
            raise ValueError("同源复用须对应独立的已有正式签收主项目")
        _, _, target_identity = identify(queue, target)
        if (proof.get("canonical_id") != target["id"]
                or proof.get("canonical_source_sha256") != target_identity["source_sha256"]):
            raise ValueError("同源证据须核到主项目当前原件及明确的复用结论")
        package = CourseProject.open(safe_path(target["package"]))
        with package.locked():
            publication = current_delivery(queue, package, target["course_folder"],
                                           (target.get("publication") or {}).get("sha256"))
        link["same_source_primary"] = related
    else:
        from vault_delivery import delivery
        package_path = safe_path(canonical_package)
        if row.get("package") and Path(row["package"]) == package_path:
            raise ValueError("同源外部主包须为独立已有签收课程")
        package = CourseProject.open(package_path)
        with package.locked():
            originals = package.validate_raw()
            state = delivery(package)
            if (not state or Path(proof.get("canonical_package", "")) != package_path
                    or proof.get("canonical_source_sha256") != source_fingerprint(originals)
                    or proof.get("canonical_publication_sha256") != state["sha256"]):
                raise ValueError("历史主包同源证据与当前原件/同版正式签收不同")
            publication = current_delivery(queue, package, state["folder"], state["sha256"])
        target_fields["package"] = str(package_path)
    change(row, REUSED, **identity, **link,
           publication=publication, scope_status=publication.get("scope_status"),
           scope_gaps=publication.get("scope_gaps", []), scope_resolutions=publication.get("scope_resolutions", []),
           reuse_target={**target_fields, "entry": publication["entry"], "sha256": publication["sha256"]},
           reason=reason.strip(), disposition_evidence=receipt, **close_re_review(row, REUSED))


def archive(queue, row, evidence, reason):
    require_turn(queue, row)
    _, _, identity = identify(queue, row)
    _, receipt = disposition_proof(row, identity, evidence, "archive")
    if not reason.strip():
        raise ValueError("请记录留档而非课程交付的具体依据")
    change(row, ARCHIVED, **identity, reason=reason.strip(), archive_evidence=receipt,
           **close_re_review(row, ARCHIVED))


def alias_review(queue, row, resolution, evidence, reason):
    primary_sequence = row.get("same_source_primary")
    if not primary_sequence or row["status"] == ACTIVE:
        raise ValueError("只有已确认同源的非在办附属项目可核实关联变化")
    primary = row_at(queue, primary_sequence)
    current = metadata(source_path(queue, row))
    if not current or not reason.strip():
        raise ValueError("同源变更复核须保留实际原件及具体依据")
    _, _, identity = identify(queue, {**row, "metadata": current, "source_sha256": None})
    changed = (current != row["metadata"] or identity["source_sha256"] != row.get("source_sha256"))
    if not changed and not row.get("requires_alias_resolution"):
        raise ValueError("附属来件未发现原件变化，仍沿既有同源主项目复用")
    proof, receipt = disposition_proof(row, identity, evidence, "alias_review")
    canonical_sha = primary.get("source_sha256")
    if not canonical_sha:
        _, _, primary_identity = identify(queue, primary)
        canonical_sha = primary_identity["source_sha256"]
    if (proof.get("resolution") != resolution or proof.get("canonical_id") != primary["id"]
            or proof.get("canonical_source_sha256") != canonical_sha):
        raise ValueError("关联复核证据未绑定既有同源主项目、当前原件和处置结论")
    preserve(row)
    fields = {"metadata": current, **identity, "disposition_evidence": receipt,
              "reason": reason.strip(), "last_changed": timestamp()}
    if resolution == "separate":
        for key in ("same_source_primary", "reuse_target", "publication", "scope_status", "scope_gaps",
                    "scope_resolutions", "package", "package_ready", "course_folder", "duplicates",
                    "duplicate_decision", "supplemented_origin", "supplemented_sha256",
                    "supplemented_confirmed", "supplement_reason"):
            row.pop(key, None)
        if row.get("re_review"):
            fields["re_review"] = {**row["re_review"], "state": "后续补件待复审", "priority_rank": None}
        row.update(status=PENDING, **fields)
        row.pop("alias_recheck", None)
        row.pop("requires_alias_resolution", None)
        row.pop("alias_previous_publication_sha256", None)
    elif resolution == "pending":
        previous_sha = ((row.get("publication") or {}).get("sha256")
                        or (primary.get("publication") or {}).get("sha256"))
        row.setdefault("alias_previous_publication_sha256", previous_sha)
        row.update(status=DEFERRED, alias_recheck=reason.strip(), requires_alias_resolution=True, **fields)
    else:
        publication = primary.get("publication") or {}
        if (primary["status"] not in PUBLISHED or proof.get("canonical_publication_sha256") != publication.get("sha256")
                or not current_note_matches(publication)):
            raise ValueError("主项目须先完成包括补件的同版签收，才能恢复附属复用")
        prior_sha = row.get("alias_previous_publication_sha256") or (row.get("publication") or {}).get("sha256")
        if (prior_sha and prior_sha == publication.get("sha256")
                and proof.get("new_material_classification") != "无新增实质教学"):
            raise ValueError("附属原件已变化而主稿仍为旧版，须补件签收或核实没有新增实质教学")
        package = CourseProject.open(safe_path(primary["package"]))
        with package.locked():
            verified = current_delivery(queue, package, primary["course_folder"], publication.get("sha256"))
        if verified["sha256"] != publication["sha256"]:
            raise ValueError("主项目新的同版签收尚未登记")
        row.update(status=REUSED, publication=verified,
                   reuse_target={"sequence": primary_sequence, "entry": verified["entry"], "sha256": verified["sha256"]},
                   scope_status=verified.get("scope_status"), scope_gaps=verified.get("scope_gaps", []),
                   scope_resolutions=verified.get("scope_resolutions", []), **fields)
        if row.get("re_review", {}).get("state") in ("关联待复审", "共同待补"):
            row["re_review"] = {**row["re_review"], "state": "同源已复审", "reviewed_at": timestamp()}
        row.pop("alias_recheck", None)
        row.pop("requires_alias_resolution", None)
        row.pop("alias_previous_publication_sha256", None)


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
    if queue.get("dispatch_registry"):
        from batch_dispatch import refresh_queue
        return refresh_queue(queue)
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
        if queue.get("dispatch_registry"):
            from batch_dispatch import candidates
            present = {path.name for path in candidates(source, queue.get("dispatch_layout"), registered)}
        else:
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


def registry_bound(queue, manifest):
    registry = queue.get("dispatch_registry")
    return (not registry or any(Path(binding["manifest"]) == manifest
                                for binding in read(Path(registry))["bindings"].values()))


def refresh_registered_catalog(queue, manifest):
    vault = Path(queue["vault"]) if queue.get("vault") else None
    if not vault or not (vault / ".obsidian").is_dir() or not registry_bound(queue, manifest):
        return
    from vault_catalog import refresh_catalog
    try:
        refresh_catalog(vault, manifests=(manifest,))
    except (OSError, ValueError) as error:
        raise ValueError(f"批次状态已登记，日常总览刷新失败：{error}") from error


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
    migrate = sub.add_parser("audit-migrate", help="按已核完整审计分开旧待补、复用、留档并一次性恢复复审")
    migrate.add_argument("manifest", type=Path)
    migrate.add_argument("audit", type=Path)
    migrate.add_argument("--priority", nargs="+", type=int, default=[],
                         help="用户明确指定的一次性待复审顺位（同源附属沿主项目）")
    migrate.add_argument("--apply", action="store_true", help="实际写批次清单；默认只读计划")
    dispatch = sub.add_parser("dispatch", help="按分类盘点原件并选择当前唯一生产项目")
    dispatch.add_argument("root", type=Path, help="分析目标下载地址总目录")
    dispatch.add_argument("--registry", type=Path, required=True, help="分类清单绑定索引")
    dispatch.add_argument("--legacy-manifest", type=Path, action="append", default=[],
                          help="接入既有正式批次清单，可多次提供")
    dispatch.add_argument("--refresh", action="store_true",
                          help="明确登记并复扫，默认仅只读盘点")
    dispatch.add_argument("--settle-seconds", type=int, default=30,
                          help="新增原件至少两次盘点相符且经过的秒数（不能证明课程齐全）")
    for name in ("claim", "defer", "reopen", "finish", "supplement", "reuse", "archive", "alias-review"):
        action = sub.add_parser(name)
        action.add_argument("manifest", type=Path)
        action.add_argument("sequence", type=int)
        if name in ("claim", "finish", "supplement"):
            action.add_argument("package", type=Path)
        if name == "claim":
            action.add_argument("--course-folder")
            action.add_argument("--separate", metavar="REASON")
            action.add_argument("--user-order-reason", metavar="DIRECTIVE",
                                help="仅在用户明确指定本项目顺序时记录其具体要求；不抢占在办项目、不跳过就绪检查")
        if name == "defer":
            action.add_argument("reason")
        if name == "supplement":
            action.add_argument("--reason", required=True, help="同一课程新增原件的核对依据")
        if name == "reopen":
            action.add_argument("--accept-changes", metavar="REASON")
        if name in ("reuse", "archive", "alias-review"):
            action.add_argument("--evidence", type=Path, required=True, help="已核原件身份及处置结论的证据 JSON")
            action.add_argument("--reason", required=True)
        if name == "reuse":
            target = action.add_mutually_exclusive_group(required=True)
            target.add_argument("--related", type=int, help="已签收的同源主项目顺位")
            target.add_argument("--canonical-package", type=Path,
                                help="本批之外真实已有签收、日常入口已核的同源课程包")
        if name == "alias-review":
            action.add_argument("--resolution", choices=("pending", "reuse", "separate"), required=True,
                                help="主项目待补、已补件同源复用，或有证据的独立新版本")


def run(args):
    try:
        action = args.batch_action
        if action == "dispatch":
            from batch_dispatch import dispatch
            result = dispatch(args.root, args.registry, args.legacy_manifest,
                              refresh=args.refresh, settle_seconds=args.settle_seconds)
        elif action == "plan":
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
            elif action == "audit-migrate":
                from batch_review import migrate
                if not args.apply:
                    result = migrate(open_queue(path), path, args.audit, args.priority)
                else:
                    queue = open_queue(path)
                    registry = queue.get("dispatch_registry")
                    # A cloned manifest can carry the original dispatch path. Do not
                    # use the real registry lock for a clone outside its binding.
                    bound = registry and registry_bound(queue, path)
                    guard = file_lock(Path(registry).with_suffix(Path(registry).suffix + ".lock")) if bound else nullcontext()
                    with guard, file_lock(path.with_suffix(path.suffix + ".lock")):
                        queue = open_queue(path)
                        result = migrate(queue, path, args.audit, args.priority, apply=True)
                        if result["applied"]:
                            put(path, queue)
                    if result["applied"]:
                        refresh_registered_catalog(queue, path)
            else:
                # Global dispatch and a claim share the same lock order.
                registry = open_queue(path).get("dispatch_registry") if action in ("claim", "reuse", "archive") else None
                guard = file_lock(Path(registry).with_suffix(Path(registry).suffix + ".lock")) if registry else nullcontext()
                with guard, file_lock(path.with_suffix(path.suffix + ".lock")):
                    queue = open_queue(path)
                    before = digest(queue)
                    if action in ("claim", "reuse", "archive") and queue.get("dispatch_registry") != registry:
                        raise ValueError("批次全局调度绑定已变，请再次领取")
                    if action == "refresh":
                        refresh(queue)
                        result = status(queue, scan=True)
                    else:
                        row = row_at(queue, args.sequence)
                        if action == "claim":
                            if registry:
                                from batch_dispatch import require_global_turn
                                require_global_turn(Path(registry), path, row, args.user_order_reason)
                            claim(queue, row, args.package, args.course_folder, args.separate,
                                  user_order_reason=args.user_order_reason)
                        elif action == "defer":
                            defer(queue, row, args.reason)
                        elif action == "reopen":
                            reopen(queue, row, args.accept_changes)
                        elif action == "supplement":
                            supplement(queue, row, args.package, args.reason)
                        elif action in ("reuse", "archive"):
                            if registry:
                                from batch_dispatch import require_global_turn
                                require_global_turn(Path(registry), path, row)
                            if action == "reuse":
                                reuse(queue, row, args.related, args.evidence, args.reason,
                                      canonical_package=args.canonical_package)
                            else:
                                archive(queue, row, args.evidence, args.reason)
                        elif action == "alias-review":
                            alias_review(queue, row, args.resolution, args.evidence, args.reason)
                        else:
                            finish(queue, row, args.package)
                        result = describe_row(queue, row)
                    changed = digest(queue) != before
                    if changed:
                        put(path, queue)
                if changed and action in ("claim", "supplement", "finish", "defer", "reuse", "archive",
                                          "reopen", "alias-review", "refresh"):
                    refresh_registered_catalog(queue, path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2
