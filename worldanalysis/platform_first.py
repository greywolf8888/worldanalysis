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

    def __init__(self, store, policy=None, allow_synthetic=False):
        self.store = store
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
                from .platform_products import inspect_product
                artifact = inspect_product(self.store, product, observation.get("relative_path"))
                if artifact.get("readable") is not True:
                    raise ContractError("product was not acquired as a readable local file: " + str(artifact.get("reason", "unreadable")))
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

    def begin_generation(self, run_id, file_id, product, *, entitlement_ref) -> dict:
        """Reserve an explicitly authorized zero-cost request; never execute it."""
        check_id(entitlement_ref)
        self.policy.require_generation()
        with self.store.transaction():
            run = self._run(run_id); self._active(run)
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
                provider = self.store.register_provider("platform-first-generation", "1.0", runtime="authorized_browser", config_version="platform-first-v1")
                revision = digest([run["inventory"]["account_ref"], item, product])
                job = self.store.queue_job(run["project_id"], "platform-generate-" + product, provider, revision, config_version="platform-first-v1", estimated_cost_minor=0, max_cost_minor=0)
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
                        size = artifact["size_bytes"]
                        if type(size) is not int or not 0 < size <= self.store.max_file_bytes:
                            raise ContractError("invalid retained product size")
                        path = safe_path(self.store.root, artifact["relative_path"])
                        # Read at most the previously validated length; no PDF/
                        # PPTX parsing is repeated merely to check its identity.
                        data = _read(path, size)
                        valid = len(data) == size and hashlib.sha256(data).hexdigest() == artifact["sha256"]
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
        current = self.status(run_id)
        review, reconcile, supplements = [], [], []
        broken = {(x["file_id"], x["product"]) for x in current["integrity_issues"]}
        provider = self.store.register_provider("platform-first-supplement-plan", "1.0", config_version="platform-first-v1")
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
                    review.append({**target, "reason": "coverage_unknown", "artifact": row["artifact"]}); continue
            selection = {"mode": "intervals", "missing_intervals": coverage["missing_intervals"]} if coverage["state"] == "partial" else {"mode": "whole_timeline", "duration_ms": item.get("duration_ms")}
            modality = "audio" if product == "manuscript" else "visual"
            revision = digest([run["inventory"]["account_ref"], item, modality, selection])
            # Unknown-cost, unexecuted plans cannot accidentally become permission
            # to run another provider, download media or bypass a blocked channel.
            job = self.store.queue_job(run["project_id"], "supplement-" + modality, provider, revision, config_version="platform-first-v1", estimated_cost_minor=None, max_cost_minor=0)
            supplements.append({**target, "modality": modality, "selection": selection, "job_id": job["job_id"], "execution_status": "planned_only", "source": {"account_ref": run["inventory"]["account_ref"], **item}, "preserve_artifacts": [p["artifact"] for p in current["products"] if p["file_id"] == fid and p["artifact"]]})
        return {"run_id": run_id, "supplements": supplements, "review": review, "reconcile": reconcile, "execution_performed": False, "course_complete": False}
