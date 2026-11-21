"""P1 document contract, safety and real-inventory tests.

Every generated source/output lives in tests/document_fixtures/.runs and is
explicitly synthetic. Real files are discovered read-only in authorized local
wangzjk.com inventory; no old files, JSON ledger or source Office files change.
Run: python -m unittest discover -s tests -p test_documents.py -v
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

from worldanalysis.documents import MAX_BLOCK_TEXT, extract, probe

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "document_fixtures"
RUNS = FIXTURES / ".runs"
SCHEMA = ROOT / "worldanalysis_agent_devpack_20261110" / "contracts" / "bundle.schema.json"
PYTHON_SCHEMA_DEFS = json.loads(SCHEMA.read_text(encoding="utf-8"))["$defs"]
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _rewrite_package(path: Path, replacements: dict[str, bytes], additions: dict[str, bytes] | None = None):
    """Synthetic test-source construction only; no production file is passed here."""
    with zipfile.ZipFile(path) as source:
        members = [(entry, source.read(entry)) for entry in source.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dest:
        for entry, data in members:
            dest.writestr(entry, replacements.pop(entry.filename, data))
        for name, data in {**replacements, **(additions or {})}.items():
            dest.writestr(name, data)


def _png() -> bytes:
    from PIL import Image
    stream = io.BytesIO()
    # Non-uniform image to detect accidental placeholder/re-encoding substitution.
    image = Image.new("RGB", (24, 16), (251, 13, 77))
    for x in range(8):
        image.putpixel((x, x), (0, 231, 19))
    image.save(stream, format="PNG")
    return stream.getvalue()


def _pdf(path: Path, *, mixed_page=False):
    """Three-page synthetic PDF: digital text, repeated raster image, blank page."""
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject, NumberObject
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    font_ref = writer._add_object(font)
    first = writer.add_blank_page(width=300, height=200)
    first[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
    text_stream = DecodedStreamObject()
    text_stream.set_data(b"BT /F1 12 Tf 20 120 Td (synthetic digital page) Tj ET")
    first[NameObject("/Contents")] = writer._add_object(text_stream)
    scan = writer.add_blank_page(width=300, height=200)
    image = DecodedStreamObject()
    image.set_data(bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0]))
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(2), NameObject("/Height"): NumberObject(2),
                  NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
    scan[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): writer._add_object(image)})})
    content = DecodedStreamObject()
    commands = b"q 60 0 0 60 20 20 cm /Im0 Do Q q 60 0 0 60 120 20 cm /Im0 Do Q"
    if mixed_page:
        scan["/Resources"][NameObject("/Font")] = DictionaryObject({NameObject("/F1"): font_ref})
        commands += b" BT /F1 12 Tf 20 120 Td (synthetic text over a screenshot) Tj ET"
    content.set_data(commands)
    scan[NameObject("/Contents")] = writer._add_object(content)
    writer.add_blank_page(width=300, height=200)
    writer.add_metadata({"/Title": "synthetic mixed page fixture"})
    writer.write(path)


class DocumentTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RUNS.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="synthetic-documents-", dir=RUNS))
        self.addCleanup(lambda: shutil.rmtree(self.work))
        self.seq = 0

    def source(self, name: str, value: str | bytes) -> Path:
        path = self.work / name
        path.write_bytes(value.encode("utf-8") if isinstance(value, str) else value)
        return path

    def run_extract(self, path: Path, options: dict | None = None) -> tuple[dict, Path]:
        self.seq += 1
        output = self.work / ("out-" + str(self.seq))
        output.mkdir()
        result = extract(path, output, options={"synthetic": True, **(options or {})})
        self.assert_raw_contract(result, output)
        return result, output

    def assert_raw_contract(self, result: dict, output: Path):
        self.assertEqual(set(result), {"media_type", "provider_id", "provider_version", "blocks", "files", "gaps", "metrics", "enumeration_state", "processing_status", "metadata"})
        self.assertIn(result["enumeration_state"], ["complete", "partial", "unknown"])
        self.assertIn(result["processing_status"], ["discovered", "partial", "verified_complete", "blocked", "failed", "deferred", "excluded"])
        key_map = {f["key"]: f for f in result["files"]}
        self.assertEqual(len(key_map), len(result["files"]))
        self.assertEqual(set(x.name for x in output.iterdir()), {x["relative_path"] for x in result["files"]})
        for f in result["files"]:
            self.assertEqual(set(f), {"key", "relative_path", "kind", "media_type"})
            self.assertIn(f["kind"], PYTHON_SCHEMA_DEFS["artifact"]["properties"]["kind"]["enum"])
            target = output / f["relative_path"]
            self.assertTrue(target.is_file())
            self.assertTrue(target.resolve().is_relative_to(output.resolve()))
            self.assertNotIn("\\", f["relative_path"])
            self.assertNotIn("..", Path(f["relative_path"]).parts)
        for block in result["blocks"]:
            self.assertEqual(set(block) - {"text"}, {"kind", "origin", "locator", "file_keys", "processing_status", "reason"})
            self.assertIn(block["kind"], PYTHON_SCHEMA_DEFS["block"]["properties"]["kind"]["enum"])
            self.assertIn(block["origin"], PYTHON_SCHEMA_DEFS["block"]["properties"]["origin"]["enum"])
            self.assertIn(block["processing_status"], PYTHON_SCHEMA_DEFS["block"]["properties"]["processing"]["properties"]["status"]["enum"])
            self.assertLessEqual(len(block.get("text", "")), MAX_BLOCK_TEXT)
            for key in block["file_keys"]:
                self.assertIn(key, key_map)
            if block["processing_status"] == "extracted":
                self.assertTrue(block["file_keys"], block)
            loc = block["locator"]
            self.assertLessEqual(set(loc), set(PYTHON_SCHEMA_DEFS["locator"]["properties"]))
            self.assertIn(loc["kind"], PYTHON_SCHEMA_DEFS["locator"]["properties"]["kind"]["enum"])
            self.assertIn(loc["precision"], PYTHON_SCHEMA_DEFS["locator"]["properties"]["precision"]["enum"])
            if "char_start" in loc and "char_end" in loc:
                self.assertGreaterEqual(loc["char_end"], loc["char_start"])
        for gap in result["gaps"]:
            self.assertEqual(set(gap), {"kind", "severity", "reason", "block_indexes"})
            self.assertIn(gap["kind"], PYTHON_SCHEMA_DEFS["gap"]["properties"]["kind"]["enum"])
            self.assertIn(gap["severity"], PYTHON_SCHEMA_DEFS["gap"]["properties"]["severity"]["enum"])
            for index in gap["block_indexes"]:
                self.assertGreaterEqual(index, 0)
                self.assertLess(index, len(result["blocks"]))
        for metric in result["metrics"]:
            self.assertEqual(set(metric), set(PYTHON_SCHEMA_DEFS["coverageMetric"]["required"]))
            self.assertLessEqual(metric["processed"], metric["observed"])
            self.assertGreaterEqual(metric["excluded"], 0)
            field = {"page": "page_number", "slide": "slide_number", "sheet": "sheet_name"}.get(metric["unit"])
            if field:
                self.assertEqual(len({r[field] for r in metric["ranges"]}), metric["processed"], metric)
        # Validate exact locator and coverageMetric fragments when host jsonschema is available.
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            pass
        else:
            for definition, objects in (("coverageMetric", result["metrics"]), ("locator", [b["locator"] for b in result["blocks"]])):
                fragment = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$defs": PYTHON_SCHEMA_DEFS, "$ref": "#/$defs/" + definition}
                validator = Draft202012Validator(fragment)
                for item in objects:
                    self.assertEqual(list(validator.iter_errors(item)), [], item)
        if any(b["kind"] == "image_occurrence" for b in result["blocks"]):
            self.assertNotEqual(result["processing_status"], "verified_complete", "Uninterpreted images never imply whole-file completion")
        self.assertFalse(result["metadata"]["content_executed"])
        self.assertFalse(result["metadata"]["external_content_loaded"])
        json.dumps(result, ensure_ascii=False)  # Result must be serializable.

    @staticmethod
    def read_file(result, output, key):
        record = next(f for f in result["files"] if f["key"] == key)
        return (output / record["relative_path"]).read_bytes()


class TextTests(DocumentTestCase):
    def test_short_txt_no_threshold_and_exact_newlines(self):
        source = self.source("short.txt", "密码：0000\r\n前提：先核实授权。\n")
        result, output = self.run_extract(source)
        self.assertTrue(probe(source)["supported"])
        self.assertEqual(result["processing_status"], "verified_complete")
        self.assertEqual("".join(b.get("text", "") for b in result["blocks"]), source.read_bytes().decode("utf-8"))
        self.assertEqual(self.read_file(result, output, result["blocks"][0]["file_keys"][0]), source.read_bytes())

    def test_empty_txt_is_not_discarded(self):
        result, output = self.run_extract(self.source("empty.txt", ""))
        self.assertEqual(len(result["blocks"]), 1)
        self.assertEqual(result["blocks"][0]["text"], "")
        self.assertEqual(result["processing_status"], "verified_complete")

    def test_chunking_preserves_entire_unicode_string(self):
        text = "头" + "a中😀\r\n" * 13000 + "尾"
        result, output = self.run_extract(self.source("long.txt", text))
        blocks = result["blocks"]
        self.assertGreater(len(blocks), 3)
        self.assertEqual("".join(b["text"] for b in blocks), text)
        self.assertEqual([b["locator"]["char_start"] for b in blocks], list(range(0, len(text), 16000)))
        self.assertEqual(blocks[-1]["locator"]["char_end"], len(text))
        self.assertEqual(len(result["files"]), 1)

    def test_encodings_strict_no_replacement(self):
        for encoding in ("utf-16", "utf-32", "utf-8-sig", "gb18030"):
            with self.subTest(encoding=encoding):
                text = "中文参数：五。\r\n"
                result, _ = self.run_extract(self.source(encoding + ".txt", text.encode(encoding)))
                self.assertEqual(result["blocks"][0]["text"], text)
                self.assertIn(result["metadata"]["encoding"], (encoding, "utf-8"))

    def test_binary_null_not_decoded_as_text(self):
        result, _ = self.run_extract(self.source("binary.txt", b"a\x00b\x00"))
        self.assertEqual(result["processing_status"], "blocked")
        self.assertFalse(any(b["kind"] == "paragraph" for b in result["blocks"]))

    def test_json_preserves_all_keys_numbers_links_and_text(self):
        text = '{"code":"001", "n":123456789012345678901234567890,"link":"https://example.invalid/a#x", "note":"中"}\n'
        result, output = self.run_extract(self.source("data.json", text))
        self.assertEqual(result["blocks"][0]["text"], text)
        self.assertEqual(result["processing_status"], "verified_complete")
        structured = next(f for f in result["files"] if f["kind"] == "structured_data")
        self.assertEqual(json.loads((output / structured["relative_path"]).read_text(encoding="utf-8"))["code"], "001")

    def test_json_nan_is_not_claimed_valid_json(self):
        result, _ = self.run_extract(self.source("nan.json", '{"value": NaN}'))
        self.assertEqual(result["processing_status"], "partial")
        self.assertTrue(any(g["kind"] == "parse_failed" for g in result["gaps"]))

    def test_invalid_json_keeps_full_native_text(self):
        text = '{"bad": invalid}\n'
        result, _ = self.run_extract(self.source("invalid.json", text))
        self.assertEqual(result["blocks"][0]["text"], text)
        self.assertEqual(result["processing_status"], "partial")
        self.assertTrue(any(g["kind"] == "parse_failed" for g in result["gaps"]))

    def test_markdown_image_not_fetched(self):
        result, _ = self.run_extract(self.source("note.md", "see ![actual](https://example.invalid/photo.png)\n"))
        self.assertEqual(len([b for b in result["blocks"] if b["kind"] == "image_occurrence"]), 1)
        self.assertTrue(any(g["kind"] == "missing_visual" for g in result["gaps"]))

    def test_csv_multiline_empty_formula_like_values_are_not_executed(self):
        text = 'id,note,formula\r\n001,"中\n文",=1+2\r\n002,"say ""hi""",\r\n'
        result, output = self.run_extract(self.source("table.csv", text))
        rows = [json.loads(b["text"]) for b in result["blocks"] if b["kind"] == "table"]
        self.assertEqual(rows[1], ["001", "中\n文", "=1+2"])
        self.assertEqual(rows[2], ["002", 'say "hi"', ""])
        self.assertEqual(result["processing_status"], "verified_complete")

    def test_long_csv_cell_is_chunked_not_truncated(self):
        value = "中" * 35000
        result, _ = self.run_extract(self.source("large.csv", '"' + value + '"\n'))
        rows = [b["text"] for b in result["blocks"] if b["kind"] == "table"]
        self.assertEqual(json.loads("".join(rows)), [value])

    def test_srt_times_global_ms_and_text(self):
        result, _ = self.run_extract(self.source("subs.srt", "1\r\n00:00:01,200 --> 00:00:03,400\r\n第一句\r\n第二行\r\n\r\n2\r\n00:01:00,000 --> 00:01:01,010\r\n末句\r\n"))
        cues = [b for b in result["blocks"] if b["locator"]["kind"] == "time"]
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["locator"]["start_ms"], 1200)
        self.assertEqual(cues[1]["locator"]["end_ms"], 61010)
        self.assertIn("第一句", cues[0]["text"])
        self.assertEqual(result["processing_status"], "verified_complete")

    def test_vtt_settings_header_and_note_not_lost(self):
        text = "WEBVTT\nKind: captions\n\nNOTE synthetic\nretained directive\n\ncue-id\n00:01.000 --> 00:02.300 align:start position:20%\n字幕\n"
        result, output = self.run_extract(self.source("subs.vtt", text))
        self.assertEqual(result["metadata"]["subtitle_cues"][0]["settings"], " align:start position:20%")
        self.assertEqual(result["metadata"]["subtitle_cues"][0]["start_ms"], 1000)
        self.assertEqual(self.read_file(result, output, result["blocks"][0]["file_keys"][0]).decode("utf-8"), text)
        self.assertEqual(result["processing_status"], "verified_complete")

    def test_invalid_subtitle_groups_are_visible_gaps(self):
        for text in ("broken text\n", "1\n00:00:03,000 --> 00:00:01,000\nbad\n", "1\n00:99:00,000 --> 00:99:01,000\nbad\n"):
            result, _ = self.run_extract(self.source("bad.srt", text))
            self.assertEqual(result["processing_status"], "partial")
            self.assertTrue(result["gaps"])

    def test_markdown_guide_with_srt_extension_retains_prose_and_examples(self):
        text = "# 字幕翻译指南\n\n## 保留时码\n\n保持编号和原时间轴。\n\n"
        text += "1\n00:00:01,000 --> 00:00:03,000\n翻译示例\n\n## 检查输出\n\n只提交译文。\n"
        result, output = self.run_extract(self.source("指南.srt", text))
        self.assertEqual(result["processing_status"], "verified_complete")
        self.assertEqual(result["metadata"]["declared_text_format"], "srt")
        self.assertEqual(result["metadata"]["text_format"], "md")
        block, = result["blocks"]
        self.assertEqual(block["locator"]["kind"], "asset")
        self.assertEqual(block["text"], text)
        self.assertEqual(self.read_file(result, output, block["file_keys"][0]).decode(), text)
        self.assertNotIn("subtitle_cues", result["metadata"])

    def test_missing_figure_reference_not_claimed_complete(self):
        result, _ = self.run_extract(self.source("missing.txt", "设置如下图所示。"))
        self.assertTrue(any(g["kind"] == "missing_visual" for g in result["gaps"]))
        self.assertEqual(result["processing_status"], "partial")


