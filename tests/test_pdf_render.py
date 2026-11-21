"""Lightweight PDF renderer: synthetic safety cases + real, read-only samples."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image
from worldanalysis.documents import extract
from worldanalysis.pdf_render import PROVIDER_VERSION, render_pdf_page
try:
    from .test_documents import DocumentTestCase, _pdf
except ImportError:
    from test_documents import DocumentTestCase, _pdf

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "document_fixtures"


def shape_pdf(path, *, text=True, rotation=0, width=200, height=100):
    """Explicitly synthetic native text plus colored vector shapes."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=width, height=height)
    commands = b"q 0.85 0.12 0.15 rg 10 10 60 25 re f Q q 0.1 0.2 0.9 RG 3 w 10 50 m 160 50 l S Q"
    if text:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        commands += b" BT /F1 9 Tf 10 75 Td (SYNTHETIC native body 42) Tj 0 -12 Td (as shown below) Tj ET"
    stream = DecodedStreamObject()
    stream.set_data(commands)
    page[NameObject("/Contents")] = writer._add_object(stream)
    if rotation:
        page.rotate(rotation)
    writer.add_metadata({"/Title": "synthetic renderer fixture"})
    writer.write(path)
    return path


def synthetic_ocr(path, *, options=None):
    return {"text": "SYNTHETIC OCR stub 42", "provider_id": "synthetic.ocr", "provider_version": "test-1",
            "processing_status": "extracted", "metadata": {"ocr_executed": True, "synthetic": True}}


