"""One-incident guarded reconciliation, READ ONLY unless --apply is explicit.

Only publish-07e53c432ffa489fb0934403196107d2 / job-9bd06f9f708c4de39876fdd5b1229fbe.
No decoder/model/network, source hash/copy, publication retry or request fulfilment.
The old Plan/intention/key/stage and primitive WAV/receipt are retained unchanged.
Default mode uses an explicitly read-only Store inspection view and no recovery.
--apply uses normal Store startup (which can quarantine incomplete staging), then
rechecks every guard before one transaction: journal CAS + reconcile_job + event.
Current publication code fingerprint is deliberately NOT substituted for the old
intention's recorded fingerprint. Any mismatch stops; no repair is guessed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from worldanalysis import audio_publication as ap
from worldanalysis.contracts import (ContractError, asset_key, canonical, digest,
                                     hash_file, reject_links, safe_path, validate_plan)
from worldanalysis.store import Store

PUBLISH = "publish-07e53c432ffa489fb0934403196107d2"
JOB = "job-9bd06f9f708c4de39876fdd5b1229fbe"
BUNDLE = "bundle-1bd430dbe7854d1b9193bf93bc5d5e88"
SOURCE_BUNDLE = "bundle-5aae4593a3d64ec8b7f2925a2a1096a7"
ASSET = ("asset-c4fbb2980dc1457d8baeef65c5a43690",
         "asset-stat-c6bebb49f5261e296622425a1aea2d14a74d807b8034867e28e33e326c6ed56e")
OLD_CODE_SHA = "553e125acdb81efa1be9264da78dc81d4a448bb1fe4186176da618a1317a1c8c"
EVENT = "audio_window_local_failure_reconciled"
SMALL_CAP, ATTEMPTED_CAP = 1048576, 67108864


def require(condition, reason):
    if not condition:
        raise ContractError("STOP: " + reason)


class ReadOnlyInspection(Store):
    """Inherited read contracts, without schema initialization/startup recovery.

    The underlying SQLite connection is mode=ro AND query_only; transaction/event
    methods fail rather than permitting a legacy read helper to create indexes.
    This object is never used for mutation or passed to an execution primitive.
    """
    def __init__(self):
        self.root = ROOT
        self.results_root = ROOT / "整理结果"
        self.db_path = self.results_root / "worldanalysis.sqlite3"
        reject_links(self.db_path)
        require(self.db_path.is_file(), "existing business DB required")
        self.max_file_bytes = 64 * 1024**2
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.execute("BEGIN")

    def transaction(self):
        raise ContractError("STOP: read-only inspection cannot write even a legacy index")

    def event(self, *args, **kwargs):
        raise ContractError("STOP: read-only inspection cannot append events")


def verify(store):
    require(ROOT == Path("G:/Worldanalysis").resolve(strict=True), "fixed authorized workspace mismatch")
    p = store.connection.execute("SELECT * FROM publications WHERE publish_id=?", (PUBLISH,)).fetchone()
    require(p is not None, "exact failed publication missing")
    p = dict(p)
    require(p["bundle_id"] == BUNDLE and p["revision"] == 4 and p["state"] in {"staging", "quarantined"}, "exact old journal identity/state mismatch")
    require(all(p[k] is None for k in ("bundle_json", "manifest_sha256", "expected_files_json", "published_at")), "attempt has prepared/published evidence; do not mark failed")
    require(p["staging_relative_path"] == "证据/.staging-" + PUBLISH and p["final_relative_path"] == "证据/v0004", "old stage/final paths changed")
    root = store.project_root(p["project_id"])
    stage = safe_path(root, p["staging_relative_path"], must_exist=False)
    final = safe_path(root, p["final_relative_path"], must_exist=False)
    require(stage.is_dir() and not list(stage.iterdir()) and not final.exists(), "stage must remain empty and final absent")
    require(not store.connection.execute("SELECT 1 FROM events WHERE kind='bundle_published' AND record_id=?", (PUBLISH,)).fetchone(), "publication completion event exists")
    _, intended = ap._event(store, "audio_window_derivation_intended", PUBLISH, p["project_id"])
    _, registered = ap._event(store, "publish_registered", PUBLISH, p["project_id"])
    spec = intended["specification"]
    require(spec["source_bundle_id"] == SOURCE_BUNDLE and spec["project_id"] == p["project_id"], "old source publication scope changed")
    require(spec["implementation"]["publication_code_sha256"] == OLD_CODE_SHA, "not this known historical local implementation")
    require(spec["implementation"]["primitive_code_sha256"] == hash_file(Path(ap.primitive.__file__))[0]
            and spec["implementation"]["media_code_sha256"] == hash_file(Path(ap.media.__file__))[0], "primitive/media code changed; outcome needs separate review")
    require(spec["locator"] == ap._window(2940, 4420) and asset_key(spec["native"]["asset"]) == ASSET, "old asset/window differs")
    require(digest([ap._POLICY, spec]) == p["idempotency_key"], "old intention/journal key mismatch")
    require(registered == {"operation": ap._POLICY, "source_bundle_id": SOURCE_BUNDLE}, "old registration event differs")
    provider = intended["provider"]
    require(intended["job_id"] == JOB and intended["request_is_authorization"] is False and intended["cost_minor"] == 0, "old execution/grant/cost differs")
    require(provider["provider_id"] == "local_audio_window" and provider["runtime"] == "local_cpu"
            and provider["implementation_version"] == "window-" + digest(spec["implementation"])[:32]
            and provider["config_version"] == "audio-" + p["idempotency_key"][:40], "old registered provider/config key differs")
    ap.published._provider(store, provider)
    job = store.connection.execute("SELECT * FROM jobs WHERE job_id=?", (JOB,)).fetchone()
    require(job is not None, "exact old job missing")
    job = dict(job)
    require(job["project_id"] == p["project_id"] and job["operation"] == "native_extract"
            and job["input_revision"] == ASSET[0] + ":" + ASSET[1] and ap.published._decode(job["provider_json"]) == provider,
            "old job project/input/provider differs")
    require(job["state"] in {"running", "failed"} and all(job[k] == 0 for k in ("estimated_cost_minor", "max_cost_minor", "reserved_cost_minor"))
            and job["settled_cost_minor"] in {None, 0} and job["request_id"] is None and job["remote_task_id"] is None,
            "job may involve unknown/remote/nonzero-cost execution; no local settlement permitted")
    require(not store.connection.execute("SELECT 1 FROM publications p JOIN json_each(p.bundle_json,'$.artifacts') a WHERE p.bundle_json IS NOT NULL AND json_extract(a.value,'$.job_id')=? LIMIT 1", (JOB,)).fetchone(), "job already owns prepared/published artifacts")
    source = ap._source(store, p["project_id"], SOURCE_BUNDLE)
    native = ap._native(store, source, *ASSET, spec["locator"])
    original_budget = ap._budget(source)
    require(original_budget["max_input_bytes"] == ATTEMPTED_CAP and spec == ap._spec(source, native, spec["locator"], spec["implementation"], original_budget), "original source/stat/grant/native/remaining budget changed")
    planrow = store.connection.execute("SELECT plan_json FROM plans WHERE plan_id=? AND project_id=?", (intended["intention_plan_id"], p["project_id"])).fetchone()
    require(planrow is not None, "durable old intention Plan missing")
    plan = ap.published._decode(planrow[0])
    validate_plan(plan)
    require(plan["program_validation"]["status"] == "pending" and digest(plan) == intended["intention_plan_sha256"], "old intention Plan changed")
    expected_plan = ap._plan(source, native, spec["locator"], provider, job, PUBLISH, original_budget, created=plan["created_at"],
        ids=(plan["capability_observations"][-1]["observation_id"], plan["permission_checks"][-1]["check_id"]))
    expected_plan["plan_id"] = plan["plan_id"]
    require(plan == expected_plan, "old durable Plan is not the original bounded local intention")
    require(intended["request"] is not None, "known incident must retain its genuine queued proposal")
    request_id = intended["request"]["request_id"]
    require(ap._request(store, request_id, p["project_id"], source, native, spec["locator"]) == intended["request"], "original queued proposal provenance changed")
    output_relative = intended["primitive_output_relative"]
    require(output_relative == "窗口派生/synthetic-tts-2940-4420.wav", "not the already-inspected primitive output")
    wav = safe_path(ROOT, output_relative)
    manifest = safe_path(ROOT, output_relative + ".manifest.json")
    require(not wav.with_name(wav.name + ".prepared.json").exists(), "primitive has unresolved prepared state")
    manifest_identity = hash_file(manifest)
    proof = ap.primitive._read_json(manifest)
    small_budget = deepcopy(original_budget)
    small_budget["max_input_bytes"] = SMALL_CAP
    ap._proof(proof, native, spec["locator"], output_relative, small_budget, wav)
    require(hash_file(manifest) == manifest_identity, "primitive manifest changed during verification")
    require(proof["received_bytes"] == 327726 and proof["sample_count"] == 23680 and proof["byte_length"] == 47404,
            "known actual primitive evidence differs")
    # _proof compares ALL request fields against this source/root/output/window,
    # identity/output cap. The sole historical failure was 1MiB != 64MiB.
    require(proof["request"]["max_received_bytes"] == SMALL_CAP and proof["sha256"].startswith("b05d"), "known receipt cap/WAV differs")
    receipt = {"reconciliation_policy": "audio-local-existing-receipt-cap-mismatch-v1", "publish_id": PUBLISH, "job_id": JOB,
        "old_idempotency_key": p["idempotency_key"], "old_provider": provider, "intention_sha256": digest(intended),
        "reason": "WINDOW_RECEIPT_BINDING_MISMATCH_existing_manifest_budget", "state": "failed",
        "local_only": True, "network_executed": False, "decoder_executed_this_attempt": False,
        "source_received_bytes_this_attempt": 0, "historical_primitive_received_bytes": proof["received_bytes"],
        "attempted_max_received_bytes": ATTEMPTED_CAP, "existing_manifest_max_received_bytes": SMALL_CAP,
        "primitive_manifest_sha256": manifest_identity[0], "derived_wav_sha256": proof["sha256"], "derived_wav_byte_length": proof["byte_length"],
        "source_registration_sha256": digest(native["registration"]), "request_id": request_id, "request_remains_queued": True,
        "settled_cost_minor": 0, "semantic_read_performed": False, "gaps_resolved": False}
    events = store.connection.execute("SELECT * FROM events WHERE kind=? AND record_id=?", (EVENT, PUBLISH)).fetchall()
    if job["state"] == "failed":
        require(p["state"] == "quarantined" and job["settled_cost_minor"] == 0 and ap.published._decode(job["receipt_json"] or "{}") == receipt,
                "old failed receipt differs; no overwrite allowed")
        require(len(events) == 1 and events[0]["project_id"] == p["project_id"] and ap.published._decode(events[0]["payload_json"]) == receipt,
                "old failed event differs")
    else:
        require(job["receipt_json"] is None and not events, "running job already has unexplained receipt/event")
    return p, job, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="after successful read-only verification, atomically reconcile only the exact incident")
    args = parser.parse_args()
    with ReadOnlyInspection() as readonly:
        before, old_job, receipt = verify(readonly)
    if not args.apply or old_job["state"] == "failed":
        print(json.dumps({"status": "already_reconciled" if old_job["state"] == "failed" else "verified_read_only",
            "publish_id": PUBLISH, "job_id": JOB, "apply_requested": args.apply, "receipt": receipt}, ensure_ascii=False, indent=2))
        return
    # The ONLY constructor with recovery is behind explicit --apply, after all
    # preflight guards. Normal Store recovery may quarantine the empty staging.
    with Store(ROOT) as store, store.transaction():
        p, job, current_receipt = verify(store)
        require(current_receipt == receipt and job["state"] == "running", "incident changed since read-only verification")
        changed = store.connection.execute("UPDATE publications SET state='quarantined',reason=? WHERE publish_id=? AND project_id=? AND state=? AND idempotency_key=? AND bundle_json IS NULL AND manifest_sha256 IS NULL AND expected_files_json IS NULL AND published_at IS NULL",
            (receipt["reason"] + "; verified local pre-decode failure, zero cost; never retry old key", PUBLISH, p["project_id"], p["state"], p["idempotency_key"])).rowcount
        require(changed == 1, "failed publication quarantine CAS did not affect exactly one row")
        settled = store.reconcile_job(JOB, state="failed", receipt=receipt, settled_cost_minor=0)
        require(settled["state"] == "failed" and settled["reserved_cost_minor"] == 0 and settled["settled_cost_minor"] == 0, "formal zero-cost reconciliation failed")
        store.event(EVENT, PUBLISH, p["project_id"], receipt)
        verify(store)  # Full immutable receipt/event checks BEFORE transaction commit.
    print(json.dumps({"status": "reconciled", "publish_id": PUBLISH, "publication_state": "quarantined", "job_id": JOB,
        "job_state": "failed", "settled_cost_minor": 0, "request_state": "queued", "old_key_retry_allowed": False,
        "source_decoded": False, "primitive_files_modified": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("FAILED_CLOSED: " + type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        raise SystemExit(2)
