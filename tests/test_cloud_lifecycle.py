"""Offline protocol tests: temp Store DBs + small real product files only.

No cloud acceptance is claimed. 'real_authorized_browser' fixtures exercise the
adapter's trusted receipt boundary; synthetic-class tests cannot confirm remote
success. There are no sleeps, live DB migrations or network/browser calls.
"""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from worldanalysis.cloud_lifecycle import (
    CloudLifecycle, DirectoryReadback, LifecycleConfig, OperationReadback,
    PROTECTED_SESSION_ENV, RemoteContainer, RemoteEntry, choose_project_date, main,
)
from worldanalysis.contracts import ContractError, now
from worldanalysis.platform_first import PlatformFirst
from worldanalysis.policy import GateError
from worldanalysis.store import Store


SYNTHETIC_PROTECTED_SESSION = "session-protected-fixture"


class CloudLifecycleTests(unittest.TestCase):
    def setUp(self):
        isolated_env = patch.dict(os.environ)
        isolated_env.start()
        self.addCleanup(isolated_env.stop)
        os.environ.pop(PROTECTED_SESSION_ENV, None)
        self.temp = tempfile.TemporaryDirectory(prefix="offline-cloud-lifecycle-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.addCleanup(lambda: self.store.close())
        self.store.submission_authorizer = lambda row: {
            "state": "granted", "grant_ref": "offline-independent-grant",
            "job_id": row["job_id"], "project_id": row["project_id"],
        }
        self.config = LifecycleConfig("account-fixture", "session-fixture", {
            "pending": RemoteContainer("10", "/apps/教育分析学习/待分析"),
            "extracting": RemoteContainer("20", "/apps/教育分析学习/正在提取"),
            "complete": RemoteContainer("30", "/apps/教育分析学习/提取完成"),
        }, protected_sessions=frozenset({SYNTHETIC_PROTECTED_SESSION}))
        CloudLifecycle.ensure_schema(self.store)
        self.flow = CloudLifecycle(self.store, self.config)
        self.fid, self.name = "101", "163378_AI视频创作"

    def listing(self, state, entries=(), **kwargs):
        container = self.config.containers[state]
        return DirectoryReadback(self.config.account_ref, container.folder_id, container.path,
            tuple(RemoteEntry(fid, container.path + "/" + name, kind) for fid, name, kind in entries),
            "offline-readback-" + state, now(), True, **kwargs)

    def source(self, state="pending"):
        return self.listing(state, [(self.fid, self.name, "folder")])

    def register(self, fid=None, name=None, state="pending", owner="session-fixture", **date_inputs):
        fid, name = fid or self.fid, name or self.name
        project = self.store.create_project(project_id="local-" + fid, topic="Offline lifecycle protocol fixture")
        listing = self.listing(state, [(fid, name, "folder")])
        with self.flow.work_lock(fid) as lock:
            result = self.flow.register_project(project, fid, listing, lock=lock,
                owner_session=owner, ownership_ref="offline-ownership" if owner else None, **date_inputs)
        return result

    def prepare(self, lock):
        return self.flow.prepare_move(self.fid, to_state="extracting", expected_state="pending",
                                     source=self.source(), destination=self.listing("extracting"), lock=lock)

    def start(self, lock):
        move = self.prepare(lock)
        granted = self.flow.begin_move(move["job_id"], source=self.source(), destination=self.listing("extracting"), lock=lock)
        self.assertTrue(granted["may_send"])
        return granted

    def extracting(self):
        self.register(course_root_name="课程_20261105", course_root_evidence_ref="offline-course-root", course_root_id="201")
        with self.flow.work_lock(self.fid) as lock:
            send = self.start(lock)
            done = self.flow.reconcile_move(send["job_id"], source=self.listing("pending"), destination=self.source("extracting"), lock=lock)
            self.assertTrue(done["remote_confirmed"])
        return self.flow.get_project(self.fid)

    def platform_run(self, *, complete=True, sealed=True, sufficient=True, summary=False, evidence_class="real_authorized_browser", bind_scope=True):
        record = self.flow.get_project(self.fid)
        root_path = record["known_paths"][0]
        if record.get("course_root_relative_path"):
            root_path += "/" + record["course_root_relative_path"]
        inventory = {"schema_version": "1.0", "inventory_id": "offline-inventory", "account_ref": self.config.account_ref,
            "root_path": root_path, "enumeration_complete": complete, "evidence_ref": "offline-list-evidence",
            "evidence_class": evidence_class, "files": [
                {"file_id": "901", "path": root_path + "/第一课.mp4", "source_revision": "source-v1", "size_bytes": 100},
                {"file_id": "902", "path": root_path + "/第二课.mp4", "source_revision": "source-v1", "size_bytes": 100}]}
        self.inventory = inventory
        platform = PlatformFirst(self.store, allow_synthetic=evidence_class == "synthetic")
        run = platform.begin_run(record["project_id"], inventory)["run_id"]
        (self.root / "transcript.txt").write_text("第一步选择画布，第二步设置角色参数。", encoding="utf-8")
        from pptx import Presentation
        deck = Presentation()
        deck.slides.add_slide(deck.slide_layouts[6]).shapes.add_textbox(0, 0, 1000000, 1000000).text_frame.text = "Offline source-checked lesson fixture"
        deck.save(self.root / "courseware.pptx")
        for fid in ("901", "902"):
            for product in ("manuscript", "courseware"):
                coverage = {"state": "sufficient", "basis": "full_transcript" if product == "manuscript" else "source_frames", "evidence_ref": "offline-quality-evidence"}
                obs = {"state": "present", "evidence_ref": "offline-product-readback",
                       "relative_path": "transcript.txt" if product == "manuscript" else "courseware.pptx",
                       "coverage": coverage if sufficient and not (summary and product == "manuscript") else {"state": "unknown"}}
                if summary and product == "manuscript": obs["content_kind"] = "summary"
                platform.record_product_observation(run, fid, product, obs)
        if sealed and complete:
            platform.seal_first_pass(run)
        if bind_scope:
            with self.flow.work_lock(self.fid) as lock:
                self.flow.record_project_scope(self.fid, self.scope_fixture(), lock=lock)
        return platform, run

    def scope_fixture(self, *, scope_id="scope-fixture", materials=(), extra_video=False, when=None, omit_leaf=False):
        record = self.flow.get_project(self.fid)
        root = record["path"]; course = root + "/" + record["course_root_relative_path"]
        course_id = record.get("course_root_id") or "201"
        stamp = when or now()
        videos = [{"file_id": f["file_id"], "path": course + "/" + Path(f["path"]).name, "kind": "file",
                   "media_kind": "video", "source_revision": f["source_revision"], "size_bytes": f["size_bytes"]} for f in self.inventory["files"]]
        if extra_video:
            videos.append({"file_id": "999", "path": course + "/new.mp4", "kind": "file", "media_kind": "video", "source_revision": "source-new", "size_bytes": 123})
        def directory(fid, path, entries):
            return {"folder_id": fid, "path": path, "evidence_ref": "offline-recursive-list-" + fid, "observed_at": stamp, "enumeration_complete": True, "entries": entries}
        directories = [directory(self.fid, root, [{"file_id": course_id, "path": course, "kind": "folder"}]), directory(course_id, course, videos)]
        if materials:
            leaf = course + "/materials"
            directories[1]["entries"].append({"file_id": "301", "path": leaf, "kind": "folder"})
            if not omit_leaf:
                directories.append(directory("301", leaf, [{"file_id": item["file_id"], "path": leaf + "/" + item.get("name", "material.txt"),
                    "kind": "file", "media_kind": "nonvideo", "source_revision": item["source_revision"], "size_bytes": item["size_bytes"]} for item in materials]))
        return {"schema_version": "1.0", "scope_id": scope_id, "account_ref": self.config.account_ref, "project_folder_id": self.fid, "root_path": root,
                "evidence_class": "real_authorized_browser", "evidence_ref": "offline-full-project-scope", "observed_at": stamp, "directories": directories}

    def material_receipt(self, item, *, state="blocked", receipt_id="material-blocked", relative_path=None):
        artifacts = []
        if relative_path:
            data = (self.root / relative_path).read_bytes()
            artifacts = [{"relative_path": relative_path, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}]
        return {"receipt_id": receipt_id, "state": state, "source_revision": item["source_revision"], "size_bytes": item["size_bytes"],
                "evidence_class": "real_authorized_browser", "evidence_ref": "offline-source-content-readback", "observed_at": now(),
                "read_basis": "unavailable" if state == "blocked" else "browser_content_readback", "reason": "Actual content reviewed; promotional-only" if state == "excluded_by_content" else "Real original material retained" if state == "retained" else "new read authorization scope is unavailable", "artifacts": artifacts}

    def test_scope_missing_recursive_hole_and_unread_material_fail_closed_with_actual_evidence(self):
        self.extracting(); _, run = self.platform_run(bind_scope=False)
        body = b"Read the real project instructions."
        materials = [{"file_id": "903", "name": "instructions.txt", "source_revision": "opaque-doc-v1", "size_bytes": len(body)},
                     {"file_id": "904", "name": "advertisement.png", "source_revision": "opaque-image-v1", "size_bytes": 124607}]
        with self.flow.work_lock(self.fid) as lock:
            missing = self.flow.refresh_content(self.fid, run, lock=lock)
            self.assertIn("project_scope_not_bound", missing["content_evidence"]["gaps"])
            blocked = self.flow.prepare_move(self.fid, to_state="complete", expected_state="extracting", source=self.source("extracting"), destination=self.listing("complete"), lock=lock)
            self.assertFalse(blocked["may_send"])
            hole = self.scope_fixture(scope_id="scope-hole", materials=materials, omit_leaf=True)
            observed = self.flow.record_project_scope(self.fid, hole, lock=lock)
            self.assertIn("child_directory_not_enumerated:301", observed["gaps"])
            self.assertEqual(self.flow.refresh_content(self.fid, run, lock=lock)["content_state"], "incomplete")
            scope = self.scope_fixture(scope_id="scope-full", materials=materials)
            self.flow.record_project_scope(self.fid, scope, lock=lock)
            self.assertIn("project_material_unreviewed:903", self.flow.refresh_content(self.fid, run, lock=lock)["content_evidence"]["gaps"])
            self.flow.record_scope_material(self.fid, "scope-full", "903", self.material_receipt(materials[0]), lock=lock)
            from PIL import Image
            Image.new("RGB", (24, 24), "green").save(self.root / "normal-readback.png")
            excluded = self.material_receipt(materials[1], state="excluded_by_content", receipt_id="actual-image-read", relative_path="normal-readback.png")
            self.flow.record_scope_material(self.fid, "scope-full", "904", excluded, lock=lock)
            evidence = self.flow.refresh_content(self.fid, run, lock=lock)["content_evidence"]
            self.assertIn("project_material_blocked:903", evidence["gaps"])
            (self.root / "instructions.txt").write_bytes(body)
            retained = self.material_receipt(materials[0], state="retained", receipt_id="actual-doc-read", relative_path="instructions.txt")
            retained["read_basis"] = "original_file"
            with self.assertRaises(ContractError):
                self.flow.record_scope_material(self.fid, "scope-full", "903", {**retained, "completed": True}, lock=lock)
            self.flow.record_scope_material(self.fid, "scope-full", "903", retained, lock=lock)
            self.assertEqual(self.flow.refresh_content(self.fid, run, lock=lock)["content_state"], "complete")
        (self.root / "instructions.txt").write_bytes(b"changed")
        self.store.connection.execute("PRAGMA query_only=ON")
        try:
            live = self.flow.queue()["projects"][0]
        finally:
            self.store.connection.execute("PRAGMA query_only=OFF")
        self.assertIn("project_material_invalid:903", live["content_evidence"]["gaps"])
        self.assertFalse(live["archive_pending"])

    def test_latest_whole_project_video_set_invalidates_old_sealed_run(self):
        self.extracting(); _, run = self.platform_run()
        with self.flow.work_lock(self.fid) as lock:
            self.assertEqual(self.flow.refresh_content(self.fid, run, lock=lock)["content_state"], "complete")
            self.flow.record_project_scope(self.fid, self.scope_fixture(scope_id="scope-added-video", extra_video=True), lock=lock)
            incomplete = self.flow.refresh_content(self.fid, run, lock=lock)
            self.assertIn("project_scope_video_inventory_mismatch", incomplete["content_evidence"]["gaps"])
            self.assertEqual(incomplete["content_state"], "incomplete")
            denied = self.flow.prepare_move(self.fid, to_state="complete", expected_state="extracting", source=self.source("extracting"), destination=self.listing("complete"), lock=lock)
            self.assertIsNone(denied["job_id"])
            self.assertFalse(denied["may_send"])
            revised = self.scope_fixture(scope_id="scope-changed-revision")
            revised["directories"][1]["entries"][0]["source_revision"] = "source-v2"
            self.flow.record_project_scope(self.fid, revised, lock=lock)
            self.assertIn("project_scope_video_inventory_mismatch", self.flow.refresh_content(self.fid, run, lock=lock)["content_evidence"]["gaps"])

    def test_scope_ttl_only_blocks_terminal_move_and_location_does_not_change_scope_identity(self):
        self.register(course_root_name="课程_20261105", course_root_evidence_ref="offline-course-root", course_root_id="201")
        _, run = self.platform_run(bind_scope=False)
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with self.flow.work_lock(self.fid) as lock:
            before = self.flow.record_project_scope(self.fid, self.scope_fixture(scope_id="scope-before-move", when=old), lock=lock)
            send = self.start(lock)
            self.flow.reconcile_move(send["job_id"], source=self.listing("pending"), destination=self.source("extracting"), lock=lock)
            self.flow.refresh_content(self.fid, run, lock=lock)
            self.assertTrue(self.flow.queue()["projects"][0]["archive_pending"])
            with self.assertRaises(GateError) as stale:
                self.flow.prepare_move(self.fid, to_state="complete", expected_state="extracting", source=self.source("extracting"), destination=self.listing("complete"), lock=lock)
            self.assertEqual(stale.exception.code, "PROJECT_SCOPE_READBACK_STALE")
            fresh = self.scope_fixture(scope_id="scope-after-move")
            after = self.flow.record_project_scope(self.fid, fresh, lock=lock)
            self.assertEqual(before["scope_identity_sha256"], after["scope_identity_sha256"])
            move = self.flow.prepare_move(self.fid, to_state="complete", expected_state="extracting", source=self.source("extracting"), destination=self.listing("complete"), lock=lock)
            # A newly assembled manifest cannot make old directory reads fresh.
            stale_head = self.scope_fixture(scope_id="scope-reassembled-old-reads")
            for directory in stale_head["directories"]: directory["observed_at"] = old
            self.flow.record_project_scope(self.fid, stale_head, lock=lock)
            self.assertTrue(self.flow.queue()["projects"][0]["archive_pending"])
            with self.assertRaises(GateError) as stale:
                self.flow.begin_move(move["job_id"], source=self.source("extracting"), destination=self.listing("complete"), lock=lock)
            self.assertEqual(stale.exception.code, "PROJECT_SCOPE_READBACK_STALE")
            self.assertEqual(self.flow.move_status(move["job_id"])["status"], "queued")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM events WHERE kind='cloud_project_scope_observed'").fetchone()[0], 3)

    def test_one_explicit_same_database_schema_no_constructor_migration(self):
        with Store(self.root / "other") as store:
            before = {r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            with self.assertRaises(ContractError): CloudLifecycle(store, self.config)
            CloudLifecycle.ensure_schema(store)
            after = {r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(after - before, {"cloud_lifecycle_projects"})
            CloudLifecycle.ensure_schema(store)
        self.register()
        with self.flow.work_lock(self.fid) as lock:
            move = self.prepare(lock)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        intent = self.store.connection.execute("SELECT payload_json FROM events WHERE kind='cloud_move_intent' AND record_id=?", (move["job_id"],)).fetchone()
        self.assertEqual(json.loads(intent[0])["source"]["parent_id"], "10")
        self.assertEqual(move["intent"]["folder_id"], self.fid)

    def test_dates_are_full_valid_fixed_and_source_calendar_day(self):
        unknown = choose_project_date("163378_20260230", project_evidence_ref="name")
        self.assertIsNone(unknown["project_date"])
        selected = choose_project_date("163378_AI", project_evidence_ref="name", course_root_name="课_20261105",
            course_root_evidence_ref="root", published_at="2026-11-04", published_evidence_ref="publication")
        self.assertEqual((selected["project_date"], selected["date_source"]), ("2026-11-05", "course_root_name"))
        self.assertTrue(selected["has_conflict"])
        conflict = choose_project_date("课20261105_20261106", project_evidence_ref="name")
        self.assertIsNone(conflict["project_date"])
        fallback = choose_project_date("163378", project_evidence_ref="name", original_created_at="2026-11-05T00:10:00+08:00", original_creation_evidence_ref="original-pre-move")
        self.assertEqual((fallback["project_date"], fallback["confidence"]), ("2026-11-05", "low"))
        with self.assertRaises(TypeError): choose_project_date("课", project_evidence_ref="name", mtime="2026-11-16")
        first = self.register(course_root_name="课20261105", course_root_evidence_ref="root")
        with self.flow.work_lock(self.fid) as lock:
            again = self.flow.register_project(first["project_id"], self.fid, self.source(), lock=lock,
                published_at="2026-11-16", published_evidence_ref="later-scan")
        self.assertEqual(first["date"], again["date"])

    def test_merged_queue_descending_date_then_stable_id_unknown_last(self):
        self.register("300", "old_20240101", state="extracting")
        self.register("200", "second_20261105")
        self.register("100", "first_20261105")
        self.register("050", "163378")
        self.register("001", "protected_20261116", owner=SYNTHETIC_PROTECTED_SESSION)
        queue = self.flow.queue()
        self.assertEqual([p["folder_id"] for p in queue["projects"]], ["100", "200", "300", "050"])
        self.assertEqual(queue["denied"][0]["reason"], "PROTECTED_SESSION_OWNERSHIP")

    def test_unknown_response_restart_reconciles_once_no_second_send(self):
        original = self.register(course_root_name="课20261105", course_root_evidence_ref="root")
        with self.flow.work_lock(self.fid) as lock: send = self.start(lock)
        self.assertEqual(self.store.connection.execute("SELECT state FROM jobs WHERE job_id=?", (send["job_id"],)).fetchone()[0], "submission_unknown")
        self.assertEqual(self.flow.get_project(self.fid)["state"], "pending")
        self.store.close(); self.store = Store(self.root); self.flow = CloudLifecycle(self.store, self.config)
        with self.flow.work_lock(self.fid) as lock:
            self.assertFalse(self.flow.begin_move(send["job_id"], lock=lock)["may_send"])
            self.assertEqual(self.flow.queue()["next_action"], "reconcile_move")
            done = self.flow.reconcile_move(send["job_id"], source=self.listing("pending"), destination=self.source("extracting"), lock=lock)
            self.assertEqual(done["status"], "remote_confirmed")
            again = self.flow.reconcile_move(send["job_id"], source=True, destination=True, lock=lock)
            self.assertFalse(again["may_send"])
            self.assertFalse(self.flow.begin_move(send["job_id"], lock=lock)["may_send"])
            duplicate = self.prepare(lock)
            self.assertEqual(duplicate["job_id"], send["job_id"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        self.assertEqual(original["date"], self.flow.get_project(self.fid)["date"])

    def test_outer_transaction_cannot_return_uncommitted_send_permission(self):
        self.register()
        with self.flow.work_lock(self.fid) as lock:
            move = self.prepare(lock)
            with self.store.transaction():
                with self.assertRaises(GateError):
                    self.flow.begin_move(move["job_id"], source=self.source(), destination=self.listing("extracting"), lock=lock)
            self.assertEqual(self.flow.move_status(move["job_id"])["status"], "queued")

    def test_independent_store_submission_grant_stays_required(self):
        self.register(); self.store.submission_authorizer = None
        with self.flow.work_lock(self.fid) as lock:
            move = self.prepare(lock)
            result = self.flow.begin_move(move["job_id"], source=self.source(), destination=self.listing("extracting"), lock=lock)
        self.assertFalse(result["may_send"])
        self.assertEqual(self.flow.move_status(move["job_id"])["status"], "queued")

    def test_model_success_stale_partial_and_wrong_account_cannot_confirm(self):
        self.register()
        stale = self.listing("pending")
        with self.flow.work_lock(self.fid) as lock:
            send = self.start(lock)
            with self.assertRaises(ContractError): self.flow.reconcile_move(send["job_id"], source=True, destination=True, lock=lock)
            with self.assertRaises(GateError): self.flow.reconcile_move(send["job_id"], source=stale, destination=self.source("extracting"), lock=lock)
            with self.assertRaises(GateError): self.flow.reconcile_move(send["job_id"], source=replace(self.listing("pending"), account_ref="other"), destination=self.source("extracting"), lock=lock)
            with self.assertRaises(GateError): self.flow.reconcile_move(send["job_id"], source=self.listing("pending", evidence_class="model"), destination=self.source("extracting"), lock=lock)
            partial = self.flow.reconcile_move(send["job_id"], source=replace(self.listing("pending"), enumeration_complete=False), destination=self.source("extracting"), lock=lock)
            self.assertEqual(partial["status"], "submission_unknown")
            self.assertFalse(partial["remote_confirmed"])

    def test_bare_model_completed_job_receipt_is_not_remote_confirmation(self):
        self.register()
        with self.flow.work_lock(self.fid) as lock:
            send = self.start(lock)
            self.store.reconcile_job(send["job_id"], state="verified_complete", receipt={"state": "remote_confirmed", "completed": True}, settled_cost_minor=0)
            with self.assertRaises(ContractError): self.flow.move_status(send["job_id"])
        self.assertEqual(self.flow.get_project(self.fid)["state"], "pending")

    def test_synthetic_receipts_never_become_remote_confirmed(self):
        self.flow = CloudLifecycle(self.store, self.config, allow_synthetic=True)
        self.register()
        with self.flow.work_lock(self.fid) as lock:
            send = self.start(lock)
            done = self.flow.reconcile_move(send["job_id"], source=self.listing("pending", evidence_class="synthetic"), destination=replace(self.source("extracting"), evidence_class="synthetic"), lock=lock)
        self.assertEqual(done["status"], "synthetic_confirmed")
        self.assertFalse(done["remote_confirmed"])

    def test_name_conflicts_and_both_sides_never_overwrite_or_resend(self):
        self.register()
        collision = self.listing("extracting", [("999", self.name, "file")])
        with self.flow.work_lock(self.fid) as lock:
            with self.assertRaises(GateError): self.flow.prepare_move(self.fid, to_state="extracting", expected_state="pending", source=self.source(), destination=collision, lock=lock)
            self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
            send = self.start(lock)
            result = self.flow.reconcile_move(send["job_id"], source=self.source(), destination=replace(collision, observed_at=now()), lock=lock)
            self.assertEqual(result["receipt"]["verdict"], "name_identity_conflict")
            both = self.flow.reconcile_move(send["job_id"], source=self.source(), destination=self.source("extracting"), lock=lock)
            self.assertEqual(both["receipt"]["verdict"], "identity_at_both_sides")
            self.assertFalse(self.flow.begin_move(send["job_id"], lock=lock)["may_send"])
        self.assertEqual(self.flow.get_project(self.fid)["state"], "pending")

    def test_missing_both_sides_stays_unknown_with_bounded_identity_search(self):
        self.register()
        with self.flow.work_lock(self.fid) as lock:
            send = self.start(lock)
            result = self.flow.reconcile_move(send["job_id"], source=self.listing("pending"), destination=self.listing("extracting"), lock=lock)
            self.assertEqual(result["receipt"]["verdict"], "identity_missing_both_sides")
            self.assertEqual(result["identity_search"]["remaining_rounds"], 3)
            outside = replace(self.listing("pending"), path="/apps/教育分析学习/unconfigured")
            with self.assertRaises(GateError): self.flow.record_identity_search(send["job_id"], [outside], lock=lock)
            for _ in range(3): self.flow.record_identity_search(send["job_id"], [self.listing("pending")], lock=lock)
            with self.assertRaises(GateError): self.flow.record_identity_search(send["job_id"], [self.listing("pending")], lock=lock)
        self.assertEqual(self.flow.move_status(send["job_id"])["status"], "submission_unknown")

    def test_source_presence_is_not_non_execution_but_correlated_proof_can_retry(self):
        self.register()
        with self.flow.work_lock(self.fid) as lock:
            send = self.start(lock)
            result = self.flow.reconcile_move(send["job_id"], source=self.source(), destination=self.listing("extracting"), lock=lock)
            self.assertEqual(result["status"], "submission_unknown")
            proof = OperationReadback(self.config.account_ref, "wrong-request", "not_executed", "offline-operation-readback", now())
            with self.assertRaises(GateError): self.flow.retry_move(send["job_id"], non_execution=proof, source=self.source(), destination=self.listing("extracting"), lock=lock)
            proof = replace(proof, request_id=send["request_id"], observed_at=now())
            retry = self.flow.retry_move(send["job_id"], non_execution=proof, source=self.source(), destination=self.listing("extracting"), lock=lock)
            self.assertTrue(retry["may_send"])
            self.assertEqual(retry["job_id"], send["job_id"])
            self.assertNotEqual(retry["request_id"], send["request_id"])
            self.assertFalse(self.flow.begin_move(send["job_id"], lock=lock)["may_send"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_unknown_other_owner_and_protected_ids_paths_are_fail_closed(self):
        for fid, owner in (("101", None), ("102", "another-session"), ("103", SYNTHETIC_PROTECTED_SESSION)):
            self.register(fid, owner=owner)
            with self.flow.work_lock(fid) as lock:
                with self.assertRaises(GateError): self.flow.prepare_move(fid, to_state="extracting", expected_state="pending", source=self.source(), destination=self.listing("extracting"), lock=lock)
        self.register("104", "owned")
        for config in (replace(self.config, protected_remote_ids=frozenset({"104"})),
                       replace(self.config, protected_paths=(self.config.containers["pending"].path + "/owned",))):
            flow = CloudLifecycle(self.store, config)
            with flow.work_lock("104") as lock:
                with self.assertRaises(GateError): flow.prepare_move("104", to_state="extracting", expected_state="pending", source=self.source(), destination=self.listing("extracting"), lock=lock)
        with self.flow.work_lock("101") as lock:
            with self.assertRaises(GateError): self.flow.register_project("local-101", "101", self.source(), lock=lock, owner_session=self.config.session_id, ownership_ref="new-claim")

    def test_missing_protection_is_readable_but_cannot_prepare_move(self):
        self.register()
        flow = CloudLifecycle(self.store, replace(self.config, protected_sessions=frozenset()))
        self.assertEqual(flow.get_project(self.fid)["folder_id"], self.fid)
        self.assertEqual(flow.queue()["denied"][0]["reason"], "PROTECTED_SESSIONS_NOT_CONFIGURED")
        with flow.work_lock(self.fid) as lock:
            with self.assertRaises(GateError) as gate:
                flow.prepare_move(self.fid, to_state="extracting", expected_state="pending",
                                  source=self.source(), destination=self.listing("extracting"), lock=lock)
        self.assertEqual(gate.exception.code, "PROTECTED_SESSIONS_NOT_CONFIGURED")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_malformed_protection_is_rejected_without_echoing_value(self):
        for malformed in ("", "not a valid opaque identifier"):
            with self.subTest(malformed=bool(malformed)):
                with patch.dict(os.environ, {PROTECTED_SESSION_ENV: malformed}):
                    with self.assertRaisesRegex(ContractError, "invalid opaque ID"):
                        CloudLifecycle(self.store, self.config)
        with self.assertRaisesRegex(ContractError, "collection of opaque IDs"):
            CloudLifecycle(self.store, replace(self.config, protected_sessions=SYNTHETIC_PROTECTED_SESSION))

    def test_environment_protection_cannot_be_overridden_by_adapter_identity(self):
        self.register(owner=SYNTHETIC_PROTECTED_SESSION)
        configured = replace(self.config, session_id=SYNTHETIC_PROTECTED_SESSION,
                             protected_sessions=frozenset({"session-other-protected-fixture"}))
        with patch.dict(os.environ, {PROTECTED_SESSION_ENV: SYNTHETIC_PROTECTED_SESSION}):
            flow = CloudLifecycle(self.store, configured)
        with flow.work_lock(self.fid) as lock:
            with self.assertRaises(GateError) as gate:
                flow.prepare_move(self.fid, to_state="extracting", expected_state="pending",
                                  source=self.source(), destination=self.listing("extracting"), lock=lock)
        self.assertEqual(gate.exception.code, "PROTECTED_SESSION_OWNERSHIP")

    def test_other_owner_remains_denied_with_valid_protection(self):
        self.register(owner="session-unrelated-fixture")
        with self.flow.work_lock(self.fid) as lock:
            with self.assertRaises(GateError) as gate:
                self.flow.prepare_move(self.fid, to_state="extracting", expected_state="pending",
                                       source=self.source(), destination=self.listing("extracting"), lock=lock)
        self.assertEqual(gate.exception.code, "PROJECT_OWNED_BY_ANOTHER_SESSION")

    def test_kernel_project_lock_cross_process_and_no_blind_steal(self):
        self.register()
        with self.flow.work_lock(self.fid) as lock:
            with self.assertRaises(GateError):
                with self.flow.work_lock(self.fid): pass
            script = ("import sys; from worldanalysis.cloud_lifecycle import ProjectWorkLock; "
                      "from worldanalysis.policy import GateError;\n"
                      "try:\n with ProjectWorkLock(sys.argv[1],sys.argv[2],sys.argv[3]): print('acquired')\n"
                      "except GateError as exc: print(exc.code)\n")
            command = [sys.executable, "-B", "-c", script, str(lock.path), lock.identity, self.config.session_id]
            child = subprocess.run(command, capture_output=True, text=True, timeout=15, check=True)
            self.assertEqual(child.stdout.strip(), "PROJECT_WORK_LOCKED")
            with self.flow.work_lock("102") as other:
                with self.assertRaises(GateError): self.prepare(other)
        child = subprocess.run(command, capture_output=True, text=True, timeout=15, check=True)
        self.assertEqual(child.stdout.strip(), "acquired")

    def test_content_complete_and_archive_pending_are_separate_actual_readback(self):
        record = self.extracting()
        platform, run = self.platform_run()
        with self.flow.work_lock(self.fid) as lock:
            checked = self.flow.refresh_content(self.fid, run, lock=lock)
            self.assertEqual(checked["content_state"], "complete")
            self.assertTrue(checked["archive_pending"])
            self.assertEqual(checked["state"], "extracting")
            self.assertEqual(self.flow.queue()["projects"][0]["action"], "archive_only")
            move = self.flow.prepare_move(self.fid, to_state="complete", expected_state="extracting", source=self.source("extracting"), destination=self.listing("complete"), lock=lock)
            send = self.flow.begin_move(move["job_id"], source=self.source("extracting"), destination=self.listing("complete"), lock=lock)
            self.assertTrue(send["may_send"])
            self.assertTrue(self.flow.get_project(self.fid)["archive_pending"])
            done = self.flow.reconcile_move(move["job_id"], source=self.listing("extracting"), destination=self.source("complete"), lock=lock)
            self.assertTrue(done["remote_confirmed"])
        final = self.flow.get_project(self.fid)
        self.assertEqual(final["state"], "complete")
        self.assertFalse(final["archive_pending"])
        self.assertEqual(final["date"], record["date"])
        self.assertEqual(platform.status(run)["total_products"], 4)

    def archive_submission(self, run, lock):
        self.flow.refresh_content(self.fid, run, lock=lock)
        move = self.flow.prepare_move(self.fid, to_state="complete", expected_state="extracting", source=self.source("extracting"), destination=self.listing("complete"), lock=lock)
        send = self.flow.begin_move(move["job_id"], source=self.source("extracting"), destination=self.listing("complete"), lock=lock)
        self.assertTrue(send["may_send"])
        return move["job_id"]

    def test_damage_after_archive_send_keeps_remote_confirmation_and_authorized_repair_move(self):
        self.extracting(); _, run = self.platform_run()
        with self.flow.work_lock(self.fid) as lock:
            archive = self.archive_submission(run, lock)
            (self.root / "transcript.txt").write_text("damaged after submission", encoding="utf-8")
            result = self.flow.reconcile_move(archive, source=self.listing("extracting"), destination=self.source("complete"), lock=lock)
            self.assertTrue(result["remote_confirmed"])
            self.assertEqual((result["location_state"], result["content_state"]), ("complete", "incomplete"))
            self.assertTrue(result["repair_required"])
            self.assertFalse(result["archive_pending"])  # The location was actually confirmed, not pending.
            self.assertEqual(self.flow.queue()["projects"][0]["action"], "repair_content")
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--db", str(self.store.db_path), "queue"]), 0)
            self.assertTrue(json.loads(output.getvalue())["projects"][0]["repair_required"])
            repair = self.flow.prepare_move(self.fid, to_state="extracting", expected_state="complete", source=self.source("complete"), destination=self.listing("extracting"), lock=lock)
            self.assertNotEqual(repair["job_id"], archive)
            grant = self.store.submission_authorizer
            self.store.submission_authorizer = None
            denied = self.flow.begin_move(repair["job_id"], source=self.source("complete"), destination=self.listing("extracting"), lock=lock)
            self.assertFalse(denied["may_send"])
            self.assertEqual(self.flow.get_project(self.fid)["state"], "complete")
            self.store.submission_authorizer = grant
            send = self.flow.begin_move(repair["job_id"], source=self.source("complete"), destination=self.listing("extracting"), lock=lock)
            self.assertTrue(send["may_send"])
            self.assertEqual(self.flow.move_status(repair["job_id"])["status"], "submission_unknown")
            self.assertFalse(self.flow.begin_move(repair["job_id"], lock=lock)["may_send"])
            self.assertEqual(self.flow.get_project(self.fid)["state"], "complete")
            moved = self.flow.reconcile_move(repair["job_id"], source=self.listing("complete"), destination=self.source("extracting"), lock=lock)
            self.assertTrue(moved["remote_confirmed"])
        record = self.flow.get_project(self.fid)
        self.assertEqual((record["state"], record["content_state"]), ("extracting", "incomplete"))
        self.assertFalse(record["repair_required"])
        self.assertEqual(self.flow.queue()["projects"][0]["action"], "process_content")
        self.assertTrue(self.flow.move_status(archive)["remote_confirmed"])

    def test_repeated_archive_ack_rechecks_new_quality_gap_without_replaying_move(self):
        self.extracting(); platform, run = self.platform_run()
        with self.flow.work_lock(self.fid) as lock:
            archive = self.archive_submission(run, lock)
            self.flow.reconcile_move(archive, source=self.listing("extracting"), destination=self.source("complete"), lock=lock)
            receipt = self.store.connection.execute("SELECT receipt_json FROM jobs WHERE job_id=?", (archive,)).fetchone()[0]
            platform.record_product_observation(run, "901", "manuscript", {"state": "present", "evidence_ref": "offline-later-quality-gap", "relative_path": "transcript.txt", "coverage": {"state": "unknown"}})
            result = self.flow.reconcile_move(archive, source=True, destination=True, lock=lock)
            self.assertTrue(result["remote_confirmed"])
            self.assertTrue(result["repair_required"])
            self.assertEqual(result["content_state"], "incomplete")
            self.assertEqual(self.flow.queue()["projects"][0]["action"], "repair_content")
            self.assertEqual(receipt, self.store.connection.execute("SELECT receipt_json FROM jobs WHERE job_id=?", (archive,)).fetchone()[0])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)

    def test_content_identity_verification_error_does_not_roll_back_proven_archive_location(self):
        self.extracting(); _, run = self.platform_run()
        with self.flow.work_lock(self.fid) as lock:
            archive = self.archive_submission(run, lock)
            other = self.store.create_project(project_id="unrelated-temp-project", topic="Offline inconsistent-run fixture")
            self.store.connection.execute("UPDATE platform_first_runs SET project_id=? WHERE run_id=?", (other, run))
            result = self.flow.reconcile_move(archive, source=self.listing("extracting"), destination=self.source("complete"), lock=lock)
            self.assertTrue(result["remote_confirmed"])
            self.assertEqual(result["location_state"], "complete")
            self.assertTrue(result["repair_required"])
            record = self.flow.get_project(self.fid)
            self.assertEqual(record["content_evidence"]["verification_error"], "GateError")
            self.assertEqual(record["content_evidence"]["verification_code"], "PLATFORM_RUN_PROJECT_IDENTITY_MISMATCH")
            self.assertEqual(self.flow.queue()["projects"][0]["action"], "repair_content")
            self.assertEqual(self.store.connection.execute("SELECT state FROM jobs WHERE job_id=?", (archive,)).fetchone()[0], "verified_complete")

    def test_queue_detects_deleted_archived_material_without_refresh_or_business_writes(self):
        self.extracting(); _, run = self.platform_run()
        with self.flow.work_lock(self.fid) as lock:
            archive = self.archive_submission(run, lock)
            self.flow.reconcile_move(archive, source=self.listing("extracting"), destination=self.source("complete"), lock=lock)
        stored = tuple(self.store.connection.execute("SELECT record_json,location_version,move_job_id FROM cloud_lifecycle_projects WHERE folder_id=?", (self.fid,)).fetchone())
        receipt = self.store.connection.execute("SELECT receipt_json FROM jobs WHERE job_id=?", (archive,)).fetchone()[0]
        (self.root / "transcript.txt").unlink()
        changes = self.store.connection.total_changes
        self.store.connection.execute("PRAGMA query_only=ON")
        try:
            queue = self.flow.queue()  # No explicit refresh/reconciliation after deletion.
        finally:
            self.store.connection.execute("PRAGMA query_only=OFF")
        self.assertEqual(self.store.connection.total_changes, changes)
        self.assertEqual(queue["next_action"], "project")
        view = queue["projects"][0]
        self.assertEqual(view["action"], "repair_content")
        self.assertEqual((view["state"], view["content_state"], view["persisted_content_state"]), ("complete", "incomplete", "complete"))
        self.assertTrue(view["repair_required"])
        self.assertFalse(view["archive_pending"])
        self.assertIn("artifact_integrity_failed", view["content_evidence"]["gaps"])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--db", str(self.store.db_path), "queue"]), 0)
        self.assertEqual(json.loads(output.getvalue())["projects"][0]["action"], "repair_content")
        self.assertEqual(stored, tuple(self.store.connection.execute("SELECT record_json,location_version,move_job_id FROM cloud_lifecycle_projects WHERE folder_id=?", (self.fid,)).fetchone()))
        self.assertEqual(receipt, self.store.connection.execute("SELECT receipt_json FROM jobs WHERE job_id=?", (archive,)).fetchone()[0])

    def test_queue_live_archive_pending_tracks_damage_and_restoration_without_writes(self):
        self.extracting(); _, run = self.platform_run()
        with self.flow.work_lock(self.fid) as lock:
            self.flow.refresh_content(self.fid, run, lock=lock)
        stored = self.store.connection.execute("SELECT record_json FROM cloud_lifecycle_projects WHERE folder_id=?", (self.fid,)).fetchone()[0]
        original = (self.root / "transcript.txt").read_bytes()
        (self.root / "transcript.txt").write_text("changed after last checkpoint", encoding="utf-8")
        changes = self.store.connection.total_changes
        self.store.connection.execute("PRAGMA query_only=ON")
        try:
            damaged = self.flow.queue()["projects"][0]
            self.assertEqual(damaged["action"], "process_content")
            self.assertFalse(damaged["archive_pending"])
            self.assertEqual(damaged["state"], "extracting")
            (self.root / "transcript.txt").write_bytes(original)
            repaired = self.flow.queue()["projects"][0]
            self.assertEqual(repaired["action"], "archive_only")
            self.assertTrue(repaired["archive_pending"])
        finally:
            self.store.connection.execute("PRAGMA query_only=OFF")
        self.assertEqual(changes, self.store.connection.total_changes)
        self.assertEqual(stored, self.store.connection.execute("SELECT record_json FROM cloud_lifecycle_projects WHERE folder_id=?", (self.fid,)).fetchone()[0])

    def test_unsealed_incomplete_inventory_and_quality_gaps_cannot_archive(self):
        self.extracting()
        _, run = self.platform_run(complete=False, sufficient=False, summary=True)
        with self.flow.work_lock(self.fid) as lock:
            record = self.flow.refresh_content(self.fid, run, lock=lock)
            self.assertEqual(record["content_state"], "incomplete")
            self.assertIn("inventory_not_complete", record["content_evidence"]["gaps"])
            self.assertIn("first_pass_not_sealed", record["content_evidence"]["gaps"])
            blocked = self.flow.prepare_move(self.fid, to_state="complete", expected_state="extracting", source=self.source("extracting"), destination=self.listing("complete"), lock=lock)
            self.assertFalse(blocked["may_send"])
            self.assertIsNone(blocked["job_id"])

    def test_damaged_product_is_rechecked_immediately_before_archive_send(self):
        self.extracting(); _, run = self.platform_run()
        with self.flow.work_lock(self.fid) as lock:
            self.flow.refresh_content(self.fid, run, lock=lock)
            move = self.flow.prepare_move(self.fid, to_state="complete", expected_state="extracting", source=self.source("extracting"), destination=self.listing("complete"), lock=lock)
            (self.root / "transcript.txt").write_text("changed", encoding="utf-8")
            blocked = self.flow.begin_move(move["job_id"], source=self.source("extracting"), destination=self.listing("complete"), lock=lock)
            self.assertFalse(blocked["may_send"])
            self.assertEqual(self.flow.get_project(self.fid)["content_state"], "incomplete")
            self.assertIn("artifact_integrity_failed", blocked["content_evidence"]["gaps"])
            self.assertEqual(self.flow.move_status(move["job_id"])["status"], "queued")

    def test_read_only_cli_and_nonexistent_database_are_safe(self):
        self.register()
        before = self.store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--db", str(self.store.db_path), "queue"]), 0)
        self.assertTrue(json.loads(output.getvalue())["read_only"])
        self.assertEqual(before, self.store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        missing = self.root / "never-create.sqlite3"
        with redirect_stdout(io.StringIO()): self.assertEqual(main(["--db", str(missing), "status"]), 2)
        self.assertFalse(missing.exists())
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["date", "163378_AI", "--course-root-name", "课程20261105"]), 0)
        self.assertEqual(json.loads(output.getvalue())["project_date"], "2026-11-05")

    def test_scope_container_identity_and_stable_local_project_binding(self):
        self.register()
        bad = dict(self.config.containers)
        bad["complete"] = RemoteContainer("10", bad["complete"].path)
        with self.assertRaises(ContractError): CloudLifecycle(self.store, replace(self.config, containers=bad))
        with self.flow.work_lock(self.fid) as lock:
            with self.assertRaises(GateError): self.flow.prepare_move(self.fid, to_state="extracting", expected_state="pending", source=self.source(), destination=replace(self.listing("extracting"), folder_id="wrong-container"), lock=lock)
            other = self.store.create_project(topic="Another independent project")
            with self.assertRaises(GateError): self.flow.register_project(other, self.fid, self.source(), lock=lock)
            with self.assertRaises(GateError): self.flow.prepare_move(self.fid, to_state="complete", expected_state="pending", source=self.source(), destination=self.listing("complete"), lock=lock)


if __name__ == "__main__":
    unittest.main()
