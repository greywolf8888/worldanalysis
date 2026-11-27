"""Two-pass platform acquisition planning on the existing Store ledger.

This module performs no network, browser, generation, ASR or frame capture.
Operators supply observed inventory/product receipts. File readability is checked
locally; source identity and content coverage are never inferred from filenames.
"""
from __future__ import annotations

import json
import hashlib
import re

from .contracts import ContractError, canonical, check_id, digest, now, safe_path, scan_secrets
from .platform_identity import content_identity, queue_content_job
from .platform_materials import _read
from .policy import GateError, Policy

PRODUCTS = ("manuscript", "courseware")
PENDING = {"waiting", "submission_unknown"}
DENIED = {"access_denied", "login_required", "identity_mismatch"}
GENERATION_BLOCKS = {"quota_exhausted", "payment_required"}
STATES = {"present", "missing", "failed", *PENDING, *DENIED, *GENERATION_BLOCKS}


def _fields(value, allowed, required):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        raise ContractError("missing or unknown platform-pass fields")


def _bounded(value, maximum=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ContractError("invalid platform-pass text")
    scan_secrets(value)
    if "://" in value:
        raise ContractError("use opaque evidence references, not URLs")
    return value


def _integer(value):
    if type(value) is not int or value < 0:
        raise ContractError("expected nonnegative integer")
    return value


class PlatformFirst:
    """Frozen directory runs and per-product decisions; no second database."""

    def __init__(self, store, policy=None, allow_synthetic=False, *, config_version="platform-first-v1"):
        self.store = store
        self.config_version = check_id(config_version)
        self.policy = policy or Policy()
        self.allow_synthetic = allow_synthetic is True
        store.connection.executescript("""
          CREATE TABLE IF NOT EXISTS platform_first_runs(
            run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects,
            inventory_json TEXT NOT NULL, inventory_sha256 TEXT NOT NULL,
            state TEXT NOT NULL, sealed INTEGER NOT NULL DEFAULT 0,
            generation_enabled INTEGER NOT NULL DEFAULT 1, reason TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS platform_first_products(
            run_id TEXT NOT NULL REFERENCES platform_first_runs,
            file_id TEXT NOT NULL, product TEXT NOT NULL,
            observation_json TEXT, artifact_json TEXT,
            generation_job_id TEXT REFERENCES jobs,
            PRIMARY KEY(run_id,file_id,product));
        """)

    def _inventory(self, value):
        _fields(value, {"schema_version", "inventory_id", "account_ref", "root_path", "enumeration_complete", "evidence_ref", "evidence_class", "files"},
                {"schema_version", "inventory_id", "account_ref", "root_path", "enumeration_complete", "evidence_ref", "evidence_class", "files"})
        if value["schema_version"] != "1.0" or type(value["enumeration_complete"]) is not bool:
            raise ContractError("invalid inventory version/completeness")
        for key in ("inventory_id", "account_ref", "evidence_ref"):
            check_id(value[key])
        if value["evidence_class"] not in {"real_authorized_browser", "synthetic"}:
            raise ContractError("unsupported inventory evidence class")
        if value["evidence_class"] == "synthetic" and not self.allow_synthetic:
            raise GateError("SYNTHETIC_OBSERVATION_NOT_ALLOWED")
        root = self.policy.require_cloud_path(value["root_path"])
        files = value["files"]
        if not isinstance(files, list) or len(files) > 10000:
            raise ContractError("invalid inventory file count")
        seen, paths, normalized = set(), set(), []
        for item in files:
            _fields(item, {"file_id", "path", "source_revision", "size_bytes", "duration_ms"}, {"file_id", "path", "source_revision", "size_bytes"})
            fid = item["file_id"]
            path = self.policy.require_cloud_path(item["path"])
            if not isinstance(fid, str) or not re.fullmatch(r"[0-9]{1,32}", fid) or fid in seen or path.casefold() in paths or not path.startswith(root + "/"):
                raise ContractError("duplicate/invalid inventory identity or out-of-run path")
            check_id(item["source_revision"])
            _integer(item["size_bytes"])
            if "duration_ms" in item:
                _integer(item["duration_ms"])
            seen.add(fid); paths.add(path.casefold())
            normalized.append({**item, "path": path})
        result = {**value, "root_path": root, "files": normalized}
        scan_secrets(result)
        if len(canonical(result)) > 2 * 1024**2:
            raise ContractError("inventory exceeds 2 MiB")
        return result

    def _run(self, run_id):
        check_id(run_id)
        row = self.store.connection.execute("SELECT * FROM platform_first_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise ContractError("unknown platform-pass run")
        result = dict(row)
        value = json.loads(result.pop("inventory_json"))
        if digest(value) != row["inventory_sha256"] or self._inventory(value) != value:
            raise ContractError("frozen inventory changed")
        result["inventory"] = value
        return result

    def _active(self, run):
        if run["state"] == "blocked":
            raise GateError("PLATFORM_RUN_BLOCKED", run["reason"] or "")
        if self.store.pipeline_state("collection") != "running":
            raise GateError("COLLECTION_PAUSED")

    def _product(self, run, file_id, product):
        if product not in PRODUCTS:
            raise ContractError("unknown platform product")
        item = next((f for f in run["inventory"]["files"] if f["file_id"] == file_id), None)
        row = self.store.connection.execute("SELECT * FROM platform_first_products WHERE run_id=? AND file_id=? AND product=?", (run["run_id"], file_id, product)).fetchone()
        if item is None or row is None:
            raise ContractError("file is not in frozen inventory")
        return item, dict(row)

    def _history(self, run):
        """Read immutable location snapshots; only identical content may inherit."""
        history = {}
        for old in self.store.connection.execute(
                "SELECT * FROM platform_first_runs WHERE project_id=? ORDER BY created_at,run_id",
                (run["project_id"],)).fetchall():
            inventory = json.loads(old["inventory_json"])
            if digest(inventory) != old["inventory_sha256"]:
                raise ContractError("frozen inventory changed")
            if any(inventory[k] != run["inventory"][k] for k in ("account_ref", "evidence_class")):
                continue
            keys = {}
            for item in inventory["files"]:
                key = digest(content_identity(inventory["account_ref"], item))
                keys[item["file_id"]] = key
                history.setdefault(key, {"items": [], "products": {p: [] for p in PRODUCTS}})["items"].append(item)
            for row in self.store.connection.execute("SELECT * FROM platform_first_products WHERE run_id=?", (old["run_id"],)).fetchall():
                history[keys[row["file_id"]]]["products"][row["product"]].append(dict(row))
        return history

    def _generation_job(self, run, item, product, history, *, create=False):
        provider = self.store.register_provider("platform-first-generation", "1.0", runtime="authorized_browser", config_version=self.config_version)
        identity = content_identity(run["inventory"]["account_ref"], item)
        peers = history[digest(identity)]["items"]
        return queue_content_job(self.store, run["project_id"], "platform-generate-" + product, provider,
                                 digest([identity, product]),
                                 legacy_revisions=[digest([run["inventory"]["account_ref"], old, product]) for old in peers],
                                 config_version=self.config_version, estimated_cost_minor=0, max_cost_minor=0, create=create)

    def _inherit_products(self, run):
        """Carry evidence and reservations forward, never missing/denial guesses.

        A completed legacy job without product readback stays in reconciliation;
        job success alone cannot establish transcript or frame completeness.
        """
        history = self._history(run)
        for item in run["inventory"]["files"]:
            peers = history[digest(content_identity(run["inventory"]["account_ref"], item))]
            for product in PRODUCTS:
                _, current = self._product(run, item["file_id"], product)
                candidates = peers["products"][product]
                present = [row for row in candidates if row["artifact_json"] and row["observation_json"]
                           and json.loads(row["observation_json"])["state"] == "present"]
                # An already retained local artifact is immutable even if a
                # peer run later reports a different export of the same video.
                if current["artifact_json"]:
                    retained = json.loads(current["artifact_json"])
                    present = [row for row in present if all(json.loads(row["artifact_json"])[key] == retained[key]
                               for key in ("relative_path", "sha256", "size_bytes"))]
                checked_present = []
                for candidate in present:
                    obs = json.loads(candidate["observation_json"])
                    kind = obs.get("content_kind", "transcript" if product == "manuscript" else "platform_courseware")
                    try:
                        coverage = self._coverage(obs.get("coverage"), item, product, kind)
                    except ContractError:
                        # Duration is an observation, not a content revision.
                        # Keep the old receipt, but never plan its invalid ranges.
                        obs["coverage_review"] = {"reason": "inherited_coverage_invalid_for_current_inventory",
                                                  "source_run_id": candidate["run_id"], "coverage": obs.get("coverage")}
                        coverage = {"state": "unknown"}
                    checked_present.append({**candidate, "observation_json": canonical({**obs, "coverage": coverage}).decode()})
                present = checked_present
                chosen = None
                if present:
                    def quality(row):
                        obs = json.loads(row["observation_json"])
                        return (obs.get("content_kind") != "summary",
                                {"sufficient": 3, "partial": 2, "unknown": 1}.get(obs.get("coverage", {}).get("state"), 0),
                                row["run_id"] == run["run_id"])
                    chosen = {**max(present, key=quality)}
                    chosen["generation_job_id"] = current["generation_job_id"] or chosen["generation_job_id"]
                else:
                    job = self._generation_job(run, item, product, history)
                    if not job and current["generation_job_id"]:
                        job = self.store.connection.execute("SELECT * FROM jobs WHERE job_id=?", (current["generation_job_id"],)).fetchone()
                    if job and job["state"] in PENDING | {"submitted", "running", "verified_complete"}:
                        prior = next((row for row in reversed(candidates) if row["generation_job_id"] == job["job_id"] and row["observation_json"]), current)
                        obs = json.loads(prior["observation_json"]) if prior["observation_json"] else {}
                        value = {"state": "submission_unknown" if job["state"] == "submission_unknown" else "waiting",
                                 "evidence_ref": obs.get("evidence_ref", "existing-generation-job"), "coverage": {"state": "unknown"},
                                 "reason": "Existing platform job; reconcile product readback before another request"}
                        if obs.get("state") in PENDING:
                            value = obs
                        chosen = {**prior, "observation_json": canonical(value).decode(), "generation_job_id": job["job_id"]}
                    elif current["observation_json"] is None:
                        chosen = next((row for row in reversed(candidates) if row["observation_json"]
                                       and json.loads(row["observation_json"])["state"] in PENDING and not row["generation_job_id"]), None)
                if chosen:
                    fields = (chosen["observation_json"], chosen["artifact_json"] or current["artifact_json"],
                              chosen["generation_job_id"] or current["generation_job_id"])
                    if fields != (current["observation_json"], current["artifact_json"], current["generation_job_id"]):
                        self.store.connection.execute("UPDATE platform_first_products SET observation_json=?,artifact_json=?,generation_job_id=? WHERE run_id=? AND file_id=? AND product=?",
                                                      (*fields, run["run_id"], item["file_id"], product))
        return history

    def begin_run(self, project_id, inventory: dict) -> dict:
        self.store.project_root(project_id)
        if self.store.pipeline_state("collection") != "running":
            raise GateError("COLLECTION_PAUSED")
        inventory = self._inventory(inventory)
        run_id = "platform-run-" + digest([project_id, inventory["inventory_id"]])[:32]
        serialized = canonical(inventory).decode()
        with self.store.transaction():
            old = self.store.connection.execute("SELECT inventory_json FROM platform_first_runs WHERE run_id=?", (run_id,)).fetchone()
            if old and old[0] != serialized:
                raise ContractError("inventory_id is immutable; use a new observed inventory revision")
            if not old:
                self.store.connection.execute("INSERT INTO platform_first_runs VALUES(?,?,?,?,?,0,1,NULL,?,?)", (run_id, project_id, serialized, digest(inventory), "first_pass", now(), now()))
                self.store.connection.executemany("INSERT INTO platform_first_products(run_id,file_id,product) VALUES(?,?,?)", [(run_id, f["file_id"], p) for f in inventory["files"] for p in PRODUCTS])
                self.store.event("platform_first_started", run_id, project_id, {"inventory_ref": inventory["evidence_ref"], "video_count": len(inventory["files"]), "evidence_class": inventory["evidence_class"]})
            self._inherit_products(self._run(run_id))
        return self.status(run_id)

    def next_first_pass(self, run_id) -> dict:
        run = self._run(run_id)
        if run["state"] == "blocked" or self.store.pipeline_state("collection") != "running":
            return {"run_id": run_id, "action": "blocked", "reason": run["reason"] or "collection paused"}
        if run["sealed"]:
            return {"run_id": run_id, "action": "first_pass_sealed"}
        for item in run["inventory"]["files"]:
            for product in PRODUCTS:
                _, row = self._product(run, item["file_id"], product)
                if row["observation_json"] is None:
                    return {"run_id": run_id, "action": "inspect_existing", "file": item, "account_ref": run["inventory"]["account_ref"], "product": product,
                            "generation_enabled": bool(run["generation_enabled"] and self.policy.request_new_generation),
                            "generation_permitted_by_policy": self.policy.request_new_generation, "entitlement_not_blocked": bool(run["generation_enabled"])}
        return {"run_id": run_id, "action": "seal_first_pass" if run["inventory"]["enumeration_complete"] else "inventory_incomplete"}

    def _coverage(self, raw, item, product, kind):
        if raw is None:
            return {"state": "unknown"}
        _fields(raw, {"state", "evidence_ref", "basis", "missing_intervals"}, {"state"})
        if raw["state"] not in {"sufficient", "partial", "unknown", "missing"}:
            raise ContractError("invalid coverage state")
        value = dict(raw)
        if raw["state"] in {"sufficient", "partial", "missing"}:
            check_id(raw.get("evidence_ref"))
        expected = "full_transcript" if product == "manuscript" else "source_frames"
        if kind == "summary" and raw["state"] in {"sufficient", "partial"}:
            raise ContractError("a summary cannot establish transcript coverage")
        if raw["state"] == "sufficient" and raw.get("basis") != expected:
            raise ContractError("sufficient coverage requires independent full-transcript/source-frame evidence")
        ranges = raw.get("missing_intervals", [])
        if raw["state"] != "partial" and ranges:
            raise ContractError("missing intervals require partial coverage")
        if raw["state"] == "partial":
            if not isinstance(ranges, list) or not ranges or len(ranges) > 1000 or not item.get("duration_ms"):
                raise ContractError("partial coverage needs bounded missing intervals and known duration")
            checked = []
            for interval in ranges:
                _fields(interval, {"start_ms", "end_ms"}, {"start_ms", "end_ms"})
                a, b = _integer(interval["start_ms"]), _integer(interval["end_ms"])
                if not a < b <= item["duration_ms"]:
                    raise ContractError("missing interval is outside the video")
                checked.append((a, b))
            merged = []
            for a, b in sorted(checked):
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            value["missing_intervals"] = [{"start_ms": a, "end_ms": b} for a, b in merged]
        scan_secrets(value)
        return value

    def record_product_observation(self, run_id, file_id, product, observation) -> dict:
        return self._record_product_observation(run_id, file_id, product, observation)

    def record_source_frame_observation(self, run_id, file_id, *, job_id, quality_review_path) -> dict:
        """Admit only an actual source execution and independently bound review."""
        from .platform_execution import SourceFrameExecution
        execution = SourceFrameExecution(self.store, self)
        with self.store.transaction():
            _, info, _, review, receipt = execution._completion_evidence(job_id, quality_review_path, run_id=run_id)
            if info["file_id"] != file_id:
                raise ContractError("source-frame file/execution mismatch")
            status = self._record_product_observation(run_id, file_id, "courseware", {
                "state": "present", "evidence_ref": review["evidence_ref"], "reconciliation_ref": review["evidence_ref"],
                "relative_path": receipt["manifest"]["relative_path"],
                "coverage": {"state": "sufficient", "basis": "source_frames", "evidence_ref": review["evidence_ref"]}},
                source_execution=(job_id, quality_review_path))
            row = self._product(self._run(run_id), file_id, "courseware")[1]
            execution._remember_decoder_validation(job_id, quality_review_path, json.loads(row["artifact_json"]), run_id=run_id)
        return status

    def _record_product_observation(self, run_id, file_id, product, observation, *, source_execution=None) -> dict:
        _fields(observation, {"state", "evidence_ref", "reason", "relative_path", "coverage", "content_kind", "reconciliation_ref"}, {"state", "evidence_ref"})
        if observation["state"] not in STATES:
            raise ContractError("invalid product state")
        check_id(observation["evidence_ref"])
        if "reason" in observation:
            _bounded(observation["reason"])
        if "reconciliation_ref" in observation:
            check_id(observation["reconciliation_ref"])
        state = observation["state"]
        with self.store.transaction():
            run = self._run(run_id); self._active(run)
            item, row = self._product(run, file_id, product)
            old = json.loads(row["observation_json"]) if row["observation_json"] else None
            pending_gate = bool(old and old["state"] in PENDING and state in GENERATION_BLOCKS and not observation.get("reconciliation_ref"))
            if old and old["state"] in PENDING and state not in PENDING | DENIED | GENERATION_BLOCKS and not observation.get("reconciliation_ref"):
                raise ContractError("pending/unknown submission requires a reconciliation receipt")
            if old and old["state"] == "present" and state != "present" and state not in DENIED:
                raise ContractError("retained product cannot be overwritten by missing/failed; use a new source revision")
            artifact = None
            kind = observation.get("content_kind", "transcript" if product == "manuscript" else "platform_courseware")
            if kind not in ({"transcript", "summary"} if product == "manuscript" else {"platform_courseware"}):
                raise ContractError("invalid product content kind")
            coverage = self._coverage(observation.get("coverage"), item, product, kind)
            if state == "present":
                from .platform_products import inspect_product, inspect_source_frame_execution
                if source_execution is None:
                    artifact = inspect_product(self.store, product, observation.get("relative_path"))
                else:
                    if product != "courseware":
                        raise ContractError("source execution can only admit courseware")
                    artifact = inspect_source_frame_execution(self.store, self, *source_execution, run_id=run_id)
                    if (artifact["relative_path"] != observation.get("relative_path")
                            or content_identity(run["inventory"]["account_ref"], item) != content_identity(artifact["metadata"]["declared_source"]["account_ref"], artifact["metadata"]["declared_source"])):
                        raise ContractError("source-frame product binding mismatch")
                if artifact.get("readable") is not True:
                    raise ContractError("product was not acquired as a readable local file: " + str(artifact.get("reason", "unreadable")))
                if (source_execution is None and artifact.get("metadata", {}).get("route") == "source_video_frames"
                        and coverage["state"] == "sufficient"):
                    raise ContractError("source-frame sufficient coverage requires an execution-bound external review")
                if row["artifact_json"]:
                    prior = json.loads(row["artifact_json"])
                    if any(prior[k] != artifact[k] for k in ("relative_path", "sha256", "size_bytes")):
                        raise ContractError("retained product bytes cannot be replaced in this source revision")
            elif "relative_path" in observation or coverage["state"] != "unknown":
                raise ContractError("only present products can declare files/coverage")
            value = {**observation, "coverage": coverage, "content_kind": kind}
            if pending_gate:
                # An exhausted account quota does not prove that an earlier
                # submitted request failed. Keep it in reconciliation state.
                value.update(state=old["state"], generation_gate=state)
            scan_secrets(value); scan_secrets(artifact)
            if state in DENIED:
                self.block_run(run_id, state)
            if state in GENERATION_BLOCKS:
                self.store.connection.execute("UPDATE platform_first_runs SET generation_enabled=0,updated_at=? WHERE run_id=?", (now(), run_id))
            # Denial evidence does not erase a previously acquired artifact.
            stored_artifact = canonical(artifact).decode() if artifact else row["artifact_json"]
            self.store.connection.execute("UPDATE platform_first_products SET observation_json=?,artifact_json=? WHERE run_id=? AND file_id=? AND product=?", (canonical(value).decode(), stored_artifact, run_id, file_id, product))
            if row["generation_job_id"]:
                job = self.store.connection.execute("SELECT state FROM jobs WHERE job_id=?", (row["generation_job_id"],)).fetchone()
                # A later run can observe the same source/job. Its failure or
                # account denial must not rewrite a completed task receipt.
                if job["state"] in PENDING | {"submitted"}:
                    effective_state = value["state"]
                    job_state = "verified_complete" if effective_state == "present" else effective_state if effective_state in PENDING else "blocked" if effective_state in DENIED | GENERATION_BLOCKS else "failed"
                    self.store.reconcile_job(row["generation_job_id"], state=job_state, receipt={"evidence_ref": value["evidence_ref"], "completion_scope": "local_product_readback_only", "product": product, "artifact": artifact}, settled_cost_minor=0 if job_state not in PENDING else None)
            self.store.event("platform_product_observed", run_id, run["project_id"], {"file_id": file_id, "product": product, "state": state, "evidence_ref": value["evidence_ref"]})
        return self.status(run_id, verify_artifacts=False)

    def record_source_frame_repair_observation(self, run_id, file_id, *, job_id, quality_review_path) -> dict:
        """Narrow repair admission, not a caller-controlled replacement flag.

        Only a currently broken completed source-frame artifact of the same
        account/revision may change. The execution ledger and a NEW bound QA
        receipt, not supplied booleans, prove the replacement and its lineage.
        """
        from .platform_execution import SourceFrameExecution
        from .platform_products import inspect_source_frame_execution, verify_retained_artifact
        with self.store.transaction():
            execution = SourceFrameExecution(self.store, self)
            job, info, run, review, receipt = execution._completion_evidence(job_id, quality_review_path, run_id=run_id)
            if file_id != info["file_id"] or job["state"] != "verified_complete":
                raise ContractError("repair requires the same completed source-frame execution")
            item, row = self._product(run, file_id, "courseware")
            prior = json.loads(row["artifact_json"]) if row["artifact_json"] else None
            old = json.loads(row["observation_json"]) if row["observation_json"] else None
            completed = execution._events("source_frames_completed", job_id)
            if (prior and completed and completed[-1].get("repair_lineage")
                    and all(completed[-1].get(k) == v for k, v in receipt.items())
                    and all(prior[k] == receipt["manifest"][k] for k in ("relative_path", "sha256", "size_bytes"))
                    and verify_retained_artifact(self.store, prior)):
                return self.status(run_id, verify_artifacts=False)
            if (not prior or not old or old["state"] != "present"
                    or prior.get("metadata", {}).get("route") != "source_video_frames"
                    or content_identity(run["inventory"]["account_ref"], item) != info["task_facts"]["content_identity"]
                    or verify_retained_artifact(self.store, prior)):
                raise ContractError("repair requires a currently invalid retained source-frame artifact")
            previous = next((e["manifest"] for e in reversed(execution._events("source_frames_completed", job_id))
                             if all(e["manifest"][k] == prior[k] for k in ("relative_path", "sha256", "size_bytes"))), None)
            if previous is None or previous == receipt["manifest"]:
                raise ContractError("repair requires distinct old and new execution-bound manifests")
            lineage = execution._repair_lineage(info, previous)
            artifact = inspect_source_frame_execution(self.store, self, job_id, quality_review_path, run_id=run_id)
            if artifact.get("readable") is not True or not verify_retained_artifact(self.store, artifact):
                raise ContractError("repaired source-frame product is not currently readable")
            value = {"state": "present", "evidence_ref": review["evidence_ref"], "reconciliation_ref": review["evidence_ref"],
                     "relative_path": artifact["relative_path"], "content_kind": "platform_courseware",
                     "coverage": {"state": "sufficient", "basis": "source_frames", "evidence_ref": review["evidence_ref"]}}
            scan_secrets(value); scan_secrets(artifact); scan_secrets(lineage)
            execution._remember_decoder_validation(job_id, quality_review_path, artifact, run_id=run_id)
            self.store.connection.execute("UPDATE platform_first_products SET observation_json=?,artifact_json=? WHERE run_id=? AND file_id=? AND product='courseware'",
                                          (canonical(value).decode(), canonical(artifact).decode(), run_id, file_id))
            self.store.event("platform_source_frame_product_repaired", run_id, run["project_id"],
                             {"file_id": file_id, "job_id": job_id, "content_identity": info["task_facts"]["content_identity"],
                              "previous_observation": old, "previous_artifact": prior, "observation": value,
                              "artifact": artifact, "quality_review": receipt["quality_review"], "repair_lineage": lineage})
            self.store.event("source_frames_completed", job_id, run["project_id"], {**receipt, "repair_lineage": lineage})
        return self.status(run_id, verify_artifacts=False)

    def begin_generation(self, run_id, file_id, product, *, entitlement_ref) -> dict:
        """Reserve an explicitly authorized zero-cost request; never execute it."""
        # A nested savepoint is not a durable reservation: an outer rollback
        # could erase it after the caller has already sent the remote request.
        if self.store.connection.in_transaction:
            raise GateError("OUTER_TRANSACTION_CANNOT_AUTHORIZE_SEND")
        check_id(entitlement_ref)
        self.policy.require_generation()
        with self.store.transaction():
            run = self._run(run_id); self._active(run)
            history = self._inherit_products(run)
            item, row = self._product(run, file_id, product)
            if not run["generation_enabled"]:
                return {"may_send": False, "status": "blocked", "reason": "platform generation quota/payment gate"}
            old = json.loads(row["observation_json"]) if row["observation_json"] else None
            if old and old["state"] == "present":
                return {"may_send": False, "status": "present", "reason": "retain_existing_product", "job_id": row["generation_job_id"]}
            if row["generation_job_id"]:
                job = self.store.connection.execute("SELECT * FROM jobs WHERE job_id=?", (row["generation_job_id"],)).fetchone()
                if run["sealed"] or old and old["state"] in PENDING:
                    return {"may_send": False, "status": job["state"], "reason": "reconcile_existing_job", "job_id": job["job_id"]}
                result = self.store.begin_submission(job["job_id"])
            else:
                if run["sealed"] or not old or old["state"] not in {"missing", "failed"}:
                    raise ContractError("generation requires first-pass observation of a missing product")
                job = self._generation_job(run, item, product, history, create=True)
                self.store.connection.execute("UPDATE platform_first_products SET generation_job_id=? WHERE run_id=? AND file_id=? AND product=?", (job["job_id"], run_id, file_id, product))
                result = self.store.begin_submission(job["job_id"])
            if result["may_send"] or result["status"] in {"submission_unknown", "submitted", "waiting", "verified_complete"}:
                value = {"state": "submission_unknown" if result["may_send"] or result["status"] == "submission_unknown" else "waiting", "evidence_ref": entitlement_ref, "coverage": {"state": "unknown"}, "reason": "Existing/reserved platform job; reconcile before another request"}
                self.store.connection.execute("UPDATE platform_first_products SET observation_json=? WHERE run_id=? AND file_id=? AND product=?", (canonical(value).decode(), run_id, file_id, product))
            return {**result, "job_id": job["job_id"]}

    def seal_first_pass(self, run_id) -> dict:
        with self.store.transaction():
            run = self._run(run_id); self._active(run)
            if not run["inventory"]["enumeration_complete"]:
                raise GateError("INVENTORY_NOT_COMPLETE")
            pending = self.store.connection.execute("SELECT COUNT(*) FROM platform_first_products WHERE run_id=? AND observation_json IS NULL", (run_id,)).fetchone()[0]
            if pending:
                raise GateError("FIRST_PASS_INCOMPLETE", str(pending) + " products unvisited")
            self.store.connection.execute("UPDATE platform_first_runs SET sealed=1,state='supplement_planning',updated_at=? WHERE run_id=?", (now(), run_id))
        return self.status(run_id)

    def block_run(self, run_id, reason) -> dict:
        _bounded(reason)
        run = self._run(run_id)
        with self.store.transaction():
            self.store.connection.execute("UPDATE platform_first_runs SET state='blocked',generation_enabled=0,reason=?,updated_at=? WHERE run_id=?", (reason, now(), run_id))
            self.store.event("platform_first_blocked", run_id, run["project_id"], {"reason": reason})
        return self.status(run_id, verify_artifacts=False)

    def status(self, run_id, *, verify_artifacts=True) -> dict:
        run = self._run(run_id)
        products, integrity_issues = [], []
        for item in run["inventory"]["files"]:
            for product in PRODUCTS:
                _, row = self._product(run, item["file_id"], product)
                observation = json.loads(row["observation_json"]) if row["observation_json"] else {"state": "unvisited"}
                artifact = json.loads(row["artifact_json"]) if row["artifact_json"] else None
                if artifact and verify_artifacts:
                    try:
                        # Hash the validated file and any declared image-set dependencies.
                        # Do not reparse PDF/PPTX or redecode every image on status reads.
                        from .platform_products import verify_retained_artifact
                        valid = verify_retained_artifact(self.store, artifact)
                    except (OSError, ValueError, KeyError, TypeError, ContractError, GateError):
                        valid = False
                    if not valid:
                        integrity_issues.append({"file_id": item["file_id"], "product": product, "reason": "retained_artifact_missing_or_changed"})
                products.append({"file_id": item["file_id"], "product": product, "observation": observation, "artifact": artifact, "generation_job_id": row["generation_job_id"]})
        return {"run_id": run_id, "project_id": run["project_id"], "state": run["state"], "reason": run["reason"],
                "inventory": run["inventory"], "first_pass_sealed": bool(run["sealed"]), "generation_enabled": bool(run["generation_enabled"] and self.policy.request_new_generation),
                "generation_permitted_by_policy": self.policy.request_new_generation, "entitlement_not_blocked": bool(run["generation_enabled"]),
                "checked_products": sum(p["observation"]["state"] != "unvisited" for p in products), "total_products": len(products),
                "products": products, "integrity_issues": integrity_issues, "artifact_integrity_checked": verify_artifacts,
                "remote_execution_performed_by_module": False}

    def plan_missing(self, run_id) -> dict:
        run = self._run(run_id); self._active(run)
        if not run["sealed"]:
            raise GateError("FIRST_PASS_NOT_SEALED")
        with self.store.transaction():
            history = self._inherit_products(run)
        return self._plan_products(run, self.status(run_id), history)

    def plan_video_validation(self, run_id, file_id) -> dict:
        """Exercise one observed video without sealing/claiming the directory pass."""
        run = self._run(run_id); self._active(run)
        self._product(run, file_id, PRODUCTS[0])
        with self.store.transaction():
            history = self._inherit_products(run)
        current = self.status(run_id)
        current["products"] = [row for row in current["products"] if row["file_id"] == file_id]
        if any(row["observation"]["state"] == "unvisited" for row in current["products"]):
            raise GateError("VIDEO_VALIDATION_INCOMPLETE")
        return {**self._plan_products(run, current, history), "validation_mode": "single_video",
                "validation_file_id": file_id, "validation_video_count": 1,
                "inventory_video_count": len(run["inventory"]["files"]),
                "inventory_enumeration_complete": run["inventory"]["enumeration_complete"],
                "directory_first_pass_sealed": bool(run["sealed"]),
                "production_first_pass_satisfied": False}

    def _plan_products(self, run, current, history):
        review, reconcile, supplements = [], [], []
        broken = {(x["file_id"], x["product"]) for x in current["integrity_issues"]}
        provider = self.store.register_provider("platform-first-supplement-plan", "1.0", config_version=self.config_version)
        for row in current["products"]:
            fid, product, obs = row["file_id"], row["product"], row["observation"]
            item, _ = self._product(run, fid, product)
            target = {"file_id": fid, "product": product}
            if (fid, product) in broken:
                review.append({**target, "reason": "retained_artifact_missing_or_changed"}); continue
            if obs["state"] in PENDING:
                reconcile.append({**target, "generation_job_id": row["generation_job_id"], "reason": obs["state"]}); continue
            coverage = obs.get("coverage", {"state": "unknown"})
            if obs["state"] == "present" and obs.get("content_kind") != "summary":
                if coverage["state"] == "sufficient":
                    continue
                if coverage["state"] == "unknown":
                    entry = {**target, "reason": "coverage_unknown", "artifact": row["artifact"]}
                    if obs.get("coverage_review"):
                        entry.update(reason=obs["coverage_review"]["reason"], coverage_review=obs["coverage_review"])
                    review.append(entry); continue
            selection = {"mode": "intervals", "missing_intervals": coverage["missing_intervals"]} if coverage["state"] == "partial" else {"mode": "whole_timeline", "duration_ms": item.get("duration_ms")}
            modality = "audio" if product == "manuscript" else "visual"
            identity = content_identity(run["inventory"]["account_ref"], item)
            revision = digest([identity, modality, selection])
            legacy = [digest([run["inventory"]["account_ref"], old, modality, selection])
                      for old in history[digest(identity)]["items"]]
            # Unknown-cost, unexecuted plans cannot accidentally become permission
            # to run another provider, download media or bypass a blocked channel.
            job = queue_content_job(self.store, run["project_id"], "supplement-" + modality, provider, revision, legacy_revisions=legacy,
                                    config_version=self.config_version, estimated_cost_minor=None, max_cost_minor=0)
            if job["state"] in PENDING | {"submitted", "running"}:
                reconcile.append({**target, "job_id": job["job_id"], "modality": modality, "job_state": job["state"],
                                  "reason": "existing_supplement_requires_reconciliation"})
                continue
            if job["state"] == "verified_complete":
                review.append({**target, "job_id": job["job_id"], "modality": modality, "job_state": job["state"],
                               "reason": "supplement_complete_requires_product_readback"})
                continue
            supplements.append({**target, "modality": modality, "selection": selection, "job_id": job["job_id"], "job_state": job["state"], "execution_status": "planned_only", "source": {"account_ref": run["inventory"]["account_ref"], **item}, "preserve_artifacts": [p["artifact"] for p in current["products"] if p["file_id"] == fid and p["artifact"]]})
        return {"run_id": run["run_id"], "supplements": supplements, "review": review, "reconcile": reconcile, "execution_performed": False, "course_complete": False}
