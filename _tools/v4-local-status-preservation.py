"""Project the registered local course state for the Worldanalysis resource library."""

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sys


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent if HERE.parent.name == ".runtime" else HERE.parent
RESULTS = ROOT / "整理结果"
INBOX = ROOT / "分析目标下载地址"
REGISTRY = RESULTS / ".course-dispatch/分类索引.json"
PRODUCTION = RESULTS / "生产控制/上线状态.json"
DATE_INDEX = Path(".obsidian/worldanalysis-project-dates.json")
FORMAL_CODE = ROOT / ".runtime/course-production-worktree/_tools/course_production"
LEGACY_STATE = ROOT / "_ingest/wangzjk-to-pan/state"
SGT = timezone(timedelta(hours=8))
SHA256 = re.compile(r"[a-f0-9]{64}")
JSON_LIMIT = 32 * 1024 * 1024
NOTE_LIMIT = 4 * 1024 * 1024

ANALYSIS_STATES = {
    "待处理": "queued", "处理中": "in_progress", "待补原件": "awaiting_originals",
    "同源复用": "same_source_reuse", "留档": "archive_only", "已验收": "completed",
    "已验收（配套待补）": "completed_received_scope", "已分析（核心待补）": "completed_received_scope",
    "历史验收待复验": "awaiting_re_review",
}
ACCEPTED = {"已验收", "已验收（配套待补）", "已分析（核心待补）", "历史验收待复验"}
SCOPE_LABELS = {"完整验收", "配套待补", "核心待补"}
OWN_EDITIONS = {"registered_edition_matches", "registered_independent_derivative_matches"}
STORAGE_STATES = {
    "指定原件副本在位": ("candidate_present", "present_identity_matches", "not_applicable"),
    "指定原件副本身份待核": ("candidate_identity_unverified", "unverified", "unknown"),
    "指定原件副本缺失待核": ("candidate_missing_unverified", "absent_unexplained", "unknown"),
    "指定原件副本清理待对账": ("cleanup_pending_reconciliation", "unverified", "unknown"),
    "指定原件副本已主动清理，待删后重开": ("removed", "removed_verified", "awaiting_reopen"),
    "指定原件副本已主动清理": ("removed", "removed_verified", "verified"),
}


def obj(value):
    return value if isinstance(value, dict) else {}


def digest(value):
    return value if isinstance(value, str) and SHA256.fullmatch(value) else None


def label(value, limit=200):
    return " ".join(value.split())[:limit] if isinstance(value, str) else None


def atomic_read(path, limit):
    """Read a bounded snapshot and retry once if a producer replaced it."""
    for _ in range(2):
        before = path.stat()
        if before.st_size > limit:
            raise ValueError("input exceeds projection limit")
        with path.open("rb") as source:
            content = source.read(limit + 1)
        after = path.stat()
        if (before.st_mtime_ns, before.st_size, before.st_ino) == (
            after.st_mtime_ns, after.st_size, after.st_ino
        ) and len(content) == before.st_size:
            return content
    raise ValueError("input changed during projection")


def json_snapshot(path):
    content = atomic_read(path, JSON_LIMIT)
    document = json.loads(content.decode("utf-8-sig"))
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise ValueError("unsupported local record schema")
    return document, {"path": path.as_posix(), "sha256": hashlib.sha256(content).hexdigest()}


def safe_path(value, root):
    if not isinstance(value, (str, Path)):
        return None
    candidate = Path(value)
    try:
        if candidate.is_absolute() and candidate.is_relative_to(root) and candidate.resolve() == candidate:
            return candidate
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def named_child(root, name):
    if not isinstance(name, str) or not name or Path(name).name != name or name in (".", ".."):
        return None
    return safe_path(root / name, root)


def file_evidence(value, root=ROOT):
    path = safe_path(value, root)
    return path.as_posix() if path and path.is_file() else None


def matching_note(path, vault):
    note = safe_path(path, vault)
    if note is None:
        return None
    parts = note.relative_to(vault).parts
    if len(parts) == 3 and parts[1] == "课件" and parts[2] == f"{parts[0]}.md":
        return note
    return None


@lru_cache(maxsize=512)
def checked_hash(path, limit=NOTE_LIMIT):
    try:
        return hashlib.sha256(atomic_read(path, limit)).hexdigest()
    except (OSError, ValueError):
        return None


