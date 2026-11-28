"""Inventory historical intake and reader evidence without changing production state."""

from argparse import ArgumentParser
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from os import walk
from pathlib import Path
import re
from stat import FILE_ATTRIBUTE_REPARSE_POINT

from batch import content_fingerprint, source_fingerprint
from library_quality import WORKSPACE, private_output, registered_batches
from project import file_lock, local_child, named_entry, put, read
from vault_dates import HISTORICAL_STATUS, current_scope, date_at, same_edition


INPUT = Path("分析目标下载地址")
MARKER = Path(".course/project.json")
INTAKE = Path(".course/ledger/intake.json")
DELIVERY = Path(".course/ledger/obsidian-delivery.json")
LEGACY_VAULT = Path("_deliveries/Obsidian")
HISTORICAL_BINDINGS = Path("整理结果/.course-dispatch/历史教材关联.json")
HISTORICAL_KIND = "worldanalysis_historical_textbook_bindings"
HISTORICAL_STATE = "historical_same_edition_readback"
EXCLUDED_TREE_NAMES = {
    ".git", ".obsidian", "__pycache__", "node_modules", "site-packages", ".venv",
    "cache", "caches", "npm-cache", ".npm", ".pnpm-store", "browser-data", "electron-user-data",
}
INFRASTRUCTURE_ROOTS = {
    "_tools", "_ledger", "_exchange", "docs", "tests", "worldanalysis", "worldanalysis_agent_devpack_20261110",
    ".agents", ".codex", ".cursor", ".dsh-local-patches", ".git", "NVIDIA Corporation",
}
ASSET_SUFFIXES = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".md", ".txt", ".png", ".jpg",
    ".jpeg", ".webp", ".mp4", ".m4v", ".mov", ".mkv", ".mp3", ".wav", ".zip", ".rar", ".7z", ".url",
}
SUBSTANTIVE_SUFFIXES = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".mp4", ".m4v", ".mov",
    ".mkv", ".mp3", ".wav", ".zip", ".rar", ".7z",
}
HEX = re.compile(r"[a-f0-9]{64}")


def relative(workspace, path):
    return path.relative_to(workspace).as_posix()


