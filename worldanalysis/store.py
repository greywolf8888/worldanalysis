"""One SQLite business ledger and immutable, recoverable evidence publication.

No legacy JSON ledger is opened or rewritten. Files contain evidence, not a
second mutable task database. A manifest alone is never a publication receipt.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import mimetypes
import marshal
import sys
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import uuid

from .contracts import (ContractError, VALIDATOR_VERSION, asset_key, canonical,
    check_id, contains_sensitive, digest, hash_file, now, redact_text,
    reject_links, safe_path, scan_secrets, validate_bundle, validate_plan,
    validate_relative_path)


def _config_value(value):
    if isinstance(value,dict):
        return {str(k):_config_value(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):
        return [_config_value(v) for v in value]
    if isinstance(value,Path):
        return str(value)
    if callable(value):
        code = getattr(value,"__code__",None)
        cells = getattr(value,"__closure__",None) or ()
        return {"callable":getattr(value,"__module__","")+"."+getattr(value,"__qualname__",type(value).__name__),"implementation":hashlib.sha256(marshal.dumps(code,0)).hexdigest() if code else type(value).__name__,"closure":[cell.cell_contents if isinstance(cell.cell_contents,(str,int,float,bool,type(None))) else type(cell.cell_contents).__name__ for cell in cells]}
    if isinstance(value,(str,int,float,bool,type(None))):
        return value
    raise ContractError("configuration requires an explicit serializable version")


def new_id(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex


def _write(path: Path, data: bytes) -> None:
    reject_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_links(path.parent)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _readonly(path: Path) -> None:
    path.chmod(stat.S_IREAD | (stat.S_IRGRP | stat.S_IROTH if os.name != "nt" else 0))


def _copy(source: Path, target: Path) -> tuple[str, int]:
    sha, size = hash_file(source)
    if target.exists():
        if hash_file(target) != (sha, size):
            raise ContractError("immutable original/artifact collision")
        return sha, size
    target.parent.mkdir(parents=True, exist_ok=True)
    reject_links(target)
    with source.open("rb") as src, target.open("xb") as dst:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    if hash_file(target) != (sha, size) or hash_file(source) != (sha, size):
        raise ContractError("source changed while copying")
    _readonly(target)
    return sha, size


class Store:
    def __init__(self, root, *, db_path=None, max_file_bytes=64 * 1024 * 1024, submission_authorizer=None):
        self.root = Path(root).absolute()
        reject_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.results_root = self.root / "整理结果"
        self.results_root.mkdir(parents=True, exist_ok=True)
        reject_links(self.results_root)
        self.db_path = Path(db_path) if db_path else self.results_root / "worldanalysis.sqlite3"
        reject_links(self.db_path)
        self.max_file_bytes = int(max_file_bytes)
        self.submission_authorizer = submission_authorizer
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self._schema()
        self.recover_publications()
        self.recover_analysis_publications()

    @property
    def conn(self):
        return self.connection

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            nested = self.connection.in_transaction
            savepoint = "sp_" + uuid.uuid4().hex
            self.connection.execute("SAVEPOINT " + savepoint if nested else "BEGIN IMMEDIATE")
            try:
                yield self.connection
                self.connection.execute("RELEASE SAVEPOINT " + savepoint if nested else "COMMIT")
            except BaseException:
                if nested:
                    self.connection.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                    self.connection.execute("RELEASE SAVEPOINT " + savepoint)
                else:
                    self.connection.execute("ROLLBACK")
                raise

    def _schema(self):
        self.connection.executescript('''
        CREATE TABLE IF NOT EXISTS projects(
          project_id TEXT PRIMARY KEY, identity_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(
          source_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects,
          kind TEXT NOT NULL, local_path TEXT, created_at TEXT NOT NULL,
          UNIQUE(project_id,local_path));
        CREATE TABLE IF NOT EXISTS source_revisions(
          source_id TEXT NOT NULL REFERENCES sources, revision TEXT NOT NULL,
          sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, observed_at TEXT NOT NULL,
          PRIMARY KEY(source_id,revision));
        CREATE TABLE IF NOT EXISTS assets(
          asset_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS asset_revisions(
          asset_id TEXT NOT NULL REFERENCES assets, asset_revision TEXT NOT NULL,
          sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, media_type TEXT NOT NULL,
          original_relative_path TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(asset_id,asset_revision));
        CREATE TABLE IF NOT EXISTS source_assets(
          source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
          asset_id TEXT NOT NULL, asset_revision TEXT NOT NULL,
          PRIMARY KEY(source_id,source_revision,asset_id,asset_revision),
          FOREIGN KEY(source_id,source_revision) REFERENCES source_revisions,
          FOREIGN KEY(asset_id,asset_revision) REFERENCES asset_revisions);
        CREATE TABLE IF NOT EXISTS providers(
          provider_id TEXT NOT NULL, implementation_version TEXT NOT NULL,
          provider_json TEXT NOT NULL, observed_at TEXT NOT NULL,
          PRIMARY KEY(provider_id,implementation_version));
        CREATE TABLE IF NOT EXISTS provider_configurations(
          provider_id TEXT NOT NULL, implementation_version TEXT NOT NULL,
          config_version TEXT NOT NULL, provider_json TEXT NOT NULL,
          PRIMARY KEY(provider_id,implementation_version,config_version),
          FOREIGN KEY(provider_id,implementation_version) REFERENCES providers);
        CREATE TABLE IF NOT EXISTS controls(
          pipeline TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(
          job_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects,
          idempotency_key TEXT NOT NULL UNIQUE, operation TEXT NOT NULL,
          provider_json TEXT NOT NULL, input_revision TEXT NOT NULL,
          state TEXT NOT NULL, estimated_cost_minor INTEGER,
          reserved_cost_minor INTEGER NOT NULL DEFAULT 0,
          settled_cost_minor INTEGER, max_cost_minor INTEGER NOT NULL,
          request_id TEXT, remote_task_id TEXT, reason TEXT, receipt_json TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS plans(
          plan_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects,
          plan_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS publications(
          publish_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects,
          bundle_id TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL,
          idempotency_key TEXT NOT NULL, state TEXT NOT NULL,
          staging_relative_path TEXT NOT NULL, final_relative_path TEXT NOT NULL,
          manifest_sha256 TEXT, bundle_json TEXT, expected_files_json TEXT,
          created_at TEXT NOT NULL, published_at TEXT, reason TEXT,
          UNIQUE(project_id,revision), UNIQUE(project_id,idempotency_key));
        CREATE TABLE IF NOT EXISTS analysis_tasks(
          task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects,
          publish_id TEXT NOT NULL REFERENCES publications,
          idempotency_key TEXT NOT NULL UNIQUE, agent_json TEXT NOT NULL,
          state TEXT NOT NULL, output_relative_path TEXT NOT NULL,
          created_at TEXT NOT NULL, claimed_at TEXT, completed_at TEXT,
          result_sha256 TEXT, result_relative_path TEXT, reason TEXT);
        CREATE TABLE IF NOT EXISTS read_receipts(
          receipt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES analysis_tasks,
          block_id TEXT, kind TEXT NOT NULL, receipt_json TEXT NOT NULL,
          content TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS analyses(
          analysis_id TEXT NOT NULL, analysis_revision TEXT NOT NULL,
          task_id TEXT NOT NULL UNIQUE REFERENCES analysis_tasks,
          project_id TEXT NOT NULL REFERENCES projects, publish_id TEXT NOT NULL,
          result_json TEXT NOT NULL, result_sha256 TEXT NOT NULL,
          result_relative_path TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(analysis_id,analysis_revision));
        CREATE TABLE IF NOT EXISTS analysis_validation_indexes(
          task_id TEXT PRIMARY KEY REFERENCES analysis_tasks,
          validation_relative_path TEXT NOT NULL, observed_sha256 TEXT NOT NULL,
          result_sha256 TEXT NOT NULL, event_id TEXT NOT NULL REFERENCES events,
          submitted_sha256 TEXT NOT NULL, provenance TEXT NOT NULL,
          observed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS analysis_publications(
          task_id TEXT PRIMARY KEY REFERENCES analysis_tasks,
          project_id TEXT NOT NULL REFERENCES projects,
          publish_id TEXT NOT NULL REFERENCES publications,
          submitted_sha256 TEXT NOT NULL, state TEXT NOT NULL,
          staging_relative_path TEXT NOT NULL, final_relative_path TEXT NOT NULL,
          result_relative_path TEXT NOT NULL, result_sha256 TEXT NOT NULL,
          result_json TEXT NOT NULL, expected_files_json TEXT NOT NULL,
          outputs_json TEXT NOT NULL, created_at TEXT NOT NULL, reason TEXT);
        CREATE TABLE IF NOT EXISTS supplement_requests(
          request_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects,
          task_id TEXT NOT NULL REFERENCES analysis_tasks, publish_id TEXT NOT NULL,
          state TEXT NOT NULL, request_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
          event_id TEXT PRIMARY KEY, project_id TEXT, kind TEXT NOT NULL,
          record_id TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS publication_ready ON publications(project_id,state,revision);
        CREATE INDEX IF NOT EXISTS analysis_ready ON analysis_tasks(state,created_at);
        ''')
        from .media_store import ensure_schema as media_schema
        media_schema(self.connection)
        for registration in self.connection.execute("SELECT * FROM providers").fetchall():
            p = json.loads(registration["provider_json"])
            self.connection.execute("INSERT OR IGNORE INTO provider_configurations VALUES(?,?,?,?)",(p["provider_id"],p["implementation_version"],p["config_version"],registration["provider_json"]))
        for pipeline in ("collection", "analysis"):
            self.connection.execute("INSERT OR IGNORE INTO controls VALUES(?,?,?)", (pipeline, "running", now()))

    def event(self, kind, record_id=None, project_id=None, payload=None):
        payload = payload or {}
        scan_secrets(payload)
        self.connection.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
            (new_id("event"), project_id, kind, record_id, canonical(payload).decode(), now()))

    def create_project(self, *, author=None, topic=None, edition=None, identity_evidence=None, project_id=None):
        identity = {"author": author, "topic": topic, "edition": edition,
                    "evidence": identity_evidence or [], "identity_confirmed": bool(author and topic and edition and identity_evidence)}
        scan_secrets(identity)
        ident = check_id(project_id) if project_id else new_id("project")
        with self.transaction():
            row = self.connection.execute("SELECT * FROM projects WHERE project_id=?", (ident,)).fetchone()
            if row:
                if json.loads(row["identity_json"]) != identity:
                    raise ContractError("project identity cannot be silently merged/changed")
                return ident
            self.connection.execute("INSERT INTO projects VALUES(?,?,?)", (ident, canonical(identity).decode(), now()))
            self.event("project_created", ident, ident)
        self.project_root(ident).mkdir(parents=True, exist_ok=True)
        return ident

    def project_root(self, project_id):
        check_id(project_id)
        if not self.connection.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
            raise ContractError("unknown project")
        path = self.results_root / project_id
        reject_links(path)
        return path

    def list_projects(self):
        return [{"project_id": r["project_id"], **json.loads(r["identity_json"]), "created_at": r["created_at"]}
                for r in self.connection.execute("SELECT * FROM projects ORDER BY created_at,project_id")]

    def control(self, pipeline, action):
        if pipeline not in {"collection", "analysis"} or action not in {"pause", "start", "resume"}:
            raise ContractError("invalid pipeline control")
        state = "paused" if action == "pause" else "running"
        with self.transaction():
            self.connection.execute("UPDATE controls SET state=?,updated_at=? WHERE pipeline=?", (state, now(), pipeline))
            self.event("pipeline_" + action, pipeline)
        return {"pipeline": pipeline, "state": state}

    def pipeline_state(self, pipeline):
        row = self.connection.execute("SELECT state FROM controls WHERE pipeline=?", (pipeline,)).fetchone()
        if not row:
            raise ContractError("unknown pipeline")
        return row[0]

    def register_provider(self, provider_id, implementation_version, *, runtime="local_cpu", config_version="default", registration_ref=None):
        check_id(provider_id)
        check_id(config_version)
        if not implementation_version or runtime not in {"local_cpu", "local_gpu", "approved_agent", "approved_remote_api", "authorized_browser"}:
            raise ContractError("provider requires an actual fixed version/runtime")
        provider = {"provider_id": provider_id, "implementation_version": implementation_version,
                    "config_version": config_version, "registration_ref": registration_ref or "registration-" + digest([provider_id,implementation_version])[:24], "runtime": runtime}
        scan_secrets(provider)
        self.connection.execute("INSERT OR IGNORE INTO providers VALUES(?,?,?,?)", (provider_id, implementation_version, canonical(provider).decode(), now()))
        row = self.connection.execute("SELECT provider_json FROM providers WHERE provider_id=? AND implementation_version=?", (provider_id, implementation_version)).fetchone()
        if {k:v for k,v in json.loads(row[0]).items() if k != "config_version"} != {k:v for k,v in provider.items() if k != "config_version"}:
            raise ContractError("provider registration version conflict")
        self.connection.execute("INSERT OR IGNORE INTO provider_configurations VALUES(?,?,?,?)",(provider_id,implementation_version,config_version,canonical(provider).decode()))
        return provider

    def queue_job(self, project_id, operation, provider, input_revision, *, config_version="default", estimated_cost_minor=None, max_cost_minor=0):
        self.project_root(project_id)
        check_id(operation)
        scan_secrets(provider)
        registration = self.connection.execute("SELECT provider_json FROM provider_configurations WHERE provider_id=? AND implementation_version=? AND config_version=?",(provider.get("provider_id"),provider.get("implementation_version"),provider.get("config_version"))).fetchone()
        if not registration or json.loads(registration[0]) != provider:
            raise ContractError("job provider/configuration not registered")
        if max_cost_minor < 0 or estimated_cost_minor is not None and estimated_cost_minor < 0:
            raise ContractError("invalid cost")
        key = digest([project_id, operation, provider, input_revision, config_version])
        ident = new_id("job")
        with self.transaction():
            self.connection.execute("INSERT OR IGNORE INTO jobs(job_id,project_id,idempotency_key,operation,provider_json,input_revision,state,estimated_cost_minor,max_cost_minor,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (ident,project_id,key,operation,canonical(provider).decode(),input_revision,"queued",estimated_cost_minor,max_cost_minor,now(),now()))
            return dict(self.connection.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone())

    def begin_submission(self, job_id):
        """Persist reservation and unknown state BEFORE the caller sends anything."""
        with self.transaction():
            row = self.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise ContractError("unknown job")
            if row["state"] == "submission_unknown":
                return {"status":"submission_unknown", "may_send":False, "request_id":row["request_id"], "reason":"hold_and_reconcile_no_automatic_resubmit"}
            if self.pipeline_state("collection") != "running":
                return {"status":"blocked", "may_send":False, "reason":"collection paused"}
            if row["state"] != "queued":
                return {"status":row["state"], "may_send":False, "reason":"not a new submission"}
            if row["estimated_cost_minor"] is None or row["estimated_cost_minor"] > row["max_cost_minor"]:
                self.connection.execute("UPDATE jobs SET state='blocked',reason=?,updated_at=? WHERE job_id=?", ("unknown or insufficient cost reservation",now(),job_id))
                return {"status":"blocked", "may_send":False, "reason":"unknown or insufficient cost reservation"}
            if not callable(self.submission_authorizer):
                return {"status":"blocked", "may_send":False, "reason":"independent submission policy/grant not configured"}
            grant = self.submission_authorizer(dict(row))
            if not isinstance(grant,dict) or grant.get("state") != "granted" or not grant.get("grant_ref") or grant.get("job_id") != job_id or grant.get("project_id") != row["project_id"]:
                return {"status":"blocked", "may_send":False, "reason":"independent submission permission denied"}
            scan_secrets(grant)
            request_id = new_id("request")
            self.connection.execute("UPDATE jobs SET state='submission_unknown',request_id=?,reserved_cost_minor=?,updated_at=? WHERE job_id=?", (request_id,row["estimated_cost_minor"],now(),job_id))
            self.event("submission_reserved",job_id,row["project_id"],{"request_id":request_id})
            return {"status":"submission_unknown", "may_send":True, "request_id":request_id, "reserved_cost_minor":row["estimated_cost_minor"]}

    def reconcile_job(self, job_id, *, state, receipt, remote_task_id=None, settled_cost_minor=None):
        if state not in {"submitted", "waiting", "verified_complete", "failed", "blocked", "submission_unknown"} or not receipt:
            raise ContractError("reconciliation requires evidence")
        scan_secrets(receipt)
        with self.transaction():
            row = self.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise ContractError("unknown job")
            if settled_cost_minor is not None and (settled_cost_minor < 0 or settled_cost_minor > row["reserved_cost_minor"]):
                raise ContractError("unreserved settlement")
            # Unknown response never releases the reservation or returns to queued.
            reserved = row["reserved_cost_minor"] if state in {"submission_unknown", "submitted", "waiting"} or settled_cost_minor is None else 0
            self.connection.execute("UPDATE jobs SET state=?,remote_task_id=?,receipt_json=?,reserved_cost_minor=?,settled_cost_minor=?,updated_at=? WHERE job_id=?", (state,remote_task_id,canonical(receipt).decode(),reserved,settled_cost_minor,now(),job_id))
            return dict(self.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def _register_input(self, project_id, path):
        project_root = self.project_root(project_id)
        project_root.mkdir(parents=True, exist_ok=True)
        path = Path(path).absolute()
        reject_links(path)
        # Stat BEFORE opening/hashing: this entry point is not a media downloader.
        size = path.stat().st_size
        guessed_media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if guessed_media.startswith(("audio/","video/")):
            raise ContractError("media unsupported by small-file ingest; use bounded media route")
        if not path.is_file() or size > self.max_file_bytes:
            raise ContractError("local small-file bound exceeded; use bounded media route")
        sha, size = hash_file(path)
        source_revision = "source-" + sha
        asset_revision = "asset-" + sha
        with self.transaction():
            source = self.connection.execute("SELECT * FROM sources WHERE project_id=? AND local_path=?", (project_id,str(path))).fetchone()
            source_id = source["source_id"] if source else new_id("source")
            if not source:
                self.connection.execute("INSERT INTO sources VALUES(?,?,?,?,?)", (source_id,project_id,"local",str(path),now()))
            self.connection.execute("INSERT OR IGNORE INTO source_revisions VALUES(?,?,?,?,?)", (source_id,source_revision,sha,size,now()))
            same = self.connection.execute("SELECT r.* FROM asset_revisions r JOIN assets a USING(asset_id) WHERE a.project_id=? AND r.sha256=? AND NOT EXISTS(SELECT 1 FROM media_sources m WHERE m.asset_id=r.asset_id AND m.asset_revision=r.asset_revision) LIMIT 1", (project_id,sha)).fetchone()
            prior = self.connection.execute("SELECT asset_id FROM source_assets WHERE source_id=? LIMIT 1", (source_id,)).fetchone()
            asset_id = same["asset_id"] if same else prior[0] if prior else new_id("asset")
            self.connection.execute("INSERT OR IGNORE INTO assets VALUES(?,?,?)", (asset_id,project_id,now()))
            suffix = path.suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                suffix = ".bin"
            original_rel = f"原始材料/{asset_id}/{asset_revision}{suffix}"
            _copy(path, safe_path(project_root,original_rel,must_exist=False))
            media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.connection.execute("INSERT OR IGNORE INTO asset_revisions VALUES(?,?,?,?,?,?,?)", (asset_id,asset_revision,sha,size,media_type,original_rel,now()))
            self.connection.execute("INSERT OR IGNORE INTO source_assets VALUES(?,?,?,?)", (source_id,source_revision,asset_id,asset_revision))
            self.event("source_observed",source_id,project_id,{"asset_id":asset_id,"asset_revision":asset_revision})
        return {"asset":{"asset_id":asset_id,"asset_revision":asset_revision,"source_ref":source_id,"media_type":media_type,"size_bytes":size,"source_binding":{"kind":"local","local_input_ref":source_id}}, "original_relative_path":original_rel, "original_sha256":sha}

    def ingest(self, project_id, inputs, extractor, *, options=None, config_version=None):
        if self.pipeline_state("collection") != "running":
            return {"status":"blocked","reason":"collection paused","project_id":project_id}
        if not inputs:
            raise ContractError("at least one actual input is required")
        entries = []
        prepared = []
        for path in inputs:
            entry = self._register_input(project_id,path)
            asset = entry["asset"]
            # The callable itself is explicitly supplied by the local operator.
            callable_name = re.sub(r"[^A-Za-z0-9_.:-]", "-", getattr(extractor,"__name__","extractor"))
            module = sys.modules.get(getattr(extractor,"__module__",""))
            implementation = _config_value(extractor)
            version = getattr(extractor,"provider_version",None) or getattr(module,"PROVIDER_VERSION",None) or "callable-"+digest(implementation)[:40]
            provider_id = getattr(extractor,"provider_id",None) or getattr(module,"PROVIDER_ID",None) or "local-callable-"+callable_name
            config = check_id(config_version) if config_version else "config-"+digest(_config_value(options))[:32]
            executor = self.register_provider(provider_id,version,config_version=config)
            job = self.queue_job(project_id,"native_extract",executor,asset["asset_id"]+":"+asset["asset_revision"],config_version=config,estimated_cost_minor=0)
            if job["state"] in {"submission_unknown","submitted","waiting"}:
                return {"status":"blocked","reason":"existing submission must be reconciled; no automatic resubmit","job_id":job["job_id"]}
            prepared.append((entry,job,config))
        if prepared and all(job["state"] == "verified_complete" and job["receipt_json"] for _,job,_ in prepared):
            wanted_assets = {asset_key(entry["asset"]) for entry,_,_ in prepared}
            wanted_jobs = {job["job_id"] for _,job,_ in prepared}
            # Exact multi-input set reuse: never rerun OCR/render just because
            # nonsemantic elapsed/usage metadata would differ on another run.
            for previous in self.connection.execute("SELECT bundle_id FROM publications WHERE project_id=? AND state='published' ORDER BY revision DESC",(project_id,)).fetchall():
                candidate = self.get_bundle(project_id,previous["bundle_id"])
                if {asset_key(a) for a in candidate["assets"]} == wanted_assets and {a["job_id"] for a in candidate["artifacts"]} == wanted_jobs:
                    return candidate
        from .extraction_cache import Cache
        cache = Cache(self)
        for entry,job,config in prepared:
            restored = cache.restore(entry,job,config)
            if restored is not None:
                self._validate_raw(restored)
                entries.append(restored)
                continue
            asset = entry["asset"]
            outdir = self.root / "下载暂存" / job["job_id"] / new_id("extraction")
            reject_links(outdir)
            outdir.mkdir(parents=True,exist_ok=False)
            with self.transaction():
                # A mixed new/old input set is currently re-parsed, but a new
                # local attempt must not invalidate a previously published job.
                self.connection.execute("UPDATE jobs SET state='running',updated_at=? WHERE job_id=? AND state!='verified_complete'",(now(),job["job_id"]))
            try:
                source = safe_path(self.project_root(project_id),entry["original_relative_path"])
                raw = extractor(source,outdir,options=options)
                if hash_file(source)[0] != entry["original_sha256"]:
                    raise ContractError("original changed during extraction")
                entry.update(raw=raw,output_dir=outdir,job_id=job["job_id"],config_version=config)
                self._validate_raw(entry)
                actual_provider = self.register_provider(raw["provider_id"],raw["provider_version"],config_version=config)
                with self.transaction():
                    if job["state"] == "verified_complete":
                        if json.loads(job["provider_json"]) != actual_provider:
                            raise ContractError("producer changed without configuration/provider version; use a new config_version")
                    else:
                        changed = self.connection.execute("UPDATE jobs SET provider_json=?,updated_at=? WHERE job_id=? AND project_id=? AND operation='native_extract' AND input_revision=? AND state='running'",(canonical(actual_provider).decode(),now(),job["job_id"],project_id,asset["asset_id"]+":"+asset["asset_revision"])).rowcount
                        if changed != 1:
                            raise ContractError("extraction job changed during execution")
            except Exception as exc:
                with self.transaction():
                    self.connection.execute("UPDATE jobs SET state='failed',reason=?,updated_at=? WHERE job_id=? AND state!='verified_complete'", (type(exc).__name__,now(),job["job_id"]))
                raise
            entries.append(entry)
        bundle = self.publish(project_id,entries)
        if bundle.get('bundle_id'):
            for entry in entries:
                cache.remember(entry,bundle)
        return bundle

    def reconcile_image_region(self, request_id, bundle_id):
        from .derived_delivery import reconcile_image_region
        return reconcile_image_region(self,request_id,bundle_id)

    def publish_image_region(self, project_id, source_bundle_id, block_id, artifact_id, bbox, *, output_relative, resize=None, request_id=None):
        from .derived_publication import publish_image_region
        return publish_image_region(self,project_id,source_bundle_id,block_id,artifact_id,bbox,output_relative=output_relative,resize=resize,request_id=request_id)

    def crop_region(self, project_id, bundle_id, block_id, artifact_id, bbox, output_relative, *, resize=None):
        from .image_region import crop_region
        return crop_region(self,project_id,bundle_id,block_id,artifact_id,bbox,output_relative,resize=resize)

    def fulfil_available(self, request_id):
        from .local_supplement import fulfil_available
        return fulfil_available(self, request_id)

    def compose(self, project_id, bundle_ids):
        from .bundle_merge import compose
        return compose(self, project_id, bundle_ids)

    def ingest_media(self, project_id, inputs, extractor=None, *, options=None, config_version=None, resume=False):
        """Publish bounded derived media, not an immutable whole-source copy.

        Default reuse is safe even for partial evidence. Explicit resume continues
        a deliberate checkpoint pause only; unknown IO never auto-replays.
        """
        self.project_root(project_id)
        if self.pipeline_state('collection') != 'running':
            return {'status':'blocked','reason':'collection paused','project_id':project_id}
        if not inputs:
            raise ContractError('at least one media input is required')
        if len(inputs) != 1:
            return {'status':'unsupported','reason':'single-source bounded media is connected; multi-source/native-media Bundle composition is not yet accepted','project_id':project_id}
        if any(not (mimetypes.guess_type(str(p))[0] or '').startswith(('audio/','video/')) for p in inputs):
            return {'status':'blocked','reason':'non-media input; no source bytes read or hashed','project_id':project_id}
        from .media import extract_media, _limits, PROVIDER_ID, PROVIDER_VERSION
        from .media_store import MediaRegistration
        from .policy import Policy
        supplied = dict(options or {})
        expected_root = self.root.resolve(strict=True)
        if supplied.get('allowed_root') is not None and Path(supplied['allowed_root']).resolve() != expected_root:
            raise ContractError('media options cannot change the authorized workspace')
        supplied['allowed_root'] = str(expected_root)
        limits = _limits(supplied)
        if limits['ram_byte_cap'] > Policy().max_ram_bytes or limits['cache_byte_cap'] > Policy().max_cache_bytes:
            raise ContractError('media RAM/cache request exceeds the registered local policy')
        scan_secrets(limits)
        extractor = extractor or extract_media
        module = sys.modules.get(getattr(extractor,'__module__',''))
        provider_id = getattr(extractor,'provider_id',None) or getattr(module,'PROVIDER_ID',None) or PROVIDER_ID
        version = getattr(extractor,'provider_version',None) or getattr(module,'PROVIDER_VERSION',None) or PROVIDER_VERSION
        config = check_id(config_version) if config_version else 'media-config-'+digest([_config_value(extractor),_config_value(limits)])[:40]
        provider = self.register_provider(provider_id,version,config_version=config)
        bridge, entries, cached = MediaRegistration(self), [], []
        for path in inputs:
            if self.pipeline_state('collection') != 'running':
                return {'status':'blocked','reason':'collection paused between media assets','project_id':project_id}
            entry = bridge.register(project_id,path)
            asset = entry['asset']
            job = self.queue_job(project_id,'native_extract',provider,asset['asset_id']+':'+asset['asset_revision'],config_version=config,estimated_cost_minor=0)
            if job['state'] in {'submission_unknown','submitted','waiting'}:
                return {'status':'blocked','reason':'existing submission requires reconciliation','job_id':job['job_id']}
            entry, previous = bridge.execute(project_id,entry,job,extractor,limits,resume=resume)
            if previous is not None:
                cached.append(previous)
                continue
            entry['config_version'] = config
            self._validate_raw(entry)
            actual = self.register_provider(entry['raw']['provider_id'],entry['raw']['provider_version'],config_version=config)
            with self.transaction():
                row = self.connection.execute('SELECT state,provider_json FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
                if row['state'] == 'verified_complete' and json.loads(row['provider_json']) != actual:
                    raise ContractError('media provider changed without a new configuration')
                if row['state'] != 'verified_complete':
                    self.connection.execute('UPDATE jobs SET provider_json=?,updated_at=? WHERE job_id=? AND project_id=? AND operation=? AND state=?',(canonical(actual).decode(),now(),job['job_id'],project_id,'native_extract','running'))
            entries.append(entry)
        if cached and entries or len(cached) > 1:
            raise ContractError('mixed cached media groups require per-asset merge; use individual inputs without replaying them')
        if cached:
            return cached[0]
        bundle = self.publish(project_id,entries)
        if 'bundle_id' in bundle:
            bridge.mark_published(entries,bundle)
        return bundle

    def _validate_raw(self, entry):
        raw = entry["raw"]
        required = {"media_type","provider_id","provider_version","blocks","files","gaps","metrics","enumeration_state","processing_status","metadata"}
        if not isinstance(raw,dict) or not required <= raw.keys():
            raise ContractError("extractor did not return the raw interface")
        check_id(raw["provider_id"])
        if not raw["provider_version"]:
            raise ContractError("extractor version required")
        keys, declared = set(), set()
        total = 0
        from .media_store import MediaRegistration
        media_registration = MediaRegistration(self)
        output_limit = media_registration.output_limit(entry) if media_registration.record(entry['asset']) else self.max_file_bytes * 4
        outdir = Path(entry["output_dir"])
        reject_links(outdir)
        for file in raw["files"]:
            key = file["key"]
            if key in keys:
                raise ContractError("duplicate raw file key")
            keys.add(key)
            rel = validate_relative_path(file["relative_path"])
            if rel.casefold() in declared:
                raise ContractError("duplicate raw output path")
            declared.add(rel.casefold())
            path = safe_path(outdir,rel)
            sha,size = hash_file(path)
            total += size
            if total > output_limit:
                raise ContractError("extraction output bound exceeded")
        actual = set()
        for path in outdir.rglob("*"):
            reject_links(path)
            if path.is_file():
                actual.add(path.relative_to(outdir).as_posix().casefold())
        if declared != actual:
            raise ContractError("raw files must exactly enumerate actual output files")
        for block in raw["blocks"]:
            if not {"kind","origin","locator","file_keys","processing_status","reason"} <= block.keys():
                raise ContractError("incomplete raw block")
            if len(block["file_keys"]) != len(set(block["file_keys"])) or not set(block["file_keys"]) <= keys:
                raise ContractError("raw block references unknown/duplicate file key")
        for gap in raw["gaps"]:
            if any(not isinstance(i,int) or i < 0 or i >= len(raw["blocks"]) for i in gap["block_indexes"]):
                raise ContractError("raw gap block index out of range")

    def _check_extraction_job(self,project_id,ref,job_id,provider,*,published=False,legacy_publish_id=None):
        job = self.connection.execute("SELECT * FROM jobs WHERE job_id=?",(job_id,)).fetchone()
        allowed_revisions = {ref["asset_revision"],ref["asset_id"]+":"+ref["asset_revision"]}
        allowed_states = {"verified_complete"} if published else {"running","verified_complete"}
        if not job or job["project_id"] != project_id or job["operation"] != "native_extract" or job["input_revision"] not in allowed_revisions or job["state"] not in allowed_states:
            raise ContractError("extraction job project/operation/input revision/state mismatch")
        recorded = json.loads(job["provider_json"])
        if recorded != provider:
            # Read-only compatibility for already-published pre-hardening local
            # callable executions. Their sealed Plan names the actual provider;
            # no pending/new publication can use this exception.
            receipt = json.loads(job["receipt_json"] or "{}")
            legacy = published and legacy_publish_id and receipt.get("publish_id") == legacy_publish_id and recorded.get("provider_id","").startswith("local-callable-") and recorded.get("implementation_version","").startswith(("operator-supplied-","callable-"))
            if not legacy:
                raise ContractError("extraction job provider execution mismatch")
        return job

    def _catalog_revision(self, project_id):
        rows = self.connection.execute("SELECT r.source_id,r.revision FROM source_revisions r JOIN sources s USING(source_id) WHERE s.project_id=? ORDER BY r.source_id,r.revision", (project_id,)).fetchall()
        return "catalog-" + digest([tuple(r) for r in rows])[:40]

    def publish(self, project_id, asset_extractions):
        if self.pipeline_state("collection") != "running":
            return {"status":"blocked","reason":"collection paused","project_id":project_id}
        root = self.project_root(project_id)
        entries = []
        for given in asset_extractions:
            entry = dict(given)
            if "asset" not in entry:
                base = self._register_input(project_id,entry["path"])
                base.update(raw=entry.get("raw",entry.get("extraction")),output_dir=entry["output_dir"])
                for optional in ("job_id","config_version"):
                    if optional in entry:
                        base[optional] = entry[optional]
                entry = base
            self._validate_raw(entry)
            raw = entry["raw"]
            provider = self.register_provider(raw["provider_id"],raw["provider_version"],config_version=entry.get("config_version","default"))
            entry["provider"] = provider
            if "job_id" not in entry:
                queued = self.queue_job(project_id,"native_extract",provider,entry["asset"]["asset_id"]+":"+entry["asset"]["asset_revision"],estimated_cost_minor=0)
                entry["job_id"] = queued["job_id"]
                if queued["state"] == "queued":
                    with self.transaction():
                        self.connection.execute("UPDATE jobs SET state='running',updated_at=? WHERE job_id=? AND project_id=? AND operation='native_extract' AND state='queued'",(now(),entry["job_id"],project_id))
            self._check_extraction_job(project_id,entry["asset"],entry["job_id"],provider)
            # Do not accept a caller-supplied asset from another project.
            row = self.connection.execute("SELECT r.* FROM assets a JOIN asset_revisions r USING(asset_id) JOIN source_assets sa USING(asset_id,asset_revision) JOIN sources s USING(source_id) WHERE a.project_id=? AND r.asset_id=? AND r.asset_revision=? AND s.source_id=? AND s.project_id=?", (project_id,*asset_key(entry["asset"]),entry["asset"]["source_ref"],project_id)).fetchone()
            if not row:
                raise ContractError("publication asset/source revision not owned by project")
            from .media_store import MediaRegistration
            registration = MediaRegistration(self)
            if registration.record(entry['asset']):
                registration.validate(project_id,entry['asset'],row,require_current=True)
                if entry.get('original_kind') != 'inventory' or entry.get('original_media_type') != 'application/json':
                    raise ContractError('media registration descriptor cannot masquerade as an original audio/video file')
            elif entry["original_relative_path"] != row["original_relative_path"] or hash_file(safe_path(root,entry["original_relative_path"])) != (row["sha256"],row["size_bytes"]):
                raise ContractError("publication original does not match registered source revision")
            if entry["asset"]["source_binding"].get("kind") != "local":
                raise ContractError("local registration cannot claim an unverified remote source binding")
            entries.append(entry)
        if not entries:
            raise ContractError("cannot publish empty fake evidence")
        # Duplicate inputs are a set, but conflicting extractions must never
        # silently replace one another merely because their source bytes match.
        grouped = {}
        for e in entries:
            ident = asset_key(e["asset"])
            file_state = [(f["key"],hash_file(safe_path(Path(e["output_dir"]),f["relative_path"]))) for f in e["raw"]["files"]]
            fingerprint = digest([e["job_id"],e["raw"],file_state])
            if ident in grouped:
                old,old_fingerprint = grouped[ident]
                if fingerprint != old_fingerprint:
                    raise ContractError("conflicting duplicate asset extraction")
                if e["asset"]["source_ref"] < old["asset"]["source_ref"]:
                    grouped[ident] = (e,fingerprint)
            else:
                grouped[ident] = (e,fingerprint)
        entries = [grouped[key][0] for key in sorted(grouped)]
        fingerprints = []
        for e in entries:
            files = [(f["key"],hash_file(safe_path(Path(e["output_dir"]),f["relative_path"]))[0]) for f in e["raw"]["files"]]
            fingerprints.append([e["asset"],e["job_id"],e["provider"],e["raw"],files])
        key = digest([project_id,self._catalog_revision(project_id),fingerprints])
        with self.transaction():
            old = self.connection.execute("SELECT * FROM publications WHERE project_id=? AND idempotency_key=?", (project_id,key)).fetchone()
            if old:
                if old["state"] != "published":
                    raise ContractError("publication already registered; recover/reconcile its publish_id")
                return self.get_bundle(project_id,old["bundle_id"])
            revision = self.connection.execute("SELECT COALESCE(MAX(revision),0)+1 FROM publications WHERE project_id=?",(project_id,)).fetchone()[0]
            publish_id,bundle_id = new_id("publish"),new_id("bundle")
            final_rel = f"证据/v{revision:04d}"
            staging_rel = f"证据/.staging-{publish_id}"
            self.connection.execute("INSERT INTO publications(publish_id,project_id,bundle_id,revision,idempotency_key,state,staging_relative_path,final_relative_path,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (publish_id,project_id,bundle_id,revision,key,"staging",staging_rel,final_rel,now()))
            self.event("publish_registered",publish_id,project_id)
        stage = root / staging_rel
        reject_links(stage)
        stage.mkdir(parents=True,exist_ok=False)
        bundle,plan = self._build_bundle(project_id,entries,publish_id,bundle_id,revision,final_rel,stage)
        # Canonical manifest and every byte hash are durably recorded before rename.
        manifest = stage / "manifest.json"
        _write(manifest,canonical(bundle))
        expected = {a["relative_path"]:{"sha256":a["sha256"],"byte_length":a["byte_length"]} for a in bundle["artifacts"]}
        expected[final_rel+"/manifest.json"] = dict(zip(("sha256","byte_length"),hash_file(manifest)))
        with self.transaction():
            self.connection.execute("INSERT INTO plans VALUES(?,?,?,?)",(plan["plan_id"],project_id,canonical(plan).decode(),now()))
            self.connection.execute("UPDATE publications SET state='validated',manifest_sha256=?,bundle_json=?,expected_files_json=? WHERE publish_id=?", (hash_file(manifest)[0],canonical(bundle).decode(),canonical(expected).decode(),publish_id))
        self._finish_publication(publish_id)
        return self.get_bundle(project_id,bundle_id)

    def _build_bundle(self, project_id, entries, publish_id, bundle_id, revision, final_rel, stage):
        root = self.project_root(project_id)
        created = now()
        report_rel = final_rel+"/validation.json"
        receipt = {"status":"passed","validator_version":VALIDATOR_VERSION,"checked_at":created,"report_relative_path":report_rel}
        bundle = {"schema_version":"1.0-draft","bundle_id":bundle_id,"project_id":project_id,
            "bundle_revision":f"v{revision:04d}","source_catalog_revision":self._catalog_revision(project_id),
            "publish_id":publish_id,"created_at":created,
            "publication":{"state":"published","journal_ref":publish_id,"manifest_relative_path":final_rel+"/manifest.json","published_at":created},
            "assets":[],"artifacts":[],"blocks":[],"coverage":{},"gaps":[],"contacts":[],"program_validation":receipt}
        media_caps = [e['media_limits'] for e in entries if e.get('media_registration')]
        policy_ref = 'bounded-local-media-v1' if media_caps else 'local-small-file-v1'
        budget = {"policy_ref":policy_ref,"currency":"CNY","max_cost_minor":0,"max_input_bytes":sum(int(c['input_byte_cap']) for c in media_caps)+self.max_file_bytes*(len(entries)-len(media_caps)),"max_cache_bytes":max([self.max_file_bytes*4]+[int(c['final_evidence_byte_cap'])+int(c['cache_byte_cap']) for c in media_caps]),"max_ram_bytes":max([512*1024*1024]+[int(c['ram_byte_cap']) for c in media_caps]),"max_vram_bytes":0,"max_wall_seconds":max([3600]+[int(c['wall_timeout_seconds']) for c in media_caps]),"max_concurrency":1}
        plan = {"schema_version":"1.0-draft","plan_id":new_id("plan"),"project_id":project_id,"created_at":created,"policy_version":policy_ref,"provider_registry_revision":"local-registered-v1","capability_observations":[],"permission_checks":[],"hard_budget":budget,"asset_plans":[],"program_validation":receipt}
        def artifact(entry,physical,rel,kind,media,restricted=False,ident=None):
            sha,size = hash_file(physical)
            item = {"artifact_id":ident or new_id("artifact"),"artifact_revision":"sha256-"+sha,"asset_ref":{k:entry["asset"][k] for k in ("asset_id","asset_revision")},"job_id":entry["job_id"],"kind":kind,"relative_path":rel,"media_type":media,"byte_length":size,"sha256":sha,"created_at":created,"access_class":"restricted_local" if restricted else "project_local","provider":entry["provider"]}
            bundle["artifacts"].append(item)
            return item
        per_asset = []
        for entry in entries:
            raw,asset,provider = entry["raw"],deepcopy(entry["asset"]),entry["provider"]
            asset["media_type"] = raw["media_type"]
            bundle["assets"].append(asset)
            ref = {k:asset[k] for k in ("asset_id","asset_revision")}
            original_path = safe_path(root,entry["original_relative_path"])
            # Originals are never directly put into an LLM envelope; scan readable text.
            try:
                original_restricted = contains_sensitive(original_path.read_text(encoding="utf-8"))
            except (UnicodeError,ValueError):
                original_restricted = False
            original = artifact(entry,original_path,entry["original_relative_path"],entry.get('original_kind','original_small_file'),entry.get('original_media_type',raw['media_type']),original_restricted)
            filemap = {}
            for i,file in enumerate(raw["files"]):
                src = safe_path(Path(entry["output_dir"]),file["relative_path"])
                suffix = src.suffix.lower()
                if not re.fullmatch(r"\.[a-z0-9]{1,10}",suffix):
                    suffix = ".bin"
                rel = f"{final_rel}/files/{asset['asset_id']}-{i}{suffix}"
                physical = stage / rel[len(final_rel)+1:]
                _copy(src,physical)
                restricted = bool(raw["metadata"].get("restricted_local"))
                if file["media_type"].startswith("text/") or file["media_type"] in {"application/json","application/x-ndjson"}:
                    try:
                        restricted = restricted or contains_sensitive(src.read_text(encoding="utf-8"))
                    except UnicodeError:
                        raise ContractError("invalid raw text artifact")
                filemap[file["key"]] = artifact(entry,physical,rel,file["kind"],file["media_type"],restricted)
            # Preserve parser placement/transform and diagnostic metadata as
            # real hashed evidence; never discard it merely for schema shape.
            metadata_rel = f"{final_rel}/metadata/{asset['asset_id']}.json"
            metadata_path = stage / metadata_rel[len(final_rel)+1:]
            metadata_bytes = canonical(raw["metadata"])
            _write(metadata_path,metadata_bytes)
            metadata_artifact = artifact(entry,metadata_path,metadata_rel,"structured_data","application/json",contains_sensitive(metadata_bytes.decode("utf-8")))
            own = []
            restricted_indexes = []
            for index,b in enumerate(raw["blocks"]):
                ident = new_id("block")
                loc = deepcopy(b["locator"])
                if "geometry" in loc:
                    key = loc["geometry"]["reference_object_ref"]
                    if key not in filemap:
                        raise ContractError("raw geometry must reference a file key")
                    loc["geometry"]["reference_object_ref"] = filemap[key]["artifact_id"]
                artifact_ids = [filemap[k]["artifact_id"] for k in b["file_keys"]]
                block = {"block_id":ident,"asset_ref":ref,"kind":b["kind"],"origin":b["origin"],"locator":loc,"artifact_ids":artifact_ids,
                    "processing":{"status":b["processing_status"],"reason":redact_text(b["reason"])[0] or "extraction observation","evidence_refs":[],"recovery_ref":entry["job_id"]},"derived_from":[]}
                contact_text = ''
                if "text" in b:
                    text,sensitive = redact_text(str(b["text"]))
                    contact_text = text
                    if sensitive:
                        original["access_class"] = "restricted_local"
                        restricted_indexes.append(index)
                    rel = f"{final_rel}/text/{ident}.txt"
                    physical = stage / rel[len(final_rel)+1:]
                    _write(physical,text.encode("utf-8"))
                    textart = artifact(entry,physical,rel,"native_text","text/plain")
                    block["artifact_ids"].append(textart["artifact_id"])
                    block["text"] = text[:16000]
                    if len(text) > 16000:
                        block["uncertainty"] = "Inline text is a 16000-character preview; the referenced native_text artifact contains the full text and analysis.read reads it completely."
                    if sensitive:
                        block["uncertainty"] = "Sensitive line withheld; corresponding bounded raw/original evidence retained restricted_local. Media registration is not a whole-source copy."
                if block["processing"]["status"] == "extracted" and not block["artifact_ids"]:
                    block["artifact_ids"] = [original["artifact_id"]]
                if b["origin"] in {"ocr","asr","visual_observation","provider_ai_note","model_inference"}:
                    block["producer"] = provider
                    block["uncertainty"] = "Derived extraction, not independently semantically verified."
                    block["derived_from"] = [{"project_id":project_id,**ref,"artifact_id":original["artifact_id"],"locator":{"kind":"asset","precision":"exact"}}]
                # Keep literal contact evidence with unknown attribution; a
                # repeated identifier is never automatically called promotion.
                # Use the complete redacted source, never only the inline preview.
                candidates = set(re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", contact_text))
                candidates.update(re.findall(r"(?:微信|[Ww]e[Cc]hat|[Qq][Qq]|客服|联系方式|邮箱|电话|手机)[：: ]+([^\s，,；;。]{2,100})", contact_text))
                candidates.update(re.findall(r"(?<!\d)1[3-9]\d{9}(?!\d)", contact_text))
                for candidate in sorted(candidates):
                    if contains_sensitive(candidate):
                        continue
                    contact_id = new_id("contact")
                    rel = f"{final_rel}/contacts/{contact_id}.txt"
                    physical = stage / rel[len(final_rel)+1:]
                    _write(physical,candidate.encode("utf-8"))
                    value = artifact(entry,physical,rel,"contacts","text/plain",True)
                    bundle["contacts"].append({"contact_id":contact_id,"subject_ref":None,"role":"unknown","value_artifact_id":value["artifact_id"],"evidence_refs":[{"project_id":project_id,**ref,"block_id":ident,"locator":loc}],"attribution_status":"unknown","classification":"contact","classification_reason":"Literal contact candidate retained with original block; author/support/distributor attribution is unproven. Repetition alone is not promotion evidence.","classification_evidence_refs":[],"disposition":"restricted_local"})
                own.append(block)
                bundle["blocks"].append(block)
            # Derivations name actual source occurrences/files and the real
            # OCR/ASR provider, not the outer document adapter version.
            for derivation in raw["metadata"].get("derivations",[]):
                producer = self.register_provider(derivation["provider_id"],derivation["provider_version"])
                sources = []
                for index in derivation.get("source_block_indexes",[]):
                    if not isinstance(index,int) or not 0 <= index < len(own):
                        raise ContractError("derivation source block index out of range")
                    sources.append({"project_id":project_id,**ref,"block_id":own[index]["block_id"],"locator":own[index]["locator"]})
                for key in derivation.get("source_file_keys",[]):
                    if key not in filemap:
                        raise ContractError("derivation source file key missing")
                    sources.append({"project_id":project_id,**ref,"artifact_id":filemap[key]["artifact_id"],"locator":{"kind":"asset","precision":"exact"}})
                for index in derivation["block_indexes"]:
                    if not isinstance(index,int) or not 0 <= index < len(own):
                        raise ContractError("derivation target block index out of range")
                    own[index]["producer"] = producer
                    own[index]["uncertainty"] = redact_text(derivation.get("uncertainty") or "Derived content; independent checking remains necessary.")[0]
                    if sources:
                        own[index]["derived_from"] = deepcopy(sources)
            gap_ids = []
            def gap(kind,severity,reason,indexes):
                item = {"gap_id":new_id("gap"),"kind":kind,"severity":severity,"status":"open","reason":redact_text(reason)[0],"target_refs":[{"project_id":project_id,**ref,"block_id":own[i]["block_id"],"locator":own[i]["locator"]} for i in indexes],"resolution_refs":[]}
                gap_ids.append(item["gap_id"])
                bundle["gaps"].append(item)
            for g in raw["gaps"]:
                gap(g["kind"],g["severity"],g["reason"],g["block_indexes"])
            if restricted_indexes:
                gap("access","informational","Credential/private URL lines withheld from model-visible text; bounded raw evidence retained restricted_local, not deleted. Original media is stat-registered rather than fully copied." if entry.get('media_registration') else "Credential/private URL lines withheld from model-visible text; exact source bytes retained restricted_local, not deleted.",restricted_indexes)
            visual_indexes = [i for i,b in enumerate(own) if b["kind"] in {"image_occurrence","chart"}]
            if visual_indexes:
                gap("semantic_gap","important","Images preserved; image meaning has not been read by the analysis agent.",visual_indexes)
                ocr_sources = {r.get("block_id") for b in own if b["origin"] == "ocr" for r in b["derived_from"]}
                no_ocr = [i for i in visual_indexes if own[i]["block_id"] not in ocr_sources]
                if no_ocr:
                    gap("unprocessed","important","OCR not executed or image textual-content applicability not established; original pixels remain available.",no_ocr)
            if raw["enumeration_state"] != "complete" and not any(g["kind"] == "unknown_inventory" for g in raw["gaps"]):
                gap("unknown_inventory","important","Extraction inventory is not proven complete.",[])
            processed = sum(b["processing"]["status"] == "extracted" for b in own)
            metrics = [m for m in deepcopy(raw["metrics"]) if m["unit"] not in {"block","visual_occurrence"}]
            metrics += [{"unit":"block","expected":len(own) if raw["enumeration_state"] == "complete" else None,"observed":len(own),"processed":processed,"excluded":sum(b["processing"]["status"] == "excluded" for b in own),"method":"structured_parse","ranges":[]}]
            if visual_indexes:
                metrics.append({"unit":"visual_occurrence","expected":len(visual_indexes) if raw["enumeration_state"] == "complete" else None,"observed":len(visual_indexes),"processed":sum(own[i]["processing"]["status"] == "extracted" for i in visual_indexes),"excluded":sum(own[i]["processing"]["status"] == "excluded" for i in visual_indexes),"method":"structured_parse","ranges":[]})
            status = raw["processing_status"]
            if status == "verified_complete" and (gap_ids or any(b["processing"]["status"] not in {"extracted","not_applicable"} for b in own)):
                status = "partial"
            per_asset.append({"asset_ref":ref,"enumeration_state":raw["enumeration_state"],"processing_status":status,"metrics":metrics,"gap_ids":gap_ids})
            observation,permission = new_id("observation"),new_id("permission")
            evidence = {"kind":"program_receipt","record_ref":entry["job_id"],"observed_at":created}
            plan["capability_observations"].append({"observation_id":observation,"provider":provider,"scope":{"source_ref":asset["source_ref"],"asset_ref":ref},"capability":"native_parse","state":"verified_available" if own else "unsupported","observed_at":created,"evidence_refs":[evidence],"limitations":["Local operator supplied extractor; extraction is not semantic reading."]})
            plan["permission_checks"].append({"check_id":permission,"operation":"native_extract","scope_ref":asset["asset_id"],"state":"granted","grant_ref":"local-input-"+asset["source_ref"],"checked_at":created,"reason":"Explicit local input; stat descriptor plus independently hashed derived artifacts; no whole-media copy/hash." if entry.get('media_registration') else "Explicit local input; immutable copy; no remote side effects."})
            plan["asset_plans"].append({"asset_ref":ref,"source_ref":asset["source_ref"],"media_type":raw["media_type"],"format_evidence_refs":[evidence,{"kind":"local_probe","record_ref":metadata_artifact['artifact_id'],"observed_at":created}] if entry.get('media_registration') else [evidence],"goals":["transcript","visual_events","metadata"] if entry.get('media_registration') else ["text","images","tables"],"jobs":[{"job_id":entry["job_id"],"operation":"native_extract","tier":"native","reason":"Preserve all discovered local content and gaps.","basis_refs":[evidence],"provider":provider,"selection":{"mode":"all_discovered_content","locators":[],"reason":"Discovered bounded audio windows and explicitly sparse visual samples; does not claim complete visual coverage." if entry.get('media_registration') else "All discovered objects, not top-k sampling."},"capability_observation_ids":[observation],"permission_check_ids":[permission],"budget":budget,"estimate":{"state":"measured","input_bytes":raw['metadata'].get('input_bytes_received') if entry.get('media_registration') else asset["size_bytes"],"peak_ram_bytes":raw['metadata'].get('ram',{}).get('peak_worker_commit_bytes') if entry.get('media_registration') else None,"peak_vram_bytes":None,"cache_bytes":None,"wall_seconds":None,"cost_minor":0,"basis_refs":[evidence]},"side_effect":"local_read_or_derivation","depends_on_job_ids":[],"fallback_job_ids":[],"stop_conditions":["permission_denied","scope_changed","budget_exhausted","deterministic_failure","submission_unknown"],"acceptance_checks":["actual_format","actual_files_readable","all_discovered_accounted","program_receipt"],"retry_policy":{"max_attempts":1,"max_route_upgrades":0,"repeat_deterministic_failure":False}}]})
        states = [a["enumeration_state"] for a in per_asset]
        inventory = "complete" if all(s == "complete" for s in states) else "unknown" if all(s == "unknown" for s in states) else "partial"
        bundle["coverage"] = {"scope":"listed_assets","inventory_state":inventory,"claimed_status":"verified_complete" if inventory == "complete" and not bundle["gaps"] and all(a["processing_status"] == "verified_complete" for a in per_asset) else "partial","discovered_counts":{"assets":len(entries),"blocks":len(bundle["blocks"]),"visual_occurrences":sum(b["kind"] in {"image_occurrence","chart"} for b in bundle["blocks"])},"per_asset":per_asset}
        # These files are evidence too; none are unmanifested side products.
        first = entries[0]
        for filename,data,kind,media in (("plan.json",canonical(plan),"structured_data","application/json"),("blocks.jsonl",b"".join(canonical(b)+b"\n" for b in bundle["blocks"]),"structured_data","application/x-ndjson"),("validation.json",canonical({"validator_version":VALIDATOR_VERSION,"publish_id":publish_id,"checked_at":created,"status":"passed","checks":["schema","files","identity","sets","references","geometry","coverage","secrets"]}),"validation_report","application/json")):
            path = stage / filename
            _write(path,data)
            artifact(first,path,final_rel+"/"+filename,kind,media)
        validate_plan(plan,registered_providers={(e["provider"]["provider_id"],e["provider"]["implementation_version"]) for e in entries})
        def resolver(rel):
            if rel.startswith(final_rel+"/"):
                return safe_path(stage,rel[len(final_rel)+1:])
            return safe_path(root,rel)
        validate_bundle(bundle,root,path_resolver=resolver)
        return bundle,plan

    def _verify_publication(self, row, *, staging=False):
        if not row["bundle_json"] or not row["manifest_sha256"] or not row["expected_files_json"]:
            raise ContractError("unprepared publication")
        root = self.project_root(row["project_id"])
        base_rel = row["staging_relative_path"] if staging else row["final_relative_path"]
        base = root / base_rel
        reject_links(base)
        final_rel = row["final_relative_path"]
        def resolver(rel):
            if rel.startswith(final_rel+"/"):
                return safe_path(base,rel[len(final_rel)+1:])
            return safe_path(root,rel)
        expected = json.loads(row["expected_files_json"])
        for rel,item in expected.items():
            if hash_file(resolver(rel)) != (item["sha256"],item["byte_length"]):
                raise ContractError("publication file/manifest hash mismatch")
        manifest = resolver(final_rel+"/manifest.json")
        bundle = json.loads(manifest.read_text(encoding="utf-8"))
        if canonical(bundle).decode() != row["bundle_json"] or bundle["publish_id"] != row["publish_id"] or bundle["project_id"] != row["project_id"] or bundle["bundle_id"] != row["bundle_id"] or bundle["bundle_revision"] != f"v{row['revision']:04d}":
            raise ContractError("publication identity/manifest mismatch")
        actual = set()
        for file in base.rglob("*"):
            reject_links(file)
            if file.is_file():
                actual.add(final_rel+"/"+file.relative_to(base).as_posix())
        declared = {r for r in expected if r.startswith(final_rel+"/")}
        if actual != declared:
            raise ContractError("unmanifested/missing publication file")
        validate_bundle(bundle,root,path_resolver=resolver)
        plan_artifact = next((a for a in bundle["artifacts"] if a["relative_path"] == final_rel+"/plan.json"),None)
        if not plan_artifact:
            raise ContractError("publication execution Plan missing")
        sealed_plan = json.loads(resolver(plan_artifact["relative_path"]).read_bytes())
        planned_jobs = {j["job_id"]:j for p in sealed_plan["asset_plans"] for j in p["jobs"]}
        for artifact in bundle["artifacts"]:
            planned = planned_jobs.get(artifact["job_id"])
            if not planned or planned["operation"] != "native_extract" or planned["provider"] != artifact.get("provider"):
                raise ContractError("artifact execution does not match sealed Plan")
            self._check_extraction_job(row["project_id"],artifact["asset_ref"],artifact["job_id"],artifact["provider"],published=row["state"] == "published",legacy_publish_id=row["publish_id"] if planned["provider"] == artifact["provider"] else None)
        for asset in bundle["assets"]:
            bound = self.connection.execute("SELECT r.* FROM asset_revisions r JOIN assets a USING(asset_id) JOIN source_assets sa USING(asset_id,asset_revision) JOIN sources s USING(source_id) WHERE a.project_id=? AND r.asset_id=? AND r.asset_revision=? AND s.source_id=? AND s.project_id=?", (row["project_id"],asset["asset_id"],asset["asset_revision"],asset["source_ref"],row["project_id"])).fetchone()
            from .media_store import MediaRegistration
            registration = MediaRegistration(self)
            if registration.record(asset):
                registration.validate(row['project_id'],asset,bound,require_current=False)
                originals = [a for a in bundle['artifacts'] if a['kind'] == 'inventory' and asset_key(a['asset_ref']) == asset_key(asset) and a['relative_path'] == bound['original_relative_path']]
                if len(originals) != 1 or originals[0]['sha256'] != bound['sha256'] or originals[0]['media_type'] != 'application/json':
                    raise ContractError('published media descriptor identity mismatch')
            else:
                originals = [a for a in bundle["artifacts"] if a["kind"] == "original_small_file" and asset_key(a["asset_ref"]) == asset_key(asset)]
                if not bound or asset["size_bytes"] != bound["size_bytes"] or len(originals) != 1 or originals[0]["relative_path"] != bound["original_relative_path"] or originals[0]["sha256"] != bound["sha256"]:
                    raise ContractError("published asset/source/original identity inconsistent with ledger")
        return bundle

    def _after_rename(self, publish_id):
        """Fault-injection seam: no business effect, used to test crash recovery."""

    def _finish_publication(self,publish_id):
        row = self.connection.execute("SELECT * FROM publications WHERE publish_id=?",(publish_id,)).fetchone()
        root = self.project_root(row["project_id"])
        stage,final = root / row["staging_relative_path"],root / row["final_relative_path"]
        if final.exists():
            if stage.exists():
                raise ContractError("both staged and renamed publication exist")
            self._verify_publication(row)
        else:
            self._verify_publication(row,staging=True)
            reject_links(final)
            # Same parent and volume; destination must not already be present.
            if stage.parent.stat().st_dev != final.parent.stat().st_dev:
                raise ContractError("publication rename crosses volumes")
            stage.rename(final)
            self._after_rename(publish_id)
        with self.transaction():
            self.connection.execute("UPDATE publications SET state='renamed' WHERE publish_id=? AND state!='published'",(publish_id,))
        row = self.connection.execute("SELECT * FROM publications WHERE publish_id=?",(publish_id,)).fetchone()
        bundle = self._verify_publication(row)
        for path in final.rglob("*"):
            if path.is_file():
                _readonly(path)
        with self.transaction():
            bundle = self._verify_publication(row)
            self.connection.execute("UPDATE publications SET state='published',published_at=?,reason=NULL WHERE publish_id=?",(now(),publish_id))
            job_artifacts = {a["job_id"]:a for a in bundle["artifacts"]}
            for job,artifact in job_artifacts.items():
                ref = artifact["asset_ref"]
                existing = self.connection.execute('SELECT state,receipt_json FROM jobs WHERE job_id=?',(job,)).fetchone()
                if existing and existing['state'] == 'verified_complete':
                    self._check_extraction_job(row['project_id'],ref,job,artifact['provider'],published=True)
                    try:
                        receipt = json.loads(existing['receipt_json'] or '{}')
                    except (ValueError,TypeError) as exc:
                        raise ContractError('invalid published extraction receipt') from exc
                    previous = receipt.get('publish_id') if isinstance(receipt,dict) else None
                    if not isinstance(previous,str) or not previous:
                        raise ContractError('published extraction receipt missing')
                    if previous != publish_id:
                        owner = self.connection.execute("SELECT 1 FROM publications WHERE publish_id=? AND project_id=? AND state='published'",(previous,row['project_id'])).fetchone()
                        if not owner:
                            raise ContractError('published extraction receipt scope/state mismatch')
                        continue
                changed = self.connection.execute("UPDATE jobs SET state='verified_complete',receipt_json=?,updated_at=? WHERE job_id=? AND project_id=? AND operation='native_extract' AND state IN ('running','verified_complete') AND input_revision IN (?,?) AND provider_json=?",(canonical({"publish_id":publish_id}).decode(),now(),job,row["project_id"],ref["asset_revision"],ref["asset_id"]+":"+ref["asset_revision"],canonical(artifact["provider"]).decode())).rowcount
                if changed != 1:
                    raise ContractError("extraction job changed before publication commit")
            if not self.connection.execute("SELECT 1 FROM events WHERE kind='bundle_published' AND record_id=?",(publish_id,)).fetchone():
                self.event("bundle_published",publish_id,row["project_id"],{"bundle_id":bundle["bundle_id"]})
        return bundle

    def recover_publications(self):
        results = []
        for row in self.connection.execute("SELECT * FROM publications ORDER BY project_id,revision").fetchall():
            if row["state"] == "quarantined":
                continue
            try:
                if row["state"] == "published":
                    self._verify_publication(row)
                elif row["bundle_json"]:
                    self._finish_publication(row["publish_id"])
                else:
                    raise ContractError("incomplete staging; extractor must be explicitly retried")
                results.append({"publish_id":row["publish_id"],"state":"published"})
            except (ContractError,OSError,ValueError,KeyError) as exc:
                # Never import an orphan directory or guess its project/version.
                with self.transaction():
                    self.connection.execute("UPDATE publications SET state='quarantined',reason=? WHERE publish_id=?",(type(exc).__name__+": "+str(exc)[:300],row["publish_id"]))
                    self.event("publication_quarantined",row["publish_id"],row["project_id"])
                results.append({"publish_id":row["publish_id"],"state":"quarantined"})
        return results

    def recover_analysis_publications(self):
        # Local import avoids an import cycle; reconciliation invokes no model.
        from .analysis import Analysis
        return Analysis(self)._recover_submissions()

    def get_bundle(self,project_id,bundle_id=None):
        self.project_root(project_id)
        row = self.connection.execute("SELECT * FROM publications WHERE project_id=? AND state='published'"+(" AND bundle_id=?" if bundle_id else "")+" ORDER BY revision DESC LIMIT 1", (project_id,bundle_id) if bundle_id else (project_id,)).fetchone()
        if not row:
            raise ContractError("no database-published bundle")
        return self._verify_publication(row)

    def list_bundles(self,project_id=None):
        query = "SELECT project_id,bundle_id,publish_id,revision,state,reason FROM publications"
        return [dict(r) for r in self.connection.execute(query+(" WHERE project_id=?" if project_id else "")+" ORDER BY project_id,revision",(project_id,) if project_id else ())]

    def list_supplements(self, project_id=None):
        return [dict(r) for r in self.connection.execute("SELECT * FROM supplement_requests"+(" WHERE project_id=?" if project_id else "")+" ORDER BY created_at",(project_id,) if project_id else ())]
