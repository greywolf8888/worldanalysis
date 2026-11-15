"""Actual Store publications and PNG bytes; OS-temp only, no cloud/media jobs."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

from PIL import Image

from worldanalysis.contracts import ContractError, hash_file
from worldanalysis.obsidian import export_vault
from worldanalysis.store import Store


def fixture_extract(source, output, options=None):
    """A real local read with two occurrences of identical image bytes."""
    with Image.new("RGB", (24, 16), (12, 80, 141)) as image:
        image.save(output / "one.png")
    (output / "two.png").write_bytes((output / "one.png").read_bytes())
    with wave.open(str(output / "excluded.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 80)
    files = [
        {"key": "one", "relative_path": "one.png", "kind": "image", "media_type": "image/png"},
        {"key": "two", "relative_path": "two.png", "kind": "image", "media_type": "image/png"},
        {"key": "audio", "relative_path": "excluded.wav", "kind": "clip", "media_type": "audio/wav"},
    ]
    blocks = [{"kind": "paragraph", "origin": "provider_ai_note",
               "locator": {"kind": "paragraph", "precision": "exact", "paragraph_index": 0},
               "text": source.read_text(encoding="utf-8"), "file_keys": [],
               "processing_status": "extracted", "reason": "Synthetic existing note read from actual local text"}]
    for index, key in enumerate(("one", "two")):
        blocks.append({"kind": "image_occurrence", "origin": "native_extract",
                       "locator": {"kind": "time", "precision": "approximate", "start_ms": index * 1000,
                                   "end_ms": index * 1000 + 500, "uncertainty_reason": "Fixture supplied time; not verified against a video"},
                       "file_keys": [key], "processing_status": "extracted",
                       "reason": "Actual local pixels, not proof of full-video coverage"})
    return {"media_type": "text/plain", "provider_id": "synthetic-obsidian-test", "provider_version": "1",
            "files": files, "blocks": blocks, "gaps": [], "metrics": [],
            "enumeration_state": "complete", "processing_status": "partial",
            "metadata": {"synthetic": True, "title": "示例课程·平台笔记", "source_material_id": "material-fixture-1",
                         "source_video_declared": {"provider": "baidu_netdisk", "file_id": "video-fixture-1",
                                                   "duration_ms": 120000, "identity_status": "declared_unverified"}}}


class ObsidianExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="worldanalysis-obsidian-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "workspace"
        self.vault = self.base / "vault"
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.project = self.store.create_project(project_id="obsidian-test", topic="示例课程")
        self.source = self.root / "source-note.txt"
        # The last sentence must survive Store's 16000-character inline preview.
        self.source.write_text("合成平台笔记。\n" + "证据文字" * 4100 + "\n文末必须保留。", encoding="utf-8")
        self.bundle = self.store.ingest(self.project, [self.source], fixture_extract, config_version="obsidian-fixture-v1")
        self.project_root = self.store.project_root(self.project)

    def export(self, **kwargs):
        return export_vault(self.root, self.project, self.vault, **kwargs)

    def snapshot(self, directory):
        return {p.relative_to(directory).as_posix(): (hash_file(p), p.stat().st_mtime_ns)
                for p in directory.rglob("*") if p.is_file()}

    def db_snapshot(self):
        return "\n".join(self.store.connection.iterdump())

    def material_page(self, result):
        return next((Path(result["course_path"]) / "资料").glob("*/资料.md"))

    def assert_local_links_exist(self, page):
        text = page.read_text(encoding="utf-8")
        for target in re.findall(r"(?<!\\)\]\(([^)]+)\)", text):
            with self.subTest(page=page.name, target=target):
                self.assertFalse(target.startswith(("http:", "https:", "file:")))
                self.assertTrue((page.parent / target).resolve().is_file(), target)

    def test_verified_bundle_text_images_origin_and_deduplication(self):
        before_db = self.db_snapshot()
        before_evidence = self.snapshot(self.project_root)
        # The export must never construct a writable Store or use a socket.
        with patch.object(Store, "__init__", side_effect=AssertionError("writable Store opened")), \
             patch("socket.create_connection", side_effect=AssertionError("network used")):
            result = self.export()
        self.assertEqual(result["bundle_id"], self.bundle["bundle_id"])
        self.assertEqual(result["status"], "exported")
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(result["image_occurrence_count"], 2)
        self.assertGreaterEqual(result["omitted_media_artifacts"], 1)
        course = Path(result["course_path"])
        images = list((course / "附件").glob("*"))
        self.assertEqual(len(images), 1)
        with Image.open(images[0]) as image:
            image.load()
            self.assertEqual(image.size, (24, 16))
        self.assertEqual(images[0].stem, hash_file(images[0])[0])
        page = self.material_page(result)
        text = page.read_text(encoding="utf-8")
        self.assertIn("文末必须保留。", text)
        self.assertIn("平台 AI 笔记", text)
        self.assertIn("source_kind: \"local\"", text)
        self.assertIn("declared_source_identity_status: \"declared_unverified\"", text)
        self.assertIn("declared_source_material_id: \"material-fixture-1\"", text)
        self.assertIn("# 示例课程·平台笔记", text)
        self.assertIn("00:00:00", text)
        self.assertIn("00:00:01", text)
        self.assertIn("approximate", text)
        self.assertIn("not_established_by_export", text)
        self.assertIn("not_assessed_by_export", text)
        self.assertEqual(text.count("![本地证据图片]"), 2)
        for candidate in (page, Path(result["overview_path"])):
            self.assert_local_links_exist(candidate)
        self.assertEqual(self.snapshot(self.project_root), before_evidence)
        self.assertEqual(self.db_snapshot(), before_db)
        self.assertEqual(self.store.get_bundle(self.project), self.bundle)

    def test_repeat_export_has_identical_bytes_and_mtimes(self):
        first = self.export()
        snapshot = self.snapshot(self.vault)
        second = self.export()
        self.assertEqual(first["course_path"], second["course_path"])
        self.assertEqual(second["created_files"], [])
        self.assertEqual(second["updated_files"], [])
        self.assertEqual(second["conflicts"], [])
        self.assertEqual(self.snapshot(self.vault), snapshot)

    def test_user_notes_and_edited_generated_pages_preserved_candidates_resolve(self):
        result = self.export()
        page = self.material_page(result)
        personal = page.with_name("我的笔记.md")
        personal.write_text("我的独立理解，不得覆盖。", encoding="utf-8")
        edited = page.read_text(encoding="utf-8") + "\n用户补充，不能静默覆盖。\n"
        page.write_text(edited, encoding="utf-8")
        overview = Path(result["overview_path"])
        overview_edited = overview.read_text(encoding="utf-8") + "\n用户总览补充。\n"
        overview.write_text(overview_edited, encoding="utf-8")
        second = self.export()
        self.assertEqual(second["status"], "partial")
        self.assertEqual(len(second["conflicts"]), 2)
        self.assertEqual(page.read_text(encoding="utf-8"), edited)
        self.assertEqual(overview.read_text(encoding="utf-8"), overview_edited)
        self.assertEqual(personal.read_text(encoding="utf-8"), "我的独立理解，不得覆盖。")
        for conflict in second["conflicts"]:
            candidate = Path(second["course_path"]) / conflict["candidate"]
            self.assertEqual(candidate.parent, (Path(second["course_path"]) / conflict["path"]).parent)
            self.assert_local_links_exist(candidate)
        snapshot = self.snapshot(self.vault)
        third = self.export()
        self.assertEqual(third["conflicts"], second["conflicts"])
        self.assertEqual(self.snapshot(self.vault), snapshot)

    def test_edited_image_preserved_all_occurrences_use_decodable_candidate(self):
        first = self.export()
        image = next((Path(first["course_path"]) / "附件").glob("*.png"))
        image.write_bytes(b"user-modified-image-content")
        result = self.export()
        self.assertEqual(image.read_bytes(), b"user-modified-image-content")
        self.assertEqual(len(result["conflicts"]), 1)
        candidate = Path(result["course_path"]) / result["conflicts"][0]["candidate"]
        with Image.open(candidate) as decoded:
            decoded.load()
            self.assertEqual(decoded.size, (24, 16))
        page = self.material_page(result)
        text = page.read_text(encoding="utf-8")
        self.assertEqual(text.count("../../" + result["conflicts"][0]["candidate"]), 2)
        self.assert_local_links_exist(page)

    def test_credentials_remote_embeds_and_media_are_not_copied(self):
        secret = "fixture-only-credential-123456789"
        self.source.write_text("普通文字\nCookie: " + secret + "\n%%这段文字必须可见%%\n![远端](https://example.invalid/image.png)\n"
                               '<img src="https://example.invalid/other.png">', encoding="utf-8")
        self.store.ingest(self.project, [self.source], fixture_extract, config_version="obsidian-fixture-v1")
        result = self.export()
        all_bytes = b"\n".join(p.read_bytes() for p in self.vault.rglob("*") if p.is_file())
        self.assertNotIn(secret.encode(), all_bytes)
        self.assertNotIn(b'<img src=', all_bytes)
        self.assertNotIn("![远端](https://".encode(), all_bytes)
        self.assertIn(r"\%\%这段文字必须可见\%\%".encode(), all_bytes)
        self.assertGreater(result["omitted_restricted_artifacts"], 0)
        self.assertTrue(all(p.suffix in {".md", ".json", ".png"} for p in self.vault.rglob("*") if p.is_file()))

    def test_new_bundle_updates_view_but_explicit_old_bundle_can_still_be_exported(self):
        first = self.export()
        old_id = self.bundle["bundle_id"]
        page = self.material_page(first)
        personal = page.with_name("我的笔记.md")
        personal.write_text("我的笔记保留。", encoding="utf-8")
        self.source.write_text("第二版的新内容。", encoding="utf-8")
        new = self.store.ingest(self.project, [self.source], fixture_extract, config_version="obsidian-fixture-v1")
        result = self.export()
        self.assertEqual(result["bundle_id"], new["bundle_id"])
        self.assertIn("第二版的新内容。", page.read_text(encoding="utf-8"))
        self.assertEqual(personal.read_text(encoding="utf-8"), "我的笔记保留。")
        self.assertEqual(result["conflicts"], [])
        old_result = self.export(bundle_id=old_id)
        self.assertEqual(old_result["bundle_id"], old_id)
        self.assertIn("文末必须保留。", page.read_text(encoding="utf-8"))
        self.assertEqual(personal.read_text(encoding="utf-8"), "我的笔记保留。")

    def test_invalid_publication_fails_before_vault_creation(self):
        artifact = next(a for a in self.bundle["artifacts"] if a["kind"] == "image")
        path = self.project_root / artifact["relative_path"]
        path.chmod(0o600)
        path.write_bytes(b"damaged source evidence")
        with self.assertRaises(ContractError):
            self.export()
        self.assertFalse(self.vault.exists())

    def test_unsafe_vault_and_symlink_destinations_are_rejected(self):
        for unsafe in (self.root, self.root / "new-vault", self.base, Path(__file__).resolve().parents[1] / "unsafe-vault"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ContractError):
                export_vault(self.root, self.project, unsafe)
        target = self.base / "outside"
        target.mkdir()
        self.vault.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ContractError):
            self.export()
        self.assertEqual(list(target.iterdir()), [])
        self.vault.unlink()
        result = self.export()
        page = self.material_page(result)
        page.unlink()
        outside_page = target / "keep.md"
        outside_page.write_text("keep", encoding="utf-8")
        page.symlink_to(outside_page)
        with self.assertRaises(ContractError):
            self.export()
        self.assertEqual(outside_page.read_text(encoding="utf-8"), "keep")

    def test_module_cli_reads_published_store_and_returns_json(self):
        completed = subprocess.run([sys.executable, "-m", "worldanalysis.obsidian", "--root", str(self.root),
                                    "--project", self.project, "--vault", str(self.vault)],
                                   cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["bundle_id"], self.bundle["bundle_id"])
        self.assertTrue(Path(result["overview_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
