"""synthetic_unit PDF path/clip routing, not production rendering evidence.

Each test creates one short PDF in its own workspace-local temporary directory.
The recording callback never rasterizes a page or returns a successful render.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject, DecodedStreamObject, DictionaryObject, NameObject,
    NumberObject, TextStringObject,
)

from worldanalysis.documents import extract

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "tests" / "document_fixtures" / ".runs"
TEXT = b"BT /F1 12 Tf 20 90 Td (synthetic_unit text) Tj ET\n"


def _stream(writer, data):
    stream = DecodedStreamObject()
    stream.set_data(data)
    return writer._add_object(stream)


def _resources(writer):
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    return DictionaryObject({NameObject("/Font"): DictionaryObject({
        NameObject("/F1"): writer._add_object(font),
    })})


def _page(writer, data, resources=None):
    page = writer.add_blank_page(width=160, height=140)
    page[NameObject("/Resources")] = resources if resources is not None else DictionaryObject()
    page[NameObject("/Contents")] = _stream(writer, data)


class PdfClippingTests(unittest.TestCase):
    def setUp(self):
        RUNS.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-unit-pdf-clipping-", dir=RUNS)
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.assertTrue(self.work.resolve().is_relative_to(ROOT))

    def _extract(self, writer):
        source = self.work / "synthetic_unit.pdf"
        writer.add_metadata({"/Title": "synthetic_unit clipping fixture"})
        with source.open("xb") as stream:
            writer.write(stream)
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        output = self.work / "out"
        output.mkdir()
        calls = []

        def recording_renderer(path, page_number, *, options=None):
            calls.append(page_number)
            return {
                "processing_status": "unsupported",
                "provider_id": "synthetic_unit.recording_renderer",
                "provider_version": "1",
                "reason": "synthetic_unit callback recording only; no render performed",
                "metadata": {"render_executed": False, "evidence_kind": "synthetic_unit"},
            }

        result = extract(source, output, options={
            "synthetic": True, "render_pdf_page": recording_renderer,
        })
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        self.assertTrue(result["metadata"]["synthetic"])
        self.assertFalse(any(f["kind"] == "page_render" for f in result["files"]))
        self.assertTrue(all(not p["render_executed"] for p in result["metadata"].get("page_renders", [])))
        self.assertFalse(any(b["origin"] == "ocr" for b in result["blocks"]))
        return result, calls

    def test_clip_only_text_does_not_render_or_defer_chart(self):
        writer = PdfWriter()
        _page(writer, b"q 10 10 100 100 re W* n 20 20 m 30 20 l 30 30 l h W n\n" + TEXT + b"Q", _resources(writer))
        result, calls = self._extract(writer)
        page = result["metadata"]["pages"][0]
        self.assertEqual((page["vector_operations"], page["vector_path_operations"], page["clip_operations"]), (0, 5, 2))
        self.assertEqual(page["route"], "native_text")
        self.assertFalse(page["render_attempted"])
        self.assertEqual(calls, [])
        self.assertTrue(any("synthetic_unit text" in b.get("text", "") for b in result["blocks"]))
        self.assertFalse(any(b["kind"] == "chart" for b in result["blocks"]))
        self.assertEqual(result["gaps"], [])

    def test_painted_path_with_text_still_renders_and_defers_chart(self):
        writer = PdfWriter()
        resources = _resources(writer)
        function = DictionaryObject({
            NameObject("/FunctionType"): NumberObject(2),
            NameObject("/Domain"): ArrayObject([NumberObject(0), NumberObject(1)]),
            NameObject("/C0"): ArrayObject([NumberObject(0)] * 3),
            NameObject("/C1"): ArrayObject([NumberObject(1)] * 3),
            NameObject("/N"): NumberObject(1),
        })
        shading = DictionaryObject({
            NameObject("/ShadingType"): NumberObject(2),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            NameObject("/Coords"): ArrayObject([NumberObject(v) for v in (0, 0, 100, 0)]),
            NameObject("/Function"): function,
        })
        resources[NameObject("/Shading")] = DictionaryObject({NameObject("/Sh0"): writer._add_object(shading)})
        _page(writer, b"10 10 100 100 re S 20 20 10 10 re f /Sh0 sh\n" + TEXT, resources)
        result, calls = self._extract(writer)
        page = result["metadata"]["pages"][0]
        self.assertEqual((page["vector_operations"], page["vector_path_operations"], page["clip_operations"]), (3, 2, 0))
        self.assertEqual(page["route"], "native_text")
        self.assertTrue(page["render_attempted"])
        self.assertEqual(calls, [1])
        self.assertTrue(any(b["kind"] == "chart" and b["processing_status"] == "deferred" for b in result["blocks"]))
        self.assertTrue(any("synthetic_unit text" in b.get("text", "") for b in result["blocks"]))
        self.assertEqual(result["processing_status"], "partial")

    def test_textless_painted_path_still_requires_render(self):
        writer = PdfWriter()
        _page(writer, b"10 10 m 60 60 l S")
        result, calls = self._extract(writer)
        page = result["metadata"]["pages"][0]
        self.assertEqual((page["vector_operations"], page["vector_path_operations"], page["clip_operations"]), (1, 2, 0))
        self.assertEqual(page["text_characters"], 0)
        self.assertEqual(page["route"], "page_render_ocr_required")
        self.assertTrue(page["render_attempted"])
        self.assertEqual(calls, [1])
        self.assertTrue(any(b["kind"] == "chart" and b["processing_status"] == "deferred" for b in result["blocks"]))
        self.assertTrue(any("renderer/OCR" in g["reason"] for g in result["gaps"]))
        self.assertEqual(result["processing_status"], "partial")

    def test_nested_form_clipping_is_counted_without_chart(self):
        writer = PdfWriter()
        resources = _resources(writer)
        image = DecodedStreamObject()
        image.set_data(bytes((255, 0, 0)))
        image.update({
            NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8),
        })
        resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Im0"): writer._add_object(image)})
        ocg = writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/OCG"), NameObject("/Name"): TextStringObject("synthetic_unit layer"),
        }))
        resources[NameObject("/Properties")] = DictionaryObject({NameObject("/Layer"): ocg})
        writer._root_object[NameObject("/OCProperties")] = DictionaryObject({
            NameObject("/OCGs"): ArrayObject([ocg]),
            NameObject("/D"): DictionaryObject({NameObject("/BaseState"): NameObject("/ON")}),
        })
        form = DecodedStreamObject()
        form.set_data(b"/OC /Layer BDC q 10 10 100 100 re W* n\n" + TEXT + b"q 8 0 0 8 20 20 cm /Im0 Do Q Q EMC")
        form.update({
            NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([NumberObject(v) for v in (0, 0, 140, 120)]),
            NameObject("/Resources"): resources,
        })
        _page(writer, b"q /Fm0 Do Q", DictionaryObject({NameObject("/XObject"): DictionaryObject({
            NameObject("/Fm0"): writer._add_object(form),
        })}))
        _page(writer, b"10 10 100 100 re W n")
        result, calls = self._extract(writer)
        pages = result["metadata"]["pages"]
        self.assertEqual([p["route"] for p in pages], ["native_text", "path_clip_only_native"])
        self.assertEqual([(p["vector_operations"], p["vector_path_operations"], p["clip_operations"]) for p in pages], [(0, 1, 1), (0, 1, 1)])
        self.assertEqual([p["image_occurrences"] for p in pages], [1, 0])
        self.assertFalse(any(p["render_attempted"] for p in pages))
        self.assertEqual(calls, [])
        self.assertTrue(any("synthetic_unit text" in b.get("text", "") for b in result["blocks"]))
        self.assertTrue(any(b["kind"] == "image_occurrence" and b["file_keys"] for b in result["blocks"]))
        self.assertTrue(any("optional-content" in b["reason"] for b in result["blocks"]))
        self.assertFalse(any(b["kind"] == "chart" for b in result["blocks"]))
        self.assertFalse(any(b["kind"] in ("paragraph", "image_occurrence") and b["locator"].get("page_number") == 2 for b in result["blocks"]))
        self.assertEqual(result["processing_status"], "partial")


if __name__ == "__main__":
    unittest.main()