def note_state(row, vault, unique_folder):
    publication = obj(row.get("publication"))
    registered_entry = matching_note(publication.get("entry"), vault)
    reuse = row["status"] == "同源复用"
    folder = named_child(vault, row.get("course_folder")) if unique_folder else None
    own_note = matching_note(folder / "课件" / f"{folder.name}.md", vault) if folder else None
    entry = registered_entry if reuse else own_note
    if not entry or not entry.is_file():
        return {"state": "missing" if publication else "unregistered", "entry": None,
                "relation": "same_source_reuse" if reuse else "own", "current_sha256": None}
    observed = checked_hash(entry)
    expected = digest(publication.get("sha256")) if entry == registered_entry else None
    state = ("registered_edition_matches" if expected and observed == expected else
             "registered_edition_mismatch" if expected and observed else "edition_unverified")
    return {"state": state, "entry": entry.as_posix(), "relation": "same_source_reuse" if reuse else "own",
            "current_sha256": observed, "registered_sha256": expected}


@lru_cache(maxsize=1)
def formal_edition_reader():
    folder = safe_path(FORMAL_CODE, ROOT)
    if not folder or not all(file_evidence(folder / name, folder) for name in (
        "project.py", "preservation.py", "preservation_delivery.py", "vault_dates.py",
    )):
        return None
    saved_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(folder))
    try:
        import preservation_delivery
        import vault_dates

        if (Path(preservation_delivery.__file__) != folder / "preservation_delivery.py"
                or Path(vault_dates.__file__) != folder / "vault_dates.py"):
            return None
        return vault_dates.same_edition
    except (ImportError, OSError, SyntaxError):
        return None
    finally:
        sys.path.pop(0)
        sys.dont_write_bytecode = saved_bytecode


def preservation_state(row, textbook, vault, package):
    """Read a signed derivative only for the current, owned, registered note."""
    textbook["preservation"] = {"state": "unknown", "evidence": None}
    cleanup = {"state": "unknown", "physical_state": "unknown", "post_cleanup_reader_state": "unknown",
               "evidence": None}
    if (row["status"] not in ACCEPTED or not package or textbook["relation"] != "own"
            or textbook["state"] != "registered_edition_mismatch"):
        return cleanup
    parent = textbook["registered_sha256"]
    child = textbook["current_sha256"]
    entry = matching_note(textbook["entry"], vault)
    directory = safe_path(package / ".course/ledger/preservation" / child, package)
    install = file_evidence(directory / "install.json", directory) if directory else None
    if not entry or not install or not (same_edition := formal_edition_reader()):
        return cleanup
    relative = entry.relative_to(vault).as_posix()
    try:
        edition = obj(same_edition(package, vault, {relative: child}))
        preservation = obj(edition.get("preservation"))
        if ((edition.get("entry"), edition.get("parent_sha256"), edition.get("sha256")) != (relative, parent, child)
                or (preservation.get("parent_body_sha256"), preservation.get("derived_body_sha256")) != (parent, child)):
            return cleanup
        status = preservation.get("status")
        storage = STORAGE_STATES.get(preservation.get("source_storage"))
        if not storage or status not in ("独立成品已重开", "独立成品已接入，待原入口重开"):
            return cleanup
        record = obj(json.loads(atomic_read(Path(install), NOTE_LIMIT).decode("utf-8-sig")))
        proof_paths = {key: file_evidence(directory / name, directory) for key, name in (
            ("manifest", "独立成品清单.json"), ("independent_review", "independent-review.json"),
            ("independent_reader", "independent-reader.json"), ("formal_reader", "formal-reader.json"),
            ("cleanup", "local-cleanup.json"), ("post_cleanup_reader", "post-cleanup-reader.json"),
        )}
        candidates = record.get("candidates")
        candidate = obj(candidates[0]) if isinstance(candidates, list) and len(candidates) == 1 else {}
        candidate_path = safe_path(candidate.get("path"), entry.parent.parent / "原始资料")
        if (not candidate_path or type(candidate.get("bytes")) is not int or candidate["bytes"] < 0
                or (record.get("parent_body_sha256"), record.get("derived_body_sha256")) != (parent, child)
                or not all(proof_paths[k] for k in ("manifest", "independent_review", "independent_reader"))
                or (status == "独立成品已重开" and not proof_paths["formal_reader"])):
            return cleanup
        state, physical, post_reader = storage
        if state == "removed" and (not proof_paths["cleanup"] or not proof_paths["formal_reader"]
                                   or (post_reader == "verified" and not proof_paths["post_cleanup_reader"])):
            return cleanup
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, StopIteration):
        return cleanup

    textbook["state"] = "registered_independent_derivative_matches"
    textbook["preservation"] = {"state": "installed_verified", "independent_review_state": "verified",
                                "formal_reader_state": "verified" if status == "独立成品已重开" else "awaiting_reopen",
                                "parent_body_sha256": parent, "derived_body_sha256": child,
                                "evidence": {"install": install, **{key: proof_paths[key] for key in (
                                    "manifest", "independent_review", "independent_reader", "formal_reader")}}}
    return {"state": state, "physical_state": physical, "post_cleanup_reader_state": post_reader,
            "scope": "one_reading_vault_copy", "candidate": {"path": candidate_path.as_posix(), "bytes": candidate["bytes"]},
            "evidence": {"receipt": proof_paths["cleanup"],
                         "post_cleanup_reader": proof_paths["post_cleanup_reader"] if post_reader == "verified" else None},
            "actual_released_bytes": None}


