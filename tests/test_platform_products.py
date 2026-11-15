"""Offline product-readability checks; all sources are small synthetic fixtures."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from worldanalysis.contracts import ContractError
from worldanalysis import documents
from worldanalysis.platform_products import inspect_product


class ProductInspectionTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix="synthetic-platform-products-")
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.store = SimpleNamespace(root=self.root, max_file_bytes=64 * 1024**2)

    def source(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return path

    def inspect(self, product, name):
        return inspect_product(self.store, product, name)

    def pptx(self, name="lesson.pptx"):
        from pptx import Presentation
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_textbox(0, 0, 1000000, 1000000).text_frame.text = "Synthetic lesson"
        from PIL import Image
        image = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(image, "PNG")
        image.seek(0)
        slide.shapes.add_picture(image, 1000000, 0)
        path = self.root / name
        presentation.save(path)
        return path

    def pdf(self, name="lesson.pdf", encrypted=False):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 12 Tf 20 100 Td (Synthetic lesson) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content)
        if encrypted:
            writer.encrypt("synthetic-test-only")
        path = self.root / name
        writer.write(path)
        return path

    def test_short_manuscript_is_hashed_and_readable_without_coverage_claim(self):
        path = self.source("short.txt", "好。")
        result = self.inspect("manuscript", "short.txt")
        self.assertTrue(result["readable"])
        self.assertEqual(result["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(result["size_bytes"], len("好。".encode()))
        self.assertEqual(result["format"], "txt")
        self.assertEqual(result["relative_path"], "short.txt")
        self.assertFalse(result["metadata"]["transcript_completeness_verified"])
        self.assertFalse(result["metadata"]["source_identity_verified"])

    def test_unicode_bom_and_normal_membership_lesson_are_accepted(self):
        text = "今天讲会员运营。出现生成失败提示时，先检查网络。"
        self.source("member.txt", text.encode("utf-16"))
        result = self.inspect("manuscript", "member.txt")
        self.assertTrue(result["readable"])
        self.assertEqual(result["metrics"]["character_count"], len(text))

    def test_blank_and_platform_message_are_not_material_success(self):
        for content in ("", " \n\t", "AI文稿生成失败，请稍后重试。", "暂无文稿", "正在生成课件，请稍候"):
            with self.subTest(content=content):
                self.source("empty.txt", content)
                self.assertFalse(self.inspect("manuscript", "empty.txt")["readable"])

    def test_valid_subtitles_report_observed_cues_but_no_full_coverage(self):
        self.source("lesson.srt", "1\n00:00:01,000 --> 00:00:02,000\n短字幕。\n")
        result = self.inspect("manuscript", "lesson.srt")
        self.assertTrue(result["readable"])
        self.assertEqual(result["metrics"]["cue_count"], 1)
        self.assertEqual((result["metrics"]["first_cue_ms"], result["metrics"]["last_cue_ms"]), (1000, 2000))
        self.assertFalse(result["metadata"]["transcript_completeness_verified"])
        self.source("lesson.vtt", "WEBVTT\n\n00:01.000 --> 00:02.000\nShort cue.\n")
        self.assertTrue(self.inspect("manuscript", "lesson.vtt")["readable"])

    def test_subtitle_header_empty_cues_or_malformed_times_fail(self):
        cases = [("bad.vtt", "WEBVTT\n"), ("bad.srt", "1\n00:00:01,000 --> 00:00:02,000\n"),
                 ("bad.srt", "1\n00:00:03,000 --> 00:00:02,000\nReversed time.\n")]
        for name, content in cases:
            with self.subTest(content=content):
                self.source(name, content)
                self.assertFalse(self.inspect("manuscript", name)["readable"])

    def test_missing_traversal_absolute_and_symlink_paths_are_rejected(self):
        target = self.source("valid.txt", "内容")
        for name in ("absent.txt", "../outside.txt", str(target), "a/../valid.txt", "C:\\video.txt"):
            with self.subTest(name=name), self.assertRaises(ContractError):
                self.inspect("manuscript", name)
        (self.root / "link.txt").symlink_to(target)
        with self.assertRaises(ContractError):
            self.inspect("manuscript", "link.txt")
        (self.root / "linkdir").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ContractError):
            self.inspect("manuscript", "linkdir/valid.txt")

    def test_renamed_media_and_input_over_budget_are_rejected(self):
        # Deliberately incomplete headers, not actual audio/video files.
        for product, name in (("manuscript", "fake.txt"), ("courseware", "fake.pdf")):
            self.source(name, b"\x00\x00\x00\x18ftypmp42not-a-complete-video")
            with self.assertRaises(ContractError):
                self.inspect(product, name)
        self.source("large.txt", "abcdefgh")
        self.store.max_file_bytes = 4
        with self.assertRaises(ContractError):
            self.inspect("manuscript", "large.txt")

    def test_real_pptx_and_pdf_are_readable_but_not_verified_source_frames(self):
        self.pptx()
        self.pdf()
        for name, key in (("lesson.pptx", "slide_count"), ("lesson.pdf", "page_count")):
            with self.subTest(name=name):
                result = self.inspect("courseware", name)
                self.assertTrue(result["readable"], result)
                self.assertEqual(result["metrics"][key], 1)
                self.assertFalse(result["metadata"]["source_frames_verified"])
                self.assertFalse(result["metadata"]["ocr_executed"])
        self.assertEqual(self.inspect("courseware", "lesson.pptx")["metrics"]["image_count"], 1)

    def test_legacy_bad_encrypted_and_extension_mismatch_are_not_success(self):
        self.source("legacy.ppt", b"\xd0\xcf\x11\xe0synthetic-legacy-header")
        self.source("broken.pdf", b"%PDF-1.7\nnot a valid PDF")
        self.pdf("locked.pdf", encrypted=True)
        actual = self.pdf("real.pdf")
        self.source("wrong.pptx", actual.read_bytes())
        for name in ("legacy.ppt", "broken.pdf", "locked.pdf", "wrong.pptx"):
            with self.subTest(name=name):
                self.assertFalse(self.inspect("courseware", name)["readable"])

    def test_unsafe_zip_members_and_archive_budget_are_rejected(self):
        path = self.pptx()
        with zipfile.ZipFile(path, "a") as package:
            package.writestr("../escaped.txt", b"synthetic")
        with self.assertRaises(ContractError):
            self.inspect("courseware", path.name)
        path = self.pptx("limited.pptx")
        with mock.patch.dict(documents.DEFAULT_LIMITS, max_archive_members=1):
            with self.assertRaises(ContractError):
                self.inspect("courseware", path.name)

    def test_product_inspection_writes_nothing_and_does_not_invoke_external_tools(self):
        self.pptx()
        self.pdf()
        self.source("lesson.md", "# 课程\n只有文字和远程链接 https://example.invalid/。")
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with mock.patch("pathlib.Path.write_bytes", side_effect=AssertionError("unexpected file write")), \
             mock.patch("pathlib.Path.write_text", side_effect=AssertionError("unexpected file write")), \
             mock.patch("pathlib.Path.mkdir", side_effect=AssertionError("unexpected directory write")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("unexpected process")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("unexpected network")):
            for product, name in (("manuscript", "lesson.md"), ("courseware", "lesson.pdf"), ("courseware", "lesson.pptx")):
                result = self.inspect(product, name)
                self.assertTrue(result["readable"], result)
                self.assertEqual(result["metadata"]["files_written"], 0)
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_changed_product_is_not_reported_under_previous_hash(self):
        path = self.source("changing.txt", "first content")
        original = documents._text
        def changing(result, fmt):
            path.write_text("changed content", encoding="utf-8")
            return original(result, fmt)
        with mock.patch.object(documents, "_text", side_effect=changing):
            with self.assertRaises(ContractError):
                self.inspect("manuscript", path.name)


if __name__ == "__main__":
    unittest.main()
