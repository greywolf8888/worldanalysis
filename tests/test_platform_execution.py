"""Synthetic JPEG/receipt fixtures on temporary SQLite; no browser acceptance claims."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from worldanalysis.contracts import ContractError, canonical, digest
from worldanalysis.platform_execution import RECEIPT_SCHEMA, REVIEW_SCHEMA, SourceFrameExecution
from worldanalysis.platform_first import PlatformFirst
from worldanalysis.policy import GateError
from worldanalysis.store import Store


class SourceFrameExecutionTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "_ledger/devpack/cloud-state-20261116/frame-repair-temp/test-tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="source-frame-execution-", dir=base)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.store.close())
        self.project = self.store.create_project(topic="Synthetic source-frame execution")
        self.coord = PlatformFirst(self.store, allow_synthetic=True)
        self.source = {"account_ref": "account-fixture", "file_id": "101", "source_revision": "real-version-fixture", "size_bytes": 300,
                       "path": "/apps/教育分析学习/测试/一.mp4"}
        inventory = {"schema_version": "1.0", "inventory_id": "synthetic-frame-inventory", "account_ref": self.source["account_ref"],
                     "root_path": "/apps/教育分析学习/测试", "enumeration_complete": True,
                     "evidence_ref": "synthetic-enumeration", "evidence_class": "synthetic",
                     "files": [{k: v for k, v in self.source.items() if k != "account_ref"} | {"duration_ms": 2000}]}
        self.run = self.coord.begin_run(self.project, inventory)["run_id"]
        (self.root / "manuscript.txt").write_text("A complete synthetic native manuscript", encoding="utf-8")
        self.coord.record_product_observation(self.run, "101", "manuscript", {"state": "present", "evidence_ref": "synthetic-native",
             "relative_path": "manuscript.txt", "coverage": {"state": "sufficient", "basis": "full_transcript", "evidence_ref": "synthetic-native-quality"}})
        self.coord.record_product_observation(self.run, "101", "courseware", {"state": "missing", "evidence_ref": "synthetic-no-native-courseware"})
        self.coord.seal_first_pass(self.run)
        (self.root / "capture.js").write_text("/* synthetic fixture helper, never executed */", encoding="utf-8")
        self.execution = SourceFrameExecution(self.store, self.coord)
        self.created = self.create()
        self.job = self.created["job_id"]
        self.parent = self.created["parent_job_id"]

    def create(self, targets=None):
        return self.execution.create(self.run, "101", [0, 1000] if targets is None else targets,
                                     helper_path="capture.js", target_evidence_ref="synthetic-caption-and-image-timestamps")

    def authorize(self):
        self.store.submission_authorizer = lambda row: {"state": "granted", "grant_ref": "synthetic-normal-video-read",
                                                       "job_id": row["job_id"], "project_id": row["project_id"]}
        return self.execution.start(self.job)

    def frame(self, target, tag="a", color="blue", size=(24, 16)):
        base = self.created["capture_root"]
        relative = base + "/frames/" + str(target) + "-" + tag + ".jpg"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = io.BytesIO(); Image.new("RGB", size, color).save(stream, "JPEG")
        data = stream.getvalue(); path.write_bytes(data)
        receipt = {"schema": RECEIPT_SCHEMA, "source": self.source, "requested_ms": target,
                   "frame_callback": {"mediaTime": target / 1000 + 0.003, "presentedFrames": 1},
                   "source_identity_check": {"file_id_match": True, "version_token_match": True},
                   "helper_sha256": self.created["helper_sha256"], "relative_path": relative,
                   "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "width": size[0], "height": size[1], "format": "jpeg",
                   "playback_duration_ms": 2000, "locator": "requestVideoFrameCallback"}
        receipt_relative = base + "/receipts/" + str(target) + "-" + tag + ".json"
        (self.root / receipt_relative).parent.mkdir(parents=True, exist_ok=True)
        self.save(receipt_relative, receipt)
        return receipt_relative, receipt

    def save(self, relative, value):
        (self.root / relative).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def review(self, manifest):
        return {"schema": REVIEW_SCHEMA, "job_id": self.job, "manifest_sha256": manifest["sha256"], "source": self.source,
                "state": "approved", "basis": "source_frames", "reviewer_ref": "synthetic-independent-reviewer",
                "evidence_ref": "synthetic-quality-review", "method": "independent_visual_review",
                "all_frames_reviewed": True, "source_alignment_verified": True, "timeline_coverage_sufficient": True,
                "unresolved_gaps": [], "notes": "Synthetic fixture assertions, not real video quality acceptance"}

    def test_start_requires_independent_durable_zero_cost_normal_read_grant(self):
        blocked = self.execution.start(self.job)
        self.assertFalse(blocked["may_capture"])
        receipt, _ = self.frame(0)
        with self.assertRaises(GateError):
            self.execution.accept_frame(self.job, receipt)
        with self.store.transaction():
            with self.assertRaisesRegex(GateError, "OUTER_TRANSACTION_CANNOT_AUTHORIZE_SEND"):
                self.execution.start(self.job)
        granted = self.authorize()
        self.assertTrue(granted["may_capture"])
        self.assertTrue(granted["may_send"])
        self.assertFalse(self.coord.policy.request_new_generation)
        with Store(self.root) as reader:
            job = reader.connection.execute("SELECT state,estimated_cost_minor,reserved_cost_minor FROM jobs WHERE job_id=?", (self.job,)).fetchone()
            self.assertEqual(tuple(job), ("submission_unknown", 0, 0))
        self.assertFalse(self.execution.start(self.job)["may_send"])
        self.assertTrue(self.execution.start(self.job)["may_capture"])

    def test_partial_restart_reuses_valid_frames_and_requires_independent_quality_review(self):
        self.authorize()
        parent_before = tuple(self.store.connection.execute("SELECT input_revision,provider_json FROM jobs WHERE job_id=?", (self.parent,)).fetchone())
        receipt, _ = self.frame(0)
        self.assertEqual(self.execution.accept_frame(self.job, receipt)["pending"], [1000])
        self.store.close(); self.store = Store(self.root)
        self.coord = PlatformFirst(self.store, allow_synthetic=True)
        self.execution = SourceFrameExecution(self.store, self.coord)
        self.assertEqual(self.create()["job_id"], self.job)
        self.assertEqual(self.execution.pending_capture_targets(self.job)["pending"], [1000])
        self.assertTrue(self.execution.start(self.job)["may_capture"])
        receipt, _ = self.frame(1000)
        self.assertEqual(self.execution.accept_frame(self.job, receipt)["pending"], [])
        manifest = self.execution.prepare_manifest(self.job)
        self.assertEqual(manifest, self.execution.prepare_manifest(self.job))
        self.assertEqual(self.coord.status(self.run)["products"][1]["observation"]["state"], "missing")
        self.assertEqual(self.store.connection.execute("SELECT state FROM jobs WHERE job_id=?", (self.job,)).fetchone()[0], "submission_unknown")
        with self.assertRaises(OSError):
            self.execution.finalize(self.job, "missing-review.json")
        review = self.review(manifest); review["timeline_coverage_sufficient"] = False
        self.save("review.json", review)
        with self.assertRaises(ContractError):
            self.execution.finalize(self.job, "review.json")
        review = self.review(manifest); review["manifest_sha256"] = "0" * 64
        self.save("review.json", review)
        with self.assertRaises(ContractError):
            self.execution.finalize(self.job, "review.json")
        self.save("review.json", self.review(manifest))
        result = self.execution.finalize(self.job, "review.json")
        self.assertTrue(result["quality_accepted"])
        self.assertEqual(self.coord.status(self.run)["products"][1]["observation"]["coverage"]["state"], "sufficient")
        self.assertEqual(self.coord.plan_missing(self.run)["supplements"], [])
        for job_id in (self.job, self.parent):
            self.assertEqual(self.store.connection.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0], "verified_complete")
        self.assertEqual(tuple(self.store.connection.execute("SELECT input_revision,provider_json FROM jobs WHERE job_id=?", (self.parent,)).fetchone()), parent_before)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
        self.assertEqual(self.create()["job_id"], self.job)
        self.assertTrue(self.execution.status(self.job)["quality_accepted"])

    def test_corrupt_frame_is_the_only_pending_target_and_new_receipt_repairs_progress(self):
        self.authorize()
        first, first_value = self.frame(0)
        second, _ = self.frame(1000)
        self.execution.accept_frame(self.job, first); self.execution.accept_frame(self.job, second)
        (self.root / first_value["relative_path"]).write_bytes(b"corrupt frame")
        progress = self.execution.pending_capture_targets(self.job)
        self.assertEqual(progress["pending"], [0])
        self.assertEqual([row["requested_ms"] for row in progress["valid_frames"]], [1000])
        repaired, _ = self.frame(0, tag="repaired", color="red")
        self.assertEqual(self.execution.accept_frame(self.job, repaired)["pending"], [])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='source_frame_accepted'").fetchone()[0], 3)

    def test_receipt_binding_rejects_wrong_source_version_proof_hash_dimensions_and_nonfinite_time(self):
        self.authorize()
        relative, value = self.frame(0)
        invalid = []
        for field, changed in (("source_revision", "different-content"), ("file_id", "102"), ("size_bytes", 301), ("account_ref", "different-account")):
            bad = copy.deepcopy(value); bad["source"][field] = changed; invalid.append(bad)
        bad = copy.deepcopy(value); bad["source_identity_check"]["file_id_match"] = 1; invalid.append(bad)
        bad = copy.deepcopy(value); bad["sha256"] = "0" * 64; invalid.append(bad)
        bad = copy.deepcopy(value); bad["width"] = 25; invalid.append(bad)
        bad = copy.deepcopy(value); bad["frame_callback"]["mediaTime"] = float("nan"); invalid.append(bad)
        bad = copy.deepcopy(value); bad["relative_path"] = "manuscript.txt"; invalid.append(bad)
        for number, bad in enumerate(invalid):
            with self.subTest(case=number):
                self.save(relative, bad)
                with self.assertRaises(ContractError):
                    self.execution.accept_frame(self.job, relative)
        self.assertEqual(self.execution.pending_capture_targets(self.job)["pending"], [0, 1000])

    def test_warm_retained_hash_checks_skip_decode_after_restart_but_first_bad_image_is_rejected(self):
        self.authorize()
        relative, value = self.frame(0)
        self.execution.accept_frame(self.job, relative)
        self.store.close(); self.store = Store(self.root)
        self.coord = PlatformFirst(self.store, allow_synthetic=True)
        self.execution = SourceFrameExecution(self.store, self.coord)
        with patch("worldanalysis.platform_execution._jpeg", side_effect=AssertionError("warm progress must hash, not decode")):
            self.assertEqual(self.execution.pending_capture_targets(self.job)["pending"], [1000])
            self.assertEqual(self.execution.status(self.job)["pending"], [1000])
        bad = bytes.fromhex("ffd8ff") + b"not a decodable JPEG"
        (self.root / value["relative_path"]).write_bytes(bad)
        value["sha256"] = hashlib.sha256(bad).hexdigest(); value["bytes"] = len(bad)
        self.save(relative, value)
        with self.assertRaises(ContractError):
            self.execution.accept_frame(self.job, relative)
        with patch("worldanalysis.platform_execution._jpeg", side_effect=AssertionError("changed retained hash must stay pending")):
            self.assertEqual(self.execution.pending_capture_targets(self.job)["pending"], [0, 1000])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='source_frame_accepted'").fetchone()[0], 1)

    def test_stale_rvfc_large_offset_is_rejected_but_40ms_seek_tolerance_is_allowed(self):
        self.authorize()
        relative, value = self.frame(1000)
        value["frame_callback"]["mediaTime"] = 392.0
        self.save(relative, value)
        with self.assertRaisesRegex(ContractError, "requested target tolerance"):
            self.execution.accept_frame(self.job, relative)
        self.assertEqual(self.execution.pending_capture_targets(self.job)["pending"], [0, 1000])
        for actual in (0.960, 1.040):
            with self.subTest(actual_seconds=actual):
                value["frame_callback"]["mediaTime"] = actual
                self.save(relative, value)
                checked = self.execution._frame(self.execution._job(self.job)[1], relative)
                self.assertAlmostEqual(checked["frame"]["media_time_ms"], actual * 1000)
        self.assertEqual(self.execution.accept_frame(self.job, relative)["pending"], [0])
        self.assertFalse(self.execution.status(self.job)["quality_accepted"])
        self.assertEqual(self.coord.status(self.run)["products"][1]["observation"]["state"], "missing")

    def test_receipt_integrity_and_revoked_execution_are_not_quality_completion(self):
        self.authorize()
        first, value = self.frame(0)
        second, _ = self.frame(1000)
        self.execution.accept_frame(self.job, first); self.execution.accept_frame(self.job, second)
        manifest = self.execution.prepare_manifest(self.job)
        self.save("review.json", self.review(manifest))
        self.store.reconcile_job(self.job, state="blocked", receipt={"evidence_ref": "synthetic-explicit-revocation"}, settled_cost_minor=0)
        with self.assertRaises(GateError):
            self.execution.finalize(self.job, "review.json")
        value["observed_at"] = "changed receipt"
        self.save(first, value)
        self.assertEqual(self.execution.pending_capture_targets(self.job)["pending"], [0])
        self.assertEqual(self.coord.status(self.run)["products"][1]["observation"]["state"], "missing")
        self.assertFalse(self.execution.status(self.job)["quality_accepted"])

    def test_cloud_location_move_reuses_execution_and_accepts_new_location_receipts(self):
        self.authorize()
        first, _ = self.frame(0)
        self.execution.accept_frame(self.job, first)
        inventory = copy.deepcopy(self.coord.status(self.run)["inventory"])
        inventory["inventory_id"] = "synthetic-moved-frame-inventory"
        inventory["root_path"] = "/apps/教育分析学习/已归档/测试"
        new_path = inventory["root_path"] + "/一.mp4"
        inventory["files"][0]["path"] = new_path
        moved_run = self.coord.begin_run(self.project, inventory)["run_id"]
        self.coord.record_product_observation(moved_run, "101", "courseware", {"state": "missing", "evidence_ref": "synthetic-moved-missing-visual"})
        self.coord.seal_first_pass(moved_run)
        reused = self.execution.create(moved_run, "101", [0, 1000], helper_path="capture.js", target_evidence_ref="synthetic-moved-target-evidence")
        self.assertEqual(reused["job_id"], self.job)
        self.assertEqual(reused["pending"], [1000])
        self.source["path"] = new_path
        second, _ = self.frame(1000)
        self.assertEqual(self.execution.accept_frame(self.job, second)["pending"], [])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs WHERE operation='capture-source-frames'").fetchone()[0], 1)
        self.assertEqual(len(self.execution.pending_capture_targets(self.job)["valid_frames"]), 2)

    def complete(self, targets=(0, 1000), review_path="review-old.json"):
        self.authorize()
        captures = [self.frame(t) for t in targets]
        for relative, _ in captures:
            self.execution.accept_frame(self.job, relative)
        manifest = self.execution.prepare_manifest(self.job)
        self.save(review_path, self.review(manifest))
        self.execution.finalize(self.job, review_path)
        return captures, manifest

    def test_complete_single_corruption_repairs_only_target_and_needs_new_bound_qa(self):
        captures, old_manifest = self.complete()
        old_qa = (self.root / "review-old.json").read_bytes()
        old_manifest_bytes = (self.root / old_manifest["relative_path"]).read_bytes()
        frozen = {j: tuple(self.store.connection.execute("SELECT input_revision,provider_json,receipt_json FROM jobs WHERE job_id=?", (j,)).fetchone()) for j in (self.job, self.parent)}
        good_bytes = (self.root / captures[1][1]["relative_path"]).read_bytes()
        (self.root / captures[0][1]["relative_path"]).write_bytes(b"synthetic corruption")
        self.assertEqual(self.create()["job_id"], self.job)
        self.assertEqual(self.execution.status(self.job)["pending"], [0])
        self.assertFalse(self.execution.status(self.job)["quality_accepted"])
        start = self.execution.start(self.job)
        self.assertTrue(start["may_capture"]); self.assertFalse(start["may_send"])
        forbidden, _ = self.frame(1000, "forbidden", "green")
        with self.assertRaisesRegex(ContractError, "intact target"):
            self.execution.accept_frame(self.job, forbidden)
        repaired, repaired_value = self.frame(0, "repair", "red")
        self.assertEqual(self.execution.accept_frame(self.job, repaired)["pending"], [])
        current = self.execution.prepare_manifest(self.job)
        self.assertNotEqual(current["sha256"], old_manifest["sha256"])
        self.assertFalse(self.execution.status(self.job)["quality_accepted"])
        with self.assertRaisesRegex(ContractError, "manifest-bound"):
            self.execution.finalize(self.job, "review-old.json")
        self.save("review-repair.json", self.review(current))
        self.assertTrue(self.execution.finalize(self.job, "review-repair.json")["quality_accepted"])
        self.assertTrue(self.execution.status(self.job)["quality_accepted"])
        self.assertFalse(self.execution.start(self.job)["may_capture"])
        self.execution.finalize(self.job, "review-repair.json")  # Idempotent, no receipt rewrite.
        self.assertEqual({j: tuple(self.store.connection.execute("SELECT input_revision,provider_json,receipt_json FROM jobs WHERE job_id=?", (j,)).fetchone()) for j in frozen}, frozen)
        self.assertEqual((self.root / "review-old.json").read_bytes(), old_qa)
        self.assertEqual((self.root / old_manifest["relative_path"]).read_bytes(), old_manifest_bytes)
        self.assertEqual((self.root / captures[0][1]["relative_path"]).read_bytes(), b"synthetic corruption")
        self.assertEqual((self.root / captures[1][1]["relative_path"]).read_bytes(), good_bytes)
        self.assertNotEqual(repaired_value["relative_path"], captures[0][1]["relative_path"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
        self.assertEqual(len(self.execution._events("source_frame_repaired", self.job)), 1)
        self.assertEqual(len(self.execution._events("source_frames_completed", self.job)), 2)
        lineage = self.execution._events("source_frames_completed", self.job)[-1]["repair_lineage"]
        self.assertEqual(lineage["previous_manifest"], old_manifest)
        self.assertEqual([e["requested_ms"] for e in lineage["repairs"]], [0])
        self.assertEqual(self.coord.status(self.run)["integrity_issues"], [])
        self.assertEqual(len(self.execution._events("platform_source_frame_product_repaired", project_id=self.project)), 1)
        self.store.close(); self.store = Store(self.root)
        self.coord = PlatformFirst(self.store, allow_synthetic=True)
        self.execution = SourceFrameExecution(self.store, self.coord)
        self.assertEqual(self.create()["job_id"], self.job)
        self.assertTrue(self.execution.status(self.job)["quality_accepted"])

    def test_intact_accepted_target_and_historical_paths_cannot_be_replaced(self):
        self.authorize()
        relative, value = self.frame(0)
        self.execution.accept_frame(self.job, relative)
        other, _ = self.frame(0, "other", "red")
        with self.assertRaisesRegex(ContractError, "intact target"):
            self.execution.accept_frame(self.job, other)
        self.execution.accept_frame(self.job, relative)
        value["observed_at"] = "modified historical receipt"
        self.save(relative, value)
        with self.assertRaisesRegex(ContractError, "fresh immutable"):
            self.execution.accept_frame(self.job, relative)
        self.assertEqual(len(self.execution._events("source_frame_accepted", self.job)), 1)
        self.assertEqual(self.execution.status(self.job)["pending"], [0, 1000])

    def test_repair_api_rejects_intact_old_artifact_wrong_revision_and_old_review_path_rewrite(self):
        captures, manifest = self.complete()
        with self.assertRaisesRegex(ContractError, "currently invalid"):
            self.coord.record_source_frame_repair_observation(self.run, "101", job_id=self.job, quality_review_path="review-old.json")
        inventory = copy.deepcopy(self.coord.status(self.run)["inventory"])
        inventory["inventory_id"] = "different-source-revision"
        inventory["files"][0]["source_revision"] = "source-v2"
        other = self.coord.begin_run(self.project, inventory)["run_id"]
        for product in ("manuscript", "courseware"):
            self.coord.record_product_observation(other, "101", product, {"state": "missing", "evidence_ref": "synthetic-different-source"})
        self.coord.seal_first_pass(other)
        with self.assertRaisesRegex(ContractError, "source content"):
            self.coord.record_source_frame_repair_observation(other, "101", job_id=self.job, quality_review_path="review-old.json")
        (self.root / captures[0][1]["relative_path"]).write_bytes(b"broken")
        fresh, _ = self.frame(0, "repair", "red")
        self.execution.accept_frame(self.job, fresh)
        new_manifest = self.execution.prepare_manifest(self.job)
        self.save("review-old.json", self.review(new_manifest))
        with self.assertRaisesRegex(ContractError, "immutable quality-review path"):
            self.execution.finalize(self.job, "review-old.json")

    def test_repair_observation_api_is_atomic_and_idempotent_without_rewriting_success(self):
        captures, _ = self.complete()
        frozen = tuple(self.store.connection.execute("SELECT input_revision,provider_json,receipt_json FROM jobs WHERE job_id=?", (self.job,)).fetchone())
        (self.root / captures[0][1]["relative_path"]).write_bytes(b"broken source fixture")
        fresh, _ = self.frame(0, "standalone-repair", "red")
        self.execution.accept_frame(self.job, fresh)
        manifest = self.execution.prepare_manifest(self.job)
        self.save("standalone-review.json", self.review(manifest))
        for _ in range(2):
            self.coord.record_source_frame_repair_observation(self.run, "101", job_id=self.job, quality_review_path="standalone-review.json")
        self.assertTrue(self.execution.status(self.job)["quality_accepted"])
        self.execution.finalize(self.job, "standalone-review.json")
        self.assertEqual(len(self.execution._events("source_frames_completed", self.job)), 2)
        self.assertEqual(tuple(self.store.connection.execute("SELECT input_revision,provider_json,receipt_json FROM jobs WHERE job_id=?", (self.job,)).fetchone()), frozen)

    def test_restored_old_target_cannot_be_replaced_at_repair_finalization(self):
        captures, _ = self.complete()
        old_path = self.root / captures[0][1]["relative_path"]
        original = old_path.read_bytes(); old_path.write_bytes(b"broken")
        fresh, _ = self.frame(0, "temporary-repair", "red")
        self.execution.accept_frame(self.job, fresh)
        manifest = self.execution.prepare_manifest(self.job)
        self.save("restored-review.json", self.review(manifest))
        old_path.write_bytes(original)  # Simulate independent local restoration.
        with self.assertRaisesRegex(ContractError, "currently invalid"):
            self.execution.finalize(self.job, "restored-review.json")
        self.assertEqual(len(self.execution._events("source_frames_completed", self.job)), 1)

    def legacy_bridge_fixture(self, bridge="669d7b2508fa9480618a009522473513f2933311e44fa2257640ab50bb985a2a"):
        # Simulate a previously persisted v1 ledger, ONLY in temporary SQLite.
        _, info = self.execution._job(self.job)
        info["task_facts"]["bridge_sha256"] = bridge
        provider = json.loads(self.store.connection.execute("SELECT provider_json FROM jobs WHERE job_id=?", (self.job,)).fetchone()[0])
        provider["config_version"] = "source-frames-" + digest([info["task_facts"]["helper_sha256"], bridge])[:40]
        with self.store.transaction():
            self.store.connection.execute("UPDATE jobs SET input_revision=?,provider_json=? WHERE job_id=?", (digest(info["task_facts"]), canonical(provider).decode(), self.job))
            self.store.connection.execute("UPDATE events SET payload_json=? WHERE kind='source_frames_created' AND record_id=?", (canonical(info).decode(), self.job))

    def use_audited_helper(self, targets):
        helper = Path(__file__).resolve().parents[1] / "_tools/browser-source-frame.js"
        (self.root / "capture.js").write_bytes(helper.read_bytes())
        self.created = self.create(targets); self.job = self.created["job_id"]; self.parent = self.created["parent_job_id"]
        self.assertEqual(self.created["helper_sha256"], "2f7991f3e140aa4ac91bcfb5f06ecdbc3cb6d2d38417baf7cb78a1422e9f5ac4")

    def test_audited_v1_alias_reuses_seven_verified_frames_and_preserves_immutable_jobs(self):
        targets = list(range(0, 700, 100))
        self.use_audited_helper(targets)
        self.complete(targets)
        self.legacy_bridge_fixture()
        frozen = [tuple(r) for r in self.store.connection.execute("SELECT job_id,input_revision,provider_json,receipt_json FROM jobs ORDER BY job_id")]
        from worldanalysis.platform_execution import _jpeg
        with patch("worldanalysis.platform_execution._jpeg", wraps=_jpeg) as decode:
            resumed = self.create(targets)
            self.assertEqual(decode.call_count, 7)  # Current-rule decoding happens once.
            self.create(targets); self.execution.start(self.job); self.execution.status(self.job)
            self.store.close(); self.store = Store(self.root)
            self.coord = PlatformFirst(self.store, allow_synthetic=True)
            self.execution = SourceFrameExecution(self.store, self.coord)
            self.create(targets); self.execution.start(self.job)
            self.assertEqual(decode.call_count, 7)  # Exact bytes stay warm across restart.
        self.assertEqual(resumed["job_id"], self.job)
        self.assertEqual(len(resumed["valid_frames"]), 7)
        self.assertTrue(resumed["quality_accepted"])
        self.assertFalse(self.execution.start(self.job)["may_capture"])
        self.assertEqual([tuple(r) for r in self.store.connection.execute("SELECT job_id,input_revision,provider_json,receipt_json FROM jobs ORDER BY job_id")], frozen)
        self.assertEqual(len(self.execution._events("source_frame_capture_contract_aliased", self.job)), 1)

    def test_audited_v1_partial_resume_decodes_each_new_frame_once(self):
        targets = list(range(0, 700, 100))
        self.use_audited_helper(targets); self.authorize()
        for target in targets[:2]:
            relative, _ = self.frame(target)
            self.execution.accept_frame(self.job, relative)
        self.legacy_bridge_fixture()
        from worldanalysis.platform_execution import _jpeg
        with patch("worldanalysis.platform_execution._jpeg", wraps=_jpeg) as decode:
            self.create(targets)
            self.assertEqual(decode.call_count, 2)
            self.execution.start(self.job)
            for number, target in enumerate(targets[2:], 3):
                relative, _ = self.frame(target)
                self.execution.accept_frame(self.job, relative)
                self.create(targets); self.execution.start(self.job)
                self.execution.accept_frame(self.job, relative)
                self.assertEqual(decode.call_count, number)
        self.assertEqual(len(self.execution._events("source_frame_capture_contract_aliased", self.job)), 1)
        self.assertEqual(len(self.execution._events("source_frame_contract_validated", self.job)), 7)
        self.assertEqual(self.execution.status(self.job)["pending"], [])
        self.assertFalse(self.execution.status(self.job)["quality_accepted"])

    def test_audited_v1_warm_cache_still_rejects_changed_jpeg_and_receipt_bytes(self):
        self.use_audited_helper([0, 1000])
        captures, _ = self.complete()
        self.legacy_bridge_fixture(); self.create()
        relative, value = captures[0]
        image_path, receipt_path = self.root / value["relative_path"], self.root / relative
        old_image, old_receipt = image_path.read_bytes(), receipt_path.read_bytes()
        with patch("worldanalysis.platform_execution._jpeg", side_effect=AssertionError("warm must hash exact bytes, not redecode")):
            image_path.write_bytes(b"changed JPEG")
            self.assertEqual(self.create()["pending"], [0])
            image_path.write_bytes(old_image)
            changed = copy.deepcopy(value); changed["observed_at"] = "changed receipt"
            self.save(relative, changed)
            self.assertEqual(self.create()["pending"], [0])
            stream = io.BytesIO(); Image.new("RGB", (24, 16), "red").save(stream, "JPEG")
            data = stream.getvalue(); image_path.write_bytes(data)
            changed["sha256"], changed["bytes"] = hashlib.sha256(data).hexdigest(), len(data)
            self.save(relative, changed)  # Self-consistent files must still match the immutable event.
            self.assertEqual(self.create()["pending"], [0])
            self.assertFalse(self.execution.status(self.job)["quality_accepted"])
            image_path.write_bytes(old_image); receipt_path.write_bytes(old_receipt)
            self.assertEqual(self.create()["pending"], [])
        self.assertEqual(len(self.execution._events("source_frame_contract_validated", self.job)), 2)
        self.assertEqual(len(self.execution._events("source_frame_capture_contract_aliased", self.job)), 1)

    def test_large_execution_and_repair_use_bounded_profile_without_changing_manifest_or_success(self):
        from worldanalysis.platform_products import inspect_product, inspect_source_frame_execution, verify_retained_artifact
        targets = list(range(0, 1600, 50))
        self.created = self.create(targets); self.job = self.created["job_id"]; self.parent = self.created["parent_job_id"]
        self.authorize()
        captures = [self.frame(t, size=(1920, 1080)) for t in targets]
        for relative, _ in captures:self.execution.accept_frame(self.job, relative)
        manifest = self.execution.prepare_manifest(self.job)
        manifest_bytes = (self.root / manifest["relative_path"]).read_bytes()
        self.save("large-qa.json", self.review(manifest))
        with self.assertRaisesRegex(ContractError, "total byte/pixel budget"):
            inspect_product(self.store, "courseware", manifest["relative_path"])
        events = self.store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        jobs = [tuple(r) for r in self.store.connection.execute("SELECT job_id,input_revision,provider_json,receipt_json,state FROM jobs ORDER BY job_id")]
        with patch("worldanalysis.platform_execution._jpeg", side_effect=AssertionError("no unbounded parent decoder")):
            artifact = inspect_source_frame_execution(self.store, self.coord, self.job, "large-qa.json")
        self.assertEqual(artifact["metrics"]["total_pixels"], 32 * 1920 * 1080)
        self.assertEqual(artifact["metrics"]["resource_validation"]["peak_inflight_frame_decodes"], 1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], events)
        self.assertEqual([tuple(r) for r in self.store.connection.execute("SELECT job_id,input_revision,provider_json,receipt_json,state FROM jobs ORDER BY job_id")], jobs)
        self.assertFalse(self.execution.status(self.job)["quality_accepted"])
        with patch("worldanalysis.platform_execution._jpeg", side_effect=AssertionError("no parent JPEG decode")):
            self.execution.finalize(self.job, "large-qa.json")
        self.assertEqual((self.root / manifest["relative_path"]).read_bytes(), manifest_bytes)
        frozen = {j: tuple(self.store.connection.execute("SELECT input_revision,provider_json,receipt_json FROM jobs WHERE job_id=?", (j,)).fetchone()) for j in (self.job, self.parent)}
        with patch("worldanalysis.source_frame_validator.validate_source_frame_files", side_effect=AssertionError("warm must not spawn decoder")):
            self.assertTrue(verify_retained_artifact(self.store, artifact))
            self.assertTrue(self.execution.status(self.job)["quality_accepted"])
            self.assertEqual(self.coord.status(self.run)["integrity_issues"], [])
            self.execution.finalize(self.job, "large-qa.json")
        (self.root / captures[0][1]["relative_path"]).write_bytes(b"synthetic broken old JPEG")
        replacement, _ = self.frame(0, "large-repair", "red", size=(1920, 1080))
        self.execution.accept_frame(self.job, replacement)
        repaired = self.execution.prepare_manifest(self.job); self.save("large-repair-qa.json", self.review(repaired))
        with patch("worldanalysis.platform_execution._jpeg", side_effect=AssertionError("repair decoder stays in child")):
            self.execution.finalize(self.job, "large-repair-qa.json")
        self.assertTrue(self.execution.status(self.job)["quality_accepted"])
        self.assertEqual({j: tuple(self.store.connection.execute("SELECT input_revision,provider_json,receipt_json FROM jobs WHERE job_id=?", (j,)).fetchone()) for j in frozen}, frozen)
        self.assertEqual(len(self.execution._events("source_frames_completed", self.job)), 2)

    def test_bound_product_rejects_unknown_job_ungranted_read_and_wrong_or_old_qa_before_decode(self):
        from worldanalysis.platform_products import inspect_source_frame_execution
        with patch("worldanalysis.source_frame_validator.validate_source_frame_files", side_effect=AssertionError("must bind before decode")):
            with self.assertRaises(ContractError):inspect_source_frame_execution(self.store, self.coord, "unknown-job", "qa.json")
            with self.assertRaises(GateError):inspect_source_frame_execution(self.store, self.coord, self.job, "qa.json")
        self.authorize()
        for target in (0, 1000):
            relative, _ = self.frame(target); self.execution.accept_frame(self.job, relative)
        manifest = self.execution.prepare_manifest(self.job)
        with self.assertRaisesRegex(ContractError, "execution-bound external review"):
            self.coord.record_product_observation(self.run, "101", "courseware", {"state": "present", "evidence_ref": "synthetic-schema-claim",
                "relative_path": manifest["relative_path"], "coverage": {"state": "sufficient", "basis": "source_frames", "evidence_ref": "synthetic-self-claim"}})
        for mutation in ({"job_id": "other-job"}, {"manifest_sha256": "0" * 64}, {"state": "rejected"}, {"unresolved_gaps": ["synthetic-gap"]}):
            self.save("bad-qa.json", {**self.review(manifest), **mutation})
            with patch("worldanalysis.source_frame_validator.validate_source_frame_files", side_effect=AssertionError("wrong QA before decode")):
                with self.assertRaisesRegex(ContractError, "manifest-bound"):
                    inspect_source_frame_execution(self.store, self.coord, self.job, "bad-qa.json")
        with self.assertRaisesRegex(ContractError, "unknown platform-pass fields"):
            self.coord.record_product_observation(self.run, "101", "courseware", {"state": "present", "evidence_ref": "synthetic-claim", "relative_path": manifest["relative_path"], "trusted": True})
        self.assertIsNone(self.coord._product(self.coord._run(self.run), "101", "courseware")[1]["artifact_json"])

    def test_timeout_and_qa_changed_after_child_never_publish_or_reconcile(self):
        import subprocess
        from worldanalysis.source_frame_validator import validate_source_frame_files
        self.authorize()
        for target in (0, 1000):
            relative, _ = self.frame(target); self.execution.accept_frame(self.job, relative)
        manifest = self.execution.prepare_manifest(self.job); review = self.review(manifest); self.save("race-qa.json", review)
        frozen = tuple(self.store.connection.execute("SELECT input_revision,provider_json,receipt_json,state FROM jobs WHERE job_id=?", (self.job,)).fetchone())
        with patch("worldanalysis.source_frame_validator.subprocess.run", side_effect=subprocess.TimeoutExpired("fixed-worker", 90)):
            with self.assertRaisesRegex(ContractError, "hard timeout"):
                self.execution.finalize(self.job, "race-qa.json")
        def changed(*args):
            result = validate_source_frame_files(*args)
            self.save("race-qa.json", {**review, "notes": "changed after bounded decode"})
            return result
        with patch("worldanalysis.source_frame_validator.validate_source_frame_files", side_effect=changed):
            with self.assertRaisesRegex(ContractError, "QA changed"):
                self.execution.finalize(self.job, "race-qa.json")
        self.assertEqual(tuple(self.store.connection.execute("SELECT input_revision,provider_json,receipt_json,state FROM jobs WHERE job_id=?", (self.job,)).fetchone()), frozen)
        self.assertIsNone(self.coord._product(self.coord._run(self.run), "101", "courseware")[1]["artifact_json"])
        self.assertFalse(self.execution.status(self.job)["quality_accepted"])

    def test_exact_d373_migration_is_read_only_until_decoder_bound_admission(self):
        from worldanalysis.platform_products import inspect_source_frame_execution
        self.use_audited_helper([0, 1000]); self.authorize()
        for target in (0, 1000):
            relative, _ = self.frame(target); self.execution.accept_frame(self.job, relative)
        manifest = self.execution.prepare_manifest(self.job); self.save("migration-qa.json", self.review(manifest))
        self.legacy_bridge_fixture("d3738f2c954a546d3a3f0534431ad467618f10225db95446b9171cd741319d41")
        frozen = tuple(self.store.connection.execute("SELECT input_revision,provider_json FROM jobs WHERE job_id=?", (self.job,)).fetchone())
        before = self.store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        with patch("worldanalysis.platform_execution._jpeg", side_effect=AssertionError("migration decode stays bounded in child")):
            inspect_source_frame_execution(self.store, self.coord, self.job, "migration-qa.json")
            self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)
            self.execution.finalize(self.job, "migration-qa.json")
            self.assertEqual(self.create()["job_id"], self.job)
            self.assertFalse(self.execution.start(self.job)["may_capture"])
        self.assertEqual(tuple(self.store.connection.execute("SELECT input_revision,provider_json FROM jobs WHERE job_id=?", (self.job,)).fetchone()), frozen)
        self.assertEqual(len(self.execution._events("source_frame_capture_contract_aliased", self.job)), 1)

    def _material_frames(self, count=2, full_hd=False):
        targets = list(range(0, count * 50, 50))
        self.created = self.create(targets); self.job = self.created["job_id"]; self.parent = self.created["parent_job_id"]
        self.authorize(); captures = []
        for index, point in enumerate(targets):
            relative, receipt = self.frame(point, color=((index * 7) % 256, (index * 13) % 256, (index * 29) % 256), size=(1920, 1080) if full_hd else (24, 16))
            self.execution.accept_frame(self.job, relative); captures.append((relative, receipt))
        manifest = self.execution.prepare_manifest(self.job)
        self.save("material-qa.json", self.review(manifest)); self.execution.finalize(self.job, "material-qa.json")
        content = "1\n00:00:00,000 --> 00:00:01,000\nORIGINAL synthetic ASR parameter 32\n\n2\n00:00:01,000 --> 00:00:01,900\nOriginal second synthetic cue\n"
        self.save("material-subtitle-response.json", {"schema": "worldanalysis.native-subtitle-response.v1", "http_status": 200,
            "acquisition": "passive_normal_page_response", "source_file_id": "101", "source_path": self.source["path"],
            "source_revision": self.source["source_revision"], "content": content, "bytes": len(content.encode())})
        return captures, manifest

    def test_large_source_collection_prepares_imports_and_reads_bundle_without_generic_decode_or_new_capture_job(self):
        from worldanalysis import course_delivery as delivery, platform_materials as materials
        captures, manifest = self._material_frames(32, True)
        source_files = [manifest["relative_path"], "material-qa.json", *[name for rel, frame in captures for name in (rel, frame["relative_path"])]]
        before = {name: (hashlib.sha256((self.root / name).read_bytes()).hexdigest(), (self.root / name).stat().st_mtime_ns) for name in source_files}
        capture_rows = [tuple(r) for r in self.store.connection.execute("SELECT job_id,input_revision,provider_json,receipt_json,state FROM jobs WHERE operation='capture-source-frames' ORDER BY job_id")]
        with self.assertRaisesRegex(ContractError, "total byte/pixel budget"):
            delivery.prepare_source_frames(self.root, self.project, manifest["relative_path"])
        with patch("worldanalysis.platform_first.PlatformFirst", return_value=self.coord):
            prepared = delivery.prepare_source_frame_execution(self.store, self.project, self.job, "material-qa.json", subtitle_response="material-subtitle-response.json")
            self.assertEqual(prepared["frame_count"], 32)
            raw = json.loads(Path(prepared["manifest_path"]).read_text("utf-8"))
            self.assertEqual(raw["source_frame_execution"]["job_id"], self.job)
            with patch("worldanalysis.platform_materials._image_readable", side_effect=AssertionError("retained JPEG must use hash readback")), patch("worldanalysis.platform_materials.extract", side_effect=AssertionError("no generic extract")), patch("worldanalysis.documents._Result.image", side_effect=AssertionError("no repeat native decode")):
                result = materials.import_materials(self.store, self.project, prepared["manifest_path"])
            self.assertIn("bundle_id", result)
            bundle = self.store.get_bundle(self.project, result["bundle_id"])
            self.assertEqual(len([b for b in bundle["blocks"] if b["kind"] == "image_occurrence"]), 32)
            images = [a for a in bundle["artifacts"] if a["kind"] == "image"]
            self.assertEqual({a["sha256"] for a in images}, {f["sha256"] for _, f in captures})
            self.assertEqual(len(images), 32)
            self.assertEqual(bundle["publication"]["state"], "published")
            metadata_artifact = next(a for a in bundle["artifacts"] if "/metadata/" in a["relative_path"])
            metadata = json.loads((self.store.project_root(self.project) / metadata_artifact["relative_path"]).read_text("utf-8"))
            self.assertEqual(metadata["source_frame_execution"], raw["source_frame_execution"])
            self.assertTrue(metadata["external_visual_review_bound"])
            self.assertEqual(len(metadata["image_inventory"]), 32)
            self.assertTrue(any(b.get("origin") == "asr" and "ORIGINAL synthetic ASR" in b.get("text", "") for b in bundle["blocks"]))
            repeated = delivery.prepare_source_frame_execution(self.store, self.project, self.job, "material-qa.json", subtitle_response="material-subtitle-response.json")
            self.assertEqual(repeated["created_files"], [])
            with patch("worldanalysis.platform_materials._extract_source_frames", side_effect=AssertionError("unchanged bound input must reuse Bundle")):
                second = materials.import_materials(self.store, self.project, repeated["manifest_path"])
            self.assertEqual(second["bundle_id"], result["bundle_id"])
        self.assertEqual([tuple(r) for r in self.store.connection.execute("SELECT job_id,input_revision,provider_json,receipt_json,state FROM jobs WHERE operation='capture-source-frames' ORDER BY job_id")], capture_rows)
        self.assertEqual({name: (hashlib.sha256((self.root / name).read_bytes()).hexdigest(), (self.root / name).stat().st_mtime_ns) for name in source_files}, before)

    def test_source_material_references_cannot_self_trust_wrong_job_qa_source_or_partial_frames(self):
        from worldanalysis import course_delivery as delivery, platform_materials as materials
        _, manifest = self._material_frames()
        with patch("worldanalysis.platform_first.PlatformFirst", return_value=self.coord):
            with self.assertRaises(ContractError):delivery.prepare_source_frame_execution(self.store, self.project, "unknown-job", "material-qa.json")
            prepared = delivery.prepare_source_frame_execution(self.store, self.project, self.job, "material-qa.json")
            original = json.loads(Path(prepared["manifest_path"]).read_text("utf-8"))
            mutations = []
            bad = copy.deepcopy(original); bad["source_frame_execution"]["job_id"] = "unknown-job"; mutations.append(bad)
            bad = copy.deepcopy(original); bad["source_frame_execution"]["trusted"] = True; mutations.append(bad)
            bad = copy.deepcopy(original); bad["source_frame_execution"]["quality_review_sha256"] = "0" * 64; mutations.append(bad)
            bad = copy.deepcopy(original); bad["source"]["file_id"] = "999"; mutations.append(bad)
            bad = copy.deepcopy(original); bad["images"] = bad["images"][:1]; mutations.append(bad)
            bad = copy.deepcopy(original); bad["images"][0]["timestamp_ms"] += 1; mutations.append(bad)
            directory = Path(prepared["manifest_path"]).parent
            for index, value in enumerate(mutations):
                path = directory / ("synthetic-bad-material-" + str(index) + ".json"); path.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
                with self.subTest(index=index), self.assertRaises(ContractError):materials.import_materials(self.store, self.project, path)
            with self.assertRaises(ContractError):materials.import_materials(self.store, self.project, prepared["manifest_path"], limits={"max_total_bytes": 128 * 1024**2})
        self.assertEqual(self.store.list_bundles(self.project), [])
        self.assertTrue(self.execution.status(self.job)["quality_accepted"])

    def test_source_material_prepare_requires_completed_job_and_the_issued_review_path(self):
        from worldanalysis import course_delivery as delivery
        self.authorize()
        for target in (0, 1000):
            relative, _ = self.frame(target); self.execution.accept_frame(self.job, relative)
        manifest = self.execution.prepare_manifest(self.job); self.save("pending-material-qa.json", self.review(manifest))
        with patch("worldanalysis.platform_first.PlatformFirst", return_value=self.coord):
            with self.assertRaisesRegex(ContractError, "completed current execution"):
                delivery.prepare_source_frame_execution(self.store, self.project, self.job, "pending-material-qa.json")
        self.execution.finalize(self.job, "pending-material-qa.json")
        (self.root / "unissued-copy-qa.json").write_bytes((self.root / "pending-material-qa.json").read_bytes())
        with patch("worldanalysis.platform_first.PlatformFirst", return_value=self.coord):
            with self.assertRaisesRegex(ContractError, "substitute another approved review"):
                delivery.prepare_source_frame_execution(self.store, self.project, self.job, "unissued-copy-qa.json")
        self.assertEqual(list((self.root / self.created["capture_root"]).glob("source-frame-material-manifest-*.json")), [])
        self.assertEqual(self.store.list_bundles(self.project), [])

    def test_changed_issued_qa_or_frame_blocks_material_import_without_reopening_source_job(self):
        from worldanalysis import course_delivery as delivery, platform_materials as materials
        captures, _ = self._material_frames()
        with patch("worldanalysis.platform_first.PlatformFirst", return_value=self.coord):
            prepared = delivery.prepare_source_frame_execution(self.store, self.project, self.job, "material-qa.json")
            frozen = tuple(self.store.connection.execute("SELECT input_revision,provider_json,receipt_json,state FROM jobs WHERE job_id=?", (self.job,)).fetchone())
            qa = (self.root / "material-qa.json").read_bytes()
            (self.root / "material-qa.json").write_bytes(qa + b" ")
            with self.assertRaises(ContractError):materials.import_materials(self.store, self.project, prepared["manifest_path"])
            (self.root / "material-qa.json").write_bytes(qa)
            (self.root / captures[0][1]["relative_path"]).write_bytes(b"changed source image")
            with self.assertRaises(ContractError):materials.import_materials(self.store, self.project, prepared["manifest_path"])
        self.assertEqual(self.store.list_bundles(self.project), [])
        self.assertEqual(tuple(self.store.connection.execute("SELECT input_revision,provider_json,receipt_json,state FROM jobs WHERE job_id=?", (self.job,)).fetchone()), frozen)

    def test_bound_consumer_rejects_every_mutated_normalized_image_projection(self):
        from worldanalysis import course_delivery as delivery, platform_materials as materials
        self._material_frames()
        with patch("worldanalysis.platform_first.PlatformFirst", return_value=self.coord):
            prepared = delivery.prepare_source_frame_execution(self.store, self.project, self.job, "material-qa.json")
            prepare = materials._prepare_package
            changes = [("state", "missing"), ("state", "unreadable"), ("timestamp_ms", 777),
                       ("sha256", "0" * 64), ("size_bytes", 1), ("occurrence", 91),
                       ("source_kind", "platform_note_image"), ("path", "frames/not-ledger.jpg"),
                       ("snapshot_path", "采集输入/not-ledger.jpg"), ("role", "note"), ("remove", None)]
            for key, replacement in changes:
                def tampered(*args, **kwargs):
                    path = prepare(*args, **kwargs)
                    value = json.loads(path.read_text("utf-8"))
                    record = next(r for r in value["attachments"] if r["role"] == "image")
                    if key == "remove":value["attachments"].remove(record)
                    else:record[key] = replacement
                    path.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
                    return path
                with self.subTest(key=key, value=replacement), patch.object(materials, "_prepare_package", side_effect=tampered):
                    with self.assertRaisesRegex(ContractError, "complete present ledger projection"):
                        materials.import_materials(self.store, self.project, prepared["manifest_path"])
                self.assertEqual(self.store.list_bundles(self.project), [])
        self.assertTrue(self.execution.status(self.job)["quality_accepted"])

    def test_bound_emitter_consumes_one_parsed_snapshot_not_a_second_read(self):
        from worldanalysis import course_delivery as delivery, platform_materials as materials
        self._material_frames()
        with patch("worldanalysis.platform_first.PlatformFirst", return_value=self.coord):
            prepared = delivery.prepare_source_frame_execution(self.store, self.project, self.job, "material-qa.json")
            emit, read = materials._extract_source_frames, materials._read
            second_reads = []
            def with_second_read_trap(path, output_dir, **kwargs):
                snapshot = kwargs["snapshot"]
                def changed_second_read(candidate, maximum):
                    if Path(candidate) == Path(path):
                        second_reads.append(str(candidate))
                        bad = copy.deepcopy(snapshot)
                        for item in bad["attachments"]:
                            if item["role"] == "image":item["state"] = "missing"
                        return json.dumps(bad, ensure_ascii=False).encode("utf-8")
                    return read(candidate, maximum)
                with patch.object(materials, "_read", side_effect=changed_second_read):
                    return emit(path, output_dir, **kwargs)
            with patch.object(materials, "_extract_source_frames", side_effect=with_second_read_trap):
                bundle = materials.import_materials(self.store, self.project, prepared["manifest_path"])
        self.assertEqual(second_reads, [])
        self.assertEqual(len([b for b in bundle["blocks"] if b["kind"] == "image_occurrence"]), 2)
        self.assertEqual(bundle["publication"]["state"], "published")
        with patch.object(materials, "_extract_source_frames", side_effect=AssertionError("healthy get_bundle must not extract")):
            self.assertEqual(self.store.get_bundle(self.project, bundle["bundle_id"])["bundle_id"], bundle["bundle_id"])

    def test_restricted_entry_points_do_not_commit_callers_memory_transaction(self):
        import sqlite3
        import threading
        from types import SimpleNamespace
        from worldanalysis import course_delivery as delivery, platform_materials as materials
        connection = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE sentinel(value INTEGER)")
        holder = SimpleNamespace(connection=connection, _lock=threading.RLock())
        entries = [lambda: materials._source_frame_state(holder, "project-fixture", {}),
                   lambda: delivery.prepare_source_frame_execution(holder, "project-fixture", "bad-job", "bad-qa.json")]
        with patch("worldanalysis.platform_first.PlatformFirst", side_effect=AssertionError("must reject before constructor/schema")):
            for entry in entries:
                connection.execute("BEGIN"); connection.execute("INSERT INTO sentinel VALUES(1)")
                with self.assertRaisesRegex(ContractError, "outer transaction"):entry()
                self.assertTrue(connection.in_transaction)
                connection.rollback()
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sentinel").fetchone()[0], 0)
                with self.assertRaisesRegex(ContractError, "outer transaction"):
                    with Store.transaction(holder):
                        connection.execute("INSERT INTO sentinel VALUES(1)")
                        entry()
                self.assertFalse(connection.in_transaction)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sentinel").fetchone()[0], 0)

    def test_unknown_bridge_or_known_alias_with_changed_evidence_configuration_is_refused(self):
        self.use_audited_helper([0, 1000])
        self.legacy_bridge_fixture("0" * 64)
        with self.assertRaisesRegex(ContractError, "bridge capture-contract alias"):
            self.create()
        with self.assertRaisesRegex(ContractError, "bridge capture-contract alias"):
            self.execution.start(self.job)
        self.legacy_bridge_fixture()
        for extra in ({"target_evidence_ref": "different-evidence"}, {"selection_basis": "different-basis"}):
            kwargs = {"helper_path": "capture.js", "target_evidence_ref": "synthetic-caption-and-image-timestamps", **extra}
            with self.assertRaisesRegex(ContractError, "bridge capture-contract alias"):
                self.execution.create(self.run, "101", [0, 1000], **kwargs)
        configured = PlatformFirst(self.store, allow_synthetic=True, config_version="different-parent-config")
        with self.assertRaisesRegex(ContractError, "bridge capture-contract alias"):
            SourceFrameExecution(self.store, configured).start(self.job)
        self.assertEqual(self.execution._events("source_frame_capture_contract_aliased", self.job), [])

    def test_target_and_actual_helper_configuration_are_bound_without_new_business_tables(self):
        before = {r[0] for r in self.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotEqual(self.create([0])["job_id"], self.job)
        (self.root / "capture.js").write_text("/* changed actual helper */", encoding="utf-8")
        with self.assertRaises(ContractError):
            self.execution.start(self.job)
        self.assertNotEqual(self.create()["job_id"], self.job)
        after = {r[0] for r in self.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(before, after)
        with self.assertRaises(ContractError):
            self.create([1000, 0])


if __name__ == "__main__":
    unittest.main()