def valid_date(value):
    try:
        return date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def edition_dates(textbook, vault, index, package):
    dates = {"analysis": None, "production": None}
    if textbook["state"] not in OWN_EDITIONS or textbook["relation"] != "own":
        return dates
    relative = Path(textbook["entry"]).relative_to(vault).as_posix()
    editions = obj(index.get(relative))
    record = obj(editions.get(textbook["current_sha256"]))
    intake = safe_path(package / ".course/ledger/intake.json", package) if package else None
    expected_intake = digest(record.get("intake_sha256"))
    if not intake or not expected_intake or checked_hash(intake) != expected_intake:
        return dates
    for kind in dates:
        value = obj(record.get(kind))
        source = file_evidence(value.get("source"))
        day = value.get("date")
        if not source or not valid_date(day):
            continue
        if kind == "production":
            certainty = value.get("certainty")
            if certainty in ("exact", "estimated", "known_earliest"):
                dates[kind] = {"date": day, "certainty": certainty, "evidence": source}
        else:
            dates[kind] = {"date": day, "kind": label(value.get("kind")), "evidence": source}
    return dates


def review_stage(row, textbook):
    status = row["status"]
    if status not in ACCEPTED:
        return {"state": "unrecorded" if status != "同源复用" else "same_source_reuse"}
    if textbook["state"] not in OWN_EDITIONS:
        return {"state": "recorded_edition_unverified"}
    derived = textbook["state"] == "registered_independent_derivative_matches"
    basis = {"edition_basis": "verified_derivative_of_signed_parent"} if derived else {}
    if status == "历史验收待复验":
        return {"state": "historical_same_edition_awaiting_review", **basis}
    scope = row.get("scope_status")
    if scope in SCOPE_LABELS and scope == obj(row.get("publication")).get("scope_status"):
        return {"state": "scope_accepted", "scope_status": scope, **basis}
    if scope or status != "已验收":
        return {"state": "recorded_scope_unverified"}
    return {"state": "historical_same_edition", **basis}


def match_progress(status, project):
    progress = obj(status.get("next_production"))
    version = project["source_version"]
    if (status.get("category"), status.get("current_sequence"), progress.get("sequence")) != (
        project["category"], project["sequence"], project["sequence"]
    ) or project["batch_status"] != "处理中":
        return None
    for key in ("source_sha256", "content_sha256"):
        if not version[key] or version[key] != progress.get(key):
            return None
    reported_id = progress.get("id") or progress.get("local_project_id") or progress.get("project_id")
    if reported_id and reported_id != project["local_project_id"]:
        return None
    release = status.get("allow_formal_accept_deliver_finish")
    return {"local_project_id": project["local_project_id"], "category": project["category"],
            "sequence": project["sequence"], "phase": label(progress.get("phase")),
            "updated_at": label(progress.get("updated_at"), 50),
            "candidate_sha256": digest(progress.get("candidate_sha256")),
            "candidate_scope_status": (progress["scope_status"] if progress.get("scope_status") in SCOPE_LABELS else None),
            "formal_release_allowed": release if isinstance(release, bool) else None,
            "evidence": {"production_status": PRODUCTION.as_posix(),
                         "candidate_manifest": file_evidence(progress.get("candidate_manifest")),
                         "independent_review_request": file_evidence(status.get("current_fact_review_request"))}}


