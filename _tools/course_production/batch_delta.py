"""Compare a new intake to the exact prior snapshot without changing the batch."""
import re
from pathlib import Path, PurePosixPath

import batch
from project import CourseProject, copy_file, inventory_files, local_child, put, read, sha, write


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
NATIVE_TEXT_SUFFIXES = {".txt", ".md"}


def prior_binding(row):
    versions = [row, *reversed(row.get("history", []))]
    bound = [item for item in versions if item.get("package") and item.get("source_sha256")]
    return next((item for item in bound if item.get("publication") and item["status"] in
                 batch.PUBLISHED | {batch.HISTORICAL}), bound[0] if bound else None)


def indexed(files):
    """Use the same paths and exact content identity as the formal batch."""
    result = {}
    normalized = set()
    if not isinstance(files, list) or not files:
        raise ValueError("没有可核对的原件清单")
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("原件清单条目无效")
        name, size, digest = item.get("file"), item.get("bytes"), item.get("sha256")
        if not isinstance(name, str) or not name:
            raise ValueError("原件路径无效")
        relative = PurePosixPath(name)
        if (relative.is_absolute() or relative.as_posix() != name or not relative.parts
                or any(part == ".." for part in relative.parts) or "\\" in name or ":" in name
                or name.casefold() in normalized or type(size) is not int or size < 0
                or not isinstance(digest, str) or not SHA256.fullmatch(digest)):
            raise ValueError("原件清单包含歧义路径或无效的文件摘要")
        normalized.add(name.casefold())
        result[name] = {"file": name, "bytes": size, "sha256": digest}
    return result


def prior_intake(source, binding):
    package = batch.safe_path(binding["package"])
    state = local_child(package, ".course")
    marker = read(local_child(state, "project.json"))
    origin = binding.get("supplemented_origin")
    expected = binding.get("supplemented_sha256") if origin else binding["source_sha256"]
    if marker.get("schema") != 1 or Path(marker["origin"]) != (Path(origin) if origin else source):
        raise ValueError("既有取材包与批次原件身份不一致")
    intake = local_child(state, "ledger/intake.json")
    files = list(indexed(read(intake)["sources"]).values())
    if batch.source_fingerprint(files) != expected:
        raise ValueError("既有入库清单与批次登记的原件摘要不一致")
    return state, intake, files


def inspect_candidate(directory):
    from batch_dispatch import PARTIAL_SUFFIXES

    before = batch.metadata(directory)
    if any(item["file"].casefold().endswith(PARTIAL_SUFFIXES) for item in before):
        raise ValueError("候选目录仍有下载中的文件")
    files = list(indexed(inventory_files(directory)).values())
    if batch.metadata(directory) != before or [(item["file"], item["bytes"]) for item in before] != [
        (item["file"], item["bytes"]) for item in files
    ]:
        raise ValueError("盘点期间候选目录内容发生变化，请稳定后再核对")
    return files


def previous_supplement(files, row, binding):
    """The registered source can be a proven subset of the effective course intake."""
    if not binding.get("supplemented_origin"):
        return []
    names = {item["file"] for item in row["metadata"]}
    base = [item for item in files if item["file"] in names]
    if batch.source_fingerprint(base) != binding["source_sha256"]:
        raise ValueError("无法分开上一版批次原件与外部补料，请提供完整候选目录比较")
    return [item for item in files if item["file"] not in names]


def reusable(state, intake, binding, unchanged):
    """Return hash-pinned static document evidence. Review and publishing are separate."""
    publication = binding.get("publication") or {}
    edition = publication.get("sha256")
    if (binding["status"] not in batch.PUBLISHED | {batch.HISTORICAL}
            or not isinstance(edition, str) or not SHA256.fullmatch(edition) or not unchanged):
        return []
    try:
        from hashlib import sha256
        from independent_facts import validate_independent_receipt
        from prepare_html import HTML_SUFFIXES
        from project import ENTRY

        receipt = read(local_child(state, f"ledger/publications/{edition}.json"))
        review = receipt["review"]
        proof = review["snapshot"]["evidence"]
        if (receipt["outputs"].get(ENTRY.as_posix()) != edition
                or sha256(receipt["body"].encode("utf-8")).hexdigest() != edition
                or proof["ledger/intake.json"] != sha(intake)):
            return []
        if review.get("schema") == 2:
            validate_independent_receipt(receipt["body"], review, review.get("independent_fact_review"))
        index_path = local_child(state, "ledger/documents-summary.json")
        if sha(index_path) != proof["ledger/documents-summary.json"]:
            return []
        docs = read(index_path)["records"]
        if not isinstance(docs, list):
            return []
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []
    evidence = []
    for entry in docs:
        if not isinstance(entry, dict):
            continue
        file, fingerprint = entry.get("file"), entry.get("sha256")
        path = entry.get("result")
        if (not isinstance(file, str) or file not in unchanged or unchanged[file]["sha256"] != fingerprint
                or Path(file).suffix.casefold() not in HTML_SUFFIXES | NATIVE_TEXT_SUFFIXES | {".url"}
                or entry.get("status") not in {"extracted", "verified_complete"} or not isinstance(path, str)
                or not path.startswith("资料/") or not path.endswith(".json")):
            continue
        relative = f"work/{path}"
        try:
            asset = local_child(state, relative)
            digest = proof.get(relative)
            if digest and sha(asset) == digest:
                evidence.append({"file": file, "source_sha256": fingerprint, "artifact": relative,
                                 "artifact_sha256": digest, "document_id": entry["id"]})
        except (OSError, ValueError, KeyError):
            continue
    return evidence


