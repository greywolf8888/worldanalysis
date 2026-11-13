"""Real PNG/JPEG pixels in temporary, genuinely published Store fixtures only."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import tempfile
import unittest
from unittest import mock
import zlib

from PIL import Image

from worldanalysis import image_region as region
from worldanalysis.contracts import ContractError, canonical, hash_file
from worldanalysis.store import Store


def bbox(left=2, top=1, right=10, bottom=7):
    return {"unit": "pixel", "origin": "top_left", "xyxy": [left, top, right, bottom]}


def cleanup(path):
    def writable(func, name, exc):
        Path(name).chmod(stat.S_IWRITE)
        func(name)
    shutil.rmtree(path, onerror=writable)


def oversized_header(width, height):
    # Synthetic PNG with valid header/chunk CRCs, intentionally insufficient
    # scanline data. It must be rejected from dimensions WITHOUT a raster load.
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\0\0\0")) + chunk(b"IEND", b""))


class ImageRegionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wa-image-region-"))
        self.addCleanup(cleanup, self.root)
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.serial = 0

    def make(self, *, geometry=None, restricted=False, kind="image", precision="exact", data=None, jpeg=False):
        self.serial += 1
        self.project = self.store.create_project(project_id="crop-project-" + str(self.serial), topic="Synthetic local crop fixture")
        self.source = self.root / ("source-" + str(self.serial) + (".jpg" if jpeg else ".png"))
        if data is None:
            with Image.new("RGB", (12, 8)) as image:
                for y in range(8):
                    for x in range(12):
                        image.putpixel((x, y), (x * 19, y * 29, (x + y) * 11))
                image.save(self.source)
        else:
            self.source.write_bytes(data)
        self.original_sha = hash_file(self.source)

        def extract(source, output, options=None):
            (output / self.source.name).write_bytes(source.read_bytes())
            locator = {"kind": "node", "precision": precision, "node_id": "actual-temporary-raster"}
            if precision in {"unknown", "approximate"}:
                locator["uncertainty_reason"] = "Synthetic uncertain location test"
            if geometry is not None:
                locator["geometry"] = {"reference_object_ref": "pixels", "coordinate_space": "original", "unit": "pixel", "origin": "top_left",
                    "frame_size": {"width": 12, "height": 8}, "original_size": {"width": 12, "height": 8, "unit": "pixel"},
                    "rotation_degrees": 0, "bbox_xywh": [0, 0, 12, 8], "transform_to_original": {"method": "identity", "operations": []}, **deepcopy(geometry)}
            blocks = [{"kind": "image_occurrence", "origin": "native_extract", "locator": locator,
                       "file_keys": ["pixels"], "processing_status": "deferred", "reason": "Actual pixels, no semantic read"},
                      {"kind": "paragraph", "origin": "native_extract", "locator": {"kind": "paragraph", "precision": "exact", "paragraph_index": 0},
                       "text": "Separate block without the image", "file_keys": [], "processing_status": "extracted", "reason": "Synthetic block boundary"}]
            return {"media_type": "image/jpeg" if jpeg else "image/png", "provider_id": "synthetic-region-fixture", "provider_version": "1",
                    "blocks": blocks, "files": [{"key": "pixels", "relative_path": self.source.name, "kind": kind, "media_type": "image/jpeg" if jpeg else "image/png"}],
                    "gaps": [], "metrics": [], "enumeration_state": "complete", "processing_status": "partial", "metadata": {"synthetic": True, "restricted_local": restricted}}

        self.bundle = self.store.ingest(self.project, [self.source], extract)
        self.block = self.bundle["blocks"][0]
        self.artifact = next(a for a in self.bundle["artifacts"] if a["artifact_id"] in self.block["artifact_ids"])
        self.project_root = self.store.project_root(self.project)
        self.sealed = self.project_root / self.artifact["relative_path"]
        return self.bundle

    def crop(self, box=None, output="补采派生/test.png", *, resize=None, block=None, artifact=None):
        return region.crop_region(self.store, self.project, self.bundle["bundle_id"], block or self.block["block_id"],
                                  artifact or self.artifact["artifact_id"], bbox() if box is None else box, output, resize=resize)

    def test_png_corner_pixels_originals_and_business_state_unchanged(self):
        self.make()
        snapshot = {a["relative_path"]: hash_file(self.project_root / a["relative_path"]) for a in self.bundle["artifacts"]}
        changes = self.store.connection.total_changes
        manifest = self.crop()
        output = self.project_root / manifest["output_relative_path"]
        with Image.open(self.source) as source, Image.open(output) as result:
            self.assertEqual(result.size, (8, 6))
            for ox, oy, sx, sy in ((0, 0, 2, 1), (7, 0, 9, 1), (0, 5, 2, 6), (7, 5, 9, 6)):
                self.assertEqual(result.getpixel((ox, oy)), source.getpixel((sx, sy)))
        self.assertEqual(hash_file(output), (manifest["new_artifact_sha256"], manifest["byte_length"]))
        self.assertEqual(manifest["geometry"]["transform_to_original"]["matrix"], [1, 0, 2, 0, 1, 1, 0, 0, 1])
        self.assertEqual(manifest["source_ref"]["locator"], self.block["locator"])
        self.assertEqual(manifest["source_ref"]["artifact_id"], self.artifact["artifact_id"])
        self.assertEqual(manifest["source_artifact_sha256"], self.artifact["sha256"])
        self.assertEqual(manifest["derived_from"][0]["evidence_ref"]["bundle_revision"], self.bundle["bundle_revision"])
        self.assertFalse(manifest["scope"]["whole_page_or_course_read"])
        self.assertFalse(manifest["scope"]["business_state_updated"])
        self.assertFalse(manifest["scope"]["publication_created"])
        for forbidden in ("artifact_id", "artifact_revision", "job_id", "producer", "publication"):
            self.assertNotIn(forbidden, manifest)
        self.assertEqual(changes, self.store.connection.total_changes)
        self.assertEqual(self.store.get_bundle(self.project, self.bundle["bundle_id"]), self.bundle)
        self.assertEqual(hash_file(self.source), self.original_sha)
        for relative, expected in snapshot.items():
            self.assertEqual(hash_file(self.project_root / relative), expected)
        sidecar = json.loads((self.project_root / manifest["manifest_relative_path"]).read_text(encoding="utf-8"))
        self.assertNotIn("reused", sidecar)
        self.assertEqual(sidecar, {k: v for k, v in manifest.items() if k != "reused"})

    def test_resize_inverse_edges_and_nearest_pixels(self):
        self.make(geometry={})
        result = self.crop(resize=(4, 3))
        matrix = result["geometry"]["transform_to_original"]["matrix"]
        self.assertEqual(matrix, [2, 0, 2, 0, 2, 1, 0, 0, 1])
        self.assertEqual(region._map(matrix, 4, 3), [10, 7])
        self.assertEqual(region._map(matrix, .5, .5), [3, 2])
        with Image.open(self.source) as source, Image.open(self.project_root / result["output_relative_path"]) as crop:
            self.assertEqual(crop.getpixel((0, 0)), source.getpixel((3, 2)))
            self.assertEqual(crop.getpixel((3, 2)), source.getpixel((9, 6)))

    def test_multilevel_matrix_composes_existing_crop_resize_then_new_crop_resize(self):
        # The published source already has a crop+resize mapping to original.
        self.make(geometry={"coordinate_space": "derived", "original_size": {"width": 48, "height": 32, "unit": "pixel"},
            "transform_to_original": {"method": "matrix_3x3", "operations": ["crop", "resize"], "matrix": [2, 0, 10, 0, 2, 6, 0, 0, 1]}})
        result = self.crop(resize=(4, 3))
        geometry = result["geometry"]
        self.assertEqual(geometry["transform_to_original"]["matrix"], [4, 0, 14, 0, 4, 8, 0, 0, 1])
        self.assertEqual(geometry["original_polygon_xy"], [[14, 8], [30, 8], [30, 20], [14, 20]])
        self.assertEqual(geometry["transform_to_original"]["operations"], ["crop", "resize", "crop", "resize"])
        self.assertEqual(geometry["reference_object_ref"], self.artifact["artifact_id"])

    def test_rotation_matrix_maps_to_original_without_rotating_decoded_source(self):
        self.make(geometry={"coordinate_space": "derived", "rotation_degrees": 90,
            "original_size": {"width": 8, "height": 12, "unit": "pixel"},
            "transform_to_original": {"method": "matrix_3x3", "operations": ["rotate"], "matrix": [0, -1, 8, 1, 0, 0, 0, 0, 1]}})
        result = self.crop()
        self.assertEqual(result["geometry"]["original_polygon_xy"], [[7, 2], [7, 10], [1, 10], [1, 2]])
        self.assertEqual(result["geometry"]["rotation_degrees"], 90)
        with Image.open(self.source) as source, Image.open(self.project_root / result["output_relative_path"]) as crop:
            self.assertEqual(crop.getpixel((0, 0)), source.getpixel((2, 1)))

    def test_unprovable_transform_rejected_not_guessed(self):
        self.make(geometry={})
        for transform in ({"method": "explicit_mapping", "operations": ["rotate"], "mapping_artifact_id": "not-verified"},
                          {"method": "matrix_3x3", "operations": ["perspective"], "matrix": [1, 0, 0, 0, 1, 0, .01, 0, 1]}):
            bad = deepcopy(self.bundle)
            bad["blocks"][0]["locator"]["geometry"]["transform_to_original"] = transform
            with mock.patch.object(self.store, "get_bundle", return_value=bad), self.assertRaisesRegex(ContractError, "UNSUPPORTED"):
                self.crop()
        self.assertFalse((self.project_root / "补采派生").exists())

    def test_explicit_units_integer_edges_finite_bool_and_bounds(self):
        self.make()
        invalid = [bbox(.1, 1, 10, 7), bbox(True, 1, 10, 7), bbox(2, float("nan"), 10, 7), bbox(2, 1, float("inf"), 7),
                   bbox(-1, 1, 10, 7), bbox(2, 1, 12.00000001, 7), bbox(2, 1, 10, 9), bbox(10, 1, 2, 7), bbox(2, 1, 2, 7),
                   {"unit": "normalized", "origin": "top_left", "xyxy": [0, 0, 1, 1]},
                   {"unit": "pixel", "origin": "bottom_left", "xyxy": [0, 0, 1, 1]}, [2, 1, 10, 7]]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ContractError):
                self.crop(value)
        for resize in ((True, 2), (0, 2), (1.2, 2), (float("nan"), 2), (5000, 5000), (2, 3, 4)):
            with self.subTest(resize=resize), self.assertRaises(ContractError):
                self.crop(resize=resize)
        self.assertFalse((self.project_root / "补采派生").exists())
        self.assertEqual(self.crop(bbox(0.0, 0.0, 12.0, 8.0))["dimensions"], {"width": 12, "height": 8})

    def test_crop_cannot_escape_exact_block_geometry(self):
        self.make(geometry={"bbox_xywh": [2, 1, 8, 6]})
        with self.assertRaisesRegex(ContractError, "exact source block geometry"):
            self.crop(bbox(1, 1, 10, 7))
        self.crop()

    def test_source_caps_checked_before_raster_load(self):
        self.make(data=oversized_header(4001, 4000))
        with mock.patch.object(Image.Image, "load", side_effect=AssertionError("must not decode")), self.assertRaisesRegex(ContractError, "BLOCKED_RESOURCE"):
            self.crop()
        self.assertFalse((self.project_root / "补采派生").exists())
        self.make()
        with mock.patch.object(region, "MAX_SOURCE_BYTES", self.artifact["byte_length"] - 1), mock.patch.object(Image.Image, "load", side_effect=AssertionError("must not decode")), self.assertRaisesRegex(ContractError, "BLOCKED_RESOURCE"):
            self.crop()

    def test_capped_png_buffer_blocks_before_any_output_write(self):
        self.make()
        with mock.patch.object(region, "MAX_OUTPUT_BYTES", 16), self.assertRaisesRegex(ContractError, "encoded PNG"):
            self.crop()
        self.assertFalse((self.project_root / "补采派生").exists())

    def test_store_verification_first_wrong_project_version_block_and_restricted(self):
        self.make()
        with mock.patch.object(self.store, "get_bundle", wraps=self.store.get_bundle) as verified, self.assertRaises(ContractError):
            self.crop([0, 0, 1, 1])
        verified.assert_called_once_with(self.project, self.bundle["bundle_id"])
        other = self.store.create_project(project_id="other-project", topic="No shared evidence")
        with self.assertRaises(ContractError):
            region.crop_region(self.store, other, self.bundle["bundle_id"], self.block["block_id"], self.artifact["artifact_id"], bbox(), "补采派生/no.png")
        with self.assertRaises(ContractError):
            region.crop_region(self.store, self.project, "wrong-bundle-version", self.block["block_id"], self.artifact["artifact_id"], bbox(), "补采派生/no.png")
        with self.assertRaisesRegex(ContractError, "exact source block"):
            self.crop(block=self.bundle["blocks"][1]["block_id"])
        self.make(restricted=True)
        with self.assertRaisesRegex(ContractError, "restricted_local"):
            self.crop()
        self.make(precision="unknown")
        with self.assertRaisesRegex(ContractError, "locator is not exact"):
            self.crop()
        self.make(kind="structured_data")
        with self.assertRaisesRegex(ContractError, "non-image"):
            self.crop()

    def test_output_paths_cannot_touch_originals_evidence_or_another_project(self):
        self.make()
        for output in ("../escape.png", "证据/v0001/files/new.png", "原材料/old.png", "/absolute.png", "C:/escape.png", "补采派生/../x.png",
                       "补采派生/a/b.png", "补采派生/a.png:ads", "补采派生\\a.png", "other-project/补采派生/a.png", "补采派生/CON.png", "补采派生/a.PNG"):
            with self.subTest(output=output), self.assertRaises(ContractError):
                self.crop(output=output)
        self.assertEqual(hash_file(self.source), self.original_sha)

    def test_source_path_escape_and_symlink_rejected(self):
        self.make()
        for relative in ("../outside.png", "证据/v0002/files/wrong.png", "证据/v0001/files/../image.png", "证据/v0001/files\\x.png"):
            bad = deepcopy(self.bundle)
            next(a for a in bad["artifacts"] if a["artifact_id"] == self.artifact["artifact_id"])["relative_path"] = relative
            with mock.patch.object(self.store, "get_bundle", return_value=bad), self.assertRaises(ContractError):
                self.crop()
        self.sealed.chmod(stat.S_IWRITE)
        self.sealed.unlink()
        try:
            self.sealed.symlink_to(self.source)
        except OSError:
            self.skipTest("OS does not grant symlink creation")
        self.addCleanup(self.sealed.unlink)
        with self.assertRaisesRegex(ContractError, "symbolic|reparse"):
            self.crop()

    def test_source_tamper_or_same_sha_identity_change_never_materializes(self):
        self.make()
        recheck = region._recheck
        changed = False
        def replace_identity(path, identity, sha):
            nonlocal changed
            if not changed:
                changed = True
                path.chmod(stat.S_IWRITE)
                info = path.stat()
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            return recheck(path, identity, sha)
        with mock.patch.object(region, "_recheck", side_effect=replace_identity), self.assertRaisesRegex(ContractError, "SHA/identity changed"):
            self.crop()
        self.assertFalse((self.project_root / "补采派生").exists())
        self.sealed.write_bytes(b"tampered sealed image")
        with self.assertRaisesRegex(ContractError, "hash"):
            self.crop()
        self.assertEqual(hash_file(self.source), self.original_sha)

    def test_repeat_requires_same_hash_geometry_parameters_and_intact_pair(self):
        self.make()
        first = self.crop()
        output = self.project_root / first["output_relative_path"]
        sidecar = self.project_root / first["manifest_relative_path"]
        stat_before = output.stat(), sidecar.stat()
        second = self.crop()
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["new_artifact_sha256"], second["new_artifact_sha256"])
        self.assertEqual(stat_before, (output.stat(), sidecar.stat()))
        for box, resize in ((bbox(1, 1, 9, 7), None), (bbox(), (4, 3))):
            with self.assertRaisesRegex(ContractError, "SHA/parameters/geometry mismatch"):
                self.crop(box, resize=resize)
        original = output.read_bytes()
        output.write_bytes(b"user replacement")
        with self.assertRaisesRegex(ContractError, "mismatch"):
            self.crop()
        self.assertEqual(output.read_bytes(), b"user replacement")
        output.write_bytes(original)
        sidecar.write_bytes(b"user manifest")
        with self.assertRaisesRegex(ContractError, "mismatch"):
            self.crop()
        self.assertEqual(sidecar.read_bytes(), b"user manifest")

    def test_existing_and_partial_files_never_overwritten_or_deleted(self):
        self.make()
        directory = self.project_root / "补采派生"
        directory.mkdir()
        output = directory / "test.png"
        output.write_bytes(b"user data")
        with self.assertRaisesRegex(ContractError, "incomplete/colliding"):
            self.crop()
        self.assertEqual(output.read_bytes(), b"user data")
        output.unlink()  # test-owned data only
        create = region._create
        def fail_sidecar(path, data):
            if path.name.endswith("manifest.json"):
                raise OSError("synthetic storage failure")
            create(path, data)
        with mock.patch.object(region, "_create", side_effect=fail_sidecar), self.assertRaisesRegex(ContractError, "partial files retained"):
            self.crop()
        self.assertTrue(output.exists())
        png_sha = hash_file(output)
        with self.assertRaisesRegex(ContractError, "incomplete/colliding"):
            self.crop()
        self.assertEqual(hash_file(output), png_sha)

    def test_sealed_jpeg_crop_uses_actual_decoded_pixels(self):
        buffer = io.BytesIO()
        with Image.new("RGB", (12, 8), (30, 90, 160)) as image:
            image.save(buffer, format="JPEG")
        self.make(data=buffer.getvalue(), jpeg=True)
        result = self.crop()
        with Image.open(self.source) as source, Image.open(self.project_root / result["output_relative_path"]) as output:
            self.assertEqual(output.getpixel((0, 0)), source.getpixel((2, 1)))
        self.assertEqual(hash_file(self.source), self.original_sha)


if __name__ == "__main__":
    unittest.main()
