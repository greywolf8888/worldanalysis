"""Offline two-pass planning tests with real local files, not cloud acceptance."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pptx import Presentation
from pptx.util import Inches

from worldanalysis.contracts import ContractError, digest
from worldanalysis.platform_first import PlatformFirst
from worldanalysis.policy import GateError, Policy
from worldanalysis.store import Store


class PlatformFirstTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "_ledger/devpack/cloud-state-20261116/frame-repair-temp/test-tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="platform-first-synthetic-", dir=base)
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
        # A new inventory may already carry retained products/reservations.
        for row in self.pass_.status(run)["products"]:
            if row["observation"]["state"] == "unvisited":
                self.record(run, row["file_id"], row["product"])

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

    def test_generation_rejects_outer_transaction_before_authorizing_and_commits_normal_reservation(self):
        run = self.start()
        self.all_missing(run)
        grants = []
        def authorize(row):
            grants.append(row["job_id"])
            return {"state": "granted", "grant_ref": "synthetic-host-grant", "job_id": row["job_id"], "project_id": row["project_id"]}
        self.store.submission_authorizer = authorize
        before = self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        with self.assertRaisesRegex(RuntimeError, "rollback outer"):
            with self.store.transaction():
                with self.assertRaisesRegex(GateError, "OUTER_TRANSACTION_CANNOT_AUTHORIZE_SEND"):
                    self.pass_.begin_generation(run, "101", "manuscript", entitlement_ref="free-observed")
                self.assertEqual(grants, [])
                self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], before)
                self.assertIsNone(self.pass_.status(run)["products"][0]["generation_job_id"])
                raise RuntimeError("rollback outer")
        self.assertFalse(self.store.connection.in_transaction)
        granted = self.pass_.begin_generation(run, "101", "manuscript", entitlement_ref="free-observed")
        self.assertTrue(granted["may_send"])
        self.assertFalse(self.store.connection.in_transaction)
        self.assertEqual(grants, [granted["job_id"]])
        # A separate connection must see the reservation before may_send escapes.
        with Store(self.root) as reader:
            job = reader.connection.execute("SELECT state,request_id FROM jobs WHERE job_id=?", (granted["job_id"],)).fetchone()
            self.assertEqual(tuple(job), ("submission_unknown", granted["request_id"]))
        with self.store.transaction():
            with self.assertRaisesRegex(GateError, "OUTER_TRANSACTION_CANNOT_AUTHORIZE_SEND"):
                self.pass_.begin_generation(run, "101", "manuscript", entitlement_ref="free-observed")
        self.assertFalse(self.pass_.begin_generation(run, "101", "manuscript", entitlement_ref="free-observed")["may_send"])
        self.assertEqual(grants, [granted["job_id"]])

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
        # A fresh inventory inherits the reservation before any missing
        # observation, rather than learning about it only at submission time.
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
                value = copy.deepcopy(self.inventory)
                value["inventory_id"] = "inventory-" + state
                for item in value["files"]:
                    item["source_revision"] = "source-" + state
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

    def test_broken_artifact_does_not_relax_ordinary_present_replacement(self):
        run = self.start()
        self.present(run, "101", "courseware")
        prior = self.pass_.status(run)["products"][1]["artifact"]
        (self.root / "replacement.pptx").write_bytes((self.root / "courseware.pptx").read_bytes())
        (self.root / "courseware.pptx").write_bytes(b"synthetic corruption")
        with self.assertRaisesRegex(ContractError, "retained product bytes"):
            self.record(run, "101", "courseware", "present", relative_path="replacement.pptx")
        with self.assertRaisesRegex(ContractError, "unknown platform-pass fields"):
            self.record(run, "101", "courseware", "present", relative_path="replacement.pptx", allow_repair=True)
        with self.assertRaisesRegex(ContractError, "unknown source-frame execution"):
            self.pass_.record_source_frame_repair_observation(run, "101", job_id="not-a-frame-job", quality_review_path="review.json")
        self.assertEqual(self.pass_.status(run)["products"][1]["artifact"], prior)

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

    def test_single_video_validation_shares_jobs_without_sealing_or_faking_directory(self):
        value = copy.deepcopy(self.inventory)
        value["enumeration_complete"] = False
        run = self.start(value)
        self.record(run, "101", "manuscript")
        with self.assertRaisesRegex(GateError, "VIDEO_VALIDATION_INCOMPLETE"):
            self.pass_.plan_video_validation(run, "101")
        self.present(run, "101", "courseware")
        validation = self.pass_.plan_video_validation(run, "101")
        self.assertEqual(validation["validation_video_count"], 1)
        self.assertEqual(validation["inventory_video_count"], 2)
        self.assertFalse(validation["inventory_enumeration_complete"])
        self.assertFalse(validation["directory_first_pass_sealed"])
        self.assertFalse(validation["production_first_pass_satisfied"])
        self.assertFalse(validation["course_complete"])
        self.assertEqual({row["file_id"] for row in validation["supplements"]}, {"101"})
        with self.assertRaises(GateError):
            self.pass_.plan_missing(run)
        self.assertFalse(self.pass_.status(run)["first_pass_sealed"])
        # A separately complete production inventory reuses the very same job.
        production = self.start({**self.inventory, "inventory_id": "inventory-production"})
        self.all_missing(production)
        self.pass_.seal_first_pass(production)
        plan = self.pass_.plan_missing(production)
        self.assertIn(validation["supplements"][0]["job_id"], {row["job_id"] for row in plan["supplements"]})

    def moved_inventory(self, name="inventory-moved"):
        value = copy.deepcopy(self.inventory)
        value["inventory_id"] = name
        value["root_path"] = "/apps/教育分析学习/已分析/测试"
        for item in value["files"]:
            item["path"] = item["path"].replace("/教育分析学习/测试/", "/教育分析学习/已分析/测试/")
        return value

    def test_move_inherits_verified_products_and_reuses_supplement_without_mutating_inventory(self):
        first = self.start()
        self.present(first, "101", "manuscript")
        self.present(first, "101", "courseware")
        self.present(first, "102", "courseware")
        self.all_missing(first)
        self.pass_.seal_first_pass(first)
        before_plan = self.pass_.plan_missing(first)
        frozen = self.store.connection.execute("SELECT inventory_json FROM platform_first_runs WHERE run_id=?", (first,)).fetchone()[0]
        with patch("worldanalysis.platform_products.inspect_product", side_effect=AssertionError("do not reparse retained bytes")):
            second = self.start(self.moved_inventory())
            self.assertEqual(self.pass_.status(second)["checked_products"], 3)
            self.all_missing(second)
            self.pass_.seal_first_pass(second)
            after_plan = self.pass_.plan_missing(second)
        self.assertEqual([r["job_id"] for r in before_plan["supplements"]], [r["job_id"] for r in after_plan["supplements"]])
        self.assertEqual(after_plan["supplements"][0]["source"]["path"], self.moved_inventory()["files"][1]["path"])
        self.assertEqual(self.store.connection.execute("SELECT inventory_json FROM platform_first_runs WHERE run_id=?", (first,)).fetchone()[0], frozen)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        self.assertFalse(self.pass_.begin_generation(second, "101", "manuscript", entitlement_ref="free-observed")["may_send"])

    def test_duration_correction_preserves_content_and_old_evidence_but_reviews_invalid_inherited_ranges(self):
        first = self.start()
        coverage = {"state": "partial", "evidence_ref": "old-duration-coverage", "missing_intervals": [{"start_ms": 90000, "end_ms": 110000}]}
        self.record(first, "101", "manuscript", "present", relative_path="manuscript.txt", coverage=coverage)
        self.present(first, "101", "courseware")
        old = self.pass_.status(first)["products"][0]
        moved = self.moved_inventory()
        moved["files"][0]["duration_ms"] = 60000
        second = self.start(moved)
        inherited = self.pass_.status(second)["products"][0]
        self.assertEqual(inherited["artifact"], old["artifact"])
        self.assertEqual(inherited["observation"]["coverage"], {"state": "unknown"})
        self.assertEqual(inherited["observation"]["coverage_review"]["coverage"], coverage)
        self.assertEqual(inherited["observation"]["coverage_review"]["source_run_id"], first)
        plan = self.pass_.plan_video_validation(second, "101")
        self.assertEqual(plan["supplements"], [])
        self.assertEqual(plan["review"][0]["reason"], "inherited_coverage_invalid_for_current_inventory")
        self.assertEqual(self.pass_.status(first)["products"][0], old)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_later_peer_evidence_cannot_replace_an_already_retained_artifact(self):
        first = self.start()
        second = self.start(self.moved_inventory())
        self.present(first, "101", "manuscript", sufficient=False)
        (self.root / "other-manuscript.txt").write_text("Another independent export", encoding="utf-8")
        self.record(second, "101", "manuscript", "present", relative_path="other-manuscript.txt",
                    coverage={"state": "sufficient", "evidence_ref": "other-export-check", "basis": "full_transcript"})
        self.record(first, "101", "courseware")
        plan = self.pass_.plan_video_validation(first, "101")
        row = self.pass_.status(first)["products"][0]
        self.assertEqual(row["artifact"]["relative_path"], "manuscript.txt")
        self.assertEqual(row["observation"]["coverage"]["state"], "unknown")
        self.assertEqual(plan["review"][0]["reason"], "coverage_unknown")

    def test_move_bridges_unlinked_legacy_pending_and_successful_generation_jobs(self):
        first = self.start()
        self.all_missing(first)
        provider = self.store.register_provider("platform-first-generation", "1.0", runtime="authorized_browser", config_version="platform-first-v1")
        jobs = []
        for item, state in zip(self.inventory["files"], ("submission_unknown", "verified_complete")):
            revision = digest([self.inventory["account_ref"], item, "manuscript"])
            job = self.store.queue_job(self.project, "platform-generate-manuscript", provider, revision, config_version="platform-first-v1", estimated_cost_minor=0)
            self.store.reconcile_job(job["job_id"], state=state, receipt={"evidence_ref": "legacy-task-receipt"})
            jobs.append((job["job_id"], revision, state))
        self.store.close()
        self.store = Store(self.root, submission_authorizer=lambda row: {"state": "granted", "grant_ref": "synthetic-grant", "job_id": row["job_id"], "project_id": row["project_id"]})
        self.pass_ = PlatformFirst(self.store, self.policy, allow_synthetic=True)
        second = self.start(self.moved_inventory())
        for item, (job_id, revision, state) in zip(self.inventory["files"], jobs):
            response = self.pass_.begin_generation(second, item["file_id"], "manuscript", entitlement_ref="free-observed")
            self.assertFalse(response["may_send"])
            self.assertEqual(response["job_id"], job_id)
            saved = self.store.connection.execute("SELECT input_revision,state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            self.assertEqual(tuple(saved), (revision, state))
        self.all_missing(second)
        self.pass_.seal_first_pass(second)
        plan = self.pass_.plan_missing(second)
        self.assertEqual(len(plan["reconcile"]), 2)
        self.assertEqual({r["modality"] for r in plan["supplements"]}, {"visual"})
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs WHERE operation='platform-generate-manuscript'").fetchone()[0], 2)

    def test_move_bridges_legacy_supplement_pending_and_success_without_rewriting_jobs(self):
        first = self.start()
        self.all_missing(first)
        self.pass_.seal_first_pass(first)
        provider = self.store.register_provider("platform-first-supplement-plan", "1.0", config_version="platform-first-v1")
        legacy = {}
        for item, state in zip(self.inventory["files"], ("waiting", "verified_complete")):
            selection = {"mode": "whole_timeline", "duration_ms": item["duration_ms"]}
            revision = digest([self.inventory["account_ref"], item, "audio", selection])
            job = self.store.queue_job(self.project, "supplement-audio", provider, revision, config_version="platform-first-v1")
            self.store.reconcile_job(job["job_id"], state=state, receipt={"evidence_ref": "legacy-supplement-receipt"})
            legacy[item["file_id"]] = (job["job_id"], revision, state)
        second = self.start(self.moved_inventory())
        self.all_missing(second)
        self.pass_.seal_first_pass(second)
        plan = self.pass_.plan_missing(second)
        self.assertEqual({row["modality"] for row in plan["supplements"]}, {"visual"})
        self.assertEqual(plan["reconcile"][0]["job_id"], legacy["101"][0])
        self.assertEqual(plan["review"][0]["job_id"], legacy["102"][0])
        for job_id, revision, state in legacy.values():
            saved = self.store.connection.execute("SELECT input_revision,state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            self.assertEqual(tuple(saved), (revision, state))
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 4)

    def test_true_content_and_processing_configuration_changes_still_create_tasks(self):
        first = self.start()
        self.all_missing(first)
        original = self.pass_.begin_generation(first, "101", "manuscript", entitlement_ref="free-observed")
        for key, changed in (("source_revision", "source-v2"), ("size_bytes", 101)):
            value = self.moved_inventory("inventory-change-" + key)
            value["files"][0][key] = changed
            second = self.start(value)
            self.all_missing(second)
            response = self.pass_.begin_generation(second, "101", "manuscript", entitlement_ref="free-observed")
            self.assertNotEqual(response["job_id"], original["job_id"])
        self.pass_.seal_first_pass(first)
        old = self.pass_.plan_missing(first)
        configured = PlatformFirst(self.store, self.policy, allow_synthetic=True, config_version="platform-first-different-processing")
        new = configured.plan_missing(first)
        self.assertTrue({r["job_id"] for r in old["supplements"]}.isdisjoint({r["job_id"] for r in new["supplements"]}))


if __name__ == "__main__":
    unittest.main()