class ImageAndSafetyTests(DocumentTestCase):
    def test_image_bytes_real_verified_and_ocr_not_fabricated(self):
        image = _png()
        result, output = self.run_extract(self.source("real-image.png", image))
        occurrence = result["blocks"][0]
        self.assertEqual(occurrence["kind"], "image_occurrence")
        self.assertEqual(self.read_file(result, output, occurrence["file_keys"][0]), image)
        self.assertFalse(any(b["origin"] == "ocr" for b in result["blocks"]))
        self.assertEqual(result["processing_status"], "partial")
        self.assertEqual(result["metadata"]["image_inventory"][0]["width"], 24)

    def test_image_header_does_not_mean_corrupt_image_is_valid(self):
        image = _png()[:-20]
        result, _ = self.run_extract(self.source("truncated.png", image))
        self.assertNotEqual(result["processing_status"], "verified_complete")
        self.assertTrue(result["gaps"])

    def test_jpeg_complete_decode_required_not_just_header(self):
        from PIL import Image
        stream = io.BytesIO()
        Image.new("RGB", (64, 64), (17, 91, 233)).save(stream, "JPEG")
        data = stream.getvalue()[:-12]
        result, _ = self.run_extract(self.source("synthetic-truncated.jpg", data))
        self.assertTrue(any(g["kind"] == "unreadable" for g in result["gaps"]))
        self.assertFalse(any(f["kind"] == "image" for f in result["files"]))

    def test_multiframe_image_validation_and_resource_limit(self):
        from PIL import Image
        stream = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(stream, "TIFF", save_all=True, append_images=[Image.new("RGB", (16, 16), "blue")])
        source = self.source("synthetic-two-frame.tiff", stream.getvalue())
        result, output = self.run_extract(source)
        self.assertEqual(result["metadata"]["image_inventory"][0]["frame_count"], 2)
        self.assertEqual(self.read_file(result, output, result["blocks"][0]["file_keys"][0]), source.read_bytes())
        limited, _ = self.run_extract(source, {"max_image_frames": 1})
        self.assertEqual(limited["processing_status"], "blocked")
        self.assertTrue(any(g["kind"] == "resource_block" for g in limited["gaps"]))

    def test_image_magic_not_extension(self):
        result, _ = self.run_extract(self.source("misnamed.bin", _png()))
        self.assertEqual(result["media_type"], "image/png")
        fake, _ = self.run_extract(self.source("fake.png", "not a picture"))
        self.assertEqual(fake["processing_status"], "blocked")
        executable, _ = self.run_extract(self.source("pretend.txt", b"MZThis is an executable, not text"))
        self.assertEqual(executable["processing_status"], "blocked")

    def test_ocr_explicit_receipt_lineage_and_semantic_gap(self):
        called = []
        def callback(path, *, options=None):
            called.append((path, options))
            return {"text": "derived synthetic OCR " * 1600, "processing_status": "extracted", "provider_id": "synthetic.ocr", "provider_version": "test-1",
                    "metadata": {"ocr_executed": True, "synthetic": True}, "confidence": 0.5, "boxes": []}
        result, output = self.run_extract(self.source("ocr.png", _png()), {"ocr": callback, "ocr_options": {"synthetic": True}})
        self.assertEqual(len(called), 1)
        derived = [b for b in result["blocks"] if b["origin"] == "ocr"]
        self.assertGreater(len(derived), 1)
        self.assertEqual("".join(b["text"] for b in derived), "derived synthetic OCR " * 1600)
        lineage = result["metadata"]["derivations"][0]
        self.assertEqual(lineage["source_block_indexes"], [0])
        self.assertEqual(lineage["source_file_keys"], result["blocks"][0]["file_keys"])
        self.assertEqual(result["processing_status"], "partial")
        self.assertTrue(any(g["kind"] == "semantic_gap" for g in result["gaps"]))

    def test_ocr_unavailable_fake_receipt_and_exceptions_not_success(self):
        callbacks = [lambda path, **kw: {"text": "fabricated", "processing_status": "unsupported", "reason": "missing weights"},
                     lambda path, **kw: {"text": "no execution receipt", "processing_status": "extracted", "provider_id": "x", "provider_version": "1"}]
        def broken(path, **kw):
            raise RuntimeError("synthetic engine failure")
        for callback in [*callbacks, broken]:
            result, _ = self.run_extract(self.source("input.png", _png()), {"ocr": callback})
            self.assertFalse(any(b["origin"] == "ocr" for b in result["blocks"]))
            self.assertEqual(result["processing_status"], "partial")

    def test_resource_budgets_explicit_and_not_silent_truncation(self):
        result, _ = self.run_extract(self.source("big.txt", "a" * 500), {"max_input_bytes": 100})
        self.assertEqual(result["processing_status"], "blocked")
        self.assertTrue(any(g["kind"] == "resource_block" for g in result["gaps"]))
        result, _ = self.run_extract(self.source("picture.png", _png()), {"max_image_pixels": 10})
        self.assertEqual(result["processing_status"], "blocked")
        result, _ = self.run_extract(self.source("output.txt", "a" * 1000), {"max_output_bytes": 200})
        self.assertEqual(result["processing_status"], "blocked")
        for value in (0, -1, True, "5"):
            output = self.work / ("bad-limit-" + str(value))
            output.mkdir()
            with self.assertRaises(ValueError):
                extract(self.work / "output.txt", output, options={"max_input_bytes": value})

    def test_nonempty_and_absent_output_dir_rejected_without_writes(self):
        source = self.source("input.txt", "hello")
        absent = self.work / "absent"
        with self.assertRaises(ValueError):
            extract(source, absent)
        self.assertFalse(absent.exists())
        full = self.work / "full"
        full.mkdir()
        keep = full / "keep.txt"
        keep.write_text("unchanged", encoding="utf-8")
        with self.assertRaises(ValueError):
            extract(source, full)
        self.assertEqual(keep.read_text(), "unchanged")

    def test_unsupported_legacy_and_macros_never_generate_body(self):
        for name in ("old.doc", "old.xls", "old.ppt", "macro.docm", "macro.xlsm", "run.exe", "script.ps1"):
            result, output = self.run_extract(self.source(name, b"not executed"))
            self.assertEqual(result["processing_status"], "blocked")
            self.assertFalse(any(b["kind"] == "paragraph" for b in result["blocks"]))
            self.assertEqual(result["files"], [])

    def test_zip_safe_enumeration_no_member_execution_or_outputs(self):
        path = self.work / "safe.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("folder/", b"")
            z.writestr("folder/run.py", "raise RuntimeError('must never execute')")
            z.writestr("short.txt", "synthetic short member")
        result, output = self.run_extract(path)
        self.assertEqual([m["name"] for m in result["metadata"]["archive_members"]], ["folder/", "folder/run.py", "short.txt"])
        self.assertEqual([f["kind"] for f in result["files"]], ["inventory"])
        self.assertFalse((self.work / "folder").exists())
        self.assertEqual(result["processing_status"], "partial")

    def test_zip_path_traversal_absolute_duplicates_and_symlink(self):
        path = self.work / "bad.zip"
        with zipfile.ZipFile(path, "w") as z:
            for name in ("../outside.txt", "/absolute.txt", "C:/evil.txt", "a\\b.txt", "OK.txt", "ok.txt"):
                z.writestr(name, "synthetic")
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(link, "../outside")
        result, output = self.run_extract(path)
        self.assertEqual(result["processing_status"], "blocked")
        reasons = json.dumps(result["metadata"]["archive_issues"])
        self.assertIn("unsafe member path", reasons)
        self.assertIn("case-colliding", reasons)
        self.assertIn("symbolic-link", reasons)
        self.assertFalse((self.work / "outside.txt").exists())

    def test_zip_compression_ratio_and_member_count_budget(self):
        path = self.work / "bomb.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("compressible.txt", "a" * 10000)
            z.writestr("second.txt", "x")
        result, _ = self.run_extract(path, {"max_compression_ratio": 10})
        self.assertEqual(result["processing_status"], "blocked")
        result, _ = self.run_extract(path, {"max_archive_members": 1})
        self.assertEqual(result["processing_status"], "blocked")
        self.assertEqual(result["enumeration_state"], "unknown")

    def test_missing_input_explicit_access_gap(self):
        result, _ = self.run_extract(self.work / "not-found.txt")
        self.assertEqual(result["processing_status"], "blocked")
        self.assertTrue(any(g["kind"] == "access" for g in result["gaps"]))


