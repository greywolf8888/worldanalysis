"""Migrate the old mixed deferral bucket using a complete, snapshot-bound audit."""
from collections import Counter
from pathlib import Path

import batch
from project import local_child, named_entry, read, sha


def indexed_rows(rows):
    index = {row["sequence"]: row for row in rows}
    if len(index) != len(rows):
        raise ValueError("审计或生产控制存在重复顺位")
    return index


def require_identity(row, audited):
    expected = audited.get("inventory", {})
    if (row["id"], row["name"], row["status"], len(row["metadata"]),
            sum(item["bytes"] for item in row["metadata"])) != (
            audited.get("id"), audited.get("folder"), audited.get("batch_status"),
            expected.get("files"), expected.get("bytes")):
        raise ValueError(f"第 {row['sequence']} 项审计身份或原件盘点与当前台账不同")


def check_control_identity(row, control):
    if control.get("id") and control["id"] != row["id"]:
        raise ValueError(f"第 {row['sequence']} 项控制记录身份冲突")
    for field in ("source_sha256", "content_sha256"):
        if control.get(field) and control[field] != row.get(field):
            raise ValueError(f"第 {row['sequence']} 项既有原件指纹冲突 {field}")


def formal_reference(queue, row, audited, control):
    target = audited["reuse_target"]
    entry = batch.safe_path(target["entry"])
    vault = Path(queue["vault"])
    if (not entry.is_relative_to(vault) or entry != named_entry(local_child(vault, entry.parent.parent.name))
            or not entry.is_file()):
        raise ValueError(f"第 {row['sequence']} 项同源正式入口无效")
    current = sha(entry)
    recorded = control.get("canonical_daily_entry") or {}
    control_entry = control.get("existing_entry") or recorded.get("path")
    control_sha = control.get("existing_md_sha256") or recorded.get("sha256")
    if not control_entry or Path(control_entry) != entry or control_sha != current:
        raise ValueError(f"第 {row['sequence']} 项同源正式入口与控制回证不符")
    check_control_identity(row, control)
    if not row.get("source_sha256") or not row.get("content_sha256"):
        raise ValueError(f"第 {row['sequence']} 项同源关系缺已确认原件指纹")
    sequence = target.get("formal_sequence")
    if sequence is not None:
        primary = batch.row_at(queue, sequence)
        publication = primary.get("publication") or {}
        if (primary["status"] not in batch.PUBLISHED or Path(publication.get("entry", "")) != entry
                or publication.get("sha256") != current or primary["id"] == row["id"]):
            raise ValueError(f"第 {row['sequence']} 项关联的正式主项目与同版签收不一致")
    else:
        publication = {"entry": str(entry), "sha256": current, "method": "已有同版正式成果复用"}
    return publication, {**target, "entry": str(entry), "sha256": current, "sequence": sequence}


def verify_alias(row, other, audited, control):
    sequence = other["sequence"]
    related = [control.get(key) for key in ("canonical_sequence", "related_deferred_sequence",
                                            "primary_material_recovery_sequence")]
    reason = str(control.get("reason") or row.get("reason") or "")
    if (sequence not in related and not (f"第{sequence}项" in reason and
                                            any(word in reason for word in ("同源", "相同", "同一")))):
        raise ValueError(f"第 {row['sequence']} 项缺共同原件关系的控制记录")
    if row["id"] == other["id"] or audited.get("related_sequence") != sequence:
        raise ValueError("同源关联不能指向自身")
    check_control_identity(row, control)


def priority_map(priorities, entries):
    result = {}
    for rank, sequence in enumerate(priorities, 1):
        row = entries.get(sequence)
        if not row or row["audit_status"] != "waiting_originals_or_evidence":
            raise ValueError(f"指定复审第 {sequence} 项不属于此次未闭合范围")
        if row.get("waiting_group", "").startswith("B_"):
            sequence = row.get("related_sequence")
            if sequence not in entries or entries[sequence]["audit_status"] != "waiting_originals_or_evidence":
                raise ValueError("指定同源来件的主项目无待复审身份")
        result.setdefault(sequence, rank)
    return result


def migration_result(queue, audit_sha, *, applied, already=False):
    counts = dict(Counter(row["status"] for row in queue["projects"]))
    priority = [row["sequence"] for row in sorted(
        (row for row in queue["projects"] if batch.review_priority(row) is not None),
        key=batch.review_priority)]
    pending = [row["sequence"] for row in queue["projects"]
               if row.get("re_review", {}).get("state") == "待复审" and batch.eligible_pending(row)]
    aliases = {str(row["sequence"]): row["same_source_primary"] for row in queue["projects"]
               if row.get("re_review", {}).get("state") == "关联待复审"}
    next_row = batch.next_row(queue)
    return {"applied": applied, "already_applied": already, "audit_sha256": audit_sha,
            "counts": counts, "one_time_re_review": pending, "priority_sequence": priority,
            "related_waiting": aliases, "next": next_row["sequence"] if next_row else None}