def compare(queue, row, candidate=None):
    source = batch.source_path(queue, row)
    binding = prior_binding(row)
    result = {"read_only": True, "project_id": row["id"], "sequence": row["sequence"],
              "already_in_progress": row["status"] == batch.ACTIVE, "baseline": None, "candidate": None}
    if binding is None:
        return {**result, "disposition": "baseline_unavailable", "reason": "没有已绑定的原件快照"}
    try:
        state, intake, files = prior_intake(source, binding)
        old = indexed(files)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        return {**result, "disposition": "baseline_unavailable", "reason": str(error)}
    result["baseline"] = {"package": str(state.parent), "source_sha256": batch.source_fingerprint(files),
                          "publication_sha256": (binding.get("publication") or {}).get("sha256"),
                          "files": len(old)}
    directory = batch.safe_path(candidate) if candidate is not None else source
    registered = directory == source
    try:
        arrival = inspect_candidate(directory)
        retained = previous_supplement(files, row, binding) if registered else []
        arrival_names = {item["file"] for item in arrival}
        effective = [*arrival, *(item for item in retained if item["file"] not in arrival_names)]
        new = indexed(effective)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {**result, "disposition": "candidate_unavailable", "reason": str(error)}
    result["candidate"] = {"directory": str(directory), "registered_source": registered,
                           "source_sha256": batch.source_fingerprint(arrival),
                           "carried_supplement_files": len(effective) - len(arrival), "files": len(arrival)}
    unchanged = {name: item for name, item in new.items() if old.get(name) == item}
    observed_unchanged = {name: item for name, item in unchanged.items() if name in arrival_names}
    added = [item for name, item in new.items() if name not in old]
    removed = [item for name, item in old.items() if name not in new]
    changed = [{"file": name, "before": old[name], "after": item} for name, item in new.items()
               if name in old and name not in unchanged]
    if not (added or removed or changed):
        disposition = "unchanged"
    elif not registered:
        disposition = "candidate_identity_pending"
    elif changed or removed:
        disposition = "registered_range_changed"
    else:
        disposition = "registered_supplement_candidate"
    return {**result, "disposition": disposition, "unchanged": list(observed_unchanged.values()),
            "carried_supplements": [item for item in effective if item["file"] not in arrival_names],
            "added": added, "removed": removed, "changed": changed,
            "reusable_evidence": reusable(state, intake, binding, observed_unchanged)}


def seed_static_documents(project, report):
    """Prefill self-contained document parses proven against the previous edition."""
    from prepare_documents import HTML_SUFFIXES, PROVIDER_VERSION

    source_state = local_child(Path(report["baseline"]["package"]), ".course")
    staged = []
    for item in report["reusable_evidence"]:
        suffix = Path(item["file"]).suffix.casefold()
        if suffix not in HTML_SUFFIXES | NATIVE_TEXT_SUFFIXES | {".url"}:
            continue
        key = f"{item['source_sha256']}-{suffix[1:]}"
        if suffix in HTML_SUFFIXES:
            key += "-static"
        elif suffix == ".url":
            key += "-shortcut"
        relative = f"work/资料/{key}.json"
        if relative != item["artifact"]:
            continue
        try:
            source = local_child(source_state, relative)
            cached = read(source)
            if sha(source) != item["artifact_sha256"]:
                continue
        except (OSError, ValueError):
            continue
        if not isinstance(cached, dict):
            continue
        target = local_child(project.state, relative)
        if target.exists() and (not target.is_file() or sha(target) != item["artifact_sha256"]):
            continue
        if suffix in NATIVE_TEXT_SUFFIXES:
            if cached.get("provider_version") != PROVIDER_VERSION or not seed_native_text(project, item, cached):
                continue
        elif cached.get("processing_status") != "extracted" or cached.get("files"):
            continue
        if not target.exists():
            copy_file(source, target, item["artifact_sha256"])
        if target.is_file() and sha(target) == item["artifact_sha256"]:
            staged.append(item)
    return staged