class OfficeTests(DocumentTestCase):
    def make_docx(self):
        from docx import Document
        from docx.shared import Inches
        doc = Document()
        doc.core_properties.title = "synthetic P1 fixture"
        p = doc.add_paragraph("图前")
        p.add_run().add_picture(io.BytesIO(_png()), width=Inches(0.5))
        p.add_run("图后")
        p.add_run().add_picture(io.BytesIO(_png()), width=Inches(0.5))
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "列一"
        table.cell(0, 1).text = "列二"
        table.cell(1, 0).text = "001"
        table.cell(1, 1).text = "表格值"
        doc.add_paragraph("尾段")
        doc.sections[0].header.paragraphs[0].text = "synthetic header"
        doc.sections[0].footer.paragraphs[0].text = "synthetic footer"
        doc.add_comment(p.runs[0], text="synthetic comment", author="Test")
        path = self.work / "synthetic.docx"
        doc.save(path)
        return path

    def test_docx_text_table_header_footer_comment_image_positions(self):
        source = self.make_docx()
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        result, output = self.run_extract(source)
        self.assertEqual(probe(source)["route"], "docx_native")
        text = "\n".join(b.get("text", "") for b in result["blocks"])
        self.assertIn("图前图后", text)
        self.assertIn("表格值", text)
        for kind, expected in (("header", "synthetic header"), ("footer", "synthetic footer"), ("comment", "synthetic comment")):
            self.assertTrue(any(b["kind"] == kind and expected in b.get("text", "") for b in result["blocks"]))
        images = [b for b in result["blocks"] if b["kind"] == "image_occurrence"]
        self.assertEqual(len(images), 2)
        self.assertNotEqual(images[0]["locator"]["node_id"], images[1]["locator"]["node_id"])
        self.assertEqual([b["locator"]["char_start"] for b in images], [2, 4])
        self.assertEqual(images[0]["file_keys"], images[1]["file_keys"])
        self.assertEqual(self.read_file(result, output, images[0]["file_keys"][0]), _png())
        self.assertFalse(any("page_number" in b["locator"] for b in result["blocks"]))
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_docx_body_table_order_and_comments_identity(self):
        result, _ = self.run_extract(self.make_docx())
        before = next(i for i, b in enumerate(result["blocks"]) if b.get("text") == "图前图后")
        table = next(i for i, b in enumerate(result["blocks"]) if b["kind"] == "table")
        after = next(i for i, b in enumerate(result["blocks"]) if b.get("text") == "尾段")
        self.assertLess(before, table)
        self.assertLess(table, after)
        self.assertEqual(result["metadata"]["comments"][0]["attributes"]["id"], result["metadata"]["comment_anchors"][0]["comment_id"])

    def test_docx_nested_textbox_image_appears_once(self):
        path = self.make_docx()
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("word/document.xml"))
        body = root.find("{" + W + "}body")
        outer = ET.SubElement(body, "{" + W + "}p")
        run = ET.SubElement(outer, "{" + W + "}r")
        ET.SubElement(run, "{" + W + "}t").text = "outer synthetic"
        textbox = ET.SubElement(run, "{" + W + "}txbxContent")
        inner = ET.SubElement(textbox, "{" + W + "}p")
        inner_run = ET.SubElement(inner, "{" + W + "}r")
        ET.SubElement(inner_run, "{" + W + "}t").text = "inner synthetic"
        picture = next(root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}blip"))
        ET.SubElement(inner_run, "{http://schemas.openxmlformats.org/drawingml/2006/main}blip", picture.attrib)
        _rewrite_package(path, {"word/document.xml": ET.tostring(root)})
        result, _ = self.run_extract(path)
        self.assertEqual(len([b for b in result["blocks"] if b["kind"] == "image_occurrence"]), 3)
        self.assertEqual(len([b for b in result["blocks"] if b.get("text") == "inner synthetic"]), 1)
        self.assertTrue(any(b.get("text") == "outer synthetic" for b in result["blocks"]))

    def test_schema_bounded_long_archive_and_part_locators(self):
        archive = self.work / "synthetic-long-path.zip"
        member = "a" * 2100 + ".txt"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr(member, "synthetic")
        result, _ = self.run_extract(archive)
        self.assertEqual(result["metadata"]["archive_members"][0]["name"], member)
        self.assertEqual(result["blocks"][0]["locator"]["kind"], "node")
        path = self.make_docx()
        long_part = "word/header" + "h" * 600 + ".xml"
        root = ET.Element("{" + W + "}hdr")
        p = ET.SubElement(root, "{" + W + "}p")
        ET.SubElement(ET.SubElement(p, "{" + W + "}r"), "{" + W + "}t").text = "synthetic long-part header"
        _rewrite_package(path, {}, {long_part: ET.tostring(root)})
        result, _ = self.run_extract(path)
        block = next(b for b in result["blocks"] if b.get("text") == "synthetic long-part header")
        self.assertLessEqual(len(block["locator"]["node_id"]), 500)
        self.assertIn(long_part, result["metadata"]["locator_node_aliases"][block["locator"]["node_id"]])

    def test_docx_unsupported_xml_root_not_false_complete(self):
        path = self.make_docx()
        _rewrite_package(path, {"word/document.xml": b"<unrelated>Not a Word root</unrelated>"})
        result, _ = self.run_extract(path)
        self.assertNotEqual(result["processing_status"], "verified_complete")
        self.assertTrue(any(g["kind"] == "parse_failed" for g in result["gaps"]))

    def test_docx_ocr_dedup_and_occurrence_lineage(self):
        calls = []
        def callback(path, *, options=None):
            calls.append(path)
            return {"text": "synthetic OCR", "provider_id": "synthetic.ocr", "provider_version": "1", "processing_status": "extracted", "metadata": {"ocr_executed": True}}
        result, _ = self.run_extract(self.make_docx(), {"ocr": callback})
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result["metadata"]["derivations"]), 2)
        self.assertNotEqual(result["metadata"]["derivations"][0]["source_block_indexes"], result["metadata"]["derivations"][1]["source_block_indexes"])

    def test_docx_external_image_never_fetched(self):
        path = self.make_docx()
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("word/_rels/document.xml.rels"))
        for rel in root:
            if rel.get("Type", "").endswith("/image"):
                rel.set("Target", "https://example.invalid/do-not-fetch.png")
                rel.set("TargetMode", "External")
        _rewrite_package(path, {"word/_rels/document.xml.rels": ET.tostring(root)})
        result, _ = self.run_extract(path)
        self.assertGreaterEqual(len(result["metadata"].get("external_images", [])), 2)
        self.assertFalse(result["metadata"]["external_content_loaded"])
        self.assertTrue(any("External image not fetched" in g["reason"] for g in result["gaps"]))

    def test_docx_macro_hidden_inside_renamed_package_not_read(self):
        path = self.make_docx()
        _rewrite_package(path, {}, {"word/vbaProject.bin": b"synthetic macro must not execute"})
        self.assertFalse(probe(path)["supported"])
        result, _ = self.run_extract(path)
        self.assertEqual(result["processing_status"], "blocked")
        self.assertFalse(any(b["kind"] == "paragraph" for b in result["blocks"]))

    def test_office_container_limits_and_entities(self):
        path = self.make_docx()
        result, _ = self.run_extract(path, {"max_member_bytes": 10})
        self.assertEqual(result["processing_status"], "blocked")
        _rewrite_package(path, {"word/document.xml": b'<!DOCTYPE x [<!ENTITY ex SYSTEM "file:///C:/secret">]><x>&ex;</x>'})
        result, _ = self.run_extract(path)
        self.assertEqual(result["processing_status"], "blocked")
        self.assertTrue(any("DTD/entity" in g["reason"] for g in result["gaps"]))
        self.assertEqual(result["files"], [])

    def test_docx_hidden_and_vector_shape_register_gaps(self):
        path = self.make_docx()
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("word/document.xml"))
        p = root.find(".//{" + W + "}p")
        run = ET.SubElement(p, "{" + W + "}r")
        props = ET.SubElement(run, "{" + W + "}rPr")
        ET.SubElement(props, "{" + W + "}vanish")
        ET.SubElement(run, "{" + W + "}t").text = "synthetic hidden"
        ET.SubElement(run, "{" + W + "}drawing")
        _rewrite_package(path, {"word/document.xml": ET.tostring(root)})
        result, _ = self.run_extract(path)
        self.assertTrue(any(b["kind"] == "hidden_object" and "Hidden Word" in b["reason"] for b in result["blocks"]))
        self.assertTrue(any("vector/shape" in g["reason"] for g in result["gaps"]))

    def make_pptx(self):
        from pptx import Presentation
        from pptx.util import Inches
        from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_textbox(Inches(0.2), Inches(0.2), Inches(4), Inches(1)).text = "synthetic slide text"
        slide.shapes.add_shape(MSO_AUTO_SHAPE_TYPE.CHEVRON, Inches(2), Inches(2), Inches(1), Inches(1))
        for x in (1, 3):
            slide.shapes.add_picture(io.BytesIO(_png()), Inches(x), Inches(1), Inches(0.5), Inches(0.5))
        slide.notes_slide.notes_text_frame.text = "synthetic speaker notes"
        slide._element.set("show", "0")
        second = prs.slides.add_slide(prs.slide_layouts[6])
        second.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text = "synthetic second slide"
        path = self.work / "synthetic.pptx"
        prs.save(path)
        return path

    def test_pptx_hidden_notes_pictures_and_shape_gaps(self):
        result, output = self.run_extract(self.make_pptx())
        self.assertEqual(len(result["metadata"]["slides"]), 2)
        self.assertTrue(result["metadata"]["slides"][0]["hidden"])
        self.assertTrue(any(b["kind"] == "note" and "synthetic speaker notes" in b.get("text", "") for b in result["blocks"]))
        self.assertTrue(any(b["kind"] == "hidden_object" and '"hidden": true' in b.get("text", "") for b in result["blocks"]))
        images = [b for b in result["blocks"] if b["kind"] == "image_occurrence"]
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0]["file_keys"], images[1]["file_keys"])
        self.assertEqual(images[0]["locator"]["slide_number"], 1)
        self.assertTrue(any(g["kind"] == "missing_visual" and "Shape" in g["reason"] for g in result["gaps"]))
        self.assertEqual(self.read_file(result, output, images[0]["file_keys"][0]), _png())
        self.assertEqual(next(m for m in result["metrics"] if m["unit"] == "slide")["expected"], 2)

    def make_xlsx(self):
        from openpyxl import Workbook
        from openpyxl.drawing.image import Image
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "合成Sheet"
        sheet["A1"] = "001"
        sheet["B1"] = 2
        sheet["C1"] = "=B1*2"
        sheet["D1"] = "=B1+1"
        sheet["A2"] = "中\n文"
        sheet.merge_cells("A3:C3")
        sheet.row_dimensions[2].hidden = True
        sheet.column_dimensions["B"].hidden = True
        image_path = self.source("synthetic.png", _png())
        sheet.add_image(Image(io.BytesIO(_png())), "E5")
        hidden = workbook.create_sheet("隐藏页")
        hidden.sheet_state = "hidden"
        hidden["A1"] = "synthetic hidden value"
        path = self.work / "synthetic.xlsx"
        workbook.save(path)
        # Writer does not calculate formulas; inject one explicitly synthetic cache.
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        c = next(c for c in root.iter("{" + S + "}c") if c.get("r") == "C1")
        v = c.find("{" + S + "}v")
        v.text = "4"
        _rewrite_package(path, {"xl/worksheets/sheet1.xml": ET.tostring(root)})
        return path

    def test_xlsx_formula_cache_image_merges_hidden_states(self):
        result, output = self.run_extract(self.make_xlsx())
        self.assertEqual(len(result["metadata"]["sheets"]), 2)
        info = result["metadata"]["sheets"][0]
        self.assertEqual(info["merged_ranges"], ["A3:C3"])
        self.assertEqual(info["hidden_rows"], ["2"])
        self.assertTrue(info["hidden_columns"])
        self.assertEqual(result["metadata"]["sheets"][1]["state"], "hidden")
        formulas = [b for b in result["blocks"] if b["kind"] == "formula"]
        by_cell = {b["locator"]["cell_range"]: json.loads(self.read_file(result, output, b["file_keys"][0])) for b in formulas}
        self.assertEqual(by_cell["C1"]["formula"], "B1*2")
        self.assertEqual(by_cell["C1"]["raw_cached_value"], "4")
        self.assertTrue(by_cell["C1"]["cache_available"])
        self.assertFalse(by_cell["D1"]["cache_available"])
        self.assertTrue(any("Formula cache missing" in g["reason"] for g in result["gaps"]))
        images = [b for b in result["blocks"] if b["kind"] == "image_occurrence"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["locator"]["cell_range"], "E5")
        self.assertEqual(images[0]["locator"]["sheet_name"], "合成Sheet")
        self.assertEqual(self.read_file(result, output, images[0]["file_keys"][0]), _png())
        records = [json.loads(self.read_file(result, output, b["file_keys"][0])) for b in result["blocks"] if b["kind"] == "table"]
        self.assertEqual(records[0]["cells"][0]["value"], "001")

    def test_xlsx_cell_budget_avoids_dimension_expansion(self):
        path = self.make_xlsx()
        result, _ = self.run_extract(path, {"max_cells": 2})
        self.assertTrue(any(g["kind"] == "resource_block" for g in result["gaps"]))
        self.assertNotEqual(result["processing_status"], "verified_complete")