def category_projects(category, binding):
    binding = obj(binding)
    manifest = safe_path(binding.get("manifest"), RESULTS)
    source = safe_path(binding.get("source"), INBOX)
    vault = safe_path(binding.get("vault"), INBOX)
    if category != binding.get("category") or not manifest or not source or not vault:
        raise ValueError("category binding is invalid")
    queue, manifest_evidence = json_snapshot(manifest)
    if (Path(queue["source"]), Path(queue["vault"]), Path(queue["dispatch_registry"])) != (source, vault, REGISTRY):
        raise ValueError("manifest is not bound to the registered category")
    rows = queue["projects"]
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("unsupported project count")
    if any(not digest(row.get("id")) or type(row.get("sequence")) is not int or row["sequence"] < 1
           or not isinstance(row.get("status"), str) for row in rows):
        raise ValueError("project identity or status is invalid")
    if len({row["id"] for row in rows}) != len(rows) or len({row["sequence"] for row in rows}) != len(rows):
        raise ValueError("duplicate local project identity")
    date_evidence, date_state = None, "missing"
    date_path = safe_path(vault / DATE_INDEX, vault)
    date_index = {}
    if date_path and date_path.is_file():
        try:
            values, date_evidence = json_snapshot(date_path)
            date_index = obj(values.get("entries"))
            date_state = "read" if isinstance(values.get("entries"), dict) else "unavailable"
        except (OSError, ValueError):
            date_state = "unavailable"
    folders = Counter(row.get("course_folder") for row in rows)
    projects = []
    for row in rows:
        status = row["status"]
        textbook = note_state(row, vault, folders[row.get("course_folder")] == 1)
        package = safe_path(row.get("package"), RESULTS)
        cleanup = preservation_state(row, textbook, vault, package)
        original = named_child(source, row.get("name"))
        present = original.is_dir() if original else None
        files = row.get("metadata")
        gaps = row.get("scope_gaps")
        projects.append({
            "local_project_id": row["id"], "category": category, "sequence": row["sequence"],
            "title": label(row.get("course_folder")) or label(row.get("name")),
            "batch_status": label(status, 60),
            "source_version": {key: digest(row.get(key)) for key in ("source_sha256", "content_sha256")},
            "download": {"directory": original.as_posix() if present else None, "directory_present": present,
                         "registered_file_count": len(files) if isinstance(files, list) else None,
                         "download_complete": None},
            "analysis": {"state": ANALYSIS_STATES.get(status, "unknown"),
                         "re_review_status": label(obj(row.get("re_review")).get("state"), 60),
                         "scope_status": row["scope_status"] if row.get("scope_status") in SCOPE_LABELS else None,
                         "scope_gap_count": len(gaps) if isinstance(gaps, list) else None},
            "acceptance": review_stage(row, textbook), "textbook": textbook,
            "dates": edition_dates(textbook, vault, date_index, package),
            "cleanup": cleanup,
            "evidence": {"manifest": manifest.as_posix()},
        })
    return projects, {"category": category, "manifest": manifest_evidence, "vault": vault.as_posix(),
                      "source_root": source.as_posix(), "date_index_status": date_state,
                      "date_index": date_evidence, "registered_project_count": len(projects)}


def legacy_links(projects):
    identities = {}
    for project in projects:
        source_hash = project["source_version"]["source_sha256"]
        if source_hash:
            identities.setdefault((project["sequence"], source_hash), []).append(project["local_project_id"])

    @lru_cache(maxsize=32)
    def read_proof(relative_path, expected_sha):
        path = safe_path(ROOT / relative_path, LEGACY_STATE / "evidence")
        if path is None or not digest(expected_sha):
            return {}
        raw = atomic_read(path, JSON_LIMIT)
        if hashlib.sha256(raw).hexdigest() != expected_sha:
            return {}
        records = obj(json.loads(raw.decode("utf-8-sig"))).get("records", [])
        facts = {}
        for entry in records if isinstance(records, list) else []:
            if obj(entry).get("matched") is not True:
                continue
            proof = obj(entry.get("p"))
            articles = proof.get("articles")
            sequence = proof.get("local_sequence")
            source_hash = digest(proof.get("source_sha256"))
            if isinstance(articles, list) and type(sequence) is int and sequence > 0 and source_hash:
                for article in articles:
                    if isinstance(article, str) and isinstance(proof.get("root_id"), str):
                        key = (article, proof["root_id"])
                        value = (sequence, source_hash)
                        facts[key] = value if key not in facts or facts[key] == value else None
        return facts

    ledger = safe_path(LEGACY_STATE / "downloaded-project-markers.json", LEGACY_STATE)
    if ledger is None:
        return []
    raw = obj(json.loads(atomic_read(ledger, JSON_LIMIT).decode("utf-8-sig")))
    entries = raw.get("entries", [])
    links = {}
    for entry in entries if isinstance(entries, list) else []:
        marker = obj(entry)
        ref = obj(marker.get("download_completion_evidence_ref"))
        key = (marker.get("article_id"), marker.get("cloud_root_id"))
        if not all(isinstance(x, str) and x.isdecimal() for x in key) or not isinstance(ref.get("path"), str):
            continue
        try:
            proof = read_proof(ref["path"], ref.get("sha256")).get(key)
        except (OSError, ValueError, TypeError):
            continue
        local_ids = identities.get(proof, [])
        if len(local_ids) == 1:
            links[key] = {"site_id": "wangzjk.com", "article_id": key[0], "cloud_root_id": key[1],
                          "local_project_id": local_ids[0], "basis": "historical_exact_root_and_source_sha256",
                          "evidence": {"path": ref["path"], "sha256": ref["sha256"]}}
    return list(links.values())


