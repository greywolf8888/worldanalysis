"""Real PNG, original schemas and ONE real temporary SQLite Store; no real DB."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from worldanalysis import derived_publication as publication
from worldanalysis.analysis import Analysis
from worldanalysis.contracts import ContractError, canonical, digest, hash_file, now, validate_bundle, validate_plan
from worldanalysis.store import Store


def bbox(left=2, top=1, right=10, bottom=7):
    return {"unit": "pixel", "origin": "top_left", "xyxy": [left, top, right, bottom]}


def cleanup(path):
    def writable(func, name, exc):
        Path(name).chmod(stat.S_IWRITE)
        func(name)
    shutil.rmtree(path, onerror=writable)


class DerivedPublicationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wa-derived-publication-"))
        self.addCleanup(cleanup, self.root)
        self.store = Store(self.root)
        self.addCleanup(lambda: self.store.close())
        self.project = self.store.create_project(project_id="derived-project", topic="Synthetic crop publication")
        self.source = self.root / "actual.png"
        with Image.new("RGB", (12, 8)) as image:
            for y in range(8):
                for x in range(12):
                    image.putpixel((x, y), (x * 19, y * 29, (x + y) * 11))
            image.save(self.source)
        self.image_kind = "image"
        def extract(source, output, options=None):
            (output / "pixels.png").write_bytes(source.read_bytes())
            return {"media_type": "image/png", "provider_id": "synthetic-derived-fixture", "provider_version": "1",
                "files": [{"key": "pixels", "relative_path": "pixels.png", "kind": self.image_kind, "media_type": "image/png"}],
                "blocks": [{"kind": "image_occurrence", "origin": "native_extract", "locator": {"kind": "slide", "precision": "exact", "slide_number": 2,
                    "geometry": {"reference_object_ref": "pixels", "coordinate_space": "original", "unit": "pixel", "origin": "top_left",
                        "frame_size": {"width": 12, "height": 8}, "original_size": {"width": 12, "height": 8, "unit": "pixel"},
                        "rotation_degrees": 0, "bbox_xywh": [0, 0, 12, 8], "transform_to_original": {"method": "identity", "operations": []}}},
                    "file_keys": ["pixels"], "processing_status": "deferred", "reason": "Actual pixels, not semantically read"},
                    {"kind": "paragraph", "origin": "native_extract", "locator": {"kind": "paragraph", "precision": "exact", "paragraph_index": 0},
                     "text": "Synthetic literal support contact fixture@example.invalid", "file_keys": [], "processing_status": "extracted", "reason": "Separate bounded text"}],
                "gaps": [], "metrics": [], "enumeration_state": "complete", "processing_status": "partial", "metadata": {"synthetic": True}}
        self.extractor = extract
        self.bundle = self.store.ingest(self.project, [self.source], extract)
        self.block = self.bundle["blocks"][0]
        self.artifact = next(a for a in self.bundle["artifacts"] if a["artifact_id"] in self.block["artifact_ids"])
        self.project_root = self.store.project_root(self.project)
        self.old_jobs = [tuple(r) for r in self.store.connection.execute("SELECT * FROM jobs ORDER BY job_id")]
        self.original_sha = hash_file(self.source)
        self.old_files = {a["relative_path"]: hash_file(self.project_root / a["relative_path"]) for a in self.bundle["artifacts"]}

    def publish(self, *, output="补采派生/region.png", box=None, resize=None, request_id=None):
        return publication.publish_image_region(self.store, self.project, self.bundle["bundle_id"], self.block["block_id"], self.artifact["artifact_id"],
            bbox() if box is None else box, output_relative=output, resize=resize, request_id=request_id)

    def counts(self):
        return tuple(self.store.connection.execute("SELECT (SELECT COUNT(*) FROM jobs),(SELECT COUNT(*) FROM publications),(SELECT COUNT(*) FROM plans)").fetchone())

    def row(self):
        return self.store.connection.execute("SELECT * FROM publications ORDER BY revision DESC LIMIT 1").fetchone()

    def restart(self):
        self.store.close()
        self.store = Store(self.root)

    def assert_original(self):
        self.assertEqual(self.store.get_bundle(self.project, self.bundle["bundle_id"]), self.bundle)
        self.assertEqual(hash_file(self.source), self.original_sha)
        for relative, value in self.old_files.items():
            self.assertEqual(hash_file(self.project_root / relative), value)
        for row in self.old_jobs:
            self.assertEqual(tuple(self.store.connection.execute("SELECT * FROM jobs WHERE job_id=?", (row[0],)).fetchone()), row)

    def test_real_publication_original_schemas_store_analysis_geometry_and_preservation(self):
        before = self.counts()
        new = self.publish(resize=(4, 3))
        self.assertNotEqual(new["bundle_id"], self.bundle["bundle_id"])
        self.assertEqual(self.store.get_bundle(self.project, new["bundle_id"]), new)
        validate_bundle(new, self.project_root)
        plan = json.loads((self.project_root / new["publication"]["manifest_relative_path"]).with_name("plan.json").read_bytes())
        validate_plan(plan)
        self.assertEqual(new["assets"], self.bundle["assets"])
        self.assertEqual(new["blocks"][:-1], self.bundle["blocks"])
        self.assertEqual(new["gaps"], self.bundle["gaps"])
        self.assertEqual(new["contacts"], self.bundle["contacts"])
        self.assertTrue(new["contacts"])
        self.assertEqual(new["artifacts"][:len(self.bundle["artifacts"])], self.bundle["artifacts"])
        self.assertEqual(new["coverage"]["claimed_status"], self.bundle["coverage"]["claimed_status"])
        self.assertEqual(new["coverage"]["discovered_counts"], {**self.bundle["coverage"]["discovered_counts"], "blocks": len(self.bundle["blocks"]) + 1})
        derived = new["blocks"][-1]
        self.assertEqual(derived["kind"], "metadata")
        image = next(a for a in new["artifacts"] if a["artifact_id"] in derived["artifact_ids"] and a["kind"] == "image")
        geometry = derived["locator"]["geometry"]
        self.assertEqual(geometry["reference_object_ref"], image["artifact_id"])
        self.assertEqual(geometry["transform_to_original"]["matrix"], [2, 0, 2, 0, 2, 1, 0, 0, 1])
        self.assertNotIn("geometry", image)  # original Artifact schema has no geometry field
        self.assertEqual(derived["derived_from"][0]["artifact_id"], self.artifact["artifact_id"])
        self.assertEqual(derived["derived_from"][0]["block_id"], self.block["block_id"])
        job = self.store.connection.execute("SELECT * FROM jobs WHERE job_id=?", (image["job_id"],)).fetchone()
        self.assertEqual(job["state"], "verified_complete")
        self.assertEqual(json.loads(job["receipt_json"]), {"publish_id": new["publish_id"]})
        self.assertEqual(job["max_cost_minor"], 0)
        self.assertEqual(job["estimated_cost_minor"], 0)
        self.assertEqual(json.loads(job["provider_json"]), image["provider"])
        local_plan = next(j for a in plan["asset_plans"] for j in a["jobs"] if j["job_id"] == image["job_id"])
        self.assertIn(self.old_jobs[0][0], local_plan["depends_on_job_ids"])
        self.assertEqual(local_plan["tier"], "structured_local")
        self.assertEqual(self.counts(), (before[0] + 1, before[1] + 1, before[2] + 2))
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM read_receipts").fetchone()[0], 0)
        analysis = Analysis(self.store)
        task = analysis.claim(self.project, model_version="synthetic-byte-viewer-1")
        self.assertEqual(task["bundle_id"], new["bundle_id"])
        def opener(data, **kw):
            self.assertEqual(hashlib.sha256(data).hexdigest(), image["sha256"])
            with Image.open(io.BytesIO(data)) as decoded, Image.open(self.source) as original:
                self.assertEqual(decoded.size, (4, 3))
                self.assertEqual(decoded.getpixel((0, 0)), original.getpixel((3, 2)))
            return {"opened": True, "sha256": kw["sha256"], "viewer_id": "synthetic-byte-consuming-viewer", "observation": "Actual test pixels opened; not production semantics."}
        opened = analysis.open_image(task["task_id"], derived["block_id"], image["artifact_id"], opener=opener)
        self.assertEqual(opened["status"], "opened")
        self.assert_original()

    def test_duplicate_and_new_scratch_name_create_no_jobs_or_publication(self):
        first = self.publish()
        before = self.counts()
        with patch.object(publication.image_region, "crop_region", side_effect=AssertionError("must not recrop")):
            self.assertEqual(first, self.publish())
            self.assertEqual(first, self.publish(output="补采派生/unused-alias.png"))
        self.assertFalse((self.project_root / "补采派生/unused-alias.png").exists())
        self.assertEqual(before, self.counts())
        self.assert_original()

    def test_code_and_crop_configuration_do_not_reuse_other_jobs(self):
        first = self.publish()
        second = self.publish(box=bbox(1, 1, 10, 7), output="补采派生/other-box.png")
        actual = publication._implementation()
        with patch.object(publication, "_implementation", return_value={**actual, "publication": "synthetic-code-2"}):
            third = self.publish(output="补采派生/new-code.png")
        self.assertEqual(len({b["publish_id"] for b in (first, second, third)}), 3)
        self.assertEqual(self.counts()[:2], (4, 4))
        self.assert_original()

    def test_page_render_and_inherited_composed_source_use_existing_primitive(self):
        self.image_kind = "page_render"
        rendered = self.store.ingest(self.project, [self.source], self.extractor, config_version="synthetic-page-render-v1")
        image_block = rendered["blocks"][0]
        image = next(a for a in rendered["artifacts"] if a["artifact_id"] in image_block["artifact_ids"])
        self.assertEqual(image["kind"], "page_render")
        from worldanalysis.bundle_merge import compose
        composed = compose(self.store, self.project, [rendered["bundle_id"]])
        with patch.object(publication.image_region, "crop_region", wraps=publication.image_region.crop_region) as crop:
            result = publication.publish_image_region(self.store, self.project, composed["bundle_id"], image_block["block_id"], image["artifact_id"],
                bbox(), output_relative="补采派生/render-region.png")
        self.assertEqual(crop.call_args.args[2], rendered["bundle_id"])
        self.assertEqual(result["blocks"][:-1], composed["blocks"])
        self.assertEqual(result["gaps"], composed["gaps"])
        self.assert_original()

    def test_crop_of_published_crop_composes_current_to_original_geometry(self):
        first = self.publish(resize=(4, 3))
        block = first["blocks"][-1]
        image = next(a for a in first["artifacts"] if a["artifact_id"] in block["artifact_ids"] and a["kind"] == "image")
        second = publication.publish_image_region(self.store, self.project, first["bundle_id"], block["block_id"], image["artifact_id"],
            bbox(0, 0, 2, 2), output_relative="补采派生/second-crop.png")
        geometry = second["blocks"][-1]["locator"]["geometry"]
        self.assertEqual(geometry["frame_size"], {"width": 2, "height": 2})
        self.assertEqual(geometry["original_size"], {"width": 12, "height": 8, "unit": "pixel"})
        self.assertEqual(geometry["transform_to_original"]["matrix"], [2, 0, 2, 0, 2, 1, 0, 0, 1])
        self.assertEqual(second["blocks"][-1]["derived_from"][0]["block_id"], block["block_id"])
        self.assert_original()

    def test_wrong_project_locator_and_nonimage_are_rejected_before_job(self):
        other = self.store.create_project(project_id="other-project", topic="No permission inheritance")
        before = self.counts()
        with self.assertRaises(ContractError):
            publication.publish_image_region(self.store, other, self.bundle["bundle_id"], self.block["block_id"], self.artifact["artifact_id"], bbox(), output_relative="补采派生/no.png")
        with self.assertRaises(ContractError):
            publication.publish_image_region(self.store, self.project, self.bundle["bundle_id"], self.bundle["blocks"][1]["block_id"], self.artifact["artifact_id"], bbox(), output_relative="补采派生/no.png")
        for box in (bbox(True), bbox(-1), bbox(2, 1, 20, 7), {"unit": "normalized", "origin": "top_left", "xyxy": [0, 0, 1, 1]}):
            with self.assertRaises(ContractError):
                self.publish(box=box)
        self.assertEqual(before, self.counts())

    def test_actual_source_bitmap_and_native_original_sha_checked_before_reuse(self):
        self.publish()
        before = self.counts()
        self.source.write_bytes(b"changed original input")
        with self.assertRaisesRegex(ContractError, "original source changed"):
            self.publish()
        self.assertEqual(before, self.counts())
        self.source.write_bytes((self.project_root / next(a for a in self.bundle["artifacts"] if a["kind"] == "original_small_file")["relative_path"]).read_bytes())
        path = self.project_root / self.artifact["relative_path"]
        path.chmod(stat.S_IWRITE)
        path.write_bytes(b"changed sealed bitmap")
        with self.assertRaisesRegex(ContractError, "hash|size mismatch"):
            self.publish()
        self.assertEqual(before, self.counts())

    def test_paused_collection_has_no_side_effects(self):
        self.store.control("collection", "pause")
        before = self.counts()
        with patch.object(publication.image_region, "crop_region", side_effect=AssertionError("paused cannot crop")):
            self.assertEqual(self.publish()["status"], "blocked")
        self.assertEqual(before, self.counts())
        self.assertFalse((self.project_root / "补采派生").exists())

    def test_intention_plan_and_running_job_are_durable_before_primitive(self):
        actual = publication.image_region.crop_region
        def crop(*args, **kwargs):
            row = self.row()
            self.assertEqual(row["state"], "staging")
            self.assertIsNone(row["bundle_json"])
            event = self.store.connection.execute("SELECT payload_json FROM events WHERE kind='image_region_derivation_intended' AND record_id=?", (row["publish_id"],)).fetchone()
            intention = json.loads(event[0])
            plan = json.loads(self.store.connection.execute("SELECT plan_json FROM plans WHERE plan_id=?", (intention["intention_plan_id"],)).fetchone()[0])
            validate_plan(plan)
            self.assertEqual(plan["program_validation"]["status"], "pending")
            self.assertEqual(digest(plan), intention["intention_plan_sha256"])
            self.assertEqual(self.store.connection.execute("SELECT state FROM jobs WHERE job_id=?", (intention["job_id"],)).fetchone()[0], "running")
            return actual(*args, **kwargs)
        with patch.object(publication.image_region, "crop_region", side_effect=crop):
            self.publish()

    def test_after_crop_before_validated_never_published_and_never_replayed(self):
        with patch.object(publication, "_build", side_effect=RuntimeError("synthetic after-crop crash")), self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.row()["state"], "staging")
        self.assertTrue((self.project_root / "补采派生/region.png").is_file())
        with patch.object(publication.image_region, "crop_region", side_effect=AssertionError("unknown must not replay")), self.assertRaisesRegex(ContractError, "recover/reconcile"):
            self.publish()
        self.restart()
        self.assertEqual(self.row()["state"], "quarantined")
        self.assert_original()

    def test_validated_before_rename_recovers_without_crop_or_receipt_overwrite(self):
        with patch.object(self.store, "_finish_publication", side_effect=RuntimeError("synthetic before-rename crash")), self.assertRaises(RuntimeError):
            self.publish()
        row = self.row()
        self.assertEqual(row["state"], "validated")
        self.assertFalse((self.project_root / row["final_relative_path"]).exists())
        with patch.object(publication.image_region, "crop_region", side_effect=AssertionError("recovery must not crop")):
            self.restart()
            result = self.publish()
        self.assertEqual(result["publish_id"], row["publish_id"])
        self.assertEqual(self.row()["state"], "published")
        self.assert_original()

    def test_rename_before_db_commit_recovers_exact_existing_files(self):
        with patch.object(self.store, "_after_rename", side_effect=RuntimeError("synthetic rename crash")), self.assertRaises(RuntimeError):
            self.publish()
        row = self.row()
        self.assertEqual(row["state"], "validated")
        self.assertTrue((self.project_root / row["final_relative_path"]).is_dir())
        self.assertFalse((self.project_root / row["staging_relative_path"]).exists())
        with patch.object(publication.image_region, "crop_region", side_effect=AssertionError("must not recrop")):
            self.restart()
            self.assertEqual(self.publish()["publish_id"], row["publish_id"])
        self.assert_original()

    def test_published_database_update_and_job_receipt_are_atomic(self):
        self.store.connection.execute("""CREATE TEMP TRIGGER synthetic_receipt_fault BEFORE UPDATE OF receipt_json ON jobs
            WHEN OLD.state='running' AND NEW.state='verified_complete'
            BEGIN SELECT RAISE(ABORT,'synthetic receipt commit fault'); END""")
        with self.assertRaises(ContractError):
            self.publish()
        row = self.row()
        self.assertEqual(row["state"], "renamed")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM publications WHERE state='published'").fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM jobs WHERE state='verified_complete'").fetchone()[0], 1)
        self.store.connection.execute("DROP TRIGGER synthetic_receipt_fault")
        self.restart()
        self.assertEqual(self.row()["state"], "published")
        self.assert_original()

    def test_untrusted_descriptor_geometry_cannot_publish(self):
        actual = publication.image_region.crop_region
        def forged(*args, **kwargs):
            descriptor = actual(*args, **kwargs)
            descriptor["geometry"]["transform_to_original"]["matrix"][2] += 1
            return descriptor
        with patch.object(publication.image_region, "crop_region", side_effect=forged), self.assertRaisesRegex(ContractError, "descriptor.*geometry"):
            self.publish()
        self.assertEqual(self.row()["state"], "staging")
        self.assert_original()

    def queue_request(self, request_id="region-request", *, box=None):
        analysis = Analysis(self.store)
        task = analysis.claim(self.project, model_version="synthetic-proposal-only-1")
        if task["status"] == "idle":
            task_id = self.store.connection.execute("SELECT task_id FROM analysis_tasks ORDER BY created_at LIMIT 1").fetchone()[0]
            task = {"task_id": task_id}
        else:
            result = {"schema_version": "1.0-draft", "analysis_id": "synthetic-region-analysis", "analysis_revision": "v1", "project_id": self.project,
                "based_bundle_id": self.bundle["bundle_id"], "based_bundle_revision": self.bundle["bundle_revision"], "based_publish_id": self.bundle["publish_id"],
                "created_at": now(), "execution_status": "blocked", "agent": deepcopy(task["agent"]),
                "block_processing": [{"block_id": b["block_id"], "asset_ref": b["asset_ref"], "status": "pending", "priority": "normal",
                    "reason": "Unread; proposal fixture only", "read_receipt_refs": [], "exclusion": None} for b in self.bundle["blocks"]],
                "coverage": {"scope": "based_bundle_only", "source_inventory_state": "complete", "discovered_blocks": len(self.bundle["blocks"]),
                    "read_blocks": 0, "failed_blocks": 0, "pending_blocks": len(self.bundle["blocks"]), "excluded_blocks": 0, "not_applicable_blocks": 0,
                    "claim": "partial", "limitations": ["No semantic reading executed."]},
                "assertions": [], "external_sources": [], "gaps": deepcopy(self.bundle["gaps"]), "supplement_requests": [],
                "main_report_relative_path": None, "reports": [], "program_validation": {"status": "pending", "validator_version": "untrusted", "checked_at": None, "report_relative_path": None}}
            path = self.project_root / task["output_relative_path"] / "submission.json"
            path.write_bytes(canonical(result))
            analysis.complete(task["task_id"], path)
        locator = deepcopy(self.block["locator"])
        xyxy = (box or bbox())["xyxy"]
        locator["geometry"]["bbox_xywh"] = [xyxy[0], xyxy[1], xyxy[2] - xyxy[0], xyxy[3] - xyxy[1]]
        request = {"request_id": request_id, "gap_ids": [self.bundle["gaps"][0]["gap_id"]], "reason": "Exact requested region, not permission or a whole-page read.",
            "minimal_targets": [{"project_id": self.project, **self.block["asset_ref"], "artifact_id": self.artifact["artifact_id"], "locator": locator}],
            "requested_outputs": ["original_image"], "priority": "important", "status": "proposed", "requires_scope_expansion": False}
        analysis.request_supplement(task["task_id"], request)
        return request

    def test_request_exact_region_provenance_and_cross_request_reuse_remains_queued(self):
        first = self.queue_request()
        second = self.queue_request("second-region-request")
        wrong = self.queue_request("wrong-region-request", box=bbox(1, 1, 10, 7))
        accepted = self.store.connection.execute("SELECT result_json,result_sha256 FROM analyses").fetchone()
        result = self.publish(request_id=first["request_id"])
        self.assertEqual(result["request_state"], "queued")
        before = self.counts()
        with patch.object(publication.image_region, "crop_region", side_effect=AssertionError("cross request must reuse")):
            replay = self.publish(request_id=second["request_id"])
        self.assertEqual(replay["publish_id"], result["publish_id"])
        self.assertTrue(replay["reused"])
        self.assertEqual(self.counts(), before)
        with self.assertRaisesRegex(ContractError, "bbox mismatch"):
            self.publish(request_id=wrong["request_id"])
        with self.assertRaises(ContractError):
            self.publish(request_id="unregistered-request")
        self.assertEqual({r[0] for r in self.store.connection.execute("SELECT state FROM supplement_requests")}, {"queued"})
        self.assertEqual(tuple(self.store.connection.execute("SELECT result_json,result_sha256 FROM analyses").fetchone()), tuple(accepted))
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM read_receipts").fetchone()[0], 0)
        self.assert_original()


if __name__ == "__main__":
    unittest.main()
