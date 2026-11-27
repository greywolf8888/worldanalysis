"""Synthetic draft/page evidence -> immutable reading files and optional Store import.

No live service, credentials or production database are used.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from worldanalysis import course_delivery as delivery
from worldanalysis.contracts import ContractError, hash_file
from worldanalysis.platform_materials import import_materials
from worldanalysis.store import Store


class CourseDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="worldanalysis-course-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = "synthetic-project"
        self.file_id = "123456789"
        self.directory = self.root / "_ingest" / "synthetic-batch" / self.project / self.file_id
        (self.directory / "pages").mkdir(parents=True)
        self.source = {"file_id": self.file_id, "source_revision": "synthetic-content-r1", "page_count": 3,
                       "path": "/apps/教育分析学习/待分析/合成课/第一讲.mp4"}
        self.draft = {"schema": "worldanalysis.browser-draft.v1", "observed_at": "2026-11-15T18:10:00Z",
                      "method": "existing_platform_dom", "source": self.source, "sentence_count": 5,
                      "sentence_times": "not_exposed_in_dom", "extraction_scope": "all_current_dom_paragraphs",
                      "full_media_coverage": "requires_independent_review",
                      "paragraphs": [{"text": "  原话那幺。不要纠正。  ", "sentence_indices": [0, 1], "start_ms": 0},
                                     {"text": "第二段\n原有换行。", "sentence_indices": [2], "start_ms": 2500},
                                     {"text": "没有段落时间。", "sentence_indices": [3], "start_ms": None},
                                     {"text": "末段。", "sentence_indices": [4], "start_ms": 9000}]}
        self.pages = {"schema": "worldanalysis.rendered-native-pages.v1", "pages": []}
        for number in range(1, 4):
            stream = io.BytesIO()
            with Image.new("RGB", (24, 16), (number * 30, 80, 120)) as image:
                image.save(stream, "JPEG")
                pixels = image.convert("RGBA").tobytes()
            data = stream.getvalue()
            relative = f"pages/{number:03d}.jpg"
            (self.directory / relative).write_bytes(data)
            self.pages["pages"].append({"schema": "worldanalysis.browser-courseware-page.v1", "page": number,
                                         "source": copy.deepcopy(self.source), "relative_path": relative,
                                         "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                         "pixel_sha256": hashlib.sha256(pixels).hexdigest(), "width": 24, "height": 16,
                                         "format": "jpeg", "representation": "normally_displayed_platform_page",
                                         "not_native_container_bytes": True})
        self.metadata = {"schema": "worldanalysis.rendered-courseware-observation.v1", "errno": 0, "http_status": 200,
                         "source_file_id": self.file_id, "source_path": self.source["path"], "page_count": 3, "file_count": 2,
                         "observed_at": "2026-11-15T18:08:00Z",
                         "list": [{"start_page": 1, "end_page": 2, "img_timestamp": ["00:00:01", "00:00:04"],
                                   "endtime_timestamp": ["00:00:03", "00:00:04"], "file_size": 111, "file_md5": "0" * 32},
                                  {"start_page": 3, "end_page": 3, "img_timestamp": ["00:00:08.000"],
                                   "endtime_timestamp": ["00:00:10.125"], "file_size": 222, "file_md5": "1" * 32}]}
        self.draft_path = self.root / "draft.json"
        self.pages_path = self.directory / "rendered-pages.json"
        self.metadata_path = self.root / "metadata.json"
        self.write_inputs()

    def write_inputs(self):
        for path, value in ((self.draft_path, self.draft), (self.pages_path, self.pages), (self.metadata_path, self.metadata)):
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def prepare(self, **kwargs):
        return delivery.prepare_delivery(self.root, self.project, self.draft_path, self.pages_path, self.metadata_path, **kwargs)

    def arguments(self):
        return ["--root", str(self.root), "--project", self.project, "--draft", str(self.draft_path),
                "--pages-manifest", str(self.pages_path), "--courseware-metadata", str(self.metadata_path)]

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (hash_file(p), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()}

    def read_json(self, name):
        return json.loads((self.directory / name).read_text(encoding="utf-8"))

    def test_prepare_literal_text_actual_starts_and_unknowns_without_store(self):
        before = self.snapshot()
        with patch.object(delivery, "Store", side_effect=AssertionError("Store opened")), \
             patch("socket.create_connection", side_effect=AssertionError("network opened")):
            result = self.prepare()
        self.assertEqual(result["status"], "prepared")
        self.assertFalse(result["published"])
        expected = "\n\n".join(p["text"] for p in self.draft["paragraphs"]) + "\n"
        self.assertEqual((self.directory / "transcript.txt").read_bytes(), expected.encode("utf-8"))
        prose = (self.directory / "文案.txt").read_text(encoding="utf-8")
        for paragraph in self.draft["paragraphs"]:
            self.assertIn(paragraph["text"], prose)
        self.assertIn("未逐字校对", prose)
        self.assertIn("逐句开始/结束时间未可靠取得", prose)
        self.assertIn("start_ms=2500", prose)
        self.assertNotIn("--> ", prose)
        associations = self.read_json("时间页关联.json")
        self.assertEqual([p["start_ms"] for p in associations["paragraphs"]], [0, 2500, None, 9000])
        self.assertTrue(all(p["end_ms"] is None and p["speaker"] is None for p in associations["paragraphs"]))
        self.assertEqual([p["pages_covering_start"] for p in associations["paragraphs"]], [[], [1], [], [3]])
        self.assertEqual([(p["start_ms"], p["end_ms"]) for p in associations["pages"]], [(1000, 3000), (4000, 4000), (8000, 10125)])
        self.assertEqual([p["metadata_container_index"] for p in associations["pages"]], [0, 0, 1])
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["transcript"], {"path": "transcript.txt", "kind": "plain_transcript", "origin": "asr", "source_method": "existing_platform_dom"})
        self.assertNotIn("note", manifest)  # Local prose must not become provider_ai_note.
        self.assertTrue(all(p["source_kind"] == "claimed_video_frame" for p in manifest["images"]))
        self.assertEqual([p["timestamp_ms"] for p in manifest["images"]], [1000, 4000, 8000])
        self.assertTrue(all((self.directory / p["path"]).is_file() for p in manifest["images"]))
        meta = self.read_json("preparation-metadata.json")
        self.assertEqual(meta["quality"], "not_assessed")
        self.assertFalse(meta["remote_identity_verified"])
        self.assertFalse(meta["full_media_coverage_verified"])
        self.assertFalse(meta["images"]["browser_pixel_equivalence_verified"])
        self.assertFalse(meta["images"]["video_frame_verified"])
        self.assertFalse(list(self.root.rglob("*.srt")))
        self.assertFalse((self.root / "整理结果").exists())
        after = self.snapshot()
        self.assertTrue(all(after[path] == value for path, value in before.items()))
        links = (self.directory / "课件索引.md").read_text(encoding="utf-8")
        for page in manifest["images"]:
            self.assertIn("[图片](<" + page["path"] + ">)", links)

    def test_repeat_is_zero_write_and_move_only_adds_location_not_content(self):
        first = self.prepare()
        before = self.snapshot()
        repeated = self.prepare()
        self.assertEqual(repeated["created_files"], [])
        self.assertEqual(self.snapshot(), before)
        current = self.source["path"].replace("/待分析/", "/正在提取/")
        moved = self.prepare(source_path=current)
        self.assertEqual(len(moved["created_files"]), 2)
        self.assertTrue(all(n.startswith(("source-location-", "material-manifest-")) for n in moved["created_files"]))
        after = self.snapshot()
        self.assertTrue(all(after[path] == value for path, value in before.items()))
        self.assertNotEqual(first["manifest_path"], moved["manifest_path"])
        self.assertEqual(json.loads(Path(moved["manifest_path"]).read_text(encoding="utf-8"))["source"]["path"], current)
        location = json.loads(Path(moved["location_path"]).read_text(encoding="utf-8"))
        self.assertIsNone(location["observed_at"])
        self.assertEqual(location["observation_method"], "explicit_cli_input")
        self.assertEqual(self.read_json("preparation-metadata.json")["original_source_observations"]["draft"]["path"], self.source["path"])

    def test_edited_outputs_are_preserved_and_conflicts_preflight_all_files(self):
        self.prepare()
        prose = self.directory / "文案.txt"
        prose.write_text("个人编辑不能被覆盖", encoding="utf-8")
        (self.directory / "transcript.txt").unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(ContractError, "existing prepared output differs"):
            self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.directory / "transcript.txt").exists())

    def test_source_identity_and_stable_output_directory_are_enforced(self):
        originals = (copy.deepcopy(self.draft), copy.deepcopy(self.pages), copy.deepcopy(self.metadata))
        mutations = [lambda: self.pages["pages"][1]["source"].update(file_id="other-video"),
                     lambda: self.pages["pages"][0]["source"].update(source_revision="different-revision"),
                     lambda: self.metadata.update(source_file_id="different-video"),
                     lambda: self.draft["source"].update(file_id="wrong-output-directory"),
                     lambda: self.metadata.update(source_revision="conflicting-revision")]
        for mutate in mutations:
            self.draft, self.pages, self.metadata = copy.deepcopy(originals)
            mutate()
            self.write_inputs()
            with self.subTest(mutate=mutate), self.assertRaises(ContractError):
                self.prepare()
            self.assertFalse((self.directory / "transcript.txt").exists())
        self.draft, self.pages, self.metadata = copy.deepcopy(originals)
        self.write_inputs()
        with self.assertRaises(ContractError):
            delivery.prepare_delivery(self.root, "wrong-project", self.draft_path, self.pages_path, self.metadata_path)

    def test_noncontiguous_sentences_backwards_starts_and_bad_counts_fail(self):
        original = copy.deepcopy(self.draft)
        mutations = [lambda: self.draft["paragraphs"][1].update(sentence_indices=[1]),
                     lambda: self.draft.update(sentence_count=6),
                     lambda: self.draft["paragraphs"][3].update(start_ms=1),
                     lambda: self.draft["paragraphs"][0].update(start_ms=True),
                     lambda: self.draft["paragraphs"][0].update(text=""),
                     lambda: self.draft.update(method="newly_generated")]
        for mutate in mutations:
            self.draft = copy.deepcopy(original)
            mutate()
            self.write_inputs()
            with self.subTest(mutate=mutate), self.assertRaises(ContractError):
                self.prepare()
            self.assertFalse((self.directory / "transcript.txt").exists())

    def test_page_hash_decode_dimension_and_contiguous_order_checks(self):
        original = copy.deepcopy(self.pages)
        mutations = [lambda: self.pages["pages"][0].update(sha256="0" * 64),
                     lambda: self.pages["pages"][0].update(bytes=1),
                     lambda: self.pages["pages"][0].update(width=100),
                     lambda: self.pages["pages"][0].update(format="png"),
                     lambda: self.pages["pages"][0].update(page=2),
                     lambda: self.pages["pages"][0].update(relative_path="../draft.json"),
                     lambda: self.pages["pages"][0].update(not_native_container_bytes=False)]
        for mutate in mutations:
            self.pages = copy.deepcopy(original)
            mutate()
            self.write_inputs()
            with self.subTest(mutate=mutate), self.assertRaises(ContractError):
                self.prepare()
            self.assertFalse((self.directory / "transcript.txt").exists())
        self.pages = copy.deepcopy(original)
        damaged = b"not a decodable image"
        (self.directory / "pages/001.jpg").write_bytes(damaged)
        self.pages["pages"][0].update(sha256=hashlib.sha256(damaged).hexdigest(), bytes=len(damaged))
        self.write_inputs()
        with self.assertRaises(ContractError):
            self.prepare()

    def test_metadata_page_ranges_and_times_cannot_be_invented(self):
        original = copy.deepcopy(self.metadata)
        mutations = [lambda: self.metadata["list"][0].update(img_timestamp=["00:00:01"]),
                     lambda: self.metadata["list"][1].update(start_page=4),
                     lambda: self.metadata["list"][0]["endtime_timestamp"].__setitem__(0, "00:00:00"),
                     lambda: self.metadata["list"][0]["img_timestamp"].__setitem__(0, "00:99:01"),
                     lambda: self.metadata.update(page_count=2),
                     lambda: self.metadata.update(http_status=403)]
        for mutate in mutations:
            self.metadata = copy.deepcopy(original)
            mutate()
            self.write_inputs()
            with self.subTest(mutate=mutate), self.assertRaises(ContractError):
                self.prepare()
            self.assertFalse((self.directory / "transcript.txt").exists())

    def test_location_time_requires_explicit_offset_without_fabricated_default(self):
        with self.assertRaises(ContractError):
            self.prepare(source_path=self.source["path"], source_path_observed_at="2026-11-16T02:00:00")
        self.assertFalse((self.directory / "transcript.txt").exists())
        result = self.prepare(source_path=self.source["path"], source_path_observed_at="2026-11-16T02:00:00+08:00")
        self.assertEqual(json.loads(Path(result["location_path"]).read_text(encoding="utf-8"))["observed_at"], "2026-11-16T02:00:00+08:00")

    def test_duplicate_json_and_symlink_inputs_refused_before_writes(self):
        self.draft_path.write_text('{"schema":"one","schema":"two"}', encoding="utf-8")
        with self.assertRaises(ContractError):
            self.prepare()
        self.write_inputs()
        image = self.directory / "pages/001.jpg"
        target = self.root / "original.jpg"
        image.rename(target)
        try:
            image.symlink_to(target)
        except OSError as exc:
            self.skipTest("OS does not permit creating test symlinks: " + str(exc))
        with self.assertRaises(ContractError):
            self.prepare()
        self.assertFalse((self.directory / "transcript.txt").exists())

    def test_default_and_explicit_prepare_cli_never_open_store(self):
        for extra in ([], ["--prepare-only"]):
            with contextlib.redirect_stdout(io.StringIO()) as output, \
                 patch.object(delivery, "Store", side_effect=AssertionError("default opened Store")), \
                 patch("sqlite3.connect", side_effect=AssertionError("default opened sqlite")):
                code = delivery.main(self.arguments() + extra)
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(output.getvalue())["published"])
        self.assertFalse((self.root / "整理结果").exists())
        with contextlib.redirect_stderr(io.StringIO()) as error, \
             patch.object(delivery, "Store", side_effect=AssertionError("missing Store created")):
            code = delivery.main(self.arguments() + ["--publish"])
        self.assertEqual(code, 1)
        self.assertIn("existing Store", error.getvalue())
        self.assertFalse((self.root / "整理结果").exists())

    def test_real_importer_reuses_content_after_source_path_move(self):
        first = self.prepare()
        with Store(self.root) as store:
            store.create_project(project_id=self.project, topic="Synthetic course only")
            bundle = import_materials(store, self.project, first["manifest_path"])
            self.assertEqual(bundle["coverage"]["claimed_status"], "partial")
            self.assertEqual(len([a for a in bundle["artifacts"] if a["kind"] == "image"]), 3)
            self.assertTrue(any(b.get("origin") == "asr" and "那幺" in b.get("text", "") for b in bundle["blocks"]))
            moved = self.prepare(source_path=self.source["path"].replace("待分析", "正在提取"))
            again = import_materials(store, self.project, moved["manifest_path"])
            self.assertEqual(bundle["bundle_id"], again["bundle_id"])
            self.assertEqual(len(store.list_bundles(self.project)), 1)
            paths = store.connection.execute("SELECT path FROM platform_material_locations").fetchall()
            self.assertEqual(len(paths), 2)
            metadata_artifact = next(a for a in bundle["artifacts"] if "/metadata/" in a["relative_path"])
            imported_meta = json.loads((store.project_root(self.project) / metadata_artifact["relative_path"]).read_text(encoding="utf-8"))
            self.assertEqual(imported_meta["transcript_declaration"]["source_method"], "existing_platform_dom")
            self.assertFalse(imported_meta["source_identity_verified"])
            self.assertFalse(imported_meta["transcript_completeness_verified"])
            self.assertTrue(all(not p["video_frame_verified"] for p in imported_meta["image_source_declarations"]))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = delivery.main(self.arguments() + ["--publish"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["published"])

    def subtitle_fixture(self):
        cue_texts = ["  原话那幺。", "不要纠正。  ", "第二段\n原有换行。", "没有段落时间。", "末段。"]
        intervals = [(0, 1000), (1010, 2000), (2500, 3500), (6000, 7000), (9000, 9500)]
        content = "0\n00:00:00.000 --> 00:00:00.000\n此字幕由AI自动生成\n\n"
        for label, ((start, end), text) in enumerate(zip(intervals, cue_texts), 1):
            content += f"{label}\n{delivery._clock(start)} --> {delivery._clock(end)}\n{text}\n\n"
        response = {"schema": "worldanalysis.native-subtitle-response.v1", "acquisition": "passive_normal_page_response",
                    "http_status": 200, "transport": "xhr-blob", "content": content, "bytes": len(content.encode("utf-8")),
                    "source_file_id": self.file_id, "source_revision": self.source["source_revision"],
                    "source_path": self.source["path"].replace("待分析", "正在提取"), "observed_at": "2026-11-15T18:30:00Z"}
        path = self.root / "subtitle-response.json"
        path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        return path, response

    def test_original_subtitle_preserved_with_zero_cue_and_real_sentence_end_times(self):
        path, response = self.subtitle_fixture()
        before = self.snapshot()
        with patch.object(delivery, "Store", side_effect=AssertionError("prepare opened Store")):
            result = self.prepare(subtitle_response=path)
        self.assertEqual((self.directory / "native-subtitle.raw.srt").read_bytes(), response["content"].encode("utf-8"))
        self.assertTrue(result["raw_srt_retained"])
        self.assertFalse(result["srt_generated"])
        self.assertEqual(result["subtitle_cue_count"], 6)
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["transcript"]["path"], "native-subtitle.raw.srt")
        self.assertEqual(manifest["transcript"]["kind"], "timed_subtitle")
        self.assertEqual(manifest["source"]["path"], response["source_path"])
        location = json.loads(Path(result["location_path"]).read_text(encoding="utf-8"))
        self.assertEqual(location["observation_method"], "native_subtitle_response")
        self.assertEqual(location["observed_at"], response["observed_at"])
        associations = self.read_json("时间页关联.json")
        self.assertEqual([p["start_ms"] for p in associations["paragraphs"]], [0, 2500, None, 9000])
        self.assertEqual([p["end_ms"] for p in associations["paragraphs"]], [2000, 3500, 7000, 9500])
        self.assertEqual([s["end_ms"] for p in associations["paragraphs"] for s in p["sentences"]], [1000, 2000, 3500, 7000, 9500])
        self.assertEqual(associations["metadata_cues_retained_in_raw_srt"][0]["text"], "此字幕由AI自动生成")
        self.assertEqual(self.read_json("preparation-metadata.json")["subtitle"]["spoken_sentence_count"], 5)
        prose = (self.directory / "文案.txt").read_text(encoding="utf-8")
        self.assertIn("00:00:09.000–00:00:09.500", prose)
        self.assertIn("严格匹配 DOM 原文与段首", prose)
        self.assertTrue(all(self.snapshot()[name] == value for name, value in before.items()))
        with Store(self.root) as store:
            store.create_project(project_id=self.project, topic="Synthetic timed course")
            bundle = import_materials(store, self.project, result["manifest_path"])
            meta_artifact = next(a for a in bundle["artifacts"] if "/metadata/" in a["relative_path"])
            meta = json.loads((store.project_root(self.project) / meta_artifact["relative_path"]).read_text(encoding="utf-8"))
            self.assertEqual(len(meta["subtitle_cues"]), 6)  # Raw metadata cue 0 is not silently dropped.
            self.assertEqual(meta["subtitle_cues"][-1]["end_ms"], 9500)
            self.assertEqual(bundle["coverage"]["claimed_status"], "partial")
        snapshot = self.snapshot()
        self.assertEqual(self.prepare(subtitle_response=path)["created_files"], [])
        self.assertEqual(self.snapshot(), snapshot)

    def test_subtitle_source_bytes_literal_alignment_and_actual_times_required(self):
        path, original = self.subtitle_fixture()
        mutations = [lambda r: r.update(source_file_id="different-video"),
                     lambda r: r.update(source_revision="different-revision"),
                     lambda r: r.update(bytes=1),
                     lambda r: r.update(content=r["content"].replace("那幺", "那么")),
                     lambda r: r.update(content=r["content"].replace("00:00:02.500", "00:00:02.510")),
                     lambda r: r.update(content=r["content"].replace("00:00:03.500", "00:00:02.400"))]
        for mutate in mutations:
            response = copy.deepcopy(original)
            mutate(response)
            if response["bytes"] != 1:
                response["bytes"] = len(response["content"].encode("utf-8"))
            path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
            with self.subTest(mutate=mutate), self.assertRaises(ContractError):
                self.prepare(subtitle_response=path)
            self.assertFalse((self.directory / "transcript.txt").exists())

    def transcript_only_fixture(self):
        path, response = self.subtitle_fixture()
        destination = self.directory / "native-subtitle-response.json"
        path.rename(destination)
        return destination, response

    def test_transcript_only_cli_needs_no_draft_or_pages_and_preserves_raw_cues(self):
        response_path, response = self.transcript_only_fixture()
        self.draft_path.unlink()
        self.pages_path.unlink()
        self.metadata_path.unlink()
        args = ["--root", str(self.root), "--project", self.project, "--transcript-only", "--subtitle-response", str(response_path)]
        with contextlib.redirect_stdout(io.StringIO()) as output, \
             patch.object(delivery, "Store", side_effect=AssertionError("opened Store")), \
             patch("sqlite3.connect", side_effect=AssertionError("opened sqlite")):
            self.assertEqual(delivery.main(args), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["mode"], "transcript_only")
        self.assertEqual(result["sentence_count"], 5)
        self.assertEqual(result["subtitle_cue_count"], 6)
        self.assertIsNone(result["dom_paragraphs"])
        self.assertFalse(result["published"])
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertNotIn("images", manifest)
        self.assertNotIn("note", manifest)
        self.assertEqual(manifest["transcript"]["source_method"], "passive_normal_page_response")
        self.assertEqual((self.directory / "native-subtitle.raw.srt").read_bytes(), response["content"].encode("utf-8"))
        times = self.read_json("字幕时间关联.json")
        self.assertIsNone(times["dom_paragraphs"])
        self.assertEqual(len(times["spoken_cues"]), 5)
        self.assertTrue(all(c["speaker"] is None for c in times["spoken_cues"]))
        expected = "\n".join(c["text"] for c in times["spoken_cues"]) + "\n"
        self.assertEqual((self.directory / "transcript.txt").read_bytes(), expected.encode("utf-8"))
        prose = (self.directory / "文案.txt").read_text(encoding="utf-8")
        self.assertIn("没有读取/重构 DOM 段落", prose)
        self.assertIn("说话人未知", prose)
        self.assertNotIn("[段落", prose)
        self.assertFalse((self.root / "整理结果").exists())
        before = self.snapshot()
        self.assertEqual(delivery.prepare_transcript_only(self.root, self.project, response_path)["created_files"], [])
        self.assertEqual(self.snapshot(), before)
        with Store(self.root) as store:
            store.create_project(project_id=self.project, topic="Synthetic subtitle-only")
            bundle = import_materials(store, self.project, result["manifest_path"])
            self.assertEqual(bundle["coverage"]["claimed_status"], "partial")
            self.assertFalse(any(a["kind"] == "image" for a in bundle["artifacts"]))
            self.assertEqual(len([b for b in bundle["blocks"] if b.get("origin") == "asr" and b["kind"] == "paragraph"]), 6)
            moved = delivery.prepare_transcript_only(self.root, self.project, response_path,
                                                     source_path=response["source_path"].replace("正在提取", "分析完成"))
            reused = import_materials(store, self.project, moved["manifest_path"])
            self.assertEqual(bundle["bundle_id"], reused["bundle_id"])

    def test_transcript_only_does_not_change_an_existing_native_page_package(self):
        self.prepare()
        response_path, _ = self.transcript_only_fixture()
        before = self.snapshot()
        with self.assertRaisesRegex(ContractError, "existing prepared output differs"):
            delivery.prepare_transcript_only(self.root, self.project, response_path)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.directory / "transcript-only-metadata.json").exists())

    def test_transcript_only_rejects_wrong_source_directory_and_mixed_inputs(self):
        response_path, response = self.transcript_only_fixture()
        response["source_file_id"] = "different-source"
        response_path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ContractError):
            delivery.prepare_transcript_only(self.root, self.project, response_path)
        self.assertFalse((self.directory / "transcript.txt").exists())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(delivery.main(["--root", str(self.root), "--project", self.project, "--transcript-only"]), 1)
            self.assertEqual(delivery.main(self.arguments() + ["--transcript-only", "--subtitle-response", str(response_path)]), 1)

    def source_frame_fixture(self):
        capture = self.root / "采集输入" / "platform-source-frames" / "synthetic-job"
        (capture / "images").mkdir(parents=True)
        (capture / "receipts").mkdir()
        requested = [0, 2500, 9000]
        actual = [0.25, 2500.125, 9000.75]
        frames = []
        for sequence, (target, media_time) in enumerate(zip(requested, actual), 1):
            data = (self.directory / f"pages/{sequence:03d}.jpg").read_bytes()  # Small synthetic fixture pixels only.
            relative = f"images/{sequence:03d}.jpg"
            (capture / relative).write_bytes(data)
            receipt = json.dumps({"synthetic": True, "media_time_ms": media_time}).encode("utf-8")
            receipt_name = f"receipts/{sequence:03d}.json"
            (capture / receipt_name).write_bytes(receipt)
            frames.append({"sequence": sequence, "relative_path": relative, "sha256": hashlib.sha256(data).hexdigest(),
                           "size_bytes": len(data), "width": 24, "height": 16, "format": "jpeg",
                           "requested_ms": target, "media_time_ms": media_time, "locator": "requestVideoFrameCallback",
                           "source_identity_check": {"file_id_match": True, "version_token_match": True},
                           "receipt_relative_path": receipt_name, "receipt_sha256": hashlib.sha256(receipt).hexdigest()})
        manifest = {"schema": "worldanalysis.source-video-frames.v1",
                    "source": {k: v for k, v in self.source.items() if k != "page_count"},
                    "selection": {"evidence_ref": "synthetic-evidence", "basis": "Synthetic scoped frame validation only"}, "frames": frames}
        manifest["source"]["frame_count"] = 3
        path = capture / "frames.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return path, manifest

    def test_source_frame_manifest_keeps_actual_times_paths_and_unverified_quality(self):
        path, frame_manifest = self.source_frame_fixture()
        subtitle_path, response = self.subtitle_fixture()
        before = self.snapshot()
        with patch.object(delivery, "Store", side_effect=AssertionError("opened Store")), \
             patch("sqlite3.connect", side_effect=AssertionError("opened sqlite")):
            result = delivery.prepare_source_frames(self.root, self.project, path, subtitle_response=subtitle_path)
        self.assertEqual(result["mode"], "source_frames")
        self.assertEqual(result["frame_count"], 3)
        self.assertNotIn("page_count", result)
        self.assertEqual(result["quality"], "not_assessed")
        self.assertTrue(all(self.snapshot()[name] == value for name, value in before.items()))
        self.assertEqual(len(list(path.parent.rglob("*.jpg"))), 3)
        self.assertEqual((path.parent / "native-subtitle.raw.srt").read_bytes(), response["content"].encode("utf-8"))
        imported = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual([p["path"] for p in imported["images"]], [f["relative_path"] for f in frame_manifest["frames"]])
        self.assertEqual([p["timestamp_ms"] for p in imported["images"]], [0, 2500, 9000])
        self.assertTrue(all(p["source_kind"] == "claimed_video_frame" for p in imported["images"]))
        times = json.loads((path.parent / "source-frame-times.json").read_text(encoding="utf-8"))
        self.assertEqual([f["media_time_ms"] for f in times["frames"]], [0.25, 2500.125, 9000.75])
        self.assertEqual([f["spoken_cues_covering_media_time"] for f in times["frames"]], [["1"], ["3"], ["5"]])
        self.assertFalse(times["timing_accuracy_verified"])
        self.assertFalse(times["video_coverage_verified"])
        index = (path.parent / "源帧索引.md").read_text(encoding="utf-8")
        self.assertIn("远程源帧，非原生PPT", index)
        self.assertIn("2500.125", index)
        self.assertNotIn("原生课件页面", index)
        meta = json.loads((path.parent / "source-frame-preparation.json").read_text(encoding="utf-8"))
        self.assertFalse(meta["images_copied"])
        self.assertFalse(meta["local_inspection"]["metadata"]["source_frames_verified"])
        after = self.snapshot()
        self.assertEqual(delivery.prepare_source_frames(self.root, self.project, path, subtitle_response=subtitle_path)["created_files"], [])
        self.assertEqual(self.snapshot(), after)
        with Store(self.root) as store:
            store.create_project(project_id=self.project, topic="Synthetic remote source frames")
            bundle = import_materials(store, self.project, result["manifest_path"])
            self.assertEqual(len([a for a in bundle["artifacts"] if a["kind"] == "image"]), 3)
            self.assertEqual(bundle["coverage"]["claimed_status"], "partial")

    def test_source_frame_partial_tampered_and_wrong_source_inputs_fail_before_output(self):
        path, original = self.source_frame_fixture()
        subtitle, response = self.subtitle_fixture()
        for mutate in (lambda value: value["source"].update(frame_count=4),
                       lambda value: value["frames"][1].update(media_time_ms=-1),
                       lambda value: value["frames"][0].update(sha256="0" * 64),
                       lambda value: value["frames"][0].update(receipt_sha256="0" * 64)):
            changed = copy.deepcopy(original)
            mutate(changed)
            path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
            with self.subTest(mutate=mutate), self.assertRaises(ContractError):
                delivery.prepare_source_frames(self.root, self.project, path, subtitle_response=subtitle)
            self.assertFalse((path.parent / "源帧索引.md").exists())
            self.assertFalse((path.parent / "native-subtitle.raw.srt").exists())
        path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
        response["source_file_id"] = "wrong-subtitle-source"
        subtitle.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ContractError):
            delivery.prepare_source_frames(self.root, self.project, path, subtitle_response=subtitle)

    def test_module_cli_outputs_json_without_database(self):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        run = subprocess.run([sys.executable, "-B", "-m", "worldanalysis.course_delivery", *self.arguments()],
                             cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["source_file_id"], self.file_id)
        self.assertTrue(Path(result["manifest_path"]).is_file())
        self.assertFalse((self.root / "整理结果").exists())


if __name__ == "__main__":
    unittest.main()
