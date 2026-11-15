"""Offline two-pass planning tests with real local files, not cloud acceptance."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pptx import Presentation
from pptx.util import Inches

from worldanalysis.contracts import ContractError
from worldanalysis.platform_first import PlatformFirst
from worldanalysis.policy import GateError, Policy
from worldanalysis.store import Store


class PlatformFirstTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="platform-first-synthetic-")
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.store.close())
        self.project = self.store.create_project(topic="Synthetic two-pass fixture")
        self.policy = Policy(request_new_generation=True)
        self.pass_ = PlatformFirst(self.store, self.policy, allow_synthetic=True)
        self.inventory = {"schema_version": "1.0", "inventory_id": "inventory-fixture", "account_ref": "account-fixture",
                          "root_path": "/apps/教育分析学习/测试", "enumeration_complete": True,
                          "evidence_ref": "synthetic-list-receipt", "evidence_class": "synthetic", "files": [
                              {"file_id": "101", "path": "/apps/教育分析学习/测试/一/同名.mp4", "source_revision": "source-v1", "size_bytes": 100, "duration_ms": 120000},
                              {"file_id": "102", "path": "/apps/教育分析学习/测试/二/同名.mp4", "source_revision": "source-v1", "size_bytes": 200, "duration_ms": 120000}]}
        (self.root / "manuscript.txt").write_text("第一步打开画布，第二步创建角色。", encoding="utf-8")
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
        box.text = "Synthetic readable platform courseware"
        deck.save(self.root / "courseware.pptx")

    def start(self, value=None):
        return self.pass_.begin_run(self.project, value or self.inventory)["run_id"]

    def record(self, run, fid, product, state="missing", **kwargs):
        return self.pass_.record_product_observation(run, fid, product, {"state": state, "evidence_ref": "synthetic-observation", **kwargs})

    def present(self, run, fid, product, sufficient=True, **kwargs):
        coverage = {"state": "sufficient", "evidence_ref": "synthetic-independent-quality", "basis": "full_transcript" if product == "manuscript" else "source_frames"} if sufficient else {"state": "unknown"}
        return self.record(run, fid, product, "present", relative_path="manuscript.txt" if product == "manuscript" else "courseware.pptx", coverage=coverage, **kwargs)

    def all_missing(self, run):
        for fid in ("101", "102"):
            for product in ("manuscript", "courseware"):
                self.record(run, fid, product)

    def test_frozen_inventory_identity_and_scope(self):
        run = self.start()
        self.assertEqual(run, self.start())
        changed = copy.deepcopy(self.inventory)
        changed["files"][0]["source_revision"] = "source-v2"
        with self.assertRaises(ContractError):
            self.start(changed)
        changed["inventory_id"] = "inventory-new-revision"
        self.assertNotEqual(run, self.start(changed))
        changed["inventory_id"] = "outside"
        changed["files"][0]["path"] = "/apps/教育分析学习/other/outside.mp4"
        with self.assertRaises(ContractError):
            self.start(changed)
        with self.assertRaises(GateError):
            PlatformFirst(self.store).begin_run(self.project, self.inventory)

    def test_first_pass_visits_both_products_of_every_video_before_fallback(self):
        run = self.start()
        self.record(run, "101", "manuscript", "failed", reason="platform did not return a document")
        self.assertEqual(self.pass_.next_first_pass(run)["product"], "courseware")
        self.record(run, "101", "courseware")
        self.record(run, "102", "manuscript")
        self.assertEqual(self.pass_.next_first_pass(run)["file"]["file_id"], "102")
        with self.assertRaises(GateError):
            self.pass_.seal_first_pass(run)
        with self.assertRaises(GateError):
            self.pass_.plan_missing(run)
        self.record(run, "102", "courseware")
        self.assertTrue(self.pass_.seal_first_pass(run)["first_pass_sealed"])

    def test_incomplete_directory_enumeration_cannot_be_sealed(self):
        value = copy.deepcopy(self.inventory)
        value["enumeration_complete"] = False
        run = self.start(value)
        self.all_missing(run)
        self.assertEqual(self.pass_.next_first_pass(run)["action"], "inventory_incomplete")
        with self.assertRaises(GateError):
            self.pass_.seal_first_pass(run)

    def test_only_missing_modality_is_planned_and_good_bytes_are_preserved(self):
        run = self.start()
        self.present(run, "101", "manuscript")
        self.record(run, "101", "courseware")
        self.record(run, "102", "manuscript")
        self.present(run, "102", "courseware")
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root / "manuscript.txt", self.root / "courseware.pptx")}
        self.pass_.seal_first_pass(run)
        plan = self.pass_.plan_missing(run)
        self.assertEqual({(r["file_id"], r["modality"]) for r in plan["supplements"]}, {("101", "visual"), ("102", "audio")})
        self.assertEqual(plan, self.pass_.plan_missing(run))
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
        self.assertFalse(plan["execution_performed"])
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root / "manuscript.txt", self.root / "courseware.pptx")})

    def test_partial_coverage_plans_only_valid_missing_intervals(self):
        run = self.start()
        partial = {"state": "partial", "evidence_ref": "synthetic-gap-check", "missing_intervals": [{"start_ms": 5000, "end_ms": 9000}, {"start_ms": 8000, "end_ms": 12000}]}
        self.record(run, "101", "manuscript", "present", relative_path="manuscript.txt", coverage=partial)
        self.present(run, "101", "courseware")
        self.present(run, "102", "manuscript")
        self.present(run, "102", "courseware")
        bad = {**partial, "missing_intervals": [{"start_ms": 0, "end_ms": 120001}]}
        with self.assertRaises(ContractError):
            self.record(run, "101", "manuscript", "present", relative_path="manuscript.txt", coverage=bad)
        self.pass_.seal_first_pass(run)
        plan = self.pass_.plan_missing(run)
        self.assertEqual(len(plan["supplements"]), 1)
        self.assertEqual(plan["supplements"][0]["selection"], {"mode": "intervals", "missing_intervals": [{"start_ms": 5000, "end_ms": 12000}]})

    def test_readable_courseware_stays_review_and_summary_does_not_replace_transcript(self):
        run = self.start()
        self.record(run, "101", "manuscript", "present", relative_path="manuscript.txt", content_kind="summary")
        self.present(run, "101", "courseware", sufficient=False)
        self.present(run, "102", "manuscript")
        self.present(run, "102", "courseware", sufficient=False)
        with self.assertRaises(ContractError):
            self.record(run, "101", "courseware", "present", relative_path="courseware.pptx", coverage={"state": "sufficient", "evidence_ref": "readability-only", "basis": "courseware_only"})
        self.pass_.seal_first_pass(run)
        plan = self.pass_.plan_missing(run)
        self.assertEqual([(r["file_id"], r["modality"]) for r in plan["supplements"]], [("101", "audio")])
        self.assertEqual(len(plan["review"]), 2)

    def test_unknown_generation_survives_restart_and_requires_reconciliation(self):
        run = self.start()
        self.all_missing(run)
        denied = self.pass_.begin_generation(run, "101", "manuscript", entitlement_ref="free-observed")
        self.assertFalse(denied["may_send"])
        self.store.submission_authorizer = lambda row: {"state": "granted", "grant_ref": "synthetic-host-grant", "job_id": row["job_id"], "project_id": row["project_id"]}
        granted = self.pass_.begin_generation(run, "101", "manuscript", entitlement_ref="free-observed")
        self.assertTrue(granted["may_send"])
        self.store.close()
        self.store = Store(self.root)
        self.pass_ = PlatformFirst(self.store, self.policy, allow_synthetic=True)
        self.assertFalse(self.pass_.begin_generation(run, "101", "manuscript", entitlement_ref="free-observed")["may_send"])
        # A fresh inventory of the same file must also reconcile the existing
        # idempotent request, not turn a may_send=False response into missing.
        second = self.start({**self.inventory, "inventory_id": "inventory-rescanned"})
        self.all_missing(second)
        reused = self.pass_.begin_generation(second, "101", "manuscript", entitlement_ref="free-observed")
        self.assertFalse(reused["may_send"])
        self.assertEqual(reused["job_id"], granted["job_id"])
        self.assertEqual(self.pass_.status(second)["products"][0]["observation"]["state"], "submission_unknown")
        self.record(second, "101", "manuscript", "quota_exhausted")
        self.pass_.seal_first_pass(second)
        self.assertEqual(self.pass_.plan_missing(second)["reconcile"][0]["file_id"], "101")
        self.pass_.seal_first_pass(run)
        plan = self.pass_.plan_missing(run)
        self.assertEqual(plan["reconcile"][0]["file_id"], "101")
        self.assertNotIn(("101", "audio"), {(r["file_id"], r["modality"]) for r in plan["supplements"]})
        with self.assertRaises(ContractError):
            self.present(run, "101", "manuscript")
        self.present(run, "101", "manuscript", reconciliation_ref="synthetic-real-readback")
        self.assertEqual(self.store.connection.execute("SELECT state FROM jobs WHERE job_id=?", (granted["job_id"],)).fetchone()[0], "verified_complete")
        self.record(second, "101", "manuscript", "access_denied")
        self.assertEqual(self.pass_.next_first_pass(second)["action"], "blocked")
        self.assertEqual(self.store.connection.execute("SELECT state FROM jobs WHERE job_id=?", (granted["job_id"],)).fetchone()[0], "verified_complete")

    def test_quota_or_payment_stops_generation_not_remaining_inspection(self):
        for state in ("quota_exhausted", "payment_required"):
            with self.subTest(state=state):
                value = {**self.inventory, "inventory_id": "inventory-" + state}
                run = self.start(value)
                self.record(run, "101", "manuscript", state)
                self.assertEqual(self.pass_.next_first_pass(run)["product"], "courseware")
                self.assertFalse(self.pass_.next_first_pass(run)["generation_enabled"])
                result = self.pass_.begin_generation(run, "101", "courseware", entitlement_ref="free-observed")
                self.assertFalse(result["may_send"])
                self.present(run, "101", "courseware")
                self.present(run, "102", "manuscript")
                self.present(run, "102", "courseware")
                self.pass_.seal_first_pass(run)
                self.assertEqual(len(self.pass_.plan_missing(run)["supplements"]), 1)

    def test_access_identity_or_login_denial_blocks_run_and_all_fallback(self):
        for state in ("access_denied", "identity_mismatch", "login_required"):
            with self.subTest(state=state):
                run = self.start({**self.inventory, "inventory_id": "inventory-" + state})
                self.record(run, "101", "manuscript", state)
                self.assertEqual(self.pass_.next_first_pass(run)["action"], "blocked")
                with self.assertRaises(GateError):
                    self.pass_.seal_first_pass(run)
                with self.assertRaises(GateError):
                    self.pass_.plan_missing(run)
                with self.assertRaises(GateError):
                    self.record(run, "102", "courseware")

    def test_export_click_is_insufficient_and_present_cannot_be_replaced_by_failure(self):
        run = self.start()
        with self.assertRaises((ContractError, OSError, TypeError)):
            self.record(run, "101", "manuscript", "present", relative_path="not-downloaded.txt")
        (self.root / "error.txt").write_text("生成失败，请稍后重试", encoding="utf-8")
        with self.assertRaises(ContractError):
            self.record(run, "101", "manuscript", "present", relative_path="error.txt")
        self.present(run, "101", "manuscript")
        with self.assertRaises(ContractError):
            self.record(run, "101", "manuscript", "missing")
        self.assertEqual(self.pass_.status(run)["checked_products"], 1)
        self.record(run, "102", "manuscript", "missing")
        denied = self.pass_.begin_generation(run, "102", "manuscript", entitlement_ref="free-observed")
        self.assertFalse(denied["may_send"])
        self.present(run, "102", "manuscript")
        self.store.submission_authorizer = lambda row: {"state": "granted", "grant_ref": "synthetic-host-grant", "job_id": row["job_id"], "project_id": row["project_id"]}
        self.assertFalse(self.pass_.begin_generation(run, "102", "manuscript", entitlement_ref="free-observed")["may_send"])

    def test_lost_or_changed_retained_artifact_becomes_review_without_full_reprocessing(self):
        run = self.start()
        self.present(run, "101", "manuscript")
        self.present(run, "101", "courseware")
        self.present(run, "102", "manuscript")
        self.present(run, "102", "courseware")
        self.pass_.seal_first_pass(run)
        old = self.pass_.status(run)["products"][0]["artifact"]["sha256"]
        (self.root / "manuscript.txt").write_text("changed data", encoding="utf-8")
        plan = self.pass_.plan_missing(run)
        self.assertEqual(len(plan["review"]), 2)
        self.assertEqual(plan["supplements"], [])
        self.assertEqual(self.pass_.status(run)["products"][0]["artifact"]["sha256"], old)
        (self.root / "courseware.pptx").unlink()
        with patch("worldanalysis.platform_products.inspect_product", side_effect=AssertionError("status must not reparse")):
            self.assertEqual(len(self.pass_.status(run)["integrity_issues"]), 4)

    def test_paused_collection_does_not_advance_or_submit(self):
        run = self.start()
        default_policy = PlatformFirst(self.store, allow_synthetic=True)
        self.assertFalse(default_policy.next_first_pass(run)["generation_enabled"])
        self.assertFalse(default_policy.status(run)["generation_permitted_by_policy"])
        self.store.control("collection", "pause")
        self.assertEqual(self.pass_.next_first_pass(run)["action"], "blocked")
        with self.assertRaises(GateError):
            self.record(run, "101", "manuscript")
        self.assertEqual(self.pass_.status(run)["checked_products"], 0)


if __name__ == "__main__":
    unittest.main()