def projection(project_id=None, with_legacy_links=False):
    checked_hash.cache_clear()
    out = {"schema_version": 1, "observed_at": datetime.now(SGT).isoformat(timespec="seconds"),
           "projection_status": "available", "evidence": {"registry": None, "production_status": None},
           "categories": [], "counts": {}, "current_production": None, "current_production_status": "unavailable",
           "projects": [], "issues": []}
    try:
        registry_path = safe_path(REGISTRY, RESULTS)
        if registry_path is None:
            raise ValueError("registry path is outside the production root")
        registry, out["evidence"]["registry"] = json_snapshot(registry_path)
        if Path(registry["root"]) != INBOX or not isinstance(registry.get("bindings"), dict):
            raise ValueError("registry is outside the production inbox")
    except (OSError, ValueError, KeyError, TypeError):
        out["projection_status"] = "unavailable"
        out["issues"].append({"code": "registry_unavailable", "path": REGISTRY.as_posix()})
        return out

    for category, binding in sorted(registry["bindings"].items()):
        try:
            projects, evidence = category_projects(category, binding)
            out["projects"].extend(projects)
            out["categories"].append(evidence)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            out["projection_status"] = "partial"
            out["issues"].append({"code": "category_manifest_unavailable", "category": label(category)})
    ids = Counter(row["local_project_id"] for row in out["projects"])
    if any(count > 1 for count in ids.values()):
        out["projection_status"] = "partial"
        out["issues"].append({"code": "duplicate_local_project_id"})
        out["projects"] = [row for row in out["projects"] if ids[row["local_project_id"]] == 1]

    if with_legacy_links:
        try:
            out["legacy_links"] = [link for link in legacy_links(out["projects"])
                                   if project_id is None or link["local_project_id"] == project_id]
        except (OSError, ValueError, TypeError):
            out["issues"].append({"code": "legacy_identity_evidence_unavailable"})
            out["legacy_links"] = []

    try:
        status_path = safe_path(PRODUCTION, RESULTS)
        if status_path is None:
            raise ValueError("production detail path is outside the production root")
        status, out["evidence"]["production_status"] = json_snapshot(status_path)
        current = [detail for row in out["projects"] if (detail := match_progress(status, row))]
        if len(current) == 1:
            out["current_production"] = current[0]
            out["current_production_status"] = "identity_bound"
        else:
            out["current_production_status"] = "unbound" if status.get("current_sequence") else "none_recorded"
    except (OSError, ValueError, TypeError):
        out["issues"].append({"code": "production_detail_unavailable", "path": PRODUCTION.as_posix()})

    out["counts"] = {"registered_projects": len(out["projects"]),
                     "batch_status": dict(Counter(row["batch_status"] for row in out["projects"])),
                     "own_textbook_entries": sum(1 for row in out["projects"] if row["textbook"]["entry"]
                                                 and row["textbook"]["relation"] == "own")}
    if project_id is not None:
        out["projects"] = [row for row in out["projects"] if row["local_project_id"] == project_id]
        out["requested_local_project_id"] = project_id
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", help="filter the project list by the exact registered local project ID")
    parser.add_argument("--with-legacy-links", action="store_true", help="include exact historical root-to-project identity links")
    args = parser.parse_args()
    if args.project_id is not None and not digest(args.project_id):
        parser.error("--project-id requires an exact lowercase SHA-256 identity")
    output = projection(args.project_id, args.with_legacy_links)
    sys.stdout.buffer.write((json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    return 1 if output["projection_status"] == "unavailable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