def seed_native_text(project, item, cached):
    from worldanalysis.documents import _decode

    asset_root = cached.get("asset_root", "")
    expected = {"key": "f000001", "relative_path": "f000001-native-text.txt", "kind": "native_text",
                "media_type": "text/plain; charset=utf-8"}
    if (cached.get("processing_status") != "verified_complete" or cached.get("files") != [expected]
            or not isinstance(asset_root, str) or PurePosixPath(asset_root).parent != PurePosixPath("资料")):
        return False
    try:
        # Native text assets are a deterministic view of the exact current original.
        raw = local_child(project.raw, item["file"])
        text, _ = _decode(raw.read_bytes(), None)
        if text != "".join(block["text"] for block in cached["blocks"]):
            return False
        target = local_child(project.work, f"{asset_root}/{expected['relative_path']}")
        if target.exists() and target.read_bytes() != text.encode("utf-8"):
            return False
        write(target, text)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def stage(queue, row, report, output, *, same_edition_reason=None, new_edition_reason=None):
    new_edition = new_edition_reason is not None
    if new_edition:
        if not new_edition_reason.strip() or report["disposition"] != "candidate_identity_pending":
            raise ValueError("新版取材需要独立候选目录、实际差异及可核对的版次依据")
    else:
        if (report["disposition"] not in {"registered_supplement_candidate", "candidate_identity_pending"}
                or not report["added"] or report["changed"] or report["removed"]
                or report["carried_supplements"]):
            raise ValueError("增量取材需要一份完整保留旧原件且确有新增原件的候选目录")
        if not report["candidate"]["registered_source"] and not (same_edition_reason or "").strip():
            raise ValueError("另一个来源须先核实确属同一课程版次，并登记核对依据")
    output = batch.safe_path(output)
    protected = [Path(queue["source"])]
    if queue.get("vault"):
        protected.append(Path(queue["vault"]))
    for item in queue["projects"]:
        protected.extend(Path(binding["package"]) for binding in [item, *item.get("history", [])]
                         if binding.get("package"))
    if any(output.is_relative_to(path) or path.is_relative_to(output) for path in protected):
        raise ValueError("增量取材须使用独立的新课程包位置")
    directory = Path(report["candidate"]["directory"])
    project = CourseProject.initialize(directory, output)
    with project.locked():
        if project.entry.exists():
            raise ValueError("已有正式教材的课程包不能用于准备增量取材")
        files = project.validate_raw()
        if batch.source_fingerprint(files) != report["candidate"]["source_sha256"]:
            raise ValueError("增量取材期间原件范围变化，请重新核对候选目录")
        record = {"schema": 1, "baseline": report["baseline"], "candidate": report["candidate"],
                  "unchanged": report["unchanged"], "added": report["added"]}
        if new_edition:
            record.update(stage_kind="new_edition", comparison_project_id=row["id"],
                          comparison_sequence=row["sequence"], new_edition_reason=new_edition_reason,
                          changed=report["changed"], removed=report["removed"])
        else:
            record.update(project_id=row["id"], sequence=row["sequence"], supplement_reason=same_edition_reason)
        path = local_child(project.work, "增量取材.json")
        if path.is_file():
            previous = read(path)
            if any(previous.get(key) != value for key, value in record.items()):
                raise ValueError("已有增量工作区登记了不同来源或范围")
        elif project.work.is_dir() and any(project.work.iterdir()):
            raise ValueError("课程包已有工作内容，请使用独立的新取材目录")
        else:
            put(path, record)
        staged = seed_static_documents(project, report)
        put(path, {**record, "reused_static_evidence": staged})
    result = {**report, "read_only": False, "staged_package": str(project.folder),
              "stage_record": str(path), "reused_static_evidence": len(staged),
              "stage_kind": "new_edition" if new_edition else "same_edition"}
    if new_edition:
        result["comparison_project_id"] = result.pop("project_id")
        result["comparison_sequence"] = result.pop("sequence")
    return result
