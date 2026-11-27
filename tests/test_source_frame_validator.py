"""Synthetic file-only source-frame budgets; never a publication/QA claim."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from worldanalysis.contracts import ContractError
from worldanalysis.platform_products import inspect_product
from worldanalysis import source_frame_validator as validator


class SourceFrameValidatorTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "_ledger/devpack/cloud-state-20261116/frame-repair-temp/test-tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="source-decoder-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cap = "采集输入/platform-source-frames/job-synthetic-decoder"
        self.source = {"account_ref": "synthetic-account", "file_id": "101", "source_revision": "synthetic-v1",
                       "size_bytes": 100, "path": "/apps/教育分析学习/synthetic.mp4"}

    def save(self, name, data):
        path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def descriptor(self, manifest):
        data = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        digest = hashlib.sha256(data).hexdigest()
        relative = self.cap + "/frames-" + digest[:24] + ".json"
        self.save(relative, data)
        return {"relative_path": relative, "sha256": digest, "size_bytes": len(data), "frame_count": len(manifest["frames"])}

    def fixture(self, count=2, size=(32, 24), declared=None, data=None):
        if data is None:
            stream = io.BytesIO(); Image.new("RGB", size, "blue").save(stream, "JPEG"); data = stream.getvalue()
        width, height = declared or size
        frames = []
        for i in range(count):
            point = i * 1000
            image = self.cap + "/images/" + str(i) + ".jpg"
            receipt_name = self.cap + "/receipts/" + str(i) + ".json"
            receipt = {"schema": "worldanalysis.browser-source-frame.v1", "source": self.source, "requested_ms": point,
                       "frame_callback": {"mediaTime": point / 1000 + 0.003},
                       "source_identity_check": {"file_id_match": True, "version_token_match": True},
                       "helper_sha256": "f" * 64, "relative_path": image, "sha256": hashlib.sha256(data).hexdigest(),
                       "bytes": len(data), "width": width, "height": height, "format": "jpeg"}
            raw = json.dumps(receipt, ensure_ascii=False).encode()
            self.save(image, data); self.save(receipt_name, raw)
            frames.append({"sequence": i + 1, "relative_path": "images/" + str(i) + ".jpg", "sha256": receipt["sha256"],
                           "size_bytes": len(data), "width": width, "height": height, "requested_ms": point,
                           "media_time_ms": receipt["frame_callback"]["mediaTime"] * 1000, "format": "jpeg", "locator": "requestVideoFrameCallback",
                           "source_identity_check": receipt["source_identity_check"], "receipt_relative_path": "receipts/" + str(i) + ".json",
                           "receipt_sha256": hashlib.sha256(raw).hexdigest()})
        manifest = {"schema": "worldanalysis.source-video-frames.v1", "source": {**self.source, "frame_count": count},
                    "selection": {"evidence_ref": "synthetic-only-selection", "basis": "synthetic-only-test"}, "frames": frames}
        return manifest, self.descriptor(manifest)

    def test_large_flat_product_streams_in_bounded_work_chunks_but_generic_still_rejects(self):
        manifest, descriptor = self.fixture(32, size=(1920, 1080))
        with self.assertRaisesRegex(ContractError, "total byte/pixel budget"):
            inspect_product(SimpleNamespace(root=self.root, max_file_bytes=64 * 1024**2), "courseware", descriptor["relative_path"])
        report = validator.validate_source_frame_files(self.root, descriptor)
        self.assertEqual(report["total_pixels"], 32 * 1920 * 1080)
        self.assertEqual(report["frame_count"], 32)
        self.assertEqual(report["peak_inflight_frame_decodes"], 1)
        self.assertEqual(report["max_frame_pixels"], 1920 * 1080)
        self.assertGreater(len(report["work_chunks"]), 1)
        self.assertTrue(all(c["work_pixels"] <= 64_000_000 for c in report["work_chunks"]))
        self.assertLessEqual(report["logical_total_bytes"], 64 * 1024**2)
        if report["peak_rss_measurement"] == "unknown":
            self.assertIsNone(report["peak_process_rss_bytes"])
        else:
            self.assertGreater(report["peak_process_rss_bytes"], 0)
        self.assertGreater(report["cpu_seconds"], 0)
        self.assertIn("not_quality_or_authorization", report["scope"])

    def test_work_pixel_budget_rejects_before_any_decode(self):
        _, descriptor = self.fixture(33, declared=(4000, 4000))
        with patch.object(validator, "_decode", side_effect=AssertionError("work budget must preflight")):
            with self.assertRaisesRegex(validator.SourceFrameValidationError, "work pixel budget"):
                validator._validate({"root": str(self.root), "manifest": descriptor})

    def test_per_frame_pixel_and_byte_caps_remain_strict(self):
        for kwargs, reason in (({"declared": (4001, 4000)}, "frame pixels"),
                               ({"data": bytes.fromhex("ffd8ff") + b"x" * (4 * 1024**2)}, "image bytes")):
            _, descriptor = self.fixture(**kwargs)
            with self.subTest(reason=reason), self.assertRaisesRegex(validator.SourceFrameValidationError, reason):
                validator._validate({"root": str(self.root), "manifest": descriptor})

    def test_count_cap_is_not_overridden_by_manifest(self):
        manifest, _ = self.fixture(1)
        first = manifest["frames"][0]
        manifest["frames"] = [{**first, "sequence": i + 1, "requested_ms": i * 1000, "media_time_ms": i * 1000 + 3} for i in range(1001)]
        manifest["source"]["frame_count"] = 1001
        descriptor = self.descriptor(manifest)
        with self.assertRaisesRegex(validator.SourceFrameValidationError, "frame count"):
            validator._validate({"root": str(self.root), "manifest": descriptor})

    def test_total_byte_cap_is_preflighted_without_decoding(self):
        manifest, _ = self.fixture(17)
        for frame in manifest["frames"]:
            frame["size_bytes"] = 4 * 1024**2
        descriptor = self.descriptor(manifest)
        original = validator._snapshot
        def snapshot(path):
            result = list(original(path))
            if path.suffix == ".jpg":result[2] = 4 * 1024**2
            return tuple(result)
        with patch.object(validator, "_snapshot", side_effect=snapshot), patch.object(validator, "_decode", side_effect=AssertionError("must preflight byte cap")):
            with self.assertRaisesRegex(validator.SourceFrameValidationError, "total byte budget"):
                validator._validate({"root": str(self.root), "manifest": descriptor})

    def test_bad_last_jpeg_is_fully_decoded_and_never_returns_success(self):
        manifest, _ = self.fixture(3)
        frame = manifest["frames"][-1]; data = bytes.fromhex("ffd8ff") + b"synthetic-broken-tail"
        image = self.cap + "/" + frame["relative_path"]
        receipt_name = self.cap + "/" + frame["receipt_relative_path"]
        receipt = json.loads((self.root / receipt_name).read_bytes())
        frame["sha256"], frame["size_bytes"] = hashlib.sha256(data).hexdigest(), len(data)
        receipt["sha256"], receipt["bytes"] = frame["sha256"], len(data)
        raw = json.dumps(receipt, ensure_ascii=False).encode(); frame["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
        self.save(image, data); self.save(receipt_name, raw)
        with self.assertRaisesRegex(validator.SourceFrameValidationError, "validation failed"):
            validator.validate_source_frame_files(self.root, self.descriptor(manifest))

    def test_changed_hash_hardlinks_and_outside_root_are_rejected(self):
        manifest, descriptor = self.fixture()
        path = self.root / self.cap / manifest["frames"][0]["relative_path"]
        original = path.read_bytes(); path.write_bytes(b"changed")
        with self.assertRaises(validator.SourceFrameValidationError):validator.validate_source_frame_files(self.root, descriptor)
        path.write_bytes(original)
        try:os.link(path, path.with_suffix(".linked"))
        except OSError:self.skipTest("hardlinks unavailable")
        with self.assertRaises(validator.SourceFrameValidationError):validator.validate_source_frame_files(self.root, descriptor)
        with self.assertRaises(validator.SourceFrameValidationError):validator.validate_source_frame_files(self.root.anchor, descriptor)

    def test_root_parent_components_reject_before_filesystem_or_child(self):
        _, descriptor = self.fixture()
        candidates = [validator.CHECKOUT / "..", self.root / "..",
                      self.root / "nested" / ".." / "..", self.root / "nested" / ".." / ".." / ".." / ".."]
        for root in candidates:
            with self.subTest(root=str(root)), patch.object(validator, "_no_links", side_effect=AssertionError("parent root must fail before filesystem access")), patch.object(validator.subprocess, "run", side_effect=AssertionError("parent root must not start child")):
                with self.assertRaisesRegex(validator.SourceFrameValidationError, "parent component"):
                    validator.validate_source_frame_files(root, descriptor)

    def test_canonical_checkout_and_nested_roots_still_work(self):
        nested = self.root / "canonical-nested"; nested.mkdir()
        self.assertEqual(validator._root(validator.CHECKOUT), validator.CHECKOUT)
        self.assertEqual(validator._root(self.root), self.root.resolve(strict=True))
        self.assertEqual(validator._root(nested), nested.resolve(strict=True))

    def test_root_reparse_and_resolved_escape_reject(self):
        original = Path.lstat
        def reparse(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.root:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=getattr(info, "st_file_attributes", 0) | 0x400)
            return info
        with patch.object(Path, "lstat", reparse):
            with self.assertRaisesRegex(validator.SourceFrameValidationError, "links/reparse"):
                validator._root(self.root)
        with patch.object(Path, "resolve", return_value=validator.CHECKOUT.parent):
            with self.assertRaisesRegex(validator.SourceFrameValidationError, "resolved decoder root"):
                validator._root(self.root)

    def test_hard_timeout_and_no_caller_budget_or_cwd_override(self):
        _, descriptor = self.fixture()
        with patch.object(validator.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixed-validator", 90)) as run:
            with self.assertRaisesRegex(validator.SourceFrameValidationError, "hard timeout"):
                validator.validate_source_frame_files(self.root, descriptor)
        self.assertEqual(run.call_args.kwargs["timeout"], 90)
        self.assertEqual(run.call_args.kwargs["cwd"], str(validator.CHECKOUT))
        with self.assertRaisesRegex(validator.SourceFrameValidationError, "fixed decoder ticket"):
            validator._validate({"root": str(self.root), "manifest": descriptor, "budget_override": True})


if __name__ == "__main__":
    unittest.main()