class PdfRenderTests(DocumentTestCase):
    def render(self, source, number=1, options=None):
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        result = render_pdf_page(source, number, options)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        self.assertFalse(result["metadata"]["network_used"])
        self.assertFalse(result["metadata"]["forms_initialized"])
        self.assertFalse(result["metadata"]["actions_executed"])
        if result["processing_status"] == "extracted":
            self.assertTrue(result["metadata"]["render_executed"])
            self.assertEqual(result["provider_version"], PROVIDER_VERSION)
            self.assertEqual(result["metadata"]["pdfium_flags"], [])
            self.assertIsInstance(result["png_bytes"], bytes)
            with Image.open(io.BytesIO(result["png_bytes"])) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, (result["width"], result["height"]))
                image.load()
            self.assertNotIn("geometry", result["metadata"])
            self.assertNotIn("transform_to_original", result["metadata"])
        else:
            self.assertIsNone(result["png_bytes"])
        return result

    def test_real_png_from_synthetic_text_and_shapes(self):
        source = shape_pdf(self.work / "synthetic-shape.pdf")
        result = self.render(source)
        self.assertEqual(result["processing_status"], "extracted", result["reason"])
        self.assertEqual((result["width"], result["height"]), (400, 200))
        self.assertFalse(result["metadata"]["uniform_rgba_channels"])
        with Image.open(io.BytesIO(result["png_bytes"])) as image:
            red = image.convert("RGB").getpixel((40, 160))
            self.assertGreater(red[0], red[1] + 80)

    def test_rotated_page_dimensions_and_rotation_receipt(self):
        source = shape_pdf(self.work / "synthetic-rotate90.pdf", rotation=90)
        result = self.render(source)
        self.assertEqual(result["processing_status"], "extracted", result["reason"])
        self.assertEqual((result["width"], result["height"]), (200, 400))
        self.assertEqual(result["metadata"]["source_page_rotation_degrees"], 90)
        self.assertFalse(result["metadata"]["uniform_rgba_channels"])
        self.assertIn("no inverse transform", result["metadata"]["coordinate_scope"])

    def test_target_pixel_gate_before_renderer_is_called(self):
        source = shape_pdf(self.work / "synthetic-huge-page.pdf", width=40000, height=40000)
        with mock.patch("pypdfium2._helpers.page.PdfPage.render", side_effect=AssertionError("must not allocate")) as draw:
            result = self.render(source)
        draw.assert_not_called()
        self.assertEqual(result["processing_status"], "blocked")
        self.assertIn("max_pixels", result["reason"])
        self.assertTrue(result["metadata"]["preflight_before_bitmap_allocation"])

    def test_bitmap_byte_gate_before_renderer_is_called(self):
        source = shape_pdf(self.work / "synthetic-byte-bound.pdf")
        with mock.patch("pypdfium2._helpers.page.PdfPage.render", side_effect=AssertionError("must not allocate")) as draw:
            result = self.render(source, options={"max_bitmap_bytes": 1024})
        draw.assert_not_called()
        self.assertEqual(result["processing_status"], "blocked")
        self.assertIn("max_bitmap_bytes", result["reason"])

    def test_input_byte_gate_and_bounded_png_encoder(self):
        source = shape_pdf(self.work / "synthetic-size.pdf")
        result = self.render(source, options={"max_input_bytes": 10})
        self.assertEqual(result["processing_status"], "blocked")
        self.assertIn("max_input_bytes", result["reason"])
        encoded = self.render(source, options={"max_output_bytes": 64})
        self.assertEqual(encoded["processing_status"], "blocked")
        self.assertIn("max_output_bytes", encoded["reason"])
        self.assertFalse(encoded["metadata"]["render_executed"])

    def test_invalid_dpi_limits_and_page_numbers_not_fake_images(self):
        source = shape_pdf(self.work / "synthetic-options.pdf")
        for dpi in (0, -1, True, "144", float("nan"), float("inf"), 601):
            self.assertEqual(self.render(source, options={"dpi": dpi})["processing_status"], "failed")
        for page_number in (0, -1, True, 2, "1"):
            self.assertEqual(self.render(source, page_number)["processing_status"], "failed")
        self.assertEqual(self.render(source, options={"max_pixels": 0})["processing_status"], "failed")

    def test_encryption_blocks_before_pdfium_no_password_or_form_actions(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=100)
        writer.encrypt("synthetic-secret")
        path = self.work / "synthetic-encrypted.pdf"
        writer.write(path)
        with mock.patch("pypdfium2.PdfDocument", side_effect=AssertionError("must not open encrypted source")) as backend:
            result = self.render(path)
        backend.assert_not_called()
        self.assertEqual(result["processing_status"], "blocked")
        self.assertIn("ENCRYPTED", result["reason"])

    def test_dependency_version_and_v8_build_are_admission_gates(self):
        source = shape_pdf(self.work / "synthetic-version.pdf")
        import importlib.metadata
        with mock.patch("worldanalysis.pdf_render.importlib.metadata.version", side_effect=importlib.metadata.PackageNotFoundError("pypdfium2")):
            missing = self.render(source)
        self.assertEqual(missing["processing_status"], "unsupported")
        with mock.patch("worldanalysis.pdf_render.importlib.metadata.version", return_value="0.0.0"):
            mismatch = self.render(source)
        self.assertEqual(mismatch["processing_status"], "unsupported")
        with mock.patch("pypdfium2.PDFIUM_INFO", SimpleNamespace(flags=("V8", "XFA"))):
            unsafe_build = self.render(source)
        self.assertEqual(unsafe_build["processing_status"], "unsupported")
        self.assertIn("BUILD_NOT_ADMITTED", unsafe_build["reason"])

    def test_local_path_and_authorized_root_only(self):
        for path in ("https://example.invalid/no.pdf", "//server/share/no.pdf", "C:/source.pdf:stream", self.work, self.work / "absent.pdf"):
            result = render_pdf_page(path, 1)
            self.assertEqual(result["processing_status"], "failed")
            self.assertIsNone(result["png_bytes"])
        source = shape_pdf(self.work / "synthetic-root.pdf")
        other = self.work / "other"
        other.mkdir()
        result = self.render(source, options={"allowed_root": other})
        self.assertIn("OUTSIDE_AUTHORIZED_ROOT", result["reason"])

    def test_oversize_source_image_blocks_before_native_decoder(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=100)
        image = DecodedStreamObject()
        image.set_data(bytes((255, 0, 0)))
        image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"), NameObject("/Width"): NumberObject(100000), NameObject("/Height"): NumberObject(100000)})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Huge"): writer._add_object(image)})})
        path = self.work / "synthetic-embedded-bomb.pdf"
        writer.write(path)
        with mock.patch("pypdfium2.PdfDocument", side_effect=AssertionError("must not decode huge image")) as backend:
            result = self.render(path)
        backend.assert_not_called()
        self.assertEqual(result["processing_status"], "blocked")
        self.assertIn("max_source_image_pixels", result["reason"])

    def test_document_digital_vector_render_does_not_trigger_ocr(self):
        source = shape_pdf(self.work / "synthetic-digital-vector.pdf")
        ocr = mock.Mock(side_effect=AssertionError("readable digital text must not be re-OCRed"))
        result, output = self.run_extract(source, {"render_pdf_page": render_pdf_page, "ocr": ocr})
        ocr.assert_not_called()
        renders = [f for f in result["files"] if f["kind"] == "page_render"]
        self.assertEqual(len(renders), 1, result["gaps"])
        self.assertEqual(result["metadata"]["pages"][0]["route"], "native_text")
        self.assertEqual(result["processing_status"], "partial")
        self.assertTrue(any(g["kind"] == "semantic_gap" for g in result["gaps"]))
        self.assertFalse(any(g["kind"] == "missing_visual" for g in result["gaps"]))
        self.assertFalse(any(b["kind"] == "image_occurrence" for b in result["blocks"]))
        render = result["metadata"]["page_renders"][0]
        self.assertLessEqual(render["metadata"]["limits"]["max_output_bytes"], 32 * 1024 * 1024)
        block = result["blocks"][render["block_indexes"][0]]
        self.assertEqual(block["kind"], "hidden_object")
        self.assertEqual(block["locator"]["precision"], "page_only")
        self.assertNotIn("geometry", block["locator"])
        self.assertIn("SYNTHETIC native body", "".join(b.get("text", "") for b in result["blocks"]))

    def test_mixed_page_keeps_native_text_and_runs_rendered_ocr(self):
        source = self.work / "synthetic-text-and-images-on-one-page.pdf"
        _pdf(source, mixed_page=True)
        ocr = mock.Mock(side_effect=synthetic_ocr)
        result, _ = self.run_extract(source, {"render_pdf_page": render_pdf_page, "ocr": ocr})
        ocr.assert_called_once()
        first, mixed, blank = result["metadata"]["pages"]
        self.assertEqual([first["route"], mixed["route"], blank["route"]],
                         ["native_text", "native_text", "blank_native"])
        self.assertEqual(mixed["ocr_source"], "page_render")
        self.assertEqual(mixed["image_occurrences"], 2)
        blocks = [b for b in result["blocks"] if b["locator"].get("page_number") == 2]
        self.assertTrue(any("text over a screenshot" in b.get("text", "") for b in blocks))
        candidate, = [b for b in blocks if b["origin"] == "ocr"]
        self.assertIn("OCR stub 42", candidate["text"])
        receipt, = result["metadata"]["derivations"]
        artifacts = {row["key"]: row["kind"] for row in result["files"]}
        self.assertEqual([artifacts[key] for key in receipt["source_file_keys"]], ["page_render"])

    def test_scan_page_render_ocr_once_preserves_each_original_occurrence(self):
        source = self.work / "synthetic-mixed-render.pdf"
        _pdf(source)
        pages, ocr_sources = [], []
        def renderer(path, page_number, options=None):
            pages.append(page_number)
            return render_pdf_page(path, page_number, options)
        def ocr(path, *, options=None):
            ocr_sources.append(path)
            return synthetic_ocr(path, options=options)
        result, output = self.run_extract(source, {"render_pdf_page": renderer, "ocr": ocr})
        self.assertEqual(pages, [2])
        self.assertEqual(len(ocr_sources), 1)
        images = [b for b in result["blocks"] if b["kind"] == "image_occurrence"]
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0]["file_keys"], images[1]["file_keys"])
        self.assertEqual(len([b for b in result["blocks"] if b["origin"] == "ocr"]), 1)
        derivation = result["metadata"]["derivations"][0]
        key = derivation["source_file_keys"][0]
        self.assertEqual(next(f["kind"] for f in result["files"] if f["key"] == key), "page_render")
        self.assertEqual(result["metadata"]["pages"][1]["ocr_source"], "page_render")

    def test_vector_only_render_has_semantic_gap_even_with_ocr(self):
        source = shape_pdf(self.work / "synthetic-vector-only.pdf", text=False)
        result, _ = self.run_extract(source, {"render_pdf_page": render_pdf_page, "ocr": synthetic_ocr})
        self.assertTrue(any(f["kind"] == "page_render" for f in result["files"]))
        self.assertTrue(any(b["origin"] == "ocr" for b in result["blocks"]))
        self.assertEqual(result["processing_status"], "partial")
        self.assertTrue(any(g["kind"] == "semantic_gap" for g in result["gaps"]))
        self.assertFalse(any(g["kind"] == "missing_visual" for g in result["gaps"]))

    def test_no_plugin_and_bad_plugin_keep_native_partial_evidence(self):
        source = shape_pdf(self.work / "synthetic-plugin-failures.pdf")
        plain, _ = self.run_extract(source)
        self.assertFalse(any(f["kind"] == "page_render" for f in plain["files"]))
        self.assertTrue(any(g["kind"] == "missing_visual" for g in plain["gaps"]))
        cases = [lambda *args, **kwargs: {"processing_status": "blocked", "reason": "BLOCKED_RESOURCE: test cap"},
                 lambda *args, **kwargs: {"processing_status": "extracted", "provider_id": "synthetic.renderer", "provider_version": "1", "png_bytes": b"fake", "width": 1, "height": 1, "metadata": {"render_executed": True}}]
        for callback in cases:
            result, _ = self.run_extract(source, {"render_pdf_page": callback})
            self.assertFalse(any(f["kind"] == "page_render" for f in result["files"]))
            self.assertIn("SYNTHETIC native body", "".join(b.get("text", "") for b in result["blocks"]))
            self.assertEqual(result["processing_status"], "partial")
        blocked, _ = self.run_extract(source, {"render_pdf_page": render_pdf_page, "render_options": {"max_pixels": 1}})
        self.assertTrue(any(g["kind"] == "resource_block" for g in blocked["gaps"]))
        self.assertFalse(any(f["kind"] == "page_render" for f in blocked["files"]))

    def test_native_font_failure_can_still_render_and_ocr_page(self):
        source = shape_pdf(self.work / "synthetic-font-failure.pdf")
        with mock.patch("pypdf._page.PageObject.extract_text", side_effect=ValueError("synthetic native font failure")):
            result, _ = self.run_extract(source, {"render_pdf_page": render_pdf_page, "ocr": synthetic_ocr})
        self.assertTrue(any(f["kind"] == "page_render" for f in result["files"]))
        self.assertTrue(any(b["origin"] == "ocr" for b in result["blocks"]))
        self.assertTrue(any(g["kind"] == "parse_failed" for g in result["gaps"]))
        self.assertEqual(result["processing_status"], "partial")

    def test_real_two_pdf_pages_retained_source_unchanged(self):
        report_path = FIXTURES / "real_inventory_results.json"
        if not report_path.is_file():
            self.skipTest("Run document inventory test first for authorized sample selection")
        inventory = json.loads(report_path.read_text(encoding="utf-8"))
        pdfs = [s for s in inventory["samples"] if s["media_type"] == "application/pdf"]
        native = next((s for s in pdfs if any(p["route"] == "native_text" for p in s["metadata"].get("pages", []))), None)
        scan = next((s for s in pdfs if any(p["route"] == "image_ocr" for p in s["metadata"].get("pages", []))), None)
        if not native or not scan:
            self.skipTest("Both real native-text and scan PDF samples are not available")
        output_root = FIXTURES / "pdf_render_evidence"
        output_root.mkdir(exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="real-pages-", dir=output_root))
        report = {"evidence_kind": "real_local_authorized_inventory", "synthetic": False, "samples": [],
                  "provider_id": "pypdfium2", "provider_version": PROVIDER_VERSION,
                  "visual_semantics_verified": False, "ocr_requested": False}
        for label, sample in (("native", native), ("scan", scan)):
            source = Path(sample["source_path"])
            result = self.render(source, 1)
            self.assertEqual(result["processing_status"], "extracted", result["reason"])
            self.assertFalse(result["metadata"]["uniform_rgba_channels"], "Populated real page must not be blank")
            png = run_dir / (label + "-page-1.png")
            png.write_bytes(result["png_bytes"])
            report["samples"].append({"label": label, "source_path": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                                      "page_number": 1, "output_path": str(png), "png_sha256": hashlib.sha256(result["png_bytes"]).hexdigest(),
                                      "width": result["width"], "height": result["height"], "png_bytes": len(result["png_bytes"]),
                                      "processing_status": result["processing_status"], "metadata": result["metadata"]})
            print("REAL_PDF_RENDER " + json.dumps({"label": label, "source_path": str(source), "width": result["width"], "height": result["height"], "png_bytes": len(result["png_bytes"]), "output_path": str(png)}, ensure_ascii=False))
        (FIXTURES / "pdf_render_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
