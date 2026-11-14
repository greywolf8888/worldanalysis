"""Explicit same-agent recovery attempts; never rewrite acceptance or reuse reads.

Root integration: claim_pending returns a claimed sqlite.Row (or None), while
an envelope hook adds initial pending scope. Only journal-committed sources are
supported; legacy acceptance is not silently upgraded. No collection is invoked.
"""
from copy import deepcopy
import json

from .contracts import ContractError, canonical, check_id, digest, now, scan_secrets, validate_evidence
from .store import new_id

_TABLE = "analysis_recovery_requests"

def _exists(analysis):
    return analysis.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)).fetchone() is not None

def _source(analysis, source_task_id, block_id):
    task = analysis._task(source_task_id)
    journal = analysis.connection.execute("SELECT * FROM analysis_publications WHERE task_id=?", (source_task_id,)).fetchone()
    if not journal or journal["state"] != "committed":
        raise ContractError("recovery unsupported: source requires journal-committed acceptance")
    result = analysis.get_result(source_task_id)
    bundle = analysis._bundle(task)
    expected = {"project_id": task["project_id"], "based_publish_id": task["publish_id"],
                "based_bundle_id": bundle["bundle_id"], "based_bundle_revision": bundle["bundle_revision"]}
    if (any(result[k] != v for k, v in expected.items())
            or task["state"] != result["execution_status"] or task["state"] not in {"completed", "partial", "blocked", "failed"}
            or result["agent"] != json.loads(task["agent_json"])
            or result["program_validation"]["status"] != "passed"
            or digest(result) != task["result_sha256"] or journal["result_sha256"] != task["result_sha256"]):
        raise ContractError("recovery source task/result/project/version mismatch")
    entries = {p["block_id"]: p for p in result["block_processing"]}
    blocks = {b["block_id"]: b for b in bundle["blocks"]}
    if (entries.keys() != blocks.keys() or len(entries) != len(result["block_processing"])
            or any(entries[k]["asset_ref"] != b["asset_ref"] for k, b in blocks.items())):
        raise ContractError("recovery source block scope mismatch")
    entry, block = entries.get(block_id), blocks.get(block_id)
    if not entry or entry["status"] != "excluded" or not block:
        raise ContractError("recovery requires an exact excluded source block")
    exclusion = entry["exclusion"]
    ref = exclusion["recovery_ref"]
    if exclusion["restore_action"] != "requeue_block" or ref not in {block_id, block["processing"]["recovery_ref"]}:
        raise ContractError("recovery_ref does not identify this source block")
    if ref != block_id:
        job = analysis.connection.execute("SELECT * FROM jobs WHERE job_id=?", (ref,)).fetchone()
        asset = block["asset_ref"]
        if (not job or job["project_id"] != task["project_id"] or job["operation"] != "native_extract"
                or job["input_revision"] not in {asset["asset_revision"], asset["asset_id"] + ":" + asset["asset_revision"]}
                or job["state"] != "verified_complete"):
            raise ContractError("recovery_ref is not a real matching published extraction job")
    for basis in exclusion["basis_refs"]:
        validate_evidence(basis, bundle, allow_source_only=False)
    return task, result


def _attempt_key(row):
    return digest(["analysis-recovery-attempt-v1", row["request_id"], row["input_sha256"], row["source_result_sha256"]])

def _validate_attempt(analysis, row):
    source, result = _source(analysis, row["source_task_id"], row["block_id"])
    task = analysis._task(row["recovery_task_id"])
    if (row["source_result_sha256"] != source["result_sha256"]
            or row["input_sha256"] != digest([row["source_task_id"], row["block_id"], row["reason"]])
            or any(task[k] != source[k] for k in ("project_id", "publish_id", "agent_json"))
            or task["idempotency_key"] != _attempt_key(row)
            or task["output_relative_path"] != "分析/" + task["task_id"] or row["initial_status"] != "pending"):
        raise ContractError("recovery attempt/source identity mismatch")
    return task, result


def _response(row, task, *, idempotent):
    return {"status": task["state"], "request_id": row["request_id"], "recovery_task_id": task["task_id"],
            "source_task_id": row["source_task_id"], "project_id": task["project_id"], "publish_id": task["publish_id"],
            "block_id": row["block_id"], "initial_target_status": "pending", "idempotent": idempotent}


