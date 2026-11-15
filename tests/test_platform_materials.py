"""Synthetic local package -> actual Store publication; no online claims."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

from PIL import Image

from worldanalysis.contracts import ContractError, validate_bundle
from worldanalysis.platform_materials import _import_lock, _prepare_package, extract, import_materials
from worldanalysis.policy import GateError
from worldanalysis.store import Store


class PlatformMaterialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="worldanalysis-materials-")
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.store.close())
        self.project = self.store.create_project(topic="Synthetic local package")
        self.package = self.root / "package"
        self.package.mkdir()
        self.manifest = self.package / "manifest.json"
        self.value = {"schema_version": "1.0", "material_id": "lesson-01", "title": "第一课（构造测试）",
                      "source": {"provider": "baidu", "file_id": "123456", "account_ref": "account-local-alias",
                                 "path": "/apps/教育分析学习/测试/课一.mp4", "duration_ms": 120000},
                      "transcript": {"path": "lesson.srt", "kind": "timed_subtitle", "origin": "asr", "source_method": "platform_export_declared"},
                      "note": {"path": "note.md"},
                      "images": [{"path": "frame.png", "source_kind": "platform_note_image", "timestamp_ms": 1000}]}
        (self.package / "lesson.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\n这是字幕原文。\n\n2\n00:00:04,000 --> 00:00:06,000\n第二句。\n", encoding="utf-8")
        (self.package / "note.md").write_text("# 平台笔记\n这是摘要，不能替代全文。\n![步骤图](frame.png)\n", encoding="utf-8")
        self.image_bytes = self.png()
        (self.package / "frame.png").write_bytes(self.image_bytes)
        self.write_manifest()

    @staticmethod
    def png(color="blue"):
        stream = io.BytesIO()
        Image.new("RGB", (24, 16), color).save(stream, "PNG")
        return stream.getvalue()

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.value, ensure_ascii=False), encoding="utf-8")

    def run_import(self, **kwargs):
        self.write_manifest()
        return import_materials(self.store, self.project, self.manifest, **kwargs)

    def metadata(self, bundle):
        item = next(a for a in bundle["artifacts"] if "/metadata/" in a["relative_path"])
        return json.loads((self.store.project_root(self.project) / item["relative_path"]).read_text(encoding="utf-8"))

    def test_normal_subtitle_note_image_publish_readable_real_bundle(self):
        bundle = self.run_import()
        self.assertEqual(bundle["publication"]["state"], "published")
        self.assertEqual(bundle["coverage"]["claimed_status"], "partial")
        validate_bundle(bundle, self.store.project_root(self.project))
        images = [a for a in bundle["artifacts"] if a["kind"] == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual((self.store.project_root(self.project) / images[0]["relative_path"]).read_bytes(), self.image_bytes)
        self.assertTrue(any(b.get("text") == "这是字幕原文。" for b in bundle["blocks"]))
        self.assertTrue(any("不能替代全文" in b.get("text", "") for b in bundle["blocks"]))
        self.assertFalse(any(p.suffix in {".mp4", ".mp3", ".wav"} for p in self.root.rglob("*")))

    def test_repeated_identical_material_and_json_formatting_do_not_publish(self):
        first = self.run_import()
        self.manifest.write_text(json.dumps(self.value, indent=4, sort_keys=True), encoding="utf-8")
        second = import_materials(self.store, self.project, self.manifest)
        self.assertEqual(first["bundle_id"], second["bundle_id"])
        self.assertEqual(len(self.store.list_bundles(self.project)), 1)

    def test_missing_image_repair_creates_revision_of_same_asset(self):
        (self.package / "frame.png").unlink()
        first = self.run_import()
        first_meta = self.metadata(first)
        record = next(a for a in first_meta["attachment_inventory"] if a["role"] == "image")
        self.assertEqual((record["state"], record["sha256"]), ("missing", None))
        self.assertFalse(any(a["kind"] == "image" for a in first["artifacts"]))
        (self.package / "frame.png").write_bytes(self.image_bytes)
        repaired = self.run_import()
        self.assertEqual(first["assets"][0]["asset_id"], repaired["assets"][0]["asset_id"])
        self.assertNotEqual(first["assets"][0]["asset_revision"], repaired["assets"][0]["asset_revision"])
        self.assertNotEqual(first["bundle_id"], repaired["bundle_id"])
        self.assertTrue(any(a["kind"] == "image" for a in repaired["artifacts"]))
        self.assertEqual(repaired["coverage"]["claimed_status"], "partial")

    def test_changed_subtitle_without_changed_manifest_changes_revision(self):
        first = self.run_import()
        path = self.package / "lesson.srt"
        path.write_text(path.read_text(encoding="utf-8").replace("第二句", "已经校对的第二句"), encoding="utf-8")
        second = self.run_import()
        self.assertEqual(first["assets"][0]["asset_id"], second["assets"][0]["asset_id"])
        self.assertNotEqual(first["assets"][0]["asset_revision"], second["assets"][0]["asset_revision"])
        self.assertTrue(any("已经校对" in b.get("text", "") for b in second["blocks"]))

    def test_note_only_is_not_a_complete_transcript(self):
        self.value.pop("transcript")
        self.value.pop("images")
        (self.package / "note.md").write_text("五个重点，一份平台摘要。", encoding="utf-8")
        bundle = self.run_import()
        self.assertEqual(bundle["coverage"]["claimed_status"], "partial")
        self.assertEqual([b["origin"] for b in bundle["blocks"]], ["provider_ai_note"])
        self.assertFalse(self.metadata(bundle)["transcript_completeness_verified"])
        self.assertTrue(any(g["kind"] == "temporal_gap" for g in bundle["gaps"]))

    def test_declared_source_never_becomes_verified_baidu_binding(self):
        bundle = self.run_import()
        self.assertEqual(bundle["assets"][0]["source_binding"]["kind"], "local")
        self.assertNotIn("verified_path", bundle["assets"][0]["source_binding"])
        meta = self.metadata(bundle)
        self.assertEqual(meta["source_video_declared"]["identity_status"], "declared_unverified")
        self.assertFalse(meta["source_identity_verified"])
        self.assertFalse(meta["online_acquisition_performed"])
        self.assertTrue(any(g["kind"] == "uncertain_origin" for g in bundle["gaps"]))

    def test_origins_occurrences_nullable_times_and_image_dedup(self):
        self.value["images"].append({"path": "frame.png", "source_kind": "claimed_video_frame"})
        bundle = self.run_import()
        self.assertTrue(any(b["origin"] == "asr" for b in bundle["blocks"]))
        self.assertTrue(any(b["origin"] == "provider_ai_note" for b in bundle["blocks"]))
        occurrences = [b for b in bundle["blocks"] if b["kind"] == "image_occurrence"]
        self.assertEqual(len(occurrences), 2)
        self.assertEqual(occurrences[0]["locator"]["start_ms"], 1000)
        self.assertEqual(occurrences[1]["locator"]["precision"], "unknown")
        self.assertEqual(len([a for a in bundle["artifacts"] if a["kind"] == "image"]), 1)
        self.assertFalse(any(a["kind"] == "keyframe" for a in bundle["artifacts"]))
        self.assertEqual(self.metadata(bundle)["image_source_declarations"][1]["timestamp_ms"], None)

    def test_bounds_paths_symlinks_scope_and_disguised_media_rejected(self):
        original = copy.deepcopy(self.value)
        for bad in ("../outside.srt", "/tmp/outside.srt", "C:\\outside.srt"):
            with self.subTest(path=bad):
                self.value = copy.deepcopy(original)
                self.value["transcript"]["path"] = bad
                with self.assertRaises(ContractError):
                    self.run_import()
        self.value = copy.deepcopy(original)
        target = self.root / "another.srt"
        target.write_text("some text", encoding="utf-8")
        link = self.package / "link.srt"
        link.symlink_to(target)
        self.value["transcript"]["path"] = "link.srt"
        with self.assertRaises(ContractError):
            self.run_import()
        self.value = copy.deepcopy(original)
        self.value["source"]["path"] = "/outside/lesson.mp4"
        with self.assertRaises(GateError):
            self.run_import()
        self.value = copy.deepcopy(original)
        for filename in ("lesson.srt", "frame.png"):
            old = (self.package / filename).read_bytes()
            (self.package / filename).write_bytes(b"\x00\x00\x00\x20ftypisom" + b"0" * 100)
            with self.subTest(disguised=filename), self.assertRaises(ContractError):
                self.run_import()
            (self.package / filename).write_bytes(old)
        with self.assertRaises(ContractError):
            self.run_import(limits={"max_image_bytes": 10})
        self.assertFalse(self.store.list_bundles(self.project))

    def test_broken_image_is_partial_and_can_be_replaced(self):
        bad = b"broken PNG bytes"
        (self.package / "frame.png").write_bytes(bad)
        first = self.run_import()
        self.assertTrue(any(g["kind"] == "unreadable" for g in first["gaps"]))
        record = next(a for a in self.metadata(first)["attachment_inventory"] if a["role"] == "image")
        self.assertEqual(record["state"], "unreadable")
        self.assertEqual((record["sha256"], record["size_bytes"]), (hashlib.sha256(bad).hexdigest(), len(bad)))
        self.assertIsNone(record["snapshot_path"])
        self.assertFalse(any(a["kind"] == "image" for a in first["artifacts"]))
        self.assertFalse(any(path.read_bytes() == bad for path in (self.root / "采集输入").rglob("*") if path.is_file()))
        (self.package / "frame.png").write_bytes(self.image_bytes)
        repaired = self.run_import()
        self.assertNotEqual(first["bundle_id"], repaired["bundle_id"])
        self.assertFalse(any(g["kind"] == "unreadable" for g in repaired["gaps"]))

    def test_snapshot_bytes_rechecked_at_extraction(self):
        with _import_lock(self.store, self.project) as acquired:
            self.assertTrue(acquired)
            snapshot = _prepare_package(self.store, self.project, self.manifest)
            value = json.loads(snapshot.read_text(encoding="utf-8"))
            target = self.root / value["attachments"][0]["snapshot_path"]
            target.write_bytes(b"changed after preparation")
            output = self.root / "raw-output"
            output.mkdir()
            with self.assertRaisesRegex(ContractError, "snapshot changed"):
                extract(snapshot, output, options={"root": str(self.root)})

    def test_interprocess_lock_covers_prepare_until_publish_and_blocks_identity_race(self):
        # A pauses exactly after preparation but before Store reads input.json.
        # B has the same material_id with a different declared source identity.
        script = """
