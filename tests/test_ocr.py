"""CPU OCR contracts. All supplied pictures are explicitly synthetic fixtures."""
from __future__ import annotations
import hashlib
from importlib import metadata
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from worldanalysis import ocr

FIXTURES = Path(__file__).parent / "media_fixtures"


class OCRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ocr-test-", dir=FIXTURES)
        self.root = Path(self.temp.name)
        self.image = self.root / "original.png"
        Image.new("RGB", (100, 60), "white").save(self.image)
        self.models = self.root / "models"
        self.models.mkdir()
        for name in ocr._MODEL_NAMES:
            (self.models / name).write_bytes(b"synthetic_fake_weight; never executed")
        self.module = types.SimpleNamespace(__file__=str(self.root / "__init__.py"))

    def tearDown(self):
        self.temp.cleanup()

    def fake(self, value, options=None, error=None):
        with patch.object(ocr.distributions, "version", return_value="1.4.4"), patch.dict(sys.modules, {"rapidocr_onnxruntime": self.module}), patch.object(ocr, "_engine", return_value=Mock(return_value=value, side_effect=error)):
            return ocr.ocr_image(self.image, options=options)

    def test_rotation_retains_original_and_maps_boxes(self):
        original = self.image.read_bytes()
        row = [[[10, 20], [30, 20], [30, 40], [10, 40]], "42", 0.9]
        result = self.fake(([row], [0.01]), {"rotation_degrees": 90})
        self.assertEqual(result["processing_status"], "extracted", result)
        self.assertTrue(result["metadata"]["ocr_executed"])
        self.assertEqual(result["metadata"]["frame_size"], {"width": 60, "height": 100})
        self.assertEqual(result["boxes"][0]["original_polygon"], [[20, 50], [20, 30], [40, 30], [40, 50]])
        self.assertEqual(result["metadata"]["rotation_degrees"], 90)
        self.assertEqual(self.image.read_bytes(), original)

    def test_empty_inference_does_not_invent_text(self):
        result = self.fake((None, None))
        self.assertEqual(result["processing_status"], "extracted")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["boxes"], [])
        self.assertTrue(result["metadata"]["ocr_executed"])
        self.assertIn("NO_TEXT", result["reason"])

    def test_engine_failure_is_not_successful_ocr(self):
        result = self.fake(None, error=RuntimeError("synthetic inference failure"))
        self.assertEqual(result["processing_status"], "failed")
        self.assertEqual(result["text"], "")
        self.assertFalse(result["metadata"]["ocr_executed"])

    def test_nonfinite_box_rejected(self):
        result = self.fake(([[[[float("nan"), 0], [10, 0], [10, 10], [0, 10]], "bad", 0.7]], [0.1]))
        self.assertEqual(result["processing_status"], "failed")
        self.assertEqual(result["text"], "")

    def test_missing_dependency_explicit_unsupported(self):
        with patch.object(ocr.distributions, "version", side_effect=metadata.PackageNotFoundError("rapidocr")):
            result = ocr.ocr_image(self.image)
        self.assertEqual(result["processing_status"], "unsupported")
        self.assertIn("DEPENDENCY_MISSING", result["reason"])
        self.assertFalse(result["metadata"]["ocr_executed"])

    def test_missing_weights_do_not_download_or_execute(self):
        with patch.object(ocr.distributions, "version", return_value="1.4.4"), patch.dict(sys.modules, {"rapidocr_onnxruntime": self.module}), patch.object(ocr, "_engine", side_effect=AssertionError("must not run")):
            result = ocr.ocr_image(self.image, options={"model_dir": self.root / "absent"})
        self.assertEqual(result["processing_status"], "unsupported")
        self.assertIn("WEIGHTS_MISSING", result["reason"])
        self.assertFalse(result["metadata"]["ocr_executed"])

    def test_wrong_version_not_admitted(self):
        with patch.object(ocr.distributions, "version", return_value="99.0"):
            result = ocr.ocr_image(self.image)
        self.assertIn("VERSION_NOT_ADMITTED", result["reason"])
        self.assertFalse(result["metadata"]["ocr_executed"])

    def test_byte_and_pixel_caps_precede_backend(self):
        for limits in ({"max_input_bytes": 1}, {"max_pixels": 10}):
            with patch.object(ocr, "_engine", side_effect=AssertionError("must not run")):
                result = ocr.ocr_image(self.image, options=limits)
            self.assertIn("BLOCKED_RESOURCE", result["reason"])
            self.assertFalse(result["metadata"]["ocr_executed"])

    def test_path_and_authorization_safety(self):
        for path in ("https://example.invalid/image.png", "file://host/image.png", "C:/dummy.png:secret", self.root / ".." / self.root.name / "original.png"):
            result = ocr.ocr_image(path)
            self.assertEqual(result["processing_status"], "failed", str(path))
            self.assertFalse(result["metadata"]["ocr_executed"])
        outside = ocr.ocr_image(self.image, options={"allowed_root": self.models})
        self.assertIn("OUTSIDE_AUTHORIZED_ROOT", outside["reason"])

    def test_multiframe_explicitly_unsupported(self):
        target = self.root / "animated.gif"
        Image.new("RGB", (20, 20), "red").save(target, save_all=True, append_images=[Image.new("RGB", (20, 20), "blue")], duration=100)
        result = ocr.ocr_image(target)
        self.assertIn("MULTIFRAME", result["reason"])
        self.assertFalse(result["metadata"]["ocr_executed"])

    def test_real_cpu_ocr_and_rotated_synthetic(self):
        try:
            metadata.version("rapidocr_onnxruntime")
        except metadata.PackageNotFoundError:
            self.skipTest("pinned project OCR backend is not installed")
        original = FIXTURES / "synthetic_ocr.png"
        rotated = FIXTURES / "synthetic_ocr_rotated.png"
        if not original.exists():
            self.skipTest("run explicit synthetic fixture generator first")
        before = hashlib.sha256(original.read_bytes()).hexdigest()
        for source, options in ((original, {}), (rotated, {"rotation_degrees": 90})):
            result = ocr.ocr_image(source, options=options)
            self.assertEqual(result["processing_status"], "extracted", result["reason"])
            self.assertEqual(result["text"].replace(" ", ""), "TEST4212345")
            self.assertTrue(result["metadata"]["ocr_executed"])
            self.assertGreater(result["confidence"], 0.8)
            self.assertTrue(all(0 <= p[0] <= result["metadata"]["original_width"] and 0 <= p[1] <= result["metadata"]["original_height"] for b in result["boxes"] for p in b["original_polygon"]))
        self.assertEqual(before, hashlib.sha256(original.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
