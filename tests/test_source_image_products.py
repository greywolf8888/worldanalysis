"""Synthetic temporary displayed-page sets; no real files, DB or browser."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image

from worldanalysis.contracts import ContractError
from worldanalysis import platform_products as products


class SourceImageProductTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix="synthetic-source-image-products-")
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.store = SimpleNamespace(root=self.root, max_file_bytes=64 * 1024**2)
        self.relative = "capture/rendered-pages.json"
        self.manifest = self.root / self.relative
        self.manifest.parent.mkdir()

    def save(self, document):
        self.manifest.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    def fixture(self, count=3, fmt="jpeg", size=(32, 18)):
        source = {"file_id": "123456789", "path": "/synthetic/course/lesson.mp4",
                  "source_revision": "synthetic-opaque-revision-1", "page_count": count}
        pages = []
        for number in range(1, count + 1):
            image = Image.new("RGB", size, ((number * 37) % 255, 50, 90))
            buffer = io.BytesIO()
            image.save(buffer, fmt.upper())
            data = buffer.getvalue()
            name = f"pages/{number:03d}.{'jpg' if fmt == 'jpeg' else 'png'}"
            path = self.manifest.parent / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
            pages.append({"schema": "worldanalysis.browser-courseware-page.v1",
                          "observed_at": "2026-11-16T00:00:00Z", "source": copy.deepcopy(source),
                          "page": number, "width": size[0], "height": size[1], "bytes": len(data), "format": fmt,
                          "sha256": hashlib.sha256(data).hexdigest(),
                          "pixel_sha256": hashlib.sha256(image.convert("RGBA").tobytes()).hexdigest(),
                          "representation": "normally_displayed_platform_page",
                          "not_native_container_bytes": True, "relative_path": name})
        document = {"schema": products.IMAGE_SET_SCHEMA, "pages": pages}
        self.save(document)
        return document

    def inspect(self, document=None):
        if document is not None:
            self.save(document)
        return products.inspect_product(self.store, "courseware", self.relative)

    def rejected(self, document):
        with self.assertRaises(ContractError):
            self.inspect(document)

    def page_path(self, document, index=0):
        return self.manifest.parent / document["pages"][index]["relative_path"]

    def replace_page(self, document, data, index=0):
        self.page_path(document, index).write_bytes(data)
        row = document["pages"][index]
        row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

    def test_eleven_jpeg_pages_are_a_displayed_image_set_not_native_or_video_coverage(self):
        document = self.fixture(11, size=(1024, 576))
        # Browser RGBA hashes need not match Pillow's JPEG decoder; retaining a
        # browser-reported fingerprint is not independent pixel verification.
        document["pages"][0]["pixel_sha256"] = "a" * 64
        result = self.inspect(document)
        self.assertTrue(result["readable"])
        self.assertEqual(result["format"], "image_set")
        self.assertEqual(result["metrics"]["page_count"], 11)
        self.assertEqual(len(result["dependencies"]), 11)
        self.assertEqual(result["sha256"], hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        self.assertEqual(result["size_bytes"], self.manifest.stat().st_size)
        meta = result["metadata"]
        self.assertEqual(meta["representation"], "normally_displayed_platform_page")
        self.assertTrue(meta["not_native_container_bytes"])
        self.assertTrue(meta["page_sequence_complete"])
        self.assertEqual(meta["declared_source"], document["pages"][0]["source"])
        for key in ("source_identity_verified", "source_frames_verified", "video_coverage_verified",
                    "server_md5_verified", "browser_display_verified", "pixel_sha256_verified"):
            self.assertIs(meta[key], False, key)
        self.assertEqual(meta["pixel_sha256_scope"], "browser_reported_rgba_not_cross_decoder_verified")
        for row, dep in zip(document["pages"], result["dependencies"]):
            self.assertEqual(dep, {"relative_path": "capture/" + row["relative_path"],
                                   "sha256": row["sha256"], "size_bytes": row["bytes"]})
        self.assertTrue(products.verify_retained_artifact(self.store, result))

    def test_explicit_png_is_also_decoded_without_container_fabrication(self):
        document = self.fixture(2, "png")
        result = self.inspect(document)
        self.assertTrue(result["readable"])
        self.assertEqual(result["metrics"]["image_count"], 2)
        self.assertEqual(result["metadata"]["route"], "rendered_native_pages")

    def test_unknown_json_is_not_supported_courseware(self):
        for document in ({}, {"schema": "some.other.v1", "pages": []}, ["not", "a", "set"]):
            with self.subTest(document=document):
                self.assertFalse(self.inspect(document)["readable"])
        for raw in ('{"schema":"a","schema":"b"}', '{"value":NaN}', '{invalid'):
            with self.subTest(raw=raw), self.assertRaises(ContractError):
                self.manifest.write_text(raw, encoding="utf-8")
                self.inspect()

    def test_empty_missing_repeated_out_of_order_and_excess_pages_fail(self):
        document = self.fixture()
        for pages in ([], document["pages"][:-1], document["pages"] * 2,
                      list(reversed(document["pages"])), None):
            value = copy.deepcopy(document)
            value["pages"] = pages
            with self.subTest(count=len(pages) if isinstance(pages, list) else None):
                self.rejected(value)
        value = copy.deepcopy(document)
        value["pages"][1]["page"] = 1
        self.rejected(value)
        with mock.patch.dict(products.IMAGE_SET_LIMITS, max_pages=2):
            self.rejected(document)

    def test_every_page_requires_the_same_complete_stable_source_declaration(self):
        document = self.fixture()
        for key, value in (("file_id", "different-id"), ("path", "/synthetic/other.mp4"),
                           ("source_revision", "another-revision"), ("page_count", 2),
                           ("file_id", ""), ("path", "relative/source.mp4"), ("source_revision", "")):
            changed = copy.deepcopy(document)
            changed["pages"][1]["source"][key] = value
            with self.subTest(key=key, value=value):
                self.rejected(changed)
        for key in document["pages"][0]["source"]:
            changed = copy.deepcopy(document)
            del changed["pages"][0]["source"][key]
            self.rejected(changed)

    def test_page_schema_representation_fields_and_strict_scalar_types(self):
        document = self.fixture()
        bad = (("page", True), ("width", True), ("height", 0), ("bytes", -1), ("bytes", "123"),
               ("sha256", "f" * 63), ("pixel_sha256", None), ("format", "webp"),
               ("schema", "native-pptx.v1"), ("not_native_container_bytes", False),
               ("not_native_container_bytes", 1), ("representation", "native_pptx"),
               ("unknown", "unexpected"), ("base64", "not-retained"))
        for key, value in bad:
            changed = copy.deepcopy(document)
            changed["pages"][0][key] = value
            with self.subTest(key=key, value=value):
                self.rejected(changed)
        changed = copy.deepcopy(document)
        changed["pages"][0]["source"]["page_count"] = True
        self.rejected(changed)
        changed = copy.deepcopy(document)
        changed["unrecognized"] = 1
        self.rejected(changed)

    def test_page_path_traversal_absolute_aliases_and_reused_paths_are_rejected(self):
        document = self.fixture()
        for name in ("../outside.jpg", "pages/../001.jpg", "pages//001.jpg", "/outside.jpg",
                     "C:/outside.jpg", "pages\\001.jpg", "pages/CON.jpg", "pages/001.jpg."):
            changed = copy.deepcopy(document)
            changed["pages"][0]["relative_path"] = name
            with self.subTest(name=name):
                self.rejected(changed)
        changed = copy.deepcopy(document)
        changed["pages"][1]["relative_path"] = changed["pages"][0]["relative_path"]
        self.rejected(changed)
        changed["pages"][1]["relative_path"] = changed["pages"][0]["relative_path"].upper()
        self.rejected(changed)

    def test_missing_page_is_not_accepted_as_metadata_only_success(self):
        document = self.fixture()
        self.page_path(document, 2).unlink()
        self.rejected(document)

    def test_page_symlink_and_parent_directory_symlink_are_rejected(self):
        document = self.fixture()
        original = self.page_path(document)
        link = original.with_name("linked.jpg")
        try:
            link.symlink_to(original)
        except OSError as exc:
            if getattr(exc, "winerror", None) != 1314:
                raise
            self.skipTest("Windows host lacks permission to create synthetic symlinks")
        document["pages"][0]["relative_path"] = "pages/linked.jpg"
        self.rejected(document)
        document["pages"][0]["relative_path"] = "linked-pages/001.jpg"
        (self.manifest.parent / "linked-pages").symlink_to(original.parent, target_is_directory=True)
        self.rejected(document)

    def test_hardlinked_page_or_manifest_is_rejected(self):
        document = self.fixture()
        link = self.root / "hardlink.jpg"
        try:
            os.link(self.page_path(document), link)
        except OSError as exc:
            self.skipTest("test host cannot create synthetic hardlinks: " + str(exc))
        self.rejected(document)
        link.unlink()
        os.link(self.manifest, self.root / "hardlinked-manifest.json")
        self.rejected(document)

    def test_windows_reparse_attribute_is_rejected_even_without_symlink_mode(self):
        document = self.fixture()
        original = Path.lstat
        target = self.page_path(document).parent
        def reparse(path, *args, **kwargs):
            if path == target:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "lstat", reparse):
            self.rejected(document)

    def test_full_image_decoding_magic_size_hash_and_dimensions_are_required(self):
        document = self.fixture()
        for key, value in (("sha256", "0" * 64), ("bytes", document["pages"][0]["bytes"] - 1),
                           ("bytes", document["pages"][0]["bytes"] + 1), ("width", 31), ("height", 19)):
            changed = copy.deepcopy(document)
            changed["pages"][0][key] = value
            with self.subTest(key=key):
                self.rejected(changed)
        good = self.page_path(document).read_bytes()
        for data in (good[:-10], b"\xff\xd8\xffnot-a-jpeg", b"%PDF-1.7\nnot-an-image"):
            changed = copy.deepcopy(document)
            self.replace_page(changed, data)
            with self.subTest(length=len(data)):
                self.rejected(changed)
        self.replace_page(document, good)
        changed = copy.deepcopy(document)
        changed["pages"][0]["format"] = "png"
        self.rejected(changed)

    def test_animated_png_and_permissive_truncated_decoder_are_rejected(self):
        document = self.fixture(1, "png")
        buffer = io.BytesIO()
        frames = [Image.new("RGB", (32, 18), color) for color in ("red", "blue")]
        frames[0].save(buffer, "PNG", save_all=True, append_images=frames[1:])
        self.replace_page(document, buffer.getvalue())
        self.rejected(document)
        document = self.fixture(1)
        with mock.patch("PIL.ImageFile.LOAD_TRUNCATED_IMAGES", True):
            self.rejected(document)

    def test_manifest_page_aggregate_bytes_and_decoded_pixel_budgets(self):
        document = self.fixture()
        total_bytes = self.manifest.stat().st_size + sum(r["bytes"] for r in document["pages"])
        for limit in ({"max_manifest_bytes": self.manifest.stat().st_size - 1},
                      {"max_image_bytes": document["pages"][0]["bytes"] - 1},
                      {"max_total_bytes": total_bytes - 1},
                      {"max_page_pixels": 32 * 18 - 1},
                      {"max_total_pixels": 3 * 32 * 18 - 1}):
            with self.subTest(limit=limit), mock.patch.dict(products.IMAGE_SET_LIMITS, **limit), \
                 mock.patch.object(products, "_decode_page", side_effect=AssertionError("must fail before decoding")):
                self.rejected(document)

    def test_retained_verifier_checks_all_dependencies_without_decode_or_directory_scan(self):
        document = self.fixture()
        result = self.inspect()
        with mock.patch("PIL.Image.open", side_effect=AssertionError("unexpected image decode")), \
             mock.patch.object(products.documents, "probe", side_effect=AssertionError("unexpected probe")), \
             mock.patch.object(Path, "iterdir", side_effect=AssertionError("unexpected scan")), \
             mock.patch.object(Path, "glob", side_effect=AssertionError("unexpected scan")), \
             mock.patch.object(Path, "rglob", side_effect=AssertionError("unexpected scan")):
            self.assertTrue(products.verify_retained_artifact(self.store, result))
            path = self.page_path(document, 2)
            original = path.read_bytes()
            path.write_bytes(b"x" + original[1:])  # Same size, different hash.
            self.assertFalse(products.verify_retained_artifact(self.store, result))
            path.write_bytes(original)
            path.unlink()
            self.assertFalse(products.verify_retained_artifact(self.store, result))
            path.write_bytes(original)
            self.assertTrue(products.verify_retained_artifact(self.store, result))
            self.manifest.write_bytes(self.manifest.read_bytes() + b" ")
            self.assertFalse(products.verify_retained_artifact(self.store, result))

    def test_retained_missing_duplicate_escape_over_budget_and_malformed_dependencies_fail(self):
        self.fixture()
        result = self.inspect()
        cases = []
        for value in (None, [], result["dependencies"][:-1], result["dependencies"] * 2):
            changed = copy.deepcopy(result)
            changed["dependencies"] = value
            cases.append(changed)
        changed = copy.deepcopy(result)
        del changed["dependencies"]
        cases.append(changed)
        for key, value in (("relative_path", "../outside.jpg"), ("relative_path", "other/page.jpg"),
                           ("size_bytes", True), ("size_bytes", 2**40), ("sha256", "invalid")):
            changed = copy.deepcopy(result)
            changed["dependencies"][0][key] = value
            cases.append(changed)
        changed = copy.deepcopy(result)
        changed["dependencies"][1] = changed["dependencies"][0]
        cases.append(changed)
        changed = copy.deepcopy(result)
        changed["format"] = "json"
        cases.append(changed)
        for value in (None, [], {}, *cases):
            with self.subTest(value_type=type(value).__name__):
                self.assertFalse(products.verify_retained_artifact(self.store, value))
        with mock.patch.dict(products.IMAGE_SET_LIMITS, max_total_bytes=1):
            self.assertFalse(products.verify_retained_artifact(self.store, result))

    def test_retained_verifier_rejects_a_page_replaced_with_a_symlink(self):
        document = self.fixture()
        result = self.inspect()
        path = self.page_path(document)
        target = self.root / "other.jpg"
        path.rename(target)
        try:
            path.symlink_to(target)
        except OSError as exc:
            if getattr(exc, "winerror", None) != 1314:
                raise
            self.skipTest("Windows host lacks permission to create synthetic symlinks")
        self.assertFalse(products.verify_retained_artifact(self.store, result))

    def test_retained_reparse_flags_fail_without_requiring_host_link_privileges(self):
        document = self.fixture()
        result = self.inspect()
        original = Path.lstat
        target = self.page_path(document)
        for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFREG, 0x400)):
            def linked(path, *args, **kwargs):
                if path == target:
                    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                return original(path, *args, **kwargs)
            with self.subTest(mode=mode), mock.patch.object(Path, "lstat", linked):
                self.assertFalse(products.verify_retained_artifact(self.store, result))

    def test_page_or_manifest_changed_during_initial_decode_is_not_retained(self):
        document = self.fixture()
        decode = products._decode_page
        first = self.page_path(document)
        def changing_page(data, row):
            if row["page"] == 2:
                raw = first.read_bytes()
                first.write_bytes(b"x" + raw[1:])
            return decode(data, row)
        with mock.patch.object(products, "_decode_page", side_effect=changing_page):
            self.rejected(document)
        document = self.fixture()
        def changing_manifest(data, row):
            if row["page"] == 1:
                self.manifest.write_bytes(self.manifest.read_bytes().replace(b"revision-1", b"revision-2"))
            return decode(data, row)
        with mock.patch.object(products, "_decode_page", side_effect=changing_manifest):
            self.rejected(document)

    def test_inspection_and_retained_checks_are_read_only_offline_and_scope_bounded(self):
        self.fixture()
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with mock.patch.object(Path, "write_bytes", side_effect=AssertionError("unexpected write")), \
             mock.patch.object(Path, "write_text", side_effect=AssertionError("unexpected write")), \
             mock.patch.object(Path, "mkdir", side_effect=AssertionError("unexpected mkdir")), \
             mock.patch.object(Path, "rglob", side_effect=AssertionError("unexpected directory scan")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("unexpected process")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("unexpected network")):
            result = self.inspect()
            self.assertTrue(result["readable"])
            self.assertEqual(result["metadata"]["files_written"], 0)
            self.assertTrue(products.verify_retained_artifact(self.store, result))
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def frame_fixture(self, count=3, fmt="jpeg", receipts=False):
        native = self.fixture(count, fmt)
        source = {**native["pages"][0]["source"], "account_ref": "synthetic-account", "size_bytes": 123456}
        del source["page_count"]
        source["frame_count"] = count
        frames = []
        for index, row in enumerate(native["pages"], 1):
            frame = {key: row[key] for key in ("relative_path", "sha256", "width", "height")}
            frame.update(sequence=index, size_bytes=row["bytes"], requested_ms=(index - 1) * 1000,
                         media_time_ms=(index - 1) * 1000 + 0.25, locator="requestVideoFrameCallback",
                         source_identity_check={"file_id_match": True, "version_token_match": True})
            if receipts:
                receipt = {"schema": "synthetic.source-frame-receipt.v1", "source": source, "frame": copy.deepcopy(frame)}
                data = json.dumps(receipt).encode()
                name = f"receipts/{index:03d}.json"
                path = self.manifest.parent / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(data)
                frame.update(receipt_relative_path=name, receipt_sha256=hashlib.sha256(data).hexdigest())
            frames.append(frame)
        document = {"schema": products.SOURCE_FRAMES_SCHEMA, "source": source,
                    "selection": {"evidence_ref": "synthetic-platform-locators.json",
                                  "basis": "platform_img_timestamps_and_native_caption_boundaries"},
                    "frames": frames}
        self.save(document)
        return document

    def test_source_video_frames_have_separate_representation_counts_and_no_coverage_claim(self):
        document = self.frame_fixture(receipts=True)
        result = self.inspect(document)
        self.assertTrue(result["readable"])
        self.assertEqual(result["format"], "image_set")
        self.assertEqual(result["metrics"]["frame_count"], 3)
        self.assertEqual(result["metrics"]["receipt_count"], 3)
        self.assertNotIn("page_count", result["metrics"])
        self.assertEqual(len(result["dependencies"]), 6)
        meta = result["metadata"]
        self.assertEqual(meta["image_set_schema"], "worldanalysis.source-video-frames.v1")
        self.assertEqual(meta["representation"], "remote_source_frame_capture")
        self.assertEqual(meta["route"], "source_video_frames")
        self.assertEqual(meta["declared_source"], document["source"])
        self.assertEqual(meta["declared_selection"], document["selection"])
        self.assertIs(meta["not_native_container_bytes"], True)
        for field in ("source_identity_verified", "source_frames_verified", "video_coverage_verified",
                      "timing_accuracy_verified", "receipt_semantics_verified", "selection_verified",
                      "capture_identity_claims_verified"):
            self.assertIs(meta[field], False, field)
        self.assertTrue(products.verify_retained_artifact(self.store, result))
        for dep in result["dependencies"]:
            path = self.root / dep["relative_path"]
            self.assertTrue(dep["relative_path"].startswith("capture/"))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), dep["sha256"])
            self.assertEqual(path.stat().st_size, dep["size_bytes"])

    def test_source_video_frames_minimal_source_and_optional_png_format_or_receipts(self):
        document = self.frame_fixture(2, "png")
        del document["source"]["account_ref"]
        del document["source"]["size_bytes"]
        document["frames"][0]["format"] = "png"
        result = self.inspect(document)
        self.assertTrue(result["readable"])
        self.assertEqual(result["metrics"]["receipt_count"], 0)
        self.assertEqual(len(result["dependencies"]), 2)
        self.assertTrue(products.verify_retained_artifact(self.store, result))

    def test_source_video_frame_count_and_continuous_sequences_are_not_file_count_coverage(self):
        document = self.frame_fixture()
        for frames in ([], None, document["frames"][:-1], list(reversed(document["frames"]))):
            changed = copy.deepcopy(document)
            changed["frames"] = frames
            self.rejected(changed)
        for count in (True, 2, 0):
            changed = copy.deepcopy(document)
            changed["source"]["frame_count"] = count
            self.rejected(changed)
        for sequence in (True, 1, 4):
            changed = copy.deepcopy(document)
            changed["frames"][1]["sequence"] = sequence
            self.rejected(changed)

    def test_source_video_frame_source_selection_and_rvfc_declarations_are_required(self):
        document = self.frame_fixture()
        for field, value in (("file_id", ""), ("path", "relative.mp4"), ("source_revision", ""),
                             ("account_ref", ""), ("size_bytes", True), ("size_bytes", -1)):
            changed = copy.deepcopy(document)
            changed["source"][field] = value
            self.rejected(changed)
        for field in ("file_id", "path", "source_revision", "frame_count"):
            changed = copy.deepcopy(document)
            del changed["source"][field]
            self.rejected(changed)
        for field in ("basis", "evidence_ref"):
            changed = copy.deepcopy(document)
            changed["selection"][field] = ""
            self.rejected(changed)
        for locator in ("currentTime", "screenShot", None):
            changed = copy.deepcopy(document)
            changed["frames"][0]["locator"] = locator
            self.rejected(changed)
        for field in ("file_id_match", "version_token_match"):
            for value in (False, 1, None):
                changed = copy.deepcopy(document)
                changed["frames"][0]["source_identity_check"][field] = value
                self.rejected(changed)
        changed = copy.deepcopy(document)
        changed["schema"] = products.IMAGE_SET_SCHEMA
        self.rejected(changed)

    def test_source_video_frame_times_are_finite_nonnegative_and_non_decreasing(self):
        document = self.frame_fixture()
        for field in ("requested_ms", "media_time_ms"):
            for value in (True, -1, "1000", None, float("inf"), float("nan"), 2**53):
                changed = copy.deepcopy(document)
                changed["frames"][1][field] = value
                self.rejected(changed)
            changed = copy.deepcopy(document)
            changed["frames"][2][field] = 0
            self.rejected(changed)
        # Readability retains the observed timing difference but cannot certify
        # that the browser sampled the intended instant or sufficient coverage.
        for frame in document["frames"]:
            frame["media_time_ms"] += 5000
        result = self.inspect(document)
        self.assertTrue(result["readable"])
        self.assertIs(result["metadata"]["timing_accuracy_verified"], False)
        self.assertIs(result["metadata"]["video_coverage_verified"], False)

    def test_source_video_frame_json_numeric_overflow_is_a_contract_error(self):
        document = self.frame_fixture()
        raw = json.dumps(document).replace('"requested_ms": 1000', '"requested_ms": 1e999', 1)
        self.manifest.write_text(raw, encoding="utf-8")
        with self.assertRaises(ContractError):
            self.inspect()

    def test_source_video_frame_images_use_the_same_hash_size_magic_decode_and_path_core(self):
        document = self.frame_fixture()
        for field, value in (("sha256", "0" * 64), ("size_bytes", 1), ("width", 31),
                             ("relative_path", "../escape.jpg"), ("format", "webp"), ("format", "png")):
            changed = copy.deepcopy(document)
            changed["frames"][0][field] = value
            self.rejected(changed)
        frame = document["frames"][0]
        path = self.manifest.parent / frame["relative_path"]
        for raw in (path.read_bytes()[:-10], b"\xff\xd8\xffnot-a-jpeg"):
            path.write_bytes(raw)
            frame.update(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
            self.rejected(document)

    def test_source_video_frame_receipts_are_paired_bounded_local_json_and_hash_checked(self):
        document = self.frame_fixture(receipts=True)
        for field, value in (("receipt_relative_path", "../escape.json"), ("receipt_relative_path", "receipts/001.jpg"),
                             ("receipt_sha256", "0" * 64), ("receipt_sha256", None)):
            changed = copy.deepcopy(document)
            changed["frames"][0][field] = value
            self.rejected(changed)
        for field in ("receipt_relative_path", "receipt_sha256"):
            changed = copy.deepcopy(document)
            del changed["frames"][0][field]
            self.rejected(changed)
        frame = document["frames"][0]
        path = self.manifest.parent / frame["receipt_relative_path"]
        for raw in (b"not-json", b"[]", b"", b" " * (products.MAX_FRAME_RECEIPT_BYTES + 1)):
            path.write_bytes(raw)
            frame["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
            self.rejected(document)

    def test_source_video_frame_receipts_and_images_both_count_towards_the_total_budget(self):
        document = self.frame_fixture(receipts=True)
        result = self.inspect(document)
        total = result["size_bytes"] + sum(d["size_bytes"] for d in result["dependencies"])
        with mock.patch.dict(products.IMAGE_SET_LIMITS, max_total_bytes=total - 1), \
             mock.patch.object(products, "_decode_page", side_effect=AssertionError("over-budget set decoded")):
            self.rejected(document)
        with mock.patch.dict(products.IMAGE_SET_LIMITS, max_total_bytes=total - 1):
            self.assertFalse(products.verify_retained_artifact(self.store, result))

    def test_source_video_frame_retained_verifier_hashes_receipts_without_decoding_or_scanning(self):
        document = self.frame_fixture(receipts=True)
        result = self.inspect()
        with mock.patch("PIL.Image.open", side_effect=AssertionError("unexpected image decode")), \
             mock.patch.object(Path, "rglob", side_effect=AssertionError("unexpected scan")):
            self.assertTrue(products.verify_retained_artifact(self.store, result))
            path = self.manifest.parent / document["frames"][1]["receipt_relative_path"]
            original = path.read_bytes()
            path.write_bytes(b"x" + original[1:])
            self.assertFalse(products.verify_retained_artifact(self.store, result))
            path.write_bytes(original)
            self.assertTrue(products.verify_retained_artifact(self.store, result))
            changed = copy.deepcopy(result)
            changed["dependencies"].pop()
            self.assertFalse(products.verify_retained_artifact(self.store, changed))
            changed = copy.deepcopy(result)
            changed["metrics"]["receipt_count"] = True
            self.assertFalse(products.verify_retained_artifact(self.store, changed))
            changed = copy.deepcopy(result)
            changed["metadata"]["image_set_schema"] = products.IMAGE_SET_SCHEMA
            self.assertFalse(products.verify_retained_artifact(self.store, changed))

    def test_source_video_frame_receipt_changed_during_decode_is_not_retained(self):
        document = self.frame_fixture(receipts=True)
        decode = products._decode_page
        path = self.manifest.parent / document["frames"][0]["receipt_relative_path"]
        def change_receipt(data, row):
            if row["sequence"] == 1:
                raw = path.read_bytes()
                path.write_bytes(b"x" + raw[1:])
            return decode(data, row)
        with mock.patch.object(products, "_decode_page", side_effect=change_receipt):
            self.rejected(document)

    def test_source_video_frame_inspection_with_receipts_is_read_only_and_offline(self):
        self.frame_fixture(receipts=True)
        with mock.patch.object(Path, "write_bytes", side_effect=AssertionError("unexpected write")), \
             mock.patch.object(Path, "write_text", side_effect=AssertionError("unexpected write")), \
             mock.patch.object(Path, "rglob", side_effect=AssertionError("unexpected scan")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("unexpected process")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("unexpected network")):
            result = self.inspect()
            self.assertTrue(result["readable"])
            self.assertTrue(products.verify_retained_artifact(self.store, result))


if __name__ == "__main__":
    unittest.main()