import json, sys
from worldanalysis.platform_materials import import_materials
from worldanalysis.store import Store
class WaitingStore(Store):
    def ingest(self, *args, **kwargs):
        print('prepared', flush=True)
        if sys.stdin.readline() != 'continue\\n':
            raise RuntimeError('test handoff missing')
        return super().ingest(*args, **kwargs)
with WaitingStore(sys.argv[1]) as store:
    result = import_materials(store, sys.argv[2], sys.argv[3])
    print(json.dumps({'bundle_id': result['bundle_id']}), flush=True)
"""
        other = copy.deepcopy(self.value)
        other["source"]["file_id"] = "different-file"
        manifest_b = self.package / "manifest-b.json"
        manifest_b.write_text(json.dumps(other), encoding="utf-8")
        process = subprocess.Popen([sys.executable, "-c", script, str(self.root), self.project, str(self.manifest)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "prepared")
            inputs = list((self.root / "采集输入").rglob("input.json"))
            self.assertEqual(len(inputs), 1)
            prepared = inputs[0].read_bytes()
            self.assertEqual(json.loads(prepared)["source"]["file_id"], "123456")
            blocked = import_materials(self.store, self.project, manifest_b)
            self.assertEqual(blocked["status"], "blocked")
            self.assertIn("retry", blocked["reason"])
            self.assertEqual(inputs[0].read_bytes(), prepared)
            self.assertFalse(self.store.list_bundles(self.project))
            output, error = process.communicate("continue\n", timeout=20)
            self.assertEqual(process.returncode, 0, error)
            bundle = self.store.get_bundle(self.project, json.loads(output)["bundle_id"])
            self.assertEqual(self.metadata(bundle)["source_video_declared"]["file_id"], "123456")
            self.assertFalse(list((self.root / "采集输入").rglob("import.lock")))
            with self.assertRaisesRegex(ContractError, "another declared source"):
                import_materials(self.store, self.project, manifest_b)
            self.assertEqual(inputs[0].read_bytes(), prepared)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()

    def test_credential_remote_undeclared_and_html_embeds_are_rejected(self):
        original = (self.package / "note.md").read_text(encoding="utf-8")
        for content in ("![remote](https://example.com/a.png)", "![local](undeclared.png)", "<img src='https://example.com/a.png'>", "![escape](../secret.png)", "BDUSS=not-a-real-secret"):
            with self.subTest(content=content):
                (self.package / "note.md").write_text(content, encoding="utf-8")
                with self.assertRaises(ContractError):
                    self.run_import()
        (self.package / "note.md").write_text(original, encoding="utf-8")
        self.value["source"]["cookie"] = "synthetic-secret"
        with self.assertRaises(ContractError):
            self.run_import()

    def test_material_identity_cannot_silently_change_source(self):
        self.run_import()
        self.value["source"]["file_id"] = "different-file"
        with self.assertRaisesRegex(ContractError, "another declared source"):
            self.run_import()

    def test_pause_prevents_reads_and_publication(self):
        self.store.control("collection", "pause")
        result = import_materials(self.store, self.project, self.root / "nonexistent.json")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse((self.root / "采集输入").exists())
        self.assertFalse(self.store.list_bundles(self.project))
        self.store.control("collection", "resume")
        self.assertIn("bundle_id", self.run_import())


if __name__ == "__main__":
    unittest.main()