def migrate(queue, manifest, audit, priorities, *, apply=False):
    manifest, audit = map(batch.safe_path, (manifest, audit))
    audit_sha = sha(audit)
    previous = queue.get("audit_review_migration")
    if previous and previous.get("sha256") == audit_sha:
        return migration_result(queue, audit_sha, applied=False, already=True)
    data = read(audit)
    snapshot = data.get("evidence_snapshot", {})
    if (data.get("schema") != 1 or Path(data.get("source_root", "")) != Path(queue["source"])
            or sha(manifest) != snapshot.get("ledger_sha256")):
        raise ValueError("审计与当前批次字节、来源目录或格式不一致，须重新核对")
    control_path = batch.safe_path(snapshot["production_control"])
    if sha(control_path) != snapshot.get("production_control_sha256"):
        raise ValueError("生产控制快照已变化，须重新绑定审计证据")
    control = read(control_path)
    reuse = indexed_rows(control["resolved_source_reuses"])
    deferred = indexed_rows(control["deferred_production_records"])
    entries = indexed_rows(data["rows"])
    if set(entries) != {row["sequence"] for row in queue["projects"]}:
        raise ValueError("审计必须完整覆盖每个既有顺位")
    rank = priority_map(priorities, entries)

    # Validate snapshot, actual metadata, canonical notes and controlled
    # reclassifications first. No partial ledger updates survive an error.
    changes = []
    for row in queue["projects"]:
        seq = row["sequence"]
        audited = entries[seq]
        require_identity(row, audited)
        batch.check_current_metadata(queue, row)
        role = audited["audit_status"]
        if role == "formal_signed":
            publication = row.get("publication") or {}
            proof = audited.get("formal_evidence", {})
            if row["status"] not in batch.PUBLISHED or publication.get("sha256") != proof.get("sha256"):
                raise ValueError(f"第 {seq} 项已签收身份不同")
            if publication.get("entry") != proof.get("entry") or not batch.current_note_matches(publication):
                raise ValueError(f"第 {seq} 项既有正式正文已变")
            continue
        if row["status"] != batch.DEFERRED:
            raise ValueError(f"第 {seq} 项不是已核对的旧待补项目")
        if apply and role in ("existing_formal_reuse", "archive_or_restricted_not_missing"):
            _, _, observed_identity = batch.identify(queue, row)
            if any(row.get(key) != value for key, value in observed_identity.items()):
                raise ValueError(f"第 {seq} 项正式迁移前的原件指纹已变化")
        if role == "existing_formal_reuse":
            recorded = reuse.get(seq)
            if not recorded or "同源" not in recorded.get("status", ""):
                raise ValueError(f"第 {seq} 项没有对应的已核同源生产回证")
            publication, target = formal_reference(queue, row, audited, recorded)
            scope = publication.get("scope_status")
            fields = {"publication": publication, "reuse_target": target,
                      "scope_status": scope, "scope_gaps": publication.get("scope_gaps", []),
                      "audit_state_evidence": {"path": str(audit), "sha256": audit_sha}}
            if target["sequence"] is not None:
                fields["same_source_primary"] = target["sequence"]
            changes.append((row, batch.REUSED, fields))
        elif role == "archive_or_restricted_not_missing":
            recorded = deferred.get(seq) or {}
            check_control_identity(row, recorded)
            reason = str(row.get("reason") or "")
            if ("留档" not in recorded.get("status", "")
                    and not any(term in reason for term in ("受限", "范围受限", "未发布为操作课"))):
                raise ValueError(f"第 {seq} 项缺既有留档依据")
            if not row.get("source_sha256") or not row.get("content_sha256"):
                raise ValueError(f"第 {seq} 项留档缺既有原件指纹")
            changes.append((row, batch.ARCHIVED,
                            {"archive_evidence": {"audit": str(audit), "sha256": audit_sha,
                                                  "summary": audited["audit_summary"]}}))
        elif role == "waiting_originals_or_evidence":
            if audited.get("ready_for_formal_claim") is not False:
                raise ValueError("旧审计不能给出新验收批准")
            review = {"state": "待复审", "audit": str(audit), "audit_sha256": audit_sha,
                      "group": audited["waiting_group"], "gaps_before_review": audited["gap_summary"],
                      "next_check": audited["candidate_next_step"]}
            fields = {"re_review": review}
            state = batch.PENDING
            if audited["waiting_group"].startswith("B_"):
                primary = batch.row_at(queue, audited["related_sequence"])
                primary_audit = entries[primary["sequence"]]
                if (primary_audit["audit_status"] != role
                        or primary_audit.get("waiting_group", "").startswith("B_")):
                    raise ValueError("同源共同待补必须关联独立主项目，不得循环")
                verify_alias(row, primary, audited, deferred.get(seq) or {})
                if seq in priorities:
                    review["requested_priority_rank"] = priorities.index(seq) + 1
                review["state"] = "关联待复审"
                fields["same_source_primary"] = primary["sequence"]
                state = batch.DEFERRED
            elif seq in rank:
                review["priority_rank"] = rank[seq]
            changes.append((row, state, fields))
        else:
            raise ValueError(f"第 {seq} 项未知审计状态 {role}")
    if set(reuse) != {row["sequence"] for row, state, _ in changes if state == batch.REUSED}:
        raise ValueError("既有同源复用记录与完整审计不一致")
    for row, state, fields in changes:
        batch.change(row, state, **fields)
    queue["audit_review_migration"] = {"path": str(audit), "sha256": audit_sha,
                                        "applied_at": batch.timestamp(), "user_priority_sequences": priorities}
    return migration_result(queue, audit_sha, applied=apply)