def requeue_block(analysis, source_task_id, block_id, *, reason, request_id=None):
    """Persist a fresh pending attempt. Replaying a request never advances work."""
    check_id(source_task_id)
    check_id(block_id)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise ContractError("recovery requires a nonempty bounded reason")
    scan_secrets(reason)
    input_sha = digest([source_task_id, block_id, reason])
    request_id = check_id(request_id) if request_id is not None else "recovery-request-" + input_sha
    with analysis.store.transaction():
        old = analysis.connection.execute("SELECT * FROM analysis_recovery_requests WHERE request_id=?", (request_id,)).fetchone() if _exists(analysis) else None
        if old:
            if old["input_sha256"] != input_sha:
                raise ContractError("recovery request ID conflict")
            task, _ = _validate_attempt(analysis, old)
            return _response(old, task, idempotent=True)
        if analysis.store.pipeline_state("analysis") != "running":
            return {"status": "blocked", "reason": "analysis paused"}
        source, _ = _source(analysis, source_task_id, block_id)
        analysis.connection.execute("""CREATE TABLE IF NOT EXISTS analysis_recovery_requests(
            request_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL,
            source_task_id TEXT NOT NULL REFERENCES analysis_tasks,
            source_result_sha256 TEXT NOT NULL, block_id TEXT NOT NULL, reason TEXT NOT NULL,
            recovery_task_id TEXT NOT NULL UNIQUE REFERENCES analysis_tasks,
            initial_status TEXT NOT NULL CHECK(initial_status='pending'), created_at TEXT NOT NULL)""")
        task_id = new_id("analysis-recovery")
        row = {"request_id": request_id, "input_sha256": input_sha, "source_task_id": source_task_id,
               "source_result_sha256": source["result_sha256"], "block_id": block_id, "reason": reason,
               "recovery_task_id": task_id, "initial_status": "pending", "created_at": now()}
        analysis.connection.execute("""INSERT INTO analysis_tasks(task_id,project_id,publish_id,idempotency_key,
            agent_json,state,output_relative_path,created_at) VALUES(?,?,?,?,?,'queued',?,?)""",
            (task_id, source["project_id"], source["publish_id"], _attempt_key(row), source["agent_json"], "分析/" + task_id, row["created_at"]))
        analysis.connection.execute("INSERT INTO analysis_recovery_requests VALUES(?,?,?,?,?,?,?,?,?)", tuple(row.values()))
        analysis.store.event("analysis_recovery_requested", task_id, source["project_id"],
            {"request_id": request_id, "source_task_id": source_task_id, "block_id": block_id,
             "publish_id": source["publish_id"], "source_result_sha256": source["result_sha256"], "initial_target_status": "pending"})
        return _response(row, analysis._task(task_id), idempotent=False)


def claim_pending(analysis, project_id, agent):
    """Claim queued recoveries only; running work still needs explicit resume."""
    if not _exists(analysis):
        return None
    with analysis.store.transaction():
        if analysis.store.pipeline_state("analysis") != "running":
            return None
        row = analysis.connection.execute("""SELECT r.* FROM analysis_recovery_requests r
            JOIN analysis_tasks t ON t.task_id=r.recovery_task_id
            WHERE t.state='queued' AND t.agent_json=?""" + (" AND t.project_id=?" if project_id else "") +
            " ORDER BY r.created_at,r.request_id LIMIT 1", (canonical(agent).decode(), project_id) if project_id else (canonical(agent).decode(),)).fetchone()
        if row is None:
            return None
        task, _ = _validate_attempt(analysis, row)
        changed = analysis.connection.execute("UPDATE analysis_tasks SET state='running',claimed_at=? WHERE task_id=? AND state='queued'", (now(), task["task_id"])).rowcount
        if changed != 1:
            return None
        analysis.store.event("analysis_claimed", task["task_id"], task["project_id"],
            {"publish_id": task["publish_id"], "model_version": agent["model_version"], "recovery_request_id": row["request_id"]})
        return analysis._task(task["task_id"])


def validate_result(analysis, task, result):
    """Recovery may append gaps, but cannot erase or revise source acceptance."""
    row = analysis.connection.execute("SELECT * FROM analysis_recovery_requests WHERE recovery_task_id=?", (task["task_id"],)).fetchone() if _exists(analysis) else None
    if row is None:
        return
    _, source = _validate_attempt(analysis, row)
    proposed = {gap["gap_id"]: gap for gap in result["gaps"]}
    for gap in source["gaps"]:
        if canonical(proposed.get(gap["gap_id"])) != canonical(gap):
            raise ContractError("recovery must preserve source accepted gaps unchanged")


def envelope(analysis, task, base):
    """Expose initial pending scope, never a receipt or a prefilled read result."""
    row = analysis.connection.execute("SELECT * FROM analysis_recovery_requests WHERE recovery_task_id=?", (task["task_id"],)).fetchone() if _exists(analysis) else None
    if row is None:
        return base
    _, result = _validate_attempt(analysis, row)
    initial = deepcopy(result["block_processing"])
    for entry in initial:
        entry.update(status="pending", read_receipt_refs=[], exclusion=None,
                     reason="Fresh recovery attempt; actual task-owned reads or existing strict reuse policy required.")
    return {**base, "gaps": deepcopy(result["gaps"]), "initial_block_processing": initial,
            "source_block_processing": deepcopy(result["block_processing"]),
            "recovery": {"request_id": row["request_id"], "source_task_id": row["source_task_id"],
                "source_result_sha256": row["source_result_sha256"], "block_id": row["block_id"],
                "reason": row["reason"], "initial_target_status": "pending"},
            "instructions": base["instructions"] + ["All initial blocks are pending; historical exclusions/receipts are context only. Recovery does not resolve any gap or authorize new collection."]}