def is_link(path):
    state = path.lstat()
    return path.is_symlink() or bool(getattr(state, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def scoped_path(workspace, raw):
    candidate = Path(raw)
    if not candidate.is_absolute() or not candidate.is_relative_to(workspace):
        return None
    try:
        return local_child(workspace, candidate.relative_to(workspace))
    except (OSError, ValueError):
        return None


def marker_json(folder, marker):
    path = local_child(folder, marker)
    return read(path) if path.is_file() and path.stat().st_size <= 16_000_000 else None


def operational_role(rel):
    parts = rel.parts
    if parts[0] in INFRASTRUCTURE_ROOTS:
        return "infrastructure"
    if parts[0] in ("_qa", "_temp", ".runtime") or any(
        part in ("测试分析稿件", "正在分析") for part in parts
    ):
        return "work_or_verification"
    if parts[0] == "_deliveries":
        return "historical_export"
    if parts[0] in ("采集输入", "下载暂存", "_extracted", "_ingest", "窗口派生"):
        return "acquisition_or_derivative"
    if parts[0] == "wangzjk.com":
        return "legacy_project_archive"
    return "project_storage"


def filtered_walk(base, *, prune=None, errors=None, excluded=None):
    def on_error(error):
        if errors is not None:
            errors.append({"path": str(error.filename or base), "issue": "unreadable_directory"})

    for name, dirs, files in walk(base, followlinks=False, onerror=on_error):
        folder = Path(name)
        accepted = []
        for child in sorted(dirs):
            path = folder / child
            try:
                blocked = child.casefold() in EXCLUDED_TREE_NAMES or is_link(path)
            except OSError as error:
                on_error(error)
                continue
            if blocked or (prune is not None and prune(folder, child)):
                if excluded is not None:
                    excluded.append(path)
                continue
            accepted.append(child)
        dirs[:] = accepted
        yield folder, dirs, sorted(files)


def file_inventory(path, errors):
    matches = Counter()
    byte_total = 0
    seen = 0
    substantive = []
    for parent, _, files in filtered_walk(path, errors=errors):
        for name in files:
            item = parent / name
            try:
                if is_link(item) or not item.is_file():
                    continue
                size = item.stat().st_size
            except OSError:
                errors.append({"path": str(item), "issue": "unreadable_file_metadata"})
                continue
            seen += 1
            byte_total += size
            suffix = item.suffix.casefold()
            if suffix in ASSET_SUFFIXES:
                matches[suffix] += 1
            if suffix in SUBSTANTIVE_SUFFIXES and size:
                substantive.append({"file": item.relative_to(path).as_posix(), "bytes": size})
    return {"files": seen, "bytes": byte_total, "candidate_extensions": dict(sorted(matches.items())),
            "substantive_file_candidates": substantive}


def catalog_binding(workspace):
    rows = []
    manifests, sources, vaults = set(), set(), set()
    for category, manifest, queue, source, vault in registered_batches(workspace):
        manifests.add(manifest)
        sources.add(source)
        vaults.add(vault)
        rows.extend((category, source, row) for row in queue["projects"])
    return rows, manifests, sources, vaults


def published_paths(workspace, rows):
    result = set()
    for _, _, row in rows:
        for edition in [row, *row.get("history", [])]:
            publication = edition.get("publication") or {}
            if publication.get("entry"):
                path = scoped_path(workspace, publication["entry"])
                if path:
                    result.add(path)
    return result


def intake_sources(folder):
    intake = marker_json(folder, INTAKE)
    sources = intake.get("sources") if isinstance(intake, dict) else None
    if isinstance(sources, list) and sources and all(
        isinstance(row, dict) and isinstance(row.get("file"), str)
        and type(row.get("bytes")) is int and row["bytes"] >= 0
        and isinstance(row.get("sha256"), str) and HEX.fullmatch(row["sha256"])
        for row in sources
    ):
        return sources
    return None


def package_record(workspace, folder, registered, by_source, by_digest, errors):
    result = {"path": relative(workspace, folder), "role": operational_role(folder.relative_to(workspace))}
    try:
        marker = marker_json(folder, MARKER)
        if not isinstance(marker, dict) or marker.get("schema") != 1 or not isinstance(marker.get("origin"), str):
            raise ValueError("project marker is invalid")
        origin = scoped_path(workspace, marker["origin"])
        result["origin"] = relative(workspace, origin) if origin else None
        result["origin_inside_project"] = origin is not None
        result["origin_present"] = origin.is_dir() if origin else None
        bound = registered.get(folder, set())
        origins = by_source.get(origin, set())
        result["registered_package_ids"] = sorted(bound)
        result["registered_origin_ids"] = sorted(origins)
        sources = intake_sources(folder)
        if sources:
            source_hash = source_fingerprint(sources)
            content_hash = content_fingerprint(sources)
            result["intake"] = {"files": len(sources), "source_sha256": source_hash, "content_sha256": content_hash}
            result["matching_registered_intake_ids"] = sorted(by_digest.get(content_hash, ()))
        delivery = marker_json(folder, DELIVERY)
        if isinstance(delivery, dict) and isinstance(delivery.get("entry"), str):
            destination = scoped_path(workspace, delivery["entry"])
            if destination is not None:
                result["delivery_entry"] = relative(workspace, destination)
        result["state"] = ("registered_package" if bound else "registered_origin" if origins
                           else "same_recorded_intake" if result.get("matching_registered_intake_ids")
                           else "unbound_package")
    except (OSError, ValueError, KeyError, TypeError):
        result["state"] = "unreadable_package"
        errors.append({"path": str(folder), "issue": "invalid_or_unreadable_project_metadata"})
    return result


def reader_gaps(workspace, vaults, published, deliveries):
    results = []
    for vault in sorted(vaults):
        for folder in sorted(vault.iterdir()):
            if is_link(folder) or not folder.is_dir() or folder.name.startswith("."):
                continue
            entry = named_entry(folder)
            if entry in published or not entry.is_file():
                continue
            result = {"entry": relative(workspace, entry), "title": folder.name, "state": "reader_identity_unverified"}
            receipts = []
            for package in deliveries.get(entry, []):
                edition = same_edition(package, vault)
                if edition:
                    row = marker_json(package, DELIVERY)
                    receipts.append({"package": relative(workspace, package),
                                     "same_edition_sha256": edition["sha256"],
                                     "intake_sha256": edition["intake_sha256"],
                                     "publication_receipt": relative(workspace, edition["publication_path"]),
                                     "reader_receipt": relative(workspace, edition["delivery_path"]),
                                     "historical_readback_date": date_at(row.get("installed_at"))})
            if len(receipts) == 1:
                result["state"] = HISTORICAL_STATE
            elif receipts:
                result["state"] = "multiple_historical_package_bindings"
            result["receipts"] = receipts
            raw = folder / "原始资料"
            if raw.is_dir() and not is_link(raw):
                result["retained_source"] = relative(workspace, raw)
            results.append(result)
    return results


def inventory(workspace):
    rows, manifests, registered_sources, vaults = catalog_binding(workspace)
    by_package, by_source, by_digest = defaultdict(set), defaultdict(set), defaultdict(set)
    published = published_paths(workspace, rows)
    for _, source, row in rows:
        by_source[local_child(source, row["name"])].add(row["id"])
        for edition in [row, *row.get("history", [])]:
            package = scoped_path(workspace, edition["package"]) if edition.get("package") else None
            if package:
                by_package[package].add(row["id"])
        if row.get("content_sha256"):
            by_digest[row["content_sha256"]].add(row["id"])

    roots = []
    packages, errors, excluded = [], [], []
    deliveries = defaultdict(list)
    other_manifests, old_archive = [], []
    for root in sorted(workspace.iterdir()):
        if not root.is_dir() or is_link(root):
            continue
        role = operational_role(root.relative_to(workspace))
        root_result = {"path": relative(workspace, root), "role": role,
                       "direct_children": len(list(root.iterdir()))}
        roots.append(root_result)
        if role == "infrastructure":
            continue

        def prune(folder, child):
            if folder in (workspace / INPUT, workspace / LEGACY_VAULT) and child.startswith("."):
                return True
            return folder / child in registered_sources or folder / child in vaults or child == "原始资料"

        directory_count = 0
        for folder, dirs, files in filtered_walk(root, prune=prune, errors=errors, excluded=excluded):
            directory_count += 1
            if "批次状态.json" in files:
                path = folder / "批次状态.json"
                if path not in manifests:
                    other_manifests.append({"path": relative(workspace, path),
                                            "role": operational_role(path.relative_to(workspace))})
            if ".course" in dirs:
                dirs.remove(".course")
                record = package_record(workspace, folder, by_package, by_source, by_digest, errors)
                packages.append(record)
                if record.get("delivery_entry"):
                    deliveries[workspace / record["delivery_entry"]].append(folder)
        root_result["scanned_directories"] = directory_count

    registered_directories = sorted(
        (source / row["name"] for _, source, row in rows), key=lambda path: path.as_posix()
    )
    extra_inputs = []
    for source in sorted(registered_sources):
        extra_inputs.extend(relative(workspace, child) for child in sorted(source.iterdir())
                            if child.is_dir() and not is_link(child) and child not in by_source)
    unregistered_categories = [
        relative(workspace, folder) for folder in sorted((workspace / INPUT).iterdir())
        if folder.is_dir() and not is_link(folder) and not folder.name.startswith(".")
        and all(not source.is_relative_to(folder) for source in registered_sources)
    ]
    archive = workspace / "wangzjk.com"
    if archive.is_dir() and not is_link(archive):
        for directory in sorted(archive.iterdir()):
            if not directory.is_dir() or is_link(directory):
                continue
            facts = file_inventory(directory, errors)
            old_archive.append({"path": relative(workspace, directory), "metadata": facts,
                                "state": "needs_version_identity" if facts["substantive_file_candidates"]
                                else "other_materials_need_classification"})

    reader = reader_gaps(workspace, vaults, published, deliveries)
    external = []
    for item in (Path("下载暂存"), Path("采集输入/platform-materials"), LEGACY_VAULT,
                 Path("_ingest/platform-native-20261116"), Path("_ingest/attached-nonvideo-20261116")):
        area = workspace / item
        if area.is_dir() and not is_link(area):
            external.append({"path": item.as_posix(),
                             "directories": sorted(child.name for child in area.iterdir()
                                                   if child.is_dir() and not is_link(child)),
                             "role": operational_role(item)})
    return {
        "schema": 1,
        "kind": "worldanalysis_historical_inventory",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "workspace": workspace.as_posix(),
            "evidence": "directory metadata, existing batch/intake/publication/reader receipts, and current reader-note bytes",
            "operations": "discovery only; original media contents were not read and no production data was modified",
            "unfollowed_paths": sorted(relative(workspace, path) for path in excluded),
            "known_intake_and_vault_trees_checked_separately": True,
        },
        "counts": {
            "registered_projects": len(rows),
            "registered_source_directories_present": sum(p.is_dir() and not is_link(p) for p in registered_directories),
            "source_categories_outside_registry": len(unregistered_categories),
            "unregistered_within_bound_sources": len(extra_inputs),
            "course_package_markers": len(packages),
            "package_classification": dict(sorted(Counter(row["state"] for row in packages).items())),
            "package_areas": dict(sorted(Counter(row["role"] for row in packages).items())),
            "vault_notes_not_bound_to_a_batch": len(reader),
            "historical_note_receipts_matched": sum(row["state"] == HISTORICAL_STATE for row in reader),
            "legacy_project_directories": len(old_archive),
            "legacy_directories_with_substantive_file_candidates": sum(
                bool(row["metadata"]["substantive_file_candidates"]) for row in old_archive
            ),
            "read_errors": len(errors),
        },
        "registered_statuses": dict(sorted(Counter(row["status"] for _, _, row in rows).items())),
        "root_areas": roots,
        "unregistered_categories": unregistered_categories,
        "unregistered_source_directories": extra_inputs,
        "historical_batch_paths": sorted(other_manifests, key=lambda row: row["path"]),
        "course_package_observations": sorted(packages, key=lambda row: row["path"]),
        "reader_registration_gaps": reader,
        "legacy_archive": old_archive,
        "historical_or_work_containers": external,
        "read_errors": errors,
    }


def exact_historical_binding(workspace, gap, categories, *, require_source=True):
    entry = local_child(workspace, gap["entry"])
    project = entry.parent.parent
    vault = project.parent
    if vault not in categories or named_entry(project) != entry or project.name != gap["title"]:
        raise ValueError(f"教材入口不属于登记的日常库 {entry}")

    receipt, = gap["receipts"]
    package_path = Path(receipt["package"])
    if not package_path.is_relative_to("整理结果") or package_path == Path("整理结果"):
        raise ValueError(f"历史教材制作包不属于项目整理目录 {entry}")
    package = local_child(workspace, package_path)
    source = local_child(project, "原始资料")
    marker = marker_json(package, MARKER)
    if (relative(workspace, source) != gap.get("retained_source") or (require_source and not source.is_dir())
            or not isinstance(marker, dict) or marker.get("schema") != 1
            or marker.get("origin") is None or scoped_path(workspace, marker["origin"]) != source):
        raise ValueError(f"历史原件目录与制作记录对应不上 {entry}")

    sources = intake_sources(package)
    edition = same_edition(package, vault)
    if not sources or not edition or vault / edition["entry"] != entry:
        raise ValueError(f"历史收件或同版记录已变动 {entry}")
    scope = current_scope(package, vault, {edition["entry"]: edition["sha256"]})
    if scope is None or scope.get("status") != HISTORICAL_STATUS:
        raise ValueError(f"当前教材不属于历史已发布范围 {entry}")
    delivery = marker_json(package, DELIVERY)
    readback_day = date_at(delivery.get("installed_at")) if isinstance(delivery, dict) else None
    proof = {
        "same_edition_sha256": edition["sha256"],
        "intake_sha256": edition["intake_sha256"],
        "publication_receipt": relative(workspace, edition["publication_path"]),
        "reader_receipt": relative(workspace, edition["delivery_path"]),
        "historical_readback_date": readback_day,
    }
    if not readback_day or any(receipt.get(key) != value for key, value in proof.items()):
        raise ValueError(f"盘点与同版记录已不一致 {entry}")

    source_digest = source_fingerprint(sources)
    original_edition = edition.get("parent_sha256", edition["sha256"])
    identity = sha256(f"worldanalysis-historical-textbook-v1\0{source_digest}\0{original_edition}".encode()).hexdigest()
    return {
        "id": f"historical_{identity}",
        "title": project.name,
        "category": categories[vault],
        "entry": relative(workspace, entry),
        "vault": relative(workspace, vault),
        "package": relative(workspace, package),
        "source": relative(workspace, source),
        "edition_sha256": proof.pop("same_edition_sha256"),
        "original_edition_sha256": original_edition,
        "source_sha256": source_digest,
        **proof,
        "historical_readback_timestamp": delivery["installed_at"],
        "first_seen_date": None,
        "analysis_completed_at": None,
        "production_date": None,
        "cleanup_eligible": None,
        "status": HISTORICAL_STATE,
    }


def vault_categories(workspace):
    rows, categories = [], {}
    for category, _, batch, source, vault in registered_batches(workspace):
        categories[vault] = category
        rows.extend((category, source, row) for row in batch["projects"])
    return categories, published_paths(workspace, rows)


def store_historical_bindings(workspace, report):
    if (report.get("schema") != 1 or report.get("kind") != "worldanalysis_historical_inventory"
            or Path(report["scope"]["workspace"]) != workspace or not date_at(report.get("observed_at"))):
        raise ValueError("盘点证据与当前项目不符")
    target = local_child(workspace, HISTORICAL_BINDINGS)
    lock = local_child(workspace, HISTORICAL_BINDINGS.with_suffix(".lock"))
    with file_lock(lock, blocking=True):
        existing = read(target) if target.is_file() else {"schema": 1, "kind": HISTORICAL_KIND, "entries": []}
        if existing.get("schema") != 1 or existing.get("kind") != HISTORICAL_KIND:
            raise ValueError("历史教材关联格式不一致")
        entries = {row["id"]: row for row in existing["entries"]}
        prior_by_path = {row["entry"]: row["id"] for row in existing["entries"]}
        if len(entries) != len(existing["entries"]) or len(prior_by_path) != len(entries):
            raise ValueError("已有历史教材关联存在冲突")
        categories, published = vault_categories(workspace)
        verified, rejected = set(), []
        for gap in report["reader_registration_gaps"]:
            if gap["state"] != HISTORICAL_STATE:
                continue
            try:
                if local_child(workspace, gap["entry"]) in published:
                    continue
                binding = exact_historical_binding(workspace, gap, categories)
                current = entries.get(binding["id"])
                prior_id = prior_by_path.get(binding["entry"])
                if (prior_id and prior_id != binding["id"]
                        or current and current["entry"] != binding["entry"]
                        and local_child(workspace, current["entry"]).exists()):
                    raise ValueError("历史教材身份对应多个在位入口")
            except (OSError, ValueError, TypeError, KeyError) as error:
                rejected.append({"entry": gap.get("entry"), "issue": str(error)})
                continue
            entries[binding["id"]] = binding
            prior_by_path[binding["entry"]] = binding["id"]
            verified.add(binding["id"])
        persisted = {**existing, "observed_at": max(existing.get("observed_at", ""), report["observed_at"]),
                     "entries": sorted(entries.values(), key=lambda row: (row["category"], row["title"], row["id"]))}
        put(target, persisted)
    return {"bound_now": len(verified), "recorded": len(entries), "rejected": rejected, "register": str(target)}


def read_historical_bindings(workspace):
    """Return only still-current, independently evidenced historical entries."""
    target = local_child(workspace, HISTORICAL_BINDINGS)
    if not target.is_file():
        return {"schema": 1, "kind": HISTORICAL_KIND, "observed_at": None, "entries": []}
    index = read(target)
    if (index.get("schema") != 1 or index.get("kind") != HISTORICAL_KIND
            or not isinstance(index.get("entries"), list) or not date_at(index.get("observed_at"))):
        raise ValueError("历史教材关联格式不一致")
    candidates = [row for row in index["entries"] if isinstance(row, dict)
                  and isinstance(row.get("id"), str) and isinstance(row.get("entry"), str)]
    ids = Counter(row["id"] for row in candidates)
    entries = Counter(Path(row["entry"]) for row in candidates)
    categories, published = vault_categories(workspace)
    verified = []
    for row in candidates:
        if (ids[row["id"]] != 1 or entries[Path(row["entry"])] != 1 or row.get("status") != HISTORICAL_STATE):
            continue
        try:
            if local_child(workspace, row["entry"]) in published:
                continue
            gap = {"entry": row["entry"], "title": row["title"], "retained_source": row["source"], "receipts": [{
                "package": row["package"],
                "same_edition_sha256": row["edition_sha256"],
                "intake_sha256": row["intake_sha256"],
                "publication_receipt": row["publication_receipt"],
                "reader_receipt": row["reader_receipt"],
                "historical_readback_date": row["historical_readback_date"],
            }]}
            binding = exact_historical_binding(workspace, gap, categories, require_source=False)
            if binding == row:
                verified.append(binding)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return {"schema": 1, "kind": HISTORICAL_KIND, "observed_at": index["observed_at"], "entries": verified}


def main(argv=None):
    parser = ArgumentParser(description="核对 Worldanalysis 全库历史库存及正式登记差额")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--output", type=Path, help="将全库盘点写入运行目录")
    actions.add_argument("--bind-historical", type=Path, help="根据已有盘点复核并登记历史教材")
    actions.add_argument("--read-historical", action="store_true", help="仅读回当前同版的历史教材关联")
    parser.add_argument("--path-version", help="使用任务启动时绑定的历史备份位置")
    args = parser.parse_args(argv)
    if args.path_version and not args.output:
        parser.error("请同时指定盘点输出文件")
    if args.read_historical:
        print(json.dumps(read_historical_bindings(WORKSPACE), allow_nan=False))
        return
    evidence = private_output(WORKSPACE, args.bind_historical or args.output, history_version=args.path_version)
    if evidence.suffix.lower() != ".json":
        parser.error("盘点文件必须是 JSON")
    if args.bind_historical:
        print(store_historical_bindings(WORKSPACE, read(evidence)))
        return
    report = inventory(WORKSPACE)
    put(evidence, report)
    print(report["counts"])


if __name__ == "__main__":
    main()