class PdfTests(DocumentTestCase):
    def test_pdf_hyperlink_targets_and_cropped_annotations_stay_offline(self):
        from pypdf import PdfWriter
        from pypdf.annotations import Link
        from pypdf.generic import RectangleObject

        path = self.work / "hyperlinks-at-page-positions.pdf"
        writer = PdfWriter()
        writer.add_blank_page(300, 200).cropbox = RectangleObject((0, 50, 300, 200))
        writer.add_blank_page(300, 200)
        target = "https://example.org/supplement?lesson=12"
        writer.add_annotation(0, Link(rect=(30, 70, 100, 93), url=target))
        writer.add_annotation(0, Link(rect=(140, 180, 280, 192), url=target))
        writer.add_annotation(0, Link(rect=(30, 12, 75, 22), url="https://example.org/cropped"))
        writer.add_annotation(1, Link(rect=(30, 20, 90, 35), target_page_index=0))
        writer.write(path)

        result, _ = self.run_extract(path)
        links = result["metadata"]["pdf_hyperlinks"]
        self.assertEqual([row["target"] for row in links], [target, target, "https://example.org/cropped"])
        self.assertEqual([row["page_number"] for row in links], [1, 1, 1])
        self.assertEqual(links[0]["rect"], [30, 70, 100, 93])
        self.assertEqual([row["intersects_page_box"] for row in links], [True, True, False])
        self.assertTrue(all(not row["loaded"] for row in links))
        self.assertFalse(result["metadata"]["external_content_loaded"])
        self.assertFalse(result["metadata"]["content_executed"])

    def test_pdf_pagewise_native_scan_blank_and_repeated_occurrences(self):
        path = self.work / "synthetic-mixed.pdf"
        _pdf(path)
        result, output = self.run_extract(path)
        self.assertEqual(result["metadata"]["page_count"], 3)
        self.assertEqual([p["route"] for p in result["metadata"]["pages"]], ["native_text", "image_ocr", "blank_native"])
        images = [b for b in result["blocks"] if b["kind"] == "image_occurrence"]
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0]["file_keys"], images[1]["file_keys"])
        self.assertEqual([b["locator"]["page_number"] for b in images], [2, 2])
        self.assertEqual(len({b["locator"]["node_id"] for b in images}), 2)
        self.assertTrue(any("synthetic digital page" in b.get("text", "") for b in result["blocks"]))
        self.assertEqual(result["processing_status"], "partial")
        self.assertTrue(any("OCR" in g["reason"] for g in result["gaps"]))
        self.assertEqual(next(m for m in result["metrics"] if m["unit"] == "page")["expected"], 3)
        from PIL import Image
        with Image.open(io.BytesIO(self.read_file(result, output, images[0]["file_keys"][0]))) as img:
            self.assertEqual(img.size, (2, 2))
            self.assertEqual(img.getpixel((0, 0)), (255, 0, 0))

    def test_pdf_image_placement_matrices_are_retained(self):
        path = self.work / "synthetic-placements.pdf"
        _pdf(path)
        result, _ = self.run_extract(path)
        places = result["metadata"]["pdf_image_placements"]
        self.assertEqual([p["bbox_xywh"] for p in places], [[20., 20., 60., 60.], [120., 20., 60., 60.]])
        self.assertTrue(all(p["origin"] == "bottom_left" for p in places))
        self.assertTrue(all("Rotate" in p["coordinate_note"] for p in places))

    def test_pdf_form_reuse_keeps_each_occurrence(self):
        from pypdf import PdfWriter
        from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject
        writer = PdfWriter()
        image = DecodedStreamObject()
        image.set_data(bytes((255, 0, 0)))
        image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"), NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1), NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
        image_ref = writer._add_object(image)
        form = DecodedStreamObject()
        form.set_data(b"q 10 0 0 10 0 0 cm /Im Do Q")
        form.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                     NameObject("/BBox"): ArrayObject([NumberObject(x) for x in (0, 0, 10, 10)]),
                     NameObject("/Resources"): DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Im"): image_ref})})})
        page = writer.add_blank_page(width=300, height=200)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Fm"): writer._add_object(form)})})
        contents = DecodedStreamObject()
        contents.set_data(b"q 1 0 0 1 20 20 cm /Fm Do Q q 1 0 0 1 70 20 cm /Fm Do Q")
        page[NameObject("/Contents")] = writer._add_object(contents)
        path = self.work / "synthetic-forms.pdf"
        writer.write(path)
        result, _ = self.run_extract(path)
        images = [b for b in result["blocks"] if b["kind"] == "image_occurrence"]
        self.assertEqual(len(images), 2)
        self.assertTrue(all(b["file_keys"] for b in images), result["gaps"])
        self.assertEqual(images[0]["file_keys"], images[1]["file_keys"])
        self.assertEqual([p["bbox_xywh"] for p in result["metadata"]["pdf_image_placements"]], [[20., 20., 10., 10.], [70., 20., 10., 10.]])

    def test_pdf_inline_image_is_an_actual_image_artifact(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        stream = DecodedStreamObject()
        stream.set_data(b"q 20 0 0 20 10 10 cm BI /W 1 /H 1 /CS /RGB /BPC 8 ID " + bytes((255, 0, 0)) + b" EI Q")
        page[NameObject("/Contents")] = writer._add_object(stream)
        path = self.work / "synthetic-inline.pdf"
        writer.write(path)
        result, _ = self.run_extract(path)
        images = [b for b in result["blocks"] if b["kind"] == "image_occurrence"]
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0]["file_keys"], result["gaps"])

    def test_pdf_ocr_registered_only_on_scan_pages_with_lineage(self):
        path = self.work / "synthetic.pdf"
        _pdf(path)
        calls = []
        def callback(path, *, options=None):
            calls.append(path)
            return {"text": "synthetic scanned words", "provider_id": "synthetic.ocr", "provider_version": "1", "processing_status": "extracted", "metadata": {"ocr_executed": True}}
        result, _ = self.run_extract(path, {"ocr": callback})
        self.assertEqual(len(calls), 1)
        ocr_blocks = [b for b in result["blocks"] if b["origin"] == "ocr"]
        self.assertEqual(len(ocr_blocks), 2)
        self.assertTrue(all(b["locator"]["page_number"] == 2 for b in ocr_blocks))
        self.assertEqual(len(result["metadata"]["derivations"]), 2)
        self.assertEqual(result["processing_status"], "partial")

    def test_pdf_text_failure_does_not_discard_native_images(self):
        path = self.work / "synthetic-text-failure.pdf"
        _pdf(path)
        with mock.patch("pypdf._page.PageObject.extract_text", side_effect=ValueError("synthetic font parse failure")):
            result, _ = self.run_extract(path)
        images = [b for b in result["blocks"] if b["kind"] == "image_occurrence"]
        self.assertEqual(len(images), 2)
        self.assertTrue(all(b["file_keys"] for b in images))
        self.assertEqual(result["metadata"]["pages"][0]["route"], "native_text_failed")
        self.assertTrue(any(g["kind"] == "parse_failed" for g in result["gaps"]))
        self.assertEqual(result["processing_status"], "partial")

    def test_encrypted_pdf_blocked_not_password_guessed(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("synthetic-secret")
        path = self.work / "encrypted.pdf"
        writer.write(path)
        result, _ = self.run_extract(path)
        self.assertEqual(result["processing_status"], "blocked")
        self.assertTrue(any(g["kind"] == "access" for g in result["gaps"]))
        self.assertEqual(result["files"], [])

    def test_corrupt_pdf_failed_without_pseudo_text(self):
        result, _ = self.run_extract(self.source("corrupt.pdf", b"%PDF-1.7\nnot a pdf"))
        self.assertEqual(result["processing_status"], "failed")
        self.assertFalse(any(b["kind"] == "paragraph" for b in result["blocks"]))

    def test_pdf_vector_only_page_reports_render_missing(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        stream = DecodedStreamObject()
        stream.set_data(b"10 10 100 100 re S")
        page[NameObject("/Contents")] = writer._add_object(stream)
        path = self.work / "vector.pdf"
        writer.write(path)
        result, _ = self.run_extract(path)
        self.assertEqual(result["metadata"]["pages"][0]["route"], "page_render_ocr_required")
        self.assertTrue(any("renderer/OCR" in g["reason"] for g in result["gaps"]))
        self.assertEqual(result["processing_status"], "partial")

    def test_pdf_page_budget_blocked(self):
        path = self.work / "synthetic.pdf"
        _pdf(path)
        result, _ = self.run_extract(path, {"max_pages": 2})
        self.assertEqual(result["processing_status"], "blocked")
        self.assertTrue(any(g["kind"] == "resource_block" for g in result["gaps"]))


class RealInventoryTests(DocumentTestCase):
    """Not production certification: actual authorized local samples, read-only."""
    def test_real_inventory_automatically_selected_and_source_unchanged(self):
        inventory_root = ROOT / "wangzjk.com"
        if not inventory_root.is_dir():
            self.skipTest("Authorized local wangzjk.com inventory is absent")
        selected = {}
        for suffix in (".txt", ".docx", ".pdf", ".pptx", ".xlsx"):
            candidates = [p for p in inventory_root.rglob("*" + suffix) if p.is_file() and p.stat().st_size < 8 * 1024 * 1024]
            if suffix == ".txt":
                # Prefer original short text over old derived text products.
                originals = [p for p in candidates if "文字提取" not in p.parts]
                candidates = originals or candidates
            if candidates:
                selected[suffix] = min(candidates, key=lambda p: (p.stat().st_size, str(p)))
        # The smallest actual PDF happens to be image-only. Independently select
        # a real native-text PDF instead of claiming that scan fixture verifies it.
        from pypdf import PdfReader
        for candidate in sorted(inventory_root.rglob("*.pdf"), key=lambda p: p.stat().st_size):
            if candidate.stat().st_size >= 8 * 1024 * 1024:
                continue
            try:
                reader = PdfReader(candidate)
                if not reader.is_encrypted and any((p.extract_text() or "").strip() for p in reader.pages):
                    selected[".pdf-native"] = candidate
                    break
            except Exception:
                continue
        report = {"evidence_kind": "real_local_authorized_inventory", "synthetic": False,
                  "selection": "Smallest source per format plus a real native-text PDF; original TXT preferred; read-only; no byte threshold for text",
                  "samples": [], "not_found": [x for x in (".txt", ".docx", ".pdf", ".pptx", ".xlsx") if x not in selected],
                  "acceptance_note": "Native extraction != semantic/production completeness; scan mix uses a separately labelled synthetic PDF test"}
        self.assertTrue(selected, "No actual authorized test samples found")
        for suffix, source in selected.items():
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            if os.environ.get("WORLDANALYSIS_DOCUMENT_RETAIN_REAL") == "1":
                retained = FIXTURES / "real_evidence"
                retained.mkdir(exist_ok=True)
                output = Path(tempfile.mkdtemp(prefix=suffix[1:] + "-", dir=retained))
            else:
                output = self.work / ("real-" + suffix[1:])
                output.mkdir()
            result = extract(source, output)
            self.assert_raw_contract(result, output)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            self.assertFalse(result["metadata"]["synthetic"])
            self.assertTrue(result["blocks"], (source, result["gaps"]))
            self.assertNotIn(result["processing_status"], ("blocked", "failed"), (source, result["gaps"]))
            report["samples"].append({"source_path": str(source), "size_bytes": source.stat().st_size,
                                       "output_dir": str(output) if os.environ.get("WORLDANALYSIS_DOCUMENT_RETAIN_REAL") == "1" else None,
                                       "artifacts": result["files"] if os.environ.get("WORLDANALYSIS_DOCUMENT_RETAIN_REAL") == "1" else [],
                                       "sha256_before": before, "sha256_after": hashlib.sha256(source.read_bytes()).hexdigest(),
                                       "media_type": result["media_type"], "route": result["metadata"]["route"],
                                       "enumeration_state": result["enumeration_state"], "processing_status": result["processing_status"],
                                       "blocks": len(result["blocks"]), "files": len(result["files"]),
                                       "visual_occurrences": sum(b["kind"] == "image_occurrence" for b in result["blocks"]),
                                       "metrics": result["metrics"], "gaps": result["gaps"], "metadata": result["metadata"]})
            print("REAL_DOCUMENT_SAMPLE " + json.dumps({k: report["samples"][-1][k] for k in ("source_path", "size_bytes", "processing_status", "blocks", "files", "visual_occurrences")}, ensure_ascii=False))
        report["real_digital_pdf_verified"] = ".pdf-native" in selected
        report["real_mixed_scan_and_digital_same_file_verified"] = any(
            any(p["route"] == "native_text" for p in sample["metadata"].get("pages", [])) and
            any(p["route"] == "image_ocr" for p in sample["metadata"].get("pages", [])) for sample in report["samples"])
        # An evidence report is a test output in this agent's dedicated fixture scope.
        (FIXTURES / "real_inventory_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
