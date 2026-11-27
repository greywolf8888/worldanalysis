"""Resumable authorized source-frame capture on Store jobs/events; no network.

This bridge grants a bounded normal-video-read task, not AI generation. JPEGs
and browser receipts are supplied by the operator. A complete target set is NOT
quality acceptance: an independent, manifest-bound review is mandatory.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
from pathlib import Path, PurePosixPath
import re
import warnings

from .contracts import ContractError, canonical, check_id, digest, reject_links, safe_path, validate_relative_path
from .platform_identity import content_identity
from .platform_materials import _fields, _json, _read
from .policy import GateError

SCHEMA = "worldanalysis.source-video-frames.v1"
RECEIPT_SCHEMA = "worldanalysis.browser-source-frame.v1"
REVIEW_SCHEMA = "worldanalysis.source-frame-quality-review.v1"
_CREATED = "source_frames_created"
_STARTED = "source_frames_started"
_ACCEPTED = "source_frame_accepted"
_REPAIRED = "source_frame_repaired"
_COMPLETED = "source_frames_completed"
_VALIDATED = "source_frame_contract_validated"
# Only this audited pre-repair bridge/helper pair shares the v1 capture contract.
# Recovery/validation code changes do not revise its immutable source inputs.
_V1_CAPTURE_ALIAS = ("669d7b2508fa9480618a009522473513f2933311e44fa2257640ab50bb985a2a",
                     "2f7991f3e140aa4ac91bcfb5f06ecdbc3cb6d2d38417baf7cb78a1422e9f5ac4")
_CAPTURE_ALIASES = frozenset({_V1_CAPTURE_ALIAS,
    ("d3738f2c954a546d3a3f0534431ad467618f10225db95446b9171cd741319d41", _V1_CAPTURE_ALIAS[1])})
_MAX_FRAMES = 1000
_MAX_RECEIPT = 16 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")


def _sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ContractError("invalid SHA-256")
    return value


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ContractError("frame time must be finite and nonnegative")
    return value


def _positive(value, maximum):
    if type(value) is not int or not 0 < value <= maximum:
        raise ContractError("frame byte/dimension bound exceeded")
    return value


def _jpeg(data, width, height):
    from PIL import Image, ImageFile
    if not data.startswith(bytes.fromhex("ffd8ff")) or ImageFile.LOAD_TRUNCATED_IMAGES:
        raise ContractError("strict JPEG source frame required")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "JPEG" or image.size != (width, height) or getattr(image, "n_frames", 1) != 1:
                    raise ContractError("frame JPEG dimensions/format disagree with receipt")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ContractError("source-frame JPEG cannot be fully decoded") from exc


class SourceFrameExecution:
    def __init__(self, store, coordinator):
        if coordinator.store is not store:
            raise ContractError("execution and coordinator must share one Store")
        self.store, self.coordinator = store, coordinator

    def _events(self, kind, job_id=None, project_id=None):
        query, args = "SELECT payload_json FROM events WHERE kind=?", [kind]
        if job_id is not None:
            query += " AND record_id=?"; args.append(job_id)
        if project_id is not None:
            query += " AND project_id=?"; args.append(project_id)
        return [_json(row[0].encode()) for row in self.store.connection.execute(query + " ORDER BY rowid", args).fetchall()]

    def _job(self, job_id):
        check_id(job_id)
        row = self.store.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        events = self._events(_CREATED, job_id)
        if not row or not events or row["operation"] != "capture-source-frames":
            raise ContractError("unknown source-frame execution")
        info = events[0]
        if row["project_id"] != info["project_id"] or row["input_revision"] != digest(info["task_facts"]):
            raise ContractError("source-frame execution binding changed")
        return dict(row), info

    def _active(self, info, run_id=None):
        run = self.coordinator._run(run_id or info["run_id"])
        self.coordinator._active(run)
        if not run["sealed"]:
            raise GateError("FIRST_PASS_NOT_SEALED")
        item, _ = self.coordinator._product(run, info["file_id"], "courseware")
        if (run["project_id"] != info["project_id"] or
                content_identity(run["inventory"]["account_ref"], item) != info["task_facts"]["content_identity"]):
            raise ContractError("source content no longer matches execution")
        return run, item

    def _capture_contract_binding(self, info, run, *, target_evidence_ref=None, selection_basis=None):
        helper = hashlib.sha256(_read(safe_path(self.store.root, info["helper_path"]), 1024**2)).hexdigest()
        bridge = hashlib.sha256(_read(Path(__file__), 1024**2)).hexdigest()
        facts = info["task_facts"]
        if helper != facts["helper_sha256"]:
            raise ContractError("capture helper changed; create a correctly versioned task")
        if bridge == facts["bridge_sha256"]:
            return
        job, _ = self._job(info["job_id"])
        provider = _json(job["provider_json"].encode())
        parent = self.store.connection.execute("SELECT * FROM jobs WHERE job_id=?", (facts["parent_job_id"],)).fetchone()
        revisions = {digest([facts["content_identity"], "visual", facts["selection"]])}
        peers = self.coordinator._history(run)[digest(facts["content_identity"])]["items"]
        revisions.update(digest([run["inventory"]["account_ref"], item, "visual", facts["selection"]]) for item in peers)
        if ((facts["bridge_sha256"], helper) not in _CAPTURE_ALIASES or not parent
                or content_identity(info["source"].get("account_ref"), info["source"]) != facts["content_identity"]
                or provider.get("provider_id") != "browser-source-frame"
                or provider.get("config_version") != "source-frames-" + digest([helper, facts["bridge_sha256"]])[:40]
                or parent["input_revision"] not in revisions
                or parent["project_id"] != info["project_id"] or parent["operation"] != "supplement-visual"
                or _json(parent["provider_json"].encode()).get("config_version") != self.coordinator.config_version
                or facts["evidence_class"] != run["inventory"]["evidence_class"]
                or (target_evidence_ref is not None and target_evidence_ref != info["target_evidence_ref"])
                or (selection_basis is not None and selection_basis != info["selection_basis"])):
            raise ContractError("unrecognized or mismatched bridge capture-contract alias")
        contract = {"capture_contract": "source-frame-capture-v1", "old_bridge_sha256": facts["bridge_sha256"],
                    "bridge_sha256": bridge, "helper_sha256": helper, "task_facts_sha256": digest(facts),
                    "target_evidence_ref": info["target_evidence_ref"], "selection_basis": info["selection_basis"],
                    "parent_config_version": self.coordinator.config_version, "parent_input_revision": parent["input_revision"],
                    "parent_provider_sha256": digest(_json(parent["provider_json"].encode())), "provider_sha256": digest(provider),
                    "source": info["source"], "capture_root": info["capture_root"], "helper_path": info["helper_path"]}
        return contract

    def _contract_cache(self, info, contract):
        key = digest(contract)
        aliased = any(e.get("contract_sha256") == key and all(e.get(k) == v for k, v in contract.items())
                      for e in self._events("source_frame_capture_contract_aliased", info["job_id"]))
        validated = {e["accepted_sha256"] for e in self._events(_VALIDATED, info["job_id"])
                     if aliased and e.get("contract_sha256") == key}
        return key, aliased, validated

    def _store_contract_validation(self, info, contract, progress, checked):
        key, aliased, validated = self._contract_cache(info, contract)
        with self.store.transaction():
            if not aliased:
                self.store.event("source_frame_capture_contract_aliased", info["job_id"], info["project_id"],
                                 {**contract, "contract_sha256": key,
                                  "validated_targets": [f["requested_ms"] for f in progress["valid_frames"]], "pending": progress["pending"]})
            for event in checked:
                if digest(event) not in validated:
                    self.store.event(_VALIDATED, info["job_id"], info["project_id"],
                                     {"contract_sha256": key, "accepted_sha256": digest(event), "requested_ms": event["requested_ms"]})
        return key

    def _capture_contract(self, info, run, *, target_evidence_ref=None, selection_basis=None):
        contract = self._capture_contract_binding(info, run, target_evidence_ref=target_evidence_ref, selection_basis=selection_basis)
        if contract is None:
            return
        _, _, validated = self._contract_cache(info, contract)
        checked = []
        progress = self._progress(info, decode=True, validated=validated, checked_events=checked)
        return self._store_contract_validation(info, contract, progress, checked)

    def _remember_decoder_validation(self, job_id, quality_review_path, artifact, *, run_id=None):
        """Remember only the fixed decoder's result after restricted admission."""
        from .source_frame_validator import LIMITS, SCHEMA as DECODER_SCHEMA
        job, info, run, _, receipt = self._completion_evidence(job_id, quality_review_path, run_id=run_id)
        binding = artifact["metadata"]["source_frame_execution"]
        report = artifact["metrics"]["resource_validation"]
        if (artifact.get("readable") is not True or binding["job_id"] != job_id
                or binding["input_revision"] != job["input_revision"] or binding["quality_review"] != receipt["quality_review"]
                or any(artifact[k] != receipt["manifest"][k] for k in ("relative_path", "sha256", "size_bytes"))
                or report["schema"] != DECODER_SCHEMA or report["status"] != "validated" or report["limits"] != LIMITS
                or report["manifest_sha256"] != receipt["manifest"]["sha256"]
                or report["frame_count"] != len(info["task_facts"]["requested_ms"])):
            raise ContractError("source decoder validation is not execution/QA bound")
        contract = self._capture_contract_binding(info, run)
        if contract is not None:
            checked = []
            progress = self._progress(info, checked_events=checked)
            if progress["pending"]:
                raise ContractError("decoded source frames changed before migration recording")
            self._store_contract_validation(info, contract, progress, checked)

    def create(self, run_id, file_id, requested_ms, *, helper_path, target_evidence_ref,
               selection_basis="platform_img_timestamps_and_native_caption_boundaries"):
        """Bind a real visual parent plan and exact targets to actual helper bytes."""
        check_id(target_evidence_ref); check_id(selection_basis)
        run = self.coordinator._run(run_id); self.coordinator._active(run)
        if not run["sealed"]:
            raise GateError("FIRST_PASS_NOT_SEALED")
        item, _ = self.coordinator._product(run, file_id, "courseware")
        if not isinstance(requested_ms, list) or not 0 < len(requested_ms) <= _MAX_FRAMES:
            raise ContractError("source-frame targets must be a bounded nonempty list")
        if any(type(t) is not int or not 0 <= t <= 2**53 - 1 for t in requested_ms) or requested_ms != sorted(set(requested_ms)):
            raise ContractError("source-frame targets must be unique ordered integer milliseconds")
        if item.get("duration_ms") and requested_ms[-1] > item["duration_ms"]:
            raise ContractError("capture target exceeds observed duration")
        helper_path = validate_relative_path(helper_path)
        helper_sha = hashlib.sha256(_read(safe_path(self.store.root, helper_path), 1024**2)).hexdigest()
        bridge_sha = hashlib.sha256(_read(Path(__file__), 1024**2)).hexdigest()
        identity = content_identity(run["inventory"]["account_ref"], item)
        # Resume even after the parent has completed: never replace its immutable
        # input/provider or create a second task merely to recover local progress.
        for info in self._events(_CREATED, project_id=run["project_id"]):
            facts = info["task_facts"]
            if (facts["content_identity"] == identity and facts["requested_ms"] == requested_ms
                    and facts["helper_sha256"] == helper_sha
                    and facts["evidence_class"] == run["inventory"]["evidence_class"]):
                parent_row = self.store.connection.execute("SELECT provider_json FROM jobs WHERE job_id=?", (facts["parent_job_id"],)).fetchone()
                if parent_row and _json(parent_row[0].encode()).get("config_version") == self.coordinator.config_version:
                    self._job(info["job_id"]); self._active(info, run_id)
                    self._capture_contract(info, run, target_evidence_ref=target_evidence_ref, selection_basis=selection_basis)
                    return self.status(info["job_id"])
        plan = self.coordinator.plan_missing(run_id)
        parent = next((row for row in plan["supplements"] if row["file_id"] == file_id and row["modality"] == "visual"), None)
        if parent is None:
            raise ContractError("no queued visual supplement plan for this video")
        selection = parent["selection"]
        if selection["mode"] == "intervals" and any(not any(r["start_ms"] <= t <= r["end_ms"] for r in selection["missing_intervals"]) for t in requested_ms):
            raise ContractError("frame target is outside the planned missing intervals")
        config = "source-frames-" + digest([helper_sha, bridge_sha])[:40]
        provider = self.store.register_provider("browser-source-frame", "sha256-" + helper_sha,
                                                runtime="authorized_browser", config_version=config)
        facts = {"content_identity": identity, "selection": selection, "requested_ms": requested_ms,
                 "parent_job_id": parent["job_id"], "helper_sha256": helper_sha, "bridge_sha256": bridge_sha,
                 "evidence_class": run["inventory"]["evidence_class"]}
        with self.store.transaction():
            job = self.store.queue_job(run["project_id"], "capture-source-frames", provider, digest(facts),
                                       config_version=config, estimated_cost_minor=0, max_cost_minor=0)
            if not self._events(_CREATED, job["job_id"]):
                info = {"job_id": job["job_id"], "project_id": run["project_id"], "run_id": run_id, "file_id": file_id,
                        "task_facts": facts, "helper_path": helper_path,
                        "source": {**identity, "path": item["path"]},
                        "target_evidence_ref": target_evidence_ref, "selection_basis": selection_basis,
                        "capture_root": "采集输入/platform-source-frames/" + job["job_id"]}
                self.store.event(_CREATED, job["job_id"], run["project_id"], info)
        return self.status(job["job_id"])

    def start(self, job_id):
        """Commit an independent zero-cost read grant before allowing capture."""
        if self.store.connection.in_transaction:
            raise GateError("OUTER_TRANSACTION_CANNOT_AUTHORIZE_SEND")
        with self.store.transaction():
            job, info = self._job(job_id); run, _ = self._active(info)
            self._capture_contract(info, run)
            result = self.store.begin_submission(job_id)
            if result["may_send"]:
                self.store.event(_STARTED, job_id, info["project_id"], {"request_id": result["request_id"], "normal_video_read": True, "ai_generation": False})
            authorized = (bool(self._events(_STARTED, job_id))
                          and result["status"] in {"submission_unknown", "submitted", "waiting", "verified_complete"}
                          and bool(self.pending_capture_targets(job_id)["pending"]))
            return {**result, "job_id": job_id, "may_capture": authorized, "capture_root": info["capture_root"]}

    def _frame(self, info, receipt_relative_path, *, decode=True):
        receipt_relative_path = validate_relative_path(receipt_relative_path)
        base = PurePosixPath(info["capture_root"])
        if not PurePosixPath(receipt_relative_path).is_relative_to(base) or Path(receipt_relative_path).suffix.lower() != ".json":
            raise ContractError("frame receipts must be inside the execution capture root")
        receipt_path = safe_path(self.store.root, receipt_relative_path)
        if receipt_path.stat().st_nlink != 1:
            raise ContractError("hardlinked frame receipt is forbidden")
        receipt_data = _read(receipt_path, _MAX_RECEIPT)
        receipt = _json(receipt_data)
        required = {"schema", "source", "requested_ms", "frame_callback", "source_identity_check", "helper_sha256", "relative_path", "sha256", "bytes", "width", "height", "format"}
        _fields(receipt, required | {"observed_at", "media_time_ms", "locator", "playback_duration_ms"}, required)
        if "playback_duration_ms" in receipt:
            _number(receipt["playback_duration_ms"])
        if receipt["schema"] != RECEIPT_SCHEMA or receipt["format"] != "jpeg" or receipt.get("locator", "requestVideoFrameCallback") != "requestVideoFrameCallback":
            raise ContractError("actual browser JPEG/rVFC receipt required")
        if _sha(receipt["helper_sha256"]) != info["task_facts"]["helper_sha256"]:
            raise ContractError("frame helper does not match execution")
        source = receipt["source"]
        source_fields = {"account_ref", "file_id", "source_revision", "size_bytes", "path"}
        _fields(source, source_fields | {"provider", "duration_ms"}, source_fields)
        if content_identity(source.get("account_ref"), source) != info["task_facts"]["content_identity"]:
            raise ContractError("frame source identity/revision/size mismatch")
        self.coordinator.policy.require_cloud_path(source["path"])
        proof = receipt["source_identity_check"]
        if (not isinstance(proof, dict) or set(proof) != {"file_id_match", "version_token_match"}
                or any(value is not True for value in proof.values())):
            raise ContractError("actual browser file-ID and version checks are required")
        target = receipt["requested_ms"]
        if type(target) is not int or target not in info["task_facts"]["requested_ms"]:
            raise ContractError("frame is not a bound requested target")
        actual = _number(receipt["frame_callback"]["mediaTime"]) * 1000
        # Match the helper seek/rVFC gate: this is frame-position tolerance,
        # never a transcript/visual-coverage sufficiency threshold. The tiny
        # epsilon only accommodates binary floating-point milliseconds.
        if abs(actual - target) > 80 + 1e-6:
            raise ContractError("actual rVFC frame is outside the requested target tolerance (80 ms)")
        if "media_time_ms" in receipt and abs(_number(receipt["media_time_ms"]) - actual) > 0.001:
            raise ContractError("actual frame time disagrees with rVFC.mediaTime")
        relative = validate_relative_path(receipt["relative_path"])
        if not PurePosixPath(relative).is_relative_to(base) or Path(relative).suffix.lower() not in {".jpg", ".jpeg"}:
            raise ContractError("JPEG must be inside the execution capture root")
        size = _positive(receipt["bytes"], min(self.store.max_file_bytes, 20 * 1024**2))
        width, height = _positive(receipt["width"], 20_000_000), _positive(receipt["height"], 20_000_000)
        _positive(width * height, 20_000_000)
        path = safe_path(self.store.root, relative)
        if path.stat().st_nlink != 1:
            raise ContractError("hardlinked source-frame JPEG is forbidden")
        data = _read(path, size)
        if len(data) != size or hashlib.sha256(data).hexdigest() != _sha(receipt["sha256"]):
            raise ContractError("source-frame JPEG bytes/hash mismatch")
        if decode:
            _jpeg(data, width, height)
        frame = {"relative_path": PurePosixPath(relative).relative_to(base).as_posix(), "sha256": receipt["sha256"],
                 "size_bytes": size, "width": width, "height": height, "requested_ms": target, "media_time_ms": actual,
                 "format": "jpeg", "locator": "requestVideoFrameCallback", "source_identity_check": proof,
                 "receipt_relative_path": PurePosixPath(receipt_relative_path).relative_to(base).as_posix(),
                 "receipt_sha256": hashlib.sha256(receipt_data).hexdigest()}
        return {"requested_ms": target, "frame": frame, "receipt_relative_path": receipt_relative_path,
                "receipt_sha256": frame["receipt_sha256"], "receipt_size_bytes": len(receipt_data)}

    def _progress(self, info, *, decode=False, validated=frozenset(), checked_events=None):
        valid, invalid, seen = {}, [], set()
        for event in reversed(self._events(_ACCEPTED, info["job_id"])):
            target = event["requested_ms"]
            if target in seen:
                continue
            seen.add(target)  # Never resurrect a superseded attempt.
            try:
                checked = self._frame(info, event["receipt_relative_path"], decode=decode and digest(event) not in validated)
                if checked != event:
                    raise ContractError("accepted frame receipt changed")
                valid[target] = checked["frame"]
                if checked_events is not None:
                    checked_events.append(checked)
            except (ContractError, OSError, KeyError, TypeError) as exc:
                invalid.append({"requested_ms": target, "reason": str(exc)[:300]})
        targets = info["task_facts"]["requested_ms"]
        return {"job_id": info["job_id"], "pending": [t for t in targets if t not in valid],
                "valid_frames": [valid[t] for t in targets if t in valid], "invalid_frames": invalid,
                "quality_accepted": False}

    def pending_capture_targets(self, job_id):
        _, info = self._job(job_id)
        return self._progress(info)

    def status(self, job_id):
        job, info = self._job(job_id)
        progress = self._progress(info)
        accepted = False
        completions = self._events(_COMPLETED, job_id)
        if job["state"] == "verified_complete" and not progress["pending"] and completions:
            try:
                receipt = completions[-1]
                _, manifest = self._manifest(info, progress["valid_frames"])
                review = receipt["quality_review"]
                data = _read(safe_path(self.store.root, review["relative_path"]), review["size_bytes"])
                manifest_data = _read(safe_path(self.store.root, manifest["relative_path"]), manifest["size_bytes"])
                accepted = (receipt["manifest"] == manifest and len(data) == review["size_bytes"]
                            and hashlib.sha256(data).hexdigest() == review["sha256"]
                            and len(manifest_data) == manifest["size_bytes"]
                            and hashlib.sha256(manifest_data).hexdigest() == manifest["sha256"])
            except (ContractError, OSError, KeyError, TypeError):
                accepted = False
        return {"job_id": job_id, "parent_job_id": info["task_facts"]["parent_job_id"], "state": job["state"],
                "capture_root": info["capture_root"], "helper_sha256": info["task_facts"]["helper_sha256"],
                "requested_ms": info["task_facts"]["requested_ms"], **progress, "quality_accepted": accepted}

    def accept_frame(self, job_id, receipt_relative_path):
        with self.store.transaction():
            job, info = self._job(job_id); run, _ = self._active(info)
            contract_key = self._capture_contract(info, run)
            if job["state"] not in {"submission_unknown", "submitted", "waiting", "verified_complete"} or not self._events(_STARTED, job_id):
                raise GateError("FRAME_CAPTURE_NOT_AUTHORIZED")
            frame = self._frame(info, receipt_relative_path, decode=False)
            history = self._events(_ACCEPTED, job_id)
            latest = next((e for e in reversed(history) if e["requested_ms"] == frame["requested_ms"]), None)
            if latest == frame:
                return self.pending_capture_targets(job_id)
            progress = self._progress(info)
            if frame["requested_ms"] not in progress["pending"]:
                raise ContractError("verified intact target cannot be replaced")
            if any(frame["receipt_relative_path"] == e["receipt_relative_path"]
                   or frame["frame"]["relative_path"] == e["frame"]["relative_path"] for e in history):
                raise ContractError("repair requires fresh immutable JPEG and receipt paths")
            if self._frame(info, receipt_relative_path) != frame:
                raise ContractError("candidate source frame changed during validation")
            if latest:
                reason = next(e["reason"] for e in progress["invalid_frames"] if e["requested_ms"] == frame["requested_ms"])
                self.store.event(_REPAIRED, job_id, info["project_id"],
                                 {"requested_ms": frame["requested_ms"], "prior": latest, "replacement": frame, "invalid_reason": reason})
            self.store.event(_ACCEPTED, job_id, info["project_id"], frame)
            if contract_key:
                self.store.event(_VALIDATED, job_id, info["project_id"],
                                 {"contract_sha256": contract_key, "accepted_sha256": digest(frame), "requested_ms": frame["requested_ms"]})
        return self.pending_capture_targets(job_id)

    def _manifest(self, info, valid_frames):
        frames = [{"sequence": i, **frame} for i, frame in enumerate(valid_frames, 1)]
        if any(b["media_time_ms"] < a["media_time_ms"] for a, b in zip(frames, frames[1:])):
            raise ContractError("actual rVFC frame times move backwards")
        source = {key: info["source"][key] for key in ("account_ref", "file_id", "source_revision", "size_bytes", "path")}
        source["frame_count"] = len(frames)
        data = canonical({"schema": SCHEMA, "source": source, "selection": {"evidence_ref": info["target_evidence_ref"], "basis": info["selection_basis"]}, "frames": frames})
        if len(data) > 1024**2:
            raise ContractError("source-frame manifest exceeds 1 MiB")
        sha = hashlib.sha256(data).hexdigest()
        relative = info["capture_root"] + "/frames-" + sha[:24] + ".json"
        return data, {"relative_path": relative, "sha256": sha, "size_bytes": len(data), "frame_count": len(frames)}

    def prepare_manifest(self, job_id):
        job, info = self._job(job_id); self._active(info)
        progress = self.pending_capture_targets(job_id)
        if progress["pending"]:
            raise GateError("SOURCE_FRAMES_INCOMPLETE")
        data, manifest = self._manifest(info, progress["valid_frames"])
        relative = manifest["relative_path"]
        path = safe_path(self.store.root, relative, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True); reject_links(path.parent)
        if path.exists():
            if _read(path, len(data)) != data:
                raise ContractError("immutable source-frame manifest changed")
        else:
            with path.open("xb") as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
        return manifest

    def _completion_evidence(self, job_id, quality_review_path, *, run_id=None):
        """Read-only ledger/hash/QA binding; the bounded child does decoding."""
        job, info = self._job(job_id); run, _ = self._active(info, run_id)
        self._capture_contract_binding(info, run)
        provider = _json(job["provider_json"].encode())
        if (job["state"] not in {"submission_unknown", "submitted", "waiting", "verified_complete"}
                or provider.get("provider_id") != "browser-source-frame" or provider.get("runtime") != "authorized_browser"
                or job["max_cost_minor"] != 0 or job["estimated_cost_minor"] != 0
                or not any(e.get("normal_video_read") is True and e.get("ai_generation") is False
                           and e.get("request_id") == job["request_id"] for e in self._events(_STARTED, job_id))):
            raise GateError("FRAME_CAPTURE_NOT_AUTHORIZED")
        progress = self._progress(info)
        if progress["pending"]:
            raise ContractError("source-frame target set is not complete")
        data, manifest = self._manifest(info, progress["valid_frames"])
        if _read(safe_path(self.store.root, manifest["relative_path"]), manifest["size_bytes"]) != data:
            raise ContractError("source-frame manifest changed before quality binding")
        quality_review_path = validate_relative_path(quality_review_path)
        review_data = _read(safe_path(self.store.root, quality_review_path), 64 * 1024)
        review = _json(review_data)
        required = {"schema", "job_id", "manifest_sha256", "source", "state", "basis", "reviewer_ref", "evidence_ref", "method", "all_frames_reviewed", "source_alignment_verified", "timeline_coverage_sufficient", "unresolved_gaps"}
        _fields(review, required | {"notes"}, required)
        if (review["schema"] != REVIEW_SCHEMA or review["job_id"] != job_id or review["manifest_sha256"] != manifest["sha256"]
                or review["state"] != "approved" or review["basis"] != "source_frames"
                or review["method"] not in {"human_visual", "independent_visual_review"}
                or any(review[key] is not True for key in ("all_frames_reviewed", "source_alignment_verified", "timeline_coverage_sufficient"))
                or review["unresolved_gaps"] != []
                or content_identity(review["source"].get("account_ref"), review["source"]) != info["task_facts"]["content_identity"]):
            raise ContractError("independent manifest-bound source-frame quality approval required")
        check_id(review["reviewer_ref"]); check_id(review["evidence_ref"])
        receipt = {"manifest": manifest, "quality_review": {"relative_path": quality_review_path,
                   "sha256": hashlib.sha256(review_data).hexdigest(), "size_bytes": len(review_data)},
                   "parent_job_id": info["task_facts"]["parent_job_id"], "completion_scope": "source_frames_with_external_quality_review"}
        for previous in self._events(_COMPLETED, job_id):
            prior_review = previous["quality_review"]
            if prior_review["relative_path"] == quality_review_path and prior_review != receipt["quality_review"]:
                raise ContractError("a new manifest requires a new immutable quality-review path")
        return job, info, run, review, receipt

    def _repair_lineage(self, info, prior_manifest):
        """Reconstruct the old completion from ledger facts, not mutable files."""
        rows = self.store.connection.execute(
            "SELECT rowid,kind,payload_json FROM events WHERE record_id=? AND kind IN (?,?,?) ORDER BY rowid",
            (info["job_id"], _ACCEPTED, _REPAIRED, _COMPLETED)).fetchall()
        parsed = [(r[0], r[1], _json(r[2].encode())) for r in rows]
        completion = next(((n, e) for n, k, e in reversed(parsed) if k == _COMPLETED and e["manifest"] == prior_manifest), None)
        if completion is None:
            raise ContractError("retained artifact is not this execution's completed manifest")
        boundary, prior = completion
        expected = {}
        for n, kind, event in parsed:
            if n < boundary and kind == _ACCEPTED:
                expected[event["requested_ms"]] = event
        targets = info["task_facts"]["requested_ms"]
        if set(expected) != set(targets) or self._manifest(info, [expected[t]["frame"] for t in targets])[1] != prior_manifest:
            raise ContractError("old completion does not match immutable accepted-frame evidence")
        repairs = []
        for n, kind, event in parsed:
            if n <= boundary or kind != _REPAIRED:
                continue
            target = event["requested_ms"]
            if expected.get(target) != event["prior"]:
                raise ContractError("source-frame repair lineage is discontinuous")
            try:
                checked = self._frame(info, event["prior"]["receipt_relative_path"], decode=False)
            except (ContractError, OSError, KeyError, TypeError):
                checked = None
            if checked == event["prior"]:
                raise ContractError("repair cannot replace a currently intact old target")
            expected[target] = event["replacement"]
            repairs.append(event)
        progress = self._progress(info)
        if (not repairs or progress["pending"]
                or [expected[t]["frame"] for t in targets] != progress["valid_frames"]):
            raise ContractError("new manifest requires target-specific invalidation and repair lineage")
        return {"previous_manifest": prior_manifest, "previous_quality_review": prior["quality_review"], "repairs": repairs}

    def finalize(self, job_id, quality_review_path, *, run_id=None):
        """Append repairs; never rewrite a successful job/provider/input/QA receipt."""
        with self.store.transaction():
            job, info, run, review, receipt = self._completion_evidence(job_id, quality_review_path, run_id=run_id)
            completed = self._events(_COMPLETED, job_id)
            if not completed or any(completed[-1].get(k) != v for k, v in receipt.items()):
                if job["state"] == "verified_complete":
                    self.coordinator.record_source_frame_repair_observation(
                        run["run_id"], info["file_id"], job_id=job_id, quality_review_path=quality_review_path)
                    # The restricted API atomically appends its completion,
                    # including when called directly or resumed independently.
                    receipt = self._events(_COMPLETED, job_id)[-1]
                else:
                    self.coordinator.record_source_frame_observation(
                        run["run_id"], info["file_id"], job_id=job_id, quality_review_path=quality_review_path)
                    self.store.reconcile_job(job_id, state="verified_complete", receipt=receipt, settled_cost_minor=0)
                    parent = self.store.connection.execute("SELECT state FROM jobs WHERE job_id=?", (info["task_facts"]["parent_job_id"],)).fetchone()
                    if parent["state"] != "verified_complete":
                        self.store.reconcile_job(info["task_facts"]["parent_job_id"], state="verified_complete", receipt=receipt, settled_cost_minor=0)
                    self.store.event(_COMPLETED, job_id, info["project_id"], receipt)
        return {"job_id": job_id, "parent_job_id": info["task_facts"]["parent_job_id"], "state": "verified_complete", "manifest": receipt["manifest"], "quality_accepted": True}
