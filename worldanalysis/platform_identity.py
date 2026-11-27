"""Path-independent platform identities and compatibility on the existing ledger.

Inventories and job receipts remain unchanged. Aliases point to legacy jobs; they
never rewrite a job's input_revision or turn an old success into a new request.
"""
from __future__ import annotations

from .contracts import ContractError, canonical, digest, now


def content_identity(account_ref, item):
    """Use observed content facts, not a folder, inventory id or display name."""
    return {"provider": "baidu", "account_ref": account_ref,
            "file_id": item["file_id"], "source_revision": item["source_revision"],
            "size_bytes": item["size_bytes"]}


def material_content(value):
    """Only the cloud location is nonsemantic; attachment bytes/config still count."""
    return {**value, "source": {k: v for k, v in value["source"].items() if k != "path"}}


def _schema(store):
    # execute (not executescript) also works inside Store's nested transactions.
    store.connection.execute("""CREATE TABLE IF NOT EXISTS platform_task_aliases(
      task_key TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs)""")
    store.connection.execute("""CREATE TABLE IF NOT EXISTS platform_material_locations(
      project_id TEXT NOT NULL REFERENCES projects, material_id TEXT NOT NULL,
      content_key TEXT NOT NULL, path TEXT NOT NULL, source_json TEXT NOT NULL,
      observed_at TEXT NOT NULL,
      PRIMARY KEY(project_id,material_id,content_key,path))""")


def remember_material_location(store, project_id, value):
    _schema(store)
    source = value["source"]
    store.connection.execute(
        "INSERT OR IGNORE INTO platform_material_locations VALUES(?,?,?,?,?,?)",
        (project_id, value["material_id"], digest(material_content(value)),
         source["path"], canonical(source).decode(), now()))


def queue_content_job(store, project_id, operation, provider, revision, *,
                      legacy_revisions=(), config_version="platform-first-v1",
                      estimated_cost_minor=None, max_cost_minor=0, create=True):
    """Reuse a compatible path-bound job before reserving a stable new one.

    Provider/configuration and selection remain part of the task key. Legacy
    revisions must be derived from frozen inventories with identical content.
    In-flight legacy duplicates win over completed/queued jobs to prevent sends.
    """
    if provider.get("config_version") != config_version:
        raise ContractError("provider and task config_version must match")
    with store.transaction():
        _schema(store)
        provider_json = canonical(provider).decode()

        def compatible(row):
            # Store has no separate task-config column. Its original key binds
            # the independent config argument; provider_json alone cannot.
            return (row["project_id"] == project_id and row["operation"] == operation
                    and row["provider_json"] == provider_json
                    and row["idempotency_key"] == digest([project_id, operation, provider, row["input_revision"], config_version]))

        key = digest([project_id, operation, provider, revision, config_version])
        alias = store.connection.execute(
            "SELECT j.* FROM platform_task_aliases a JOIN jobs j USING(job_id) WHERE a.task_key=?",
            (key,)).fetchone()
        revisions = {revision, *legacy_revisions}
        candidates = [dict(row) for row in store.connection.execute(
            "SELECT * FROM jobs WHERE project_id=? AND operation=? AND provider_json=? ORDER BY created_at,job_id",
            (project_id, operation, provider_json)).fetchall()
            if row["input_revision"] in revisions and compatible(row)]
        if alias and compatible(alias) and all(row["job_id"] != alias["job_id"] for row in candidates):
            candidates.append(dict(alias))
        priority = {"submission_unknown": 0, "submitted": 0, "waiting": 0,
                    "running": 0, "verified_complete": 1, "queued": 2}
        if not candidates and not create:
            return None
        job = min(candidates, key=lambda row: priority.get(row["state"], 3)) if candidates else store.queue_job(
            project_id, operation, provider, revision, config_version=config_version,
            estimated_cost_minor=estimated_cost_minor, max_cost_minor=max_cost_minor)
        store.connection.execute(
            "INSERT INTO platform_task_aliases VALUES(?,?) ON CONFLICT(task_key) DO UPDATE SET job_id=excluded.job_id",
            (key, job["job_id"]))
        return job
