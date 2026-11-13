"""Temp-only local delivery tests: real TXT/PNG, real Store/Analysis publications."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from worldanalysis import local_supplement as local
from worldanalysis.analysis import Analysis
from worldanalysis.contracts import ContractError, canonical, hash_file, now
from worldanalysis.store import Store


def cleanup(path):
    def writable(func, name, exc):
        Path(name).chmod(stat.S_IWRITE)
        func(name)
    shutil.rmtree(path, onerror=writable)


class LocalSupplementTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wa-local-supplement-"))
        self.addCleanup(cleanup, self.root)
        self.store = Store(self.root)
        self.addCleanup(lambda: self.store.close())
        self.serial = self.extract_calls = 0

    def make(self, mode="text", *, accepted_request=False):
        self.serial += 1
        self.project = self.store.create_project(project_id="local-project-" + str(self.serial), topic="Synthetic local evidence")
        self.source = self.root / ("source-" + str(self.serial) + (".png" if mode in {"image", "geometry", "crop"} else ".txt"))
        if self.source.suffix == ".png":
            Image.new("RGB", (6, 4), "red").save(self.source)
        else:
            self.source.write_text("Actual bounded local source text.\n", encoding="utf-8")

        def extract(source, output, options=None):
            self.extract_calls += 1
            files = []
            locator = {"kind": "paragraph", "precision": "exact", "paragraph_index": 0}
            block = {"kind": "paragraph", "origin": "native_extract", "locator": locator,
                     "text": source.read_text(encoding="utf-8") if source.suffix == ".txt" else "",
                     "file_keys": [], "processing_status": "extracted", "reason": "Actual temporary source bytes"}
            if mode in {"image", "geometry", "crop", "render"}:
                image = output / "actual.png"
                if source.suffix == ".png":
                    image.write_bytes(source.read_bytes())
                else:
                    Image.new("RGB", (6, 4), "blue").save(image)
                kind = "page_render" if mode == "render" else "image"
                files = [{"key": "pixels", "relative_path": "actual.png", "kind": kind, "media_type": "image/png"}]
                locator = {"kind": "page", "precision": "page_only", "page_number": 1} if mode == "render" else {"kind": "node", "precision": "exact", "node_id": "original-image"}
                if mode in {"geometry", "crop"}:
                    locator["geometry"] = {"reference_object_ref": "pixels", "coordinate_space": "original", "unit": "pixel", "origin": "top_left",
                        "frame_size": {"width": 6, "height": 4}, "original_size": {"width": 6, "height": 4, "unit": "pixel"},
                        "rotation_degrees": 0, "bbox_xywh": [0, 0, 6 if mode == "geometry" else 3, 4],
                        "transform_to_original": {"method": "identity", "operations": []}}
                block = {"kind": "image_occurrence" if mode != "render" else "hidden_object", "origin": "native_extract", "locator": locator,
                         "file_keys": ["pixels"], "processing_status": "deferred", "reason": "Actual pixels retained, not semantically read"}
            if mode == "ocr":
                block["origin"] = "ocr"
            if mode == "missing":
                block.pop("text")
                block["processing_status"] = "deferred"
            if mode == "descriptor":
                block = {"kind": "paragraph", "origin": "native_extract", "locator": {"kind": "time", "precision": "exact", "start_ms": 0, "end_ms": 1000},
                    "text": "Media registration description only; no audio bytes.", "file_keys": [], "processing_status": "extracted", "reason": "Synthetic description, not an audio artifact"}
            return {"media_type": "image/png" if source.suffix == ".png" else "text/plain",
                "provider_id": "synthetic-local-supplement", "provider_version": "1", "blocks": [block], "files": files,
                "gaps": [{"kind": "semantic_gap", "severity": "important", "reason": "Unresolved semantic limitation", "block_indexes": [0]}],
                "metrics": [], "enumeration_state": "complete", "processing_status": "partial", "metadata": {"synthetic": True}}

        self.bundle = self.store.ingest(self.project, [self.source], extract)
        self.analysis = Analysis(self.store)
        self.task = self.analysis.claim(self.project, model_version="synthetic-local-test-1")
        self.target = {"project_id": self.project, **self.bundle["blocks"][0]["asset_ref"],
                       "block_id": self.bundle["blocks"][0]["block_id"], "locator": deepcopy(self.bundle["blocks"][0]["locator"])}
        request = self.proposal("original_image" if mode in {"image", "geometry", "crop"} else "original_text")
        result = {"schema_version": "1.0-draft", "analysis_id": "local-analysis-" + str(self.serial), "analysis_revision": "v1",
            "project_id": self.project, "based_bundle_id": self.bundle["bundle_id"], "based_bundle_revision": self.bundle["bundle_revision"],
            "based_publish_id": self.bundle["publish_id"], "created_at": now(), "execution_status": "blocked", "agent": deepcopy(self.task["agent"]),
            "block_processing": [{"block_id": b["block_id"], "asset_ref": b["asset_ref"], "status": "pending", "priority": "normal",
                                  "reason": "Deliberately unread; delivery test is not semantic analysis", "read_receipt_refs": [], "exclusion": None} for b in self.bundle["blocks"]],
            "coverage": {"scope": "based_bundle_only", "source_inventory_state": "complete", "discovered_blocks": len(self.bundle["blocks"]),
                         "read_blocks": 0, "failed_blocks": 0, "pending_blocks": len(self.bundle["blocks"]), "excluded_blocks": 0, "not_applicable_blocks": 0,
                         "claim": "partial", "limitations": ["No semantic read performed; original gaps remain open."]},
            "assertions": [], "external_sources": [], "gaps": deepcopy(self.bundle["gaps"]),
            "supplement_requests": [request] if accepted_request else [], "main_report_relative_path": None, "reports": [],
            "program_validation": {"status": "pending", "validator_version": "untrusted-placeholder", "checked_at": None, "report_relative_path": None}}
        path = self.store.project_root(self.project) / self.task["output_relative_path"] / "submitted.json"
        path.write_bytes(canonical(result))
        self.analysis.complete(self.task["task_id"], path)
        self.accepted = self.analysis.get_result(self.task["task_id"])
        self.accepted_path = self.store.project_root(self.project) / self.store.connection.execute("SELECT result_relative_path FROM analyses WHERE task_id=?", (self.task["task_id"],)).fetchone()[0]
        self.accepted_sha = hash_file(self.accepted_path)
        self.initial_jobs = [tuple(r) for r in self.store.connection.execute("SELECT * FROM jobs ORDER BY job_id")]
        self.old_files = {a["relative_path"]: hash_file(self.store.project_root(self.project) / a["relative_path"]) for a in self.bundle["artifacts"]}
        if accepted_request:
            return self.accepted["supplement_requests"][0]
        return request

    def proposal(self, output="original_text", **overrides):
        result = {"request_id": "local-request-" + str(self.serial), "gap_ids": [self.bundle["gaps"][0]["gap_id"]],
                  "reason": "Deliver only an existing exact artifact; not a new parse or semantic reading",
                  "minimal_targets": [deepcopy(self.target)], "requested_outputs": [output], "priority": "important",
                  "status": "proposed", "requires_scope_expansion": False}
        result.update(overrides)
        return result

    def queue(self, request):
        return self.analysis.request_supplement(self.task["task_id"], request)

    def publications(self):
        return self.store.connection.execute("SELECT COUNT(*) FROM publications").fetchone()[0]

    def request_state(self, request):
        return self.store.connection.execute("SELECT state FROM supplement_requests WHERE request_id=?", (request["request_id"],)).fetchone()[0]

    def deliver(self, request):
        return local.fulfil_available(self.store, request["request_id"])

    def assert_delivered(self, request, result):
        self.assertEqual(result["status"], "fulfilled")
        self.assertEqual(result["delivery_kind"], "reused_existing_evidence")
        self.assertNotEqual(result["bundle_id"], self.bundle["bundle_id"])
        self.assertEqual(result["source_bundle_id"], self.bundle["bundle_id"])
        self.assertFalse(result["new_extraction_executed"])
        self.assertFalse(result["semantic_read_performed"])
        self.assertFalse(result["gaps_resolved"])
        self.assertEqual(self.request_state(request), "fulfilled")
        new = self.store.get_bundle(self.project, result["bundle_id"])
        self.assertEqual({g["gap_id"]: g for g in new["gaps"]}, {g["gap_id"]: g for g in self.bundle["gaps"]})
        for item in result["artifacts"]:
            artifact = next(a for a in new["artifacts"] if a["artifact_id"] == item["artifact_id"])
            self.assertEqual(hash_file(self.store.project_root(self.project) / artifact["relative_path"]), (item["sha256"], item["byte_length"]))
        self.assertEqual(hash_file(self.accepted_path), self.accepted_sha)
        self.assertEqual([tuple(r) for r in self.store.connection.execute("SELECT * FROM jobs ORDER BY job_id")], self.initial_jobs)
        for relative, identity in self.old_files.items():
            self.assertEqual(hash_file(self.store.project_root(self.project) / relative), identity)
        self.assertEqual(self.extract_calls, self.serial)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM read_receipts").fetchone()[0], 0)

    def test_real_text_postaccepted_API_new_wrapper_and_idempotent(self):
        request = self.make()
        self.assertEqual(self.accepted["supplement_requests"], [])
        self.queue(request)
        result = self.deliver(request)
        self.assert_delivered(request, result)
        with patch.object(self.store, "compose", side_effect=AssertionError("must not recompose")):
            replay = self.deliver(request)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["bundle_id"], result["bundle_id"])
        events = [r[0] for r in self.store.connection.execute("SELECT kind FROM events WHERE record_id=? AND kind LIKE 'local_supplement_%' ORDER BY rowid", (request["request_id"],))]
        self.assertEqual(events, ["local_supplement_queued", "local_supplement_prepared", "local_supplement_published", "local_supplement_fulfilled"])

    def test_real_image_and_full_geometry_supported(self):
        for mode in ("image", "geometry"):
            with self.subTest(mode=mode):
                request = self.make(mode)
                self.queue(request)
                result = self.deliver(request)
                self.assert_delivered(request, result)
                artifact = next(a for a in self.bundle["artifacts"] if a["artifact_id"] == result["artifacts"][0]["artifact_id"])
                self.assertEqual(artifact["kind"], "image")

    def test_accepted_JSON_request_has_original_commit_provenance(self):
        request = self.make(accepted_request=True)
        self.assert_delivered(request, self.deliver(request))

    def test_page_render_is_not_original_image(self):
        request = self.make("render")
        request["requested_outputs"] = ["original_image"]
        self.queue(request)
        with patch.object(self.store, "compose", side_effect=AssertionError("no matching image")):
            self.assertEqual(self.deliver(request)["status"], "pending")
        request2 = self.proposal("page_render", request_id="actual-render-request")
        self.queue(request2)
        self.assert_delivered(request2, self.deliver(request2))

    def test_original_image_cannot_be_text_or_metadata(self):
        request = self.make()
        request["requested_outputs"] = ["original_image"]
        self.queue(request)
        with patch.object(self.store, "compose", side_effect=AssertionError("no original image")):
            self.assertEqual(self.deliver(request)["status"], "pending")
        self.assertEqual(self.publications(), 1)
        self.assertEqual(self.request_state(request), "queued")

    def test_OCR_text_never_claimed_original(self):
        request = self.make("ocr")
        self.queue(request)
        with patch.object(self.store, "compose", side_effect=AssertionError("OCR is not original text")):
            self.assertEqual(self.deliver(request)["status"], "unsupported")
        self.assertEqual(self.publications(), 1)

    def test_missing_artifact_stays_pending(self):
        request = self.make("missing")
        self.queue(request)
        self.assertEqual(self.deliver(request)["status"], "pending")
        self.assertEqual(self.request_state(request), "queued")
        self.assertEqual(self.publications(), 1)

    def test_table_transcript_audio_and_media_description_unsupported(self):
        request = self.make("descriptor")
        for output in ("audio_window", "transcript", "table", "video_clip"):
            req = deepcopy(request)
            req.update(request_id="unsupported-" + output, requested_outputs=[output])
            self.queue(req)
            with patch.object(self.store, "compose", side_effect=AssertionError("never publish unsupported output")):
                result = self.deliver(req)
            self.assertEqual(result["status"], "unsupported")
            self.assertEqual(result["artifacts"], [])
            self.assertEqual(self.request_state(req), "queued")
        self.assertEqual(self.publications(), 1)

    def test_all_targets_required_no_partial_fulfilment(self):
        request = self.make()
        other = deepcopy(self.target)
        other.pop("block_id")
        other["locator"] = {"kind": "paragraph", "precision": "exact", "paragraph_index": 999}
        request["minimal_targets"].append(other)
        self.queue(request)
        self.assertEqual(self.deliver(request)["status"], "unsupported")
        self.assertEqual(self.publications(), 1)

    def test_no_target_and_crop_remain_unsupported(self):
        request = self.make("crop")
        self.queue(request)
        self.assertEqual(self.deliver(request)["status"], "unsupported")
        empty = self.proposal("original_image", request_id="empty-target", minimal_targets=[])
        self.queue(empty)
        self.assertEqual(self.deliver(empty)["status"], "unsupported")
        self.assertEqual(self.publications(), 1)

    def test_collection_pause_and_scope_expansion_do_not_execute(self):
        request = self.make()
        expanded = self.proposal(request_id="expansion", requires_scope_expansion=True)
        self.queue(expanded)
        self.assertEqual(self.request_state(expanded), "blocked_scope_expansion")
        with patch.object(self.store, "compose", side_effect=AssertionError("blocked request")):
            self.assertEqual(self.deliver(expanded)["status"], "blocked")
            self.assertEqual(self.request_state(expanded), "blocked_scope_expansion")
            self.queue(request)
            self.store.control("collection", "pause")
            self.assertEqual(self.deliver(request)["status"], "blocked")
        self.assertEqual(self.publications(), 1)
        self.assertEqual(self.request_state(request), "queued")
        self.assertFalse(self.store.connection.execute("SELECT 1 FROM sqlite_master WHERE name='local_supplement_deliveries'").fetchone())

    def test_fake_request_wrong_task_crossproject_and_invalid_schema_rejected(self):
        request = self.make()
        self.queue(request)
        with patch.object(self.store, "compose", side_effect=AssertionError("invalid evidence must not publish")):
            fake = deepcopy(request)
            fake["request_id"] = "forged-request"
            self.store.connection.execute("INSERT INTO supplement_requests VALUES(?,?,?,?,?,?,?)", (fake["request_id"], self.project, self.task["task_id"], self.bundle["publish_id"], "queued", canonical(fake).decode(), now()))
            with self.assertRaises(ContractError):
                self.deliver(fake)
            for mutate in (lambda r: r["requested_outputs"].append("not_a_type"), lambda r: r["gap_ids"].append("missing-gap"),
                           lambda r: r["minimal_targets"][0].update(project_id="another-project"),
                           lambda r: r["minimal_targets"][0].update(asset_revision="another-revision")):
                bad = deepcopy(request)
                mutate(bad)
                self.store.connection.execute("UPDATE supplement_requests SET request_json=? WHERE request_id=?", (canonical(bad).decode(), request["request_id"]))
                with self.assertRaises(ContractError):
                    self.deliver(request)
            self.store.connection.execute("UPDATE supplement_requests SET request_json=? WHERE request_id=?", (canonical(request).decode(), request["request_id"]))
            other = self.store.create_project(project_id="other-project", topic="Must not cross")
            self.store.connection.execute("UPDATE supplement_requests SET project_id=? WHERE request_id=?", (other, request["request_id"]))
            with self.assertRaises(ContractError):
                self.deliver(request)
            self.store.connection.execute("UPDATE supplement_requests SET project_id=? WHERE request_id=?", (self.project, request["request_id"]))
            wrong = self.analysis.claim(self.project, model_version="wrong-source-task-test")
            self.store.connection.execute("UPDATE supplement_requests SET task_id=? WHERE request_id=?", (wrong["task_id"], request["request_id"]))
            with self.assertRaises(ContractError):
                self.deliver(request)
            self.store.connection.execute("UPDATE supplement_requests SET task_id=? WHERE request_id=?", (self.task["task_id"], request["request_id"]))
            self.store.connection.execute("UPDATE events SET payload_json='{}' WHERE kind='supplement_requested' AND record_id=?", (request["request_id"],))
            with self.assertRaises(ContractError):
                self.deliver(request)
        self.assertEqual(self.publications(), 1)

    def test_missing_or_changed_local_source_fails_closed(self):
        request = self.make()
        self.queue(request)
        moved = self.source.with_suffix(".moved")
        self.source.rename(moved)
        with self.assertRaises(ContractError):
            self.deliver(request)
        moved.rename(self.source)
        self.source.write_text("Tampered original source", encoding="utf-8")
        with self.assertRaises(ContractError):
            self.deliver(request)
        self.assertEqual(self.publications(), 1)

    def test_tampered_published_metadata_fails_closed(self):
        request = self.make()
        self.queue(request)
        metadata = next(a for a in self.bundle["artifacts"] if "/metadata/" in a["relative_path"])
        path = self.store.project_root(self.project) / metadata["relative_path"]
        path.chmod(stat.S_IWRITE)
        path.write_bytes(b"{}")
        with self.assertRaises(ContractError):
            self.deliver(request)
        self.assertEqual(self.request_state(request), "queued")
        self.assertEqual(self.publications(), 1)

    def reopen(self):
        self.store.close()
        self.store = Store(self.root)
        self.analysis = Analysis(self.store)

    def test_compose_finished_then_crash_reconciles_without_republication(self):
        request = self.make()
        self.queue(request)
        compose = self.store.compose
        def crash(*args):
            compose(*args)
            raise RuntimeError("crash after publish before local state update")
        with patch.object(self.store, "compose", side_effect=crash):
            with self.assertRaises(RuntimeError):
                self.deliver(request)
        self.assertEqual(local._row(self.store, request["request_id"])["state"], "prepared")
        self.assertEqual(self.publications(), 2)
        self.reopen()
        with patch.object(self.store, "compose", side_effect=AssertionError("must reconcile existing_bundle only")):
            result = self.deliver(request)
        self.assert_delivered(request, result)
        self.assertEqual(self.publications(), 2)

    def test_rename_before_DB_update_restart_uses_existing_publication(self):
        request = self.make()
        self.queue(request)
        with patch.object(self.store, "_after_rename", side_effect=OSError("rename completed, DB not updated")):
            with self.assertRaises(ContractError):
                self.deliver(request)
        self.assertEqual(self.publications(), 2)
        self.reopen()
        with patch.object(self.store, "compose", side_effect=AssertionError("must not republish after rename")):
            self.assert_delivered(request, self.deliver(request))
        self.assertEqual(self.publications(), 2)

    def test_unknown_prepared_without_publication_never_retries_compose(self):
        request = self.make()
        self.queue(request)
        with patch.object(self.store, "compose", side_effect=RuntimeError("unknown before compose journal")):
            with self.assertRaises(RuntimeError):
                self.deliver(request)
        self.reopen()
        with patch.object(self.store, "compose", side_effect=AssertionError("unknown must not automatically replay")):
            result = self.deliver(request)
        self.assertEqual(result["status"], "pending")
        self.assertIn("reconcile_existing_bundle_only", result["reason"])
        self.assertEqual(self.request_state(request), "queued")
        self.assertEqual(self.publications(), 1)

    def test_published_checkpoint_and_request_fulfilled_CAS_are_atomic(self):
        request = self.make()
        self.queue(request)
        cas = local._cas
        def crash(store, row, state, **kwargs):
            if state == "fulfilled":
                raise RuntimeError("before fulfilled commit")
            return cas(store, row, state, **kwargs)
        with patch.object(local, "_cas", side_effect=crash):
            with self.assertRaises(RuntimeError):
                self.deliver(request)
        self.assertEqual(local._row(self.store, request["request_id"])["state"], "published")
        self.assertEqual(self.request_state(request), "queued")
        self.reopen()
        with patch.object(self.store, "compose", side_effect=AssertionError("published must reconcile")):
            self.assert_delivered(request, self.deliver(request))
        self.assertEqual(self.publications(), 2)


if __name__ == "__main__":
    unittest.main()
