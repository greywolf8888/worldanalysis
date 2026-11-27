"""Integration coverage for fresh course folders, independent of the archived batch."""
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
from struct import pack
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "_tools/course_production/course.py"
ENTRY = Path("课件/00 开始学习.md")
OBSIDIAN = os.environ.get("WORLDANALYSIS_TEST_OBSIDIAN")


class CourseProductionTest(unittest.TestCase):
    def test_hidden_provenance_round_trips_attachment_punctuation(self):
        with patch.object(sys, "path", [str(CLI.parent), *sys.path]):
            from acceptance import validate_supplements
            from publish import hidden_source
            from reader_review import validate_cases

        label = "[26]--图示&练习.pdf · 第 1 页"
        comment = hidden_source("段落出处", label)
        self.assertNotIn("--", comment.removeprefix("<!--").removesuffix("-->"))
        self.assertIn("[26]&#45;&#45;图示&amp;练习.pdf", comment)
        supplements = [{"anchor": "练习见附页", "sources": [label]}]
        validate_supplements(f"练习见附页。 {comment}", supplements, supplements)

        body = f"## 示例练习\n\n准备图示 打开面板 填入参数 读取结果 纠正尺寸 {comment}"
        case = {
            "goal": "照原附页复核尺寸", "chapter": "示例练习", "sources": [label],
            "checks": {"input": "准备图示", "operation": "打开面板", "setting": "填入参数",
                       "result": "读取结果", "recovery": "纠正尺寸"},
            "visual_note": "页中的尺寸和示例文字可跟做。",
        }
        validate_cases(body, [case])
        with self.assertRaisesRegex(ValueError, "无法定位"):
            validate_cases(body, [{**case, "sources": [label.replace("第 1 页", "第 2 页")]}])

    def test_reused_footage_requires_a_visible_target_and_current_clip_proof(self):
        with patch.object(sys, "path", [str(CLI.parent), *sys.path]):
            from acceptance import validate_shared_visual

        chapter = "第 1 讲 打开比例菜单"
        figure = "菜单.png"
        proof = {"path": "第2讲复看/原画对照.json", "sha256": "a" * 64}
        episode = {"display_number": 2, "visual_basis": "复用同一原画", "checked_sources": [proof],
                   "shared_visual": {"chapter": chapter, "figure": figure, "proof": proof}}
        figures = {figure: {"lesson": 7}}
        earlier = {chapter: {"lesson": 7, "figures": {figure}}}
        passage = f"参数选项沿用原界面，见[[#{chapter}|前一讲的比例菜单原图]]。"
        validate_shared_visual(episode, passage, figures, earlier)

        with self.assertRaisesRegex(ValueError, "指纹化"):
            validate_shared_visual({**episode, "checked_sources": []}, passage, figures, earlier)
        with self.assertRaisesRegex(ValueError, "前文已插入"):
            validate_shared_visual(episode, passage, figures, {})
        with self.assertRaisesRegex(ValueError, "读者定位"):
            validate_shared_visual(episode, f"<!-- {passage} -->", figures, earlier)

    def test_legacy_word_picture_inventory_keeps_original_bytes_and_separate_citations(self):
        from PIL import Image
        with patch.object(sys, "path", [str(CLI.parent), *sys.path]):
            import prepare_documents
            from source_coverage import document_locator_valid, document_units, unit_matches
            from word_pictures import scan_word_blips

        def image_record(record_type, instance, format_name, color, uid_count):
            content = BytesIO()
            Image.new("RGB", (8, 5), color).save(content, format=format_name)
            payload = b"\x00" * 16 * uid_count + b"\xff" + content.getvalue()
            return pack("<HHI", instance << 4, record_type, len(payload)) + payload, content.getvalue()

        jpeg, original = image_record(0xF01D, 0x46A, "JPEG", "navy", 1)
        png, original_png = image_record(0xF01E, 0x6E1, "PNG", "maroon", 2)
        pictures, gaps = scan_word_blips([("Data", b"\xff\xd8\xff" + jpeg + jpeg), ("1Table", png)])
        self.assertFalse(gaps)
        self.assertEqual([payload for _, payload in pictures], [original, original_png])
        self.assertEqual([row["picture_number"] for row, _ in pictures], [1, 2])
        self.assertEqual(len(pictures[0][0]["occurrences"]), 2)
        document = {"units": document_units({"metadata": {"word_pictures": [row for row, _ in pictures]}})}
        self.assertEqual([row["key"] for row in document["units"]], ["picture:1", "picture:2", "file"])
        self.assertTrue(unit_matches(document["units"][0], "第 1 幅内嵌原图，图左上角"))
        self.assertFalse(unit_matches(document["units"][1], "第 1 幅内嵌原图"))
        self.assertTrue(document_locator_valid(document, "正文 · 发稿参数"))
        self.assertFalse(document_locator_valid(document, "第 3 幅内嵌原图"))
        _, truncated = scan_word_blips([("Data", jpeg[:-16])])
        self.assertIn("超出原件数据范围", truncated[0]["reason"])
        with (patch.object(prepare_documents, "legacy_doc_text", side_effect=ValueError("字体表无法读取")),
              patch.object(prepare_documents, "read_word_pictures", return_value=(pictures, []))):
            partial = prepare_documents.legacy_doc(self.source / "讲义.doc")
        self.assertEqual(partial["processing_status"], "partial")
        self.assertEqual(len(partial["metadata"]["word_pictures"]), 2)
        self.assertIn("文字提取需要补查", partial["gaps"][0]["reason"])

    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.source = self.root / "课程原件"
        self.source.mkdir()
        self.package = self.root / "新教材"
        self.work = self.package / ".course/work"
        self.ledger = self.package / ".course/ledger"
        self.env = {**os.environ, "PYTHONUTF8": "1"}
        for setting in ("WORLDANALYSIS_COURSE_ASR_EXE", "WORLDANALYSIS_COURSE_SENSEVOICE_MODEL",
                        "WORLDANALYSIS_COURSE_WHISPER_MODEL"):
            self.env.pop(setting, None)

    def command(self, action, *arguments, okay=True):
        output = subprocess.run([sys.executable, "-B", str(CLI), action, *map(str, arguments)],
                                cwd=self.root, env=self.env, text=True, encoding="utf-8",
                                capture_output=True, timeout=160)
        self.assertEqual(output.returncode == 0, okay, output.stdout + output.stderr)
        return output.stdout + output.stderr

    def data(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, path, data):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def make_guide(self):
        from PIL import Image, ImageDraw

        guide = self.source / "A 操作指南.pdf"
        image = Image.new("RGB", (850, 500), "white")
        draw = ImageDraw.Draw(image)
        draw.text((50, 55), "Canvas export guide", fill="black", font_size=34)
        draw.text((50, 160), "Ratio 3:4   Export preview", fill="black", font_size=27)
        draw.rectangle((500, 80, 720, 360), outline="navy", width=4)
        image.save(guide)
        return guide

    def start_course(self):
        self.command("start", self.source, "--output", self.package, "--workers", 2)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要本地 FFmpeg")
    def test_presentation_embedded_video_is_an_original_media_source(self):
        standalone = self.source / "01 无声录屏.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=navy:s=96x96:d=1", "-an",
                        "-c:v", "libx264", str(standalone)], check=True, timeout=20)
        slide_video = self.root / "演示页录屏.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=orange:s=96x96:d=1", "-an",
                        "-c:v", "libx264", str(slide_video)], check=True, timeout=20)
        video = slide_video.read_bytes()
        deck = self.source / "演示讲义.pptx"
        relationship = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        xmlns = "http://schemas.openxmlformats.org/presentationml/2006/main"
        with ZipFile(deck, "w") as book:
            book.writestr("ppt/presentation.xml",
                          f'<p:presentation xmlns:p="{xmlns}" xmlns:r="{relationship}">'
                          '<p:sldIdLst><p:sldId r:id="rId1"/></p:sldIdLst></p:presentation>')
            book.writestr("ppt/_rels/presentation.xml.rels",
                          f'<Relationships><Relationship Id="rId1" Type="{relationship}/slide" '
                          'Target="slides/slide3.xml"/></Relationships>')
            book.writestr("ppt/slides/slide3.xml", "<slide><t>滤镜合成</t></slide>")
            book.writestr("ppt/slides/_rels/slide3.xml.rels", "<Relationships>"
                          f'<Relationship Id="rId1" Type="{relationship}/video" Target="../media/media1.mp4"/>'
                          f'<Relationship Id="rId2" Type="{relationship}/media" Target="../media/media1.mp4"/>'
                          "</Relationships>")
            book.writestr("ppt/media/media1.mp4", video)

        self.start_course()
        media_file = self.ledger / "media-summary.json"
        media = self.data(media_file)
        first, second = media["videos"]
        self.assertEqual(first["source"], standalone.name)
        self.assertEqual(second["source"], "演示讲义.pptx!ppt/media/media1.mp4")
        self.assertEqual(second["embedded"]["pages"], [1])
        self.assertEqual(second["sha256"], hashlib.sha256(video).hexdigest())
        clip = self.work / second["embedded"]["work_file"]
        self.assertEqual(clip.read_bytes(), video)
        self.assertEqual(self.data(self.ledger / "documents-summary.json")["total"], 1)
        self.assertEqual(len(self.data(self.ledger / "intake.json")["sources"]), 2)
        self.save(self.work / "讲次顺序.json", [1])
        self.command("prepare", self.package)
        self.assertEqual(self.data(self.work / "讲次顺序.json"), [1, 2])
        self.pick([{"name": "内嵌动作", "lesson": 2, "time": .4}])
        (self.work / "教材.md").write_text(
            "# 滤镜合成\n\n## 第 1 讲 准备\n\n打开画板。{{01|00:00–00:01}}\n\n"
            "## 第 2 讲 试看\n\n把滤镜配到画面。{{02|00:00–00:01}} {{D001|第 1 页}}\n\n"
            "{{img:内嵌动作|450}}\n", encoding="utf-8")
        self.command("preview", self.package)
        preview = (self.work / "阅读预览" / ENTRY).read_text(encoding="utf-8")
        self.assertIn("演示讲义.pptx · 第 1 页内嵌视频", preview)
        episode = self.data(self.work / "验收填写模板.json")["episodes"][1]
        self.assertEqual(episode["embedded"], second["embedded"])

        self.save(media_file, {**media, "videos": [first]})
        self.assertIn("教学媒体和归类结果不一致", self.command("preview", self.package, okay=False))
        self.save(media_file, media)
        clip.write_bytes(b"changed")
        self.assertIn("原视频身份已变", self.command("preview", self.package, okay=False))
        self.command("prepare", self.package)
        self.assertEqual(clip.read_bytes(), video)
        self.command("preview", self.package)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要本地 FFmpeg")
    def test_unreadable_video_leaves_a_gap_and_still_prepares_later_sources(self):
        damaged = self.source / "01 缺少索引.mp4"
        damaged.write_bytes(bytes.fromhex("000000146674797069736f6d0000020069736f6d000000086d646174"))
        valid = self.source / "02 正常无声录屏.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=96x96:d=1", "-an",
                        "-c:v", "libx264", str(valid)], check=True, timeout=20)

        output = self.command("start", self.source, "--output", self.package, "--workers", 1, okay=False)
        self.assertIn(damaged.name, output)
        summary = self.data(self.ledger / "media-summary.json")
        unreadable, = summary["unreadable"]
        self.assertEqual((unreadable["lesson"], unreadable["source"]), (1, damaged.name))
        self.assertEqual(unreadable["sha256"], hashlib.sha256(damaged.read_bytes()).hexdigest())
        self.assertIn("moov", unreadable["reason"])
        self.assertEqual(self.data(self.work / "讲次顺序.json"), [1, 2])
        self.assertEqual(self.data(self.work / "媒体补查/L02.json")["source"], valid.name)
        self.assertTrue((self.work / "采用转写/02.json").is_file())
        self.assertEqual((self.package / "原始资料" / damaged.name).read_bytes(), damaged.read_bytes())

        self.save(self.work / "讲次顺序.json", [2])
        output = self.command("preview", self.package, okay=False)
        self.assertIn("原视频仍有取材缺口", output)
        self.assertIn(damaged.name, output)
        self.assertFalse((self.work / "验收填写模板.json").exists())

        recovery = self.work / "媒体恢复"
        recovery.mkdir()
        copy = recovery / "01 可读副本.mp4"
        shutil.copy2(valid, copy)
        scope = "这一段使用恢复画面，原时间轴不可核实。"
        self.save(recovery / "01.json", {
            "original_sha256": unreadable["sha256"], "file": copy.name,
            "sha256": hashlib.sha256(copy.read_bytes()).hexdigest(), "method": "测试夹具",
            "scope": scope,
        })
        self.save(self.work / "媒体设置.json", {"1": {"recovery": "01.json"}})
        self.save(self.work / "讲次顺序.json", [1, 2])
        self.command("prepare", self.package, "--workers", 1)
        self.assertNotIn("unreadable", self.data(self.ledger / "media-summary.json"))
        self.assertEqual(self.data(self.ledger / "01.json")["source"], damaged.name)
        self.assertEqual(self.data(self.work / "采用转写/01.json")["recovery"]["scope"], scope)
        self.command("window", self.package, 1, 0, .7, "--label", "恢复画面")
        self.pick([{"name": "恢复帧", "lesson": 1, "time": .2}])
        (self.work / "教材.md").write_text(
            f"# 恢复画面\n\n## 第 1 讲 恢复内容\n\n{scope} {{{{01|00:00–00:01}}}}\n\n"
            "{{img:恢复帧|360}}\n\n## 第 2 讲 正常内容\n\n查看原图。 {{02|00:00–00:01}}\n", encoding="utf-8")
        self.command("preview", self.package)
        template = self.data(self.work / "验收填写模板.json")
        evidence = template["snapshot"]["evidence"]
        self.assertIn("work/媒体恢复/01.json", evidence)
        self.assertIn(f"work/媒体恢复/{copy.name}", evidence)
        self.assertEqual(template["episodes"][0]["recovery"]["scope"], scope)

        copy.write_bytes(b"changed")
        output = self.command("preview", self.package, okay=False)
        self.assertIn("恢复媒体缺失或已变动", output)
        self.assertEqual((self.package / "原始资料" / damaged.name).read_bytes(), damaged.read_bytes())

    def test_gpu_silence_warning_applies_to_its_own_decoded_window(self):
        with patch.object(sys, "path", [str(CLI.parent), *sys.path]):
            from _tools.course_production import gpu_asr

        gpu = gpu_asr.VulkanASR.__new__(gpu_asr.VulkanASR)
        gpu.backend = "sensevoice"
        gpu.device = {"index": 0}
        gpu.executable = self.root / "crispasr"
        gpu.model = self.root / "model"
        windows = [(0, 1, 0), (1, 1, 1)]

        pcm_samples = {0: bytes(32000), 1: b"\x01\x00" * 16000}

        def decode(source, start, length, mode, track):
            self.assertEqual((source.name, length, mode, track), ("source.mp4", 1, "fixed", 0))
            data = pcm_samples[start]
            yield start, data[:16000]
            yield start + .5, data[16000:]

        def runner(warning):
            def run(command, **options):
                for i, start in enumerate(pcm_samples):
                    with wave.open(str(Path(options["cwd"]) / f"transcript-{i:02}.wav")) as sound:
                        self.assertEqual(sound.getframerate(), 16000)
                        self.assertEqual(sound.readframes(sound.getnframes()), pcm_samples[start])
                output = Path(options["cwd"]) / "transcript-01.json"
                self.save(output, {"transcription": [{"offsets": {"from": 100, "to": 480}, "text": "演示"}]})
                return subprocess.CompletedProcess(command, 0, b"", warning)
            return run

        with patch.object(gpu_asr, "RUNTIME", self.root), patch.object(gpu_asr, "audio_windows", side_effect=decode):
            with patch.object(gpu_asr.subprocess, "run",
                              side_effect=runner(b"crispasr: warning: no speech detected in 'transcript-00.wav'\n")):
                result = gpu.transcribe_windows(self.root / "source.mp4", 2, 0, "zh", windows)
            self.assertEqual(result, [[], [{"start": 1.1, "end": 1.48, "text": "演示"}]])
            self.assertEqual([row["samples"] for row in gpu.decoded_windows], [16000, 16000])
            with patch.object(gpu_asr.subprocess, "run",
                              side_effect=runner(b"crispasr: warning: no speech detected in 'transcript-01.wav'\n")):
                with self.assertRaisesRegex(ValueError, "未写入转写文件"):
                    gpu.transcribe_windows(self.root / "source.mp4", 2, 0, "zh", windows)

    def pick(self, records):
        selection = self.work / "取图清单.json"
        self.save(selection, records)
        self.command("keep", self.package, selection)

    def follow_along(self, heading, sources, figures):
        return {
            "goal": "从空白画板导出预览", "chapter": heading,
            "checks": {"input": "准备空白画板", "operation": "打开比例菜单",
                       "setting": "选 3:4", "result": "保存预览", "recovery": "重新选择比例"},
            "sources": sources, "figures": figures,
        }

    def complete_claims(self, review):
        for claim in review["source_coverage"]["claims"]:
            if claim["sources"]:
                claim.update(outcome="原件已核", note="已按相邻出处对照本样本的原始操作或讲义说明")
            else:
                self.assertIn("练习", claim["text"])
                claim.update(outcome="整理示例", note="这一段是作者明确标记的练习步骤")

    def test_archive_member_retains_unicode_path_and_content(self):
        tool = shutil.which("bsdtar") or shutil.which("tar")
        if not tool or b"bsdtar" not in subprocess.check_output([tool, "--version"], timeout=10):
            self.skipTest("libarchive tar is needed to build a 7z fixture offline")
        inputs = self.root / "附件样例"
        text = "从正面拍一张，再从侧面拍一张。"
        member = Path("练习/镜头规则.txt")
        (inputs / member.parent).mkdir(parents=True)
        (inputs / member).write_text(text, encoding="utf-8")
        source = self.source / "随课练习.7z"
        subprocess.run([tool, "-cf", str(source), "--format", "7zip", "-C", str(inputs),
                        member.as_posix()], check=True, timeout=10)
        old_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        (self.source / "零字节.rar").write_bytes(b"")
        self.assertIn("零字节.rar", self.command("start", self.source, "--output", self.package, okay=False))
        docs = self.data(self.ledger / "documents-summary.json")["records"]
        doc = next(record for record in docs if record["file"] == source.name)
        failed = next(record for record in docs if record["file"] == "零字节.rar")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("文件头与扩展名不一致", failed["gaps_detail"][0]["reason"])
        self.assertIsNone(failed["result"])
        result = self.data(self.work / doc["result"])
        self.assertEqual(doc["sha256"], old_digest)
        self.assertEqual(doc["status"], "extracted")
        self.assertEqual((self.package / "原始资料" / source.name).read_bytes(), source.read_bytes())
        self.assertEqual(result["blocks"][0]["locator"]["member_path"], member.as_posix())
        self.assertEqual(result["blocks"][0]["text"], text)
        record = result["archive_members"][0]
        retained = self.work / result["asset_root"] / record["file"]
        self.assertEqual(retained.read_text(encoding="utf-8"), text)
        self.assertEqual(hashlib.sha256(retained.read_bytes()).hexdigest(), record["sha256"])
        self.assertIn("零字节.rar", self.command("prepare", self.package, okay=False))
        self.assertEqual(self.data(self.ledger / "documents-summary.json")["records"], docs)

    def test_html_handout_preserves_sections_links_and_unread_visuals(self):
        source = self.source / "随课操作指南.htm"
        html = (
            '<title>随课指南</title><section id="practice"><h2>课堂练习</h2>'
            '<p>先打开 <strong>原件</strong>，再 <a href="例题.pdf?part=1&amp;page=3">看练习</a>。</p>'
            '<table><tr><th>字段</th><th>限制</th></tr><tr><td>比例</td><td>3:4</td></tr></table>'
            '<img src="步骤.png" alt="导出按钮">'
            '<template><template>内部占位</template><p>未使用的模板</p></template>'
            '<script>document.write("脚本模拟正文");</script><p>先核导出结果。</p></section>'
        )
        source.write_text(html, encoding="utf-8")
        self.start_course()
        self.assertEqual(source.read_bytes(), (self.package / "原始资料" / source.name).read_bytes())
        doc, = self.data(self.ledger / "documents-summary.json")["records"]
        self.assertTrue(doc["result"].endswith("-htm-static.json"))
        self.assertEqual(doc["status"], "partial")
        result = self.data(self.work / doc["result"])
        texts = [row["text"] for row in result["blocks"]]
        self.assertIn("比例 | 3:4", texts)
        self.assertIn("先打开 原件，再 看练习。", texts)
        self.assertIn("先核导出结果。", texts)
        self.assertIn("导出按钮 · 步骤.png", texts)
        self.assertFalse(any("占位" in line or "模拟正文" in line or "未使用的模板" in line for line in texts))
        link, = [row for row in result["blocks"] if row["locator"].get("field") == "href"]
        self.assertEqual(link["text"], "例题.pdf?part=1&page=3")
        self.assertEqual(link["locator"]["sections"], ["practice"])
        self.assertEqual({g["locator"]["element"] for g in result["gaps"]}, {"img", "template"})
        self.command("prepare", self.package)
        self.assertEqual(self.data(self.ledger / "documents-summary.json")["records"], [doc])

    def publish(self, expected_videos):
        self.assertFalse((self.package / ENTRY).exists())
        if OBSIDIAN:
            self.command("accept", self.package, "--obsidian-exe", OBSIDIAN)
            verified = self.command("verify", self.package)
            self.assertIn(f'"videos": {expected_videos}', verified)
            self.assertTrue(json.loads(verified)["package_verified"])
            self.assertFalse(json.loads(verified)["verified"])
            self.assertIn("尚未交付到日常", self.command("verify", self.package, "--require-vault", okay=False))
            self.assertEqual(list((self.package / "课件").rglob("*.md")), [self.package / ENTRY])
            self.assertIn("<!-- 段落出处", (self.package / ENTRY).read_text(encoding="utf-8"))
        else:
            missing = self.root / "不存在的 Obsidian.exe"
            self.assertIn(missing.name, self.command("accept", self.package, "--obsidian-exe", missing, okay=False))
            self.assertFalse((self.package / ENTRY).exists())

    def test_document_only_intake_preview_and_publication(self):
        guide = self.make_guide()
        resource = self.source / "B 课堂练习80%&参考.url"
        url = "https://course.example.invalid/practice?q=%E7%BB%83"
        resource.write_text(f"[InternetShortcut]\nURL={url}\n", encoding="utf-16")
        self.start_course()
        self.assertEqual(guide.read_bytes(), (self.package / "原始资料" / guide.name).read_bytes())
        figure = "比例图,预览@原图"
        self.pick([{"name": figure, "document": "D001", "page": 1, "scale": 1.2}])
        draft = self.work / "教材.md"
        draft.write_text(
            "# 画板导出\n\n## 设好画板比例\n\n"
            "我先准备空白画板，打开比例菜单，选 3:4。{{D001|第 1 页，比例和预览设置}}\n\n"
            f"{{{{img:{figure}|450}}}}\n\n练习中我保存预览。如果比例显示错误，我重新选择比例。\n\n"
            "~~~text\nratio={{current_ratio}}\n~~~\n\n"
            "## 随课参考\n\n讲义首页用于复习画板尺寸。"
            "[打开讲义](../原始资料/A%20操作指南.pdf){{D001|第 1 页，比例和预览设置}}\n\n"
            "课堂练习可用随课的原始入口。{{D002|InternetShortcut 中的 URL}}\n",
            encoding="utf-8",
        )
        supplemental = {"anchor": "讲义首页用于复习画板尺寸",
                        "sources": [f"{guide.name} · 第 1 页，比例和预览设置"]}
        supplement_file = self.work / "补充内容复核.json"
        self.save(supplement_file, [{**supplemental, "sources": [f"{guide.name} · 第 2 页"]}])
        self.command("preview", self.package)
        preview = (self.work / "阅读预览" / ENTRY).read_text(encoding="utf-8")
        self.assertIn("{{current_ratio}}", preview)
        self.assertIn(",%E9%A2%84%E8%A7%88@", preview)
        docs = self.data(self.ledger / "documents-summary.json")["records"]
        self.assertGreater(docs[0]["text_blocks"], 0)
        self.assertEqual(docs[1]["status"], "extracted")
        self.assertEqual(self.data(self.work / docs[1]["result"])["blocks"][0]["text"], url)
        review = self.data(self.work / "验收填写模板.json")
        self.assertEqual(review["episodes"], [])
        review["documents"][0]["note"] = "已查看第 1 页的尺寸标签和预览示意"
        review["documents"][1].update(inspected_at="InternetShortcut 节和 URL 条目", note="随课练习入口")
        review["follow_along"] = [self.follow_along(
            "设好画板比例", ["第 1 页，比例和预览设置"], [figure + ".png"])]
        review_path = self.work / "验收记录.json"
        self.save(review_path, review)
        self.assertIn("附件须登记实际检查的页", self.command("accept", self.package, okay=False))
        review["documents"][0]["inspected_at"] = "第 1 页的文字和图形布局"
        review.pop("supplements")
        self.save(review_path, review)
        self.assertIn("补充内容验收需与当前登记一致", self.command("accept", self.package, okay=False))
        review["supplements"] = self.data(supplement_file)
        self.save(review_path, review)
        self.assertIn("补充内容缺可见正文或相邻隐藏出处", self.command("accept", self.package, okay=False))
        self.save(supplement_file, [supplemental])
        self.command("preview", self.package)
        review["snapshot"] = self.data(self.work / "验收填写模板.json")["snapshot"]
        review["supplements"] = [supplemental]
        for doc, (anchor, note) in zip(review["source_coverage"]["documents"], [
            ("选 3:4", "讲义第一页图文给出画板比例和导出预览"),
            ("课堂练习可用随课的原始入口", "原快捷方式的 URL 提供独立练习入口"),
        ]):
            for unit in doc["units"]:
                unit.update(outcome="正文已覆盖", note=note, anchor=anchor)
            for candidate in doc["candidates"]:
                candidate.update(kind="非渠道", note="原快捷方式的 URL 指向课后练习，未声称是联系账号")
        self.complete_claims(review)
        self.save(review_path, review)
        self.publish(expected_videos=0)
        if OBSIDIAN:
            render_check = self.data(self.work / "阅读预览" / "obsidian-render.json")["checks"][0]
            self.assertEqual(render_check["rawLinks"], 2)
            self.assertEqual(render_check["missingRawLinks"], [])
            (self.root / ".obsidian").mkdir()
            target = self.package / ENTRY.parent / f"{self.package.name}.md"
            installed = json.loads(self.command(
                "deliver", self.package, "--vault", self.root, "--course-folder", self.package.name))
            self.assertEqual(Path(installed["entry"]), target)
            self.assertFalse((self.package / ENTRY).exists())
            self.command("verify-reader", self.package, "--obsidian-exe", OBSIDIAN)
            result = json.loads(self.command("verify", self.package, "--require-vault"))
            self.assertTrue(result["verified"])
            render = self.work / "日常库接口验收" / f"{installed['sha256']}.json"
            self.assertTrue(self.data(render)["checks"][0]["reopened"])
            render.write_text("{}", encoding="utf-8")
            self.assertIn("尚待 Obsidian 重开", self.command("verify", self.package, "--require-vault", okay=False))
            archived = self.package / "原始资料" / guide.name
            with archived.open("ab") as stream:
                stream.write(b"\n")
            self.assertIn("原件有增删或变更", self.command("verify", self.package, okay=False))

    def test_supplements_require_visible_text_and_exact_nearby_sources(self):
        sys.path.insert(0, str(CLI.parent))
        from acceptance import validate_supplements

        guide = "作者资料.pdf · 第 3 页"
        film = "第 1 讲 00:00–00:12"
        item = [{"anchor": "在作者页领取练习", "sources": [guide, film]}]
        hidden = f"<!-- 段落出处 {guide} · {film} -->"
        text = f"在作者页领取练习。{hidden}"
        validate_supplements(text, item, item)
        for misplaced in (
            f"> [!info]- 资料\n> {text}\n",
            f"<details>\n{text}\n</details>",
            f"~~~text\n{text}\n~~~",
            f"<!-- 在作者页领取练习。 -->{hidden}",
            f"在作者页领取练习。\n\n{hidden}",
            text.replace("第 3 页", "第 3 页附注"),
        ):
            with self.subTest(body=misplaced), self.assertRaisesRegex(ValueError, "缺可见正文或相邻隐藏出处"):
                validate_supplements(misplaced, item, item)

    def test_vault_delivery_preserves_the_existing_entry_and_requires_reopen(self):
        sys.path.insert(0, str(CLI.parent))
        from project import CourseProject, sha, write
        from urllib.parse import unquote
        from vault_delivery import confirm, delivery_status, install

        write(self.source / "说明.txt", "原内容")
        project = CourseProject.initialize(self.source, self.package)
        vault = self.root / "用户日常库"
        (vault / ".obsidian").mkdir(parents=True)
        course_name = "已有学习入口"
        old_target = vault / course_name / ENTRY
        target = old_target.with_name(f"{course_name}.md")
        write(old_target, "# 保存的旧稿\n")
        previous = sha(old_target)

        picture = Path("课件/原图/指示图.png")
        file = self.package / picture
        file.parent.mkdir(parents=True)
        file.write_bytes(b"publication fixture")
        body = (
            "# 新版\n\n## 导出预览\n\n![指示图](原图/指示图.png)\n\n"
            "> [!info]- 原课与配套资料\n> [说明](../原始资料/说明.txt)\n"
        )
        write(project.entry, body)
        published = {"body": body, "outputs": {ENTRY.as_posix(): sha(project.entry), picture.as_posix(): sha(file)}}
        with self.assertRaisesRegex(ValueError, "已有不同的教材"):
            install(project, published, vault, course_name)
        self.assertEqual(sha(old_target), previous)

        shortcut = vault / "旧入口索引.md"
        write(shortcut, f"[[{course_name}/{ENTRY.with_suffix('').as_posix()}]]")
        with self.assertRaisesRegex(ValueError, "旧笔记有入链"):
            install(project, published, vault, course_name, previous)
        self.assertTrue(old_target.is_file())
        shortcut.unlink()
        state = install(project, published, vault, course_name, previous)
        self.assertFalse(old_target.exists())
        self.assertEqual(sha(target), sha(project.entry))
        self.assertEqual(unquote(state["uri"]), f"obsidian://open?path={target.as_posix()}")
        self.assertEqual(sha(project.ledger / "obsidian-previous" / previous / ENTRY.name), previous)
        self.assertTrue((vault / course_name / picture).is_file())
        self.assertTrue((vault / course_name / "原始资料/说明.txt").is_file())
        with self.assertRaisesRegex(ValueError, "尚待 Obsidian 重开"):
            delivery_status(project, published, required=True)
        observed = {
            **{key: state[key] for key in ("entry", "sha256", "installed_at")},
            "mode": "preview", "reopened": True, "reached_bottom": True, "references_collapsed": True,
            "visible_citations": False, "section_seen": "导出预览", "image_seen": picture.as_posix(),
            "note": "只用于验证记录格式的测试样例",
        }
        with self.assertRaisesRegex(ValueError, "日常 Obsidian 库重开"):
            confirm(project, published, {**observed, "entry": str(project.entry)})
        confirm(project, published, observed)
        self.assertEqual(delivery_status(project, published, required=True)["status"], "已记录日常库重开")
        self.assertEqual(install(project, published)["observation"], observed)

        write(project.entry, body.replace("# 新版", "# 再版"))
        revised = {"body": project.entry.read_text("utf8"),
                   "outputs": {**published["outputs"], ENTRY.as_posix(): sha(project.entry)}}
        self.assertEqual(delivery_status(project, revised)["status"], "阅读库待更新")
        install(project, revised)
        with self.assertRaisesRegex(ValueError, "尚待 Obsidian 重开"):
            delivery_status(project, revised, required=True)
        write(target, "# 读者的现场编辑\n")
        self.assertEqual(delivery_status(project, revised)["status"], "阅读库文件待核对")
        with self.assertRaisesRegex(ValueError, "已有不同的教材"):
            install(project, revised)

    def make_clips(self):
        from PIL import Image, ImageDraw

        pictures = self.root / "录屏像素"
        pictures.mkdir()
        for index in range(60):
            image = Image.new("RGB", (640, 360), "#eef1f5")
            draw = ImageDraw.Draw(image)
            draw.rectangle((60, 50, 315, 295), fill="white", outline="black", width=3)
            draw.text((410, 55), "Ratio", fill="black", font_size=20)
            if 20 <= index < 32:
                draw.rectangle((410, 85, 510, 136), fill="white", outline="black", width=3)
                draw.text((428, 96), "3:4", fill="black", font_size=22)
            elif index >= 32:
                draw.text((410, 95), "3:4 applied", fill="black", font_size=20)
            image.save(pictures / f"{index:02}.png")
        for chapter, sound in (("02 录屏 [一].mp4", []),
                               ("10 录屏二.mp4", ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono"])):
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-framerate", "20", "-i", str(pictures / "%02d.png"),
                            *sound, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "100",
                            "-t", "3", str(self.source / chapter)], check=True, timeout=30)
        subtitle = self.source / "02 录屏 [一].zh-Hans.srt"
        subtitle.write_text("1\n00:00:00,500 --> 00:00:01,500\n我打开比例菜单，选三比四。\n", encoding="utf-8")
        summary = self.source / "10 录屏二.srt"
        summary.write_text("1\n00:00:00,000 --> 00:00:03,000\n设置比例。\n", encoding="utf-8")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要本地 FFmpeg")
    def test_mixed_course_checks_every_video_and_rejects_stale_review(self):
        self.make_guide()
        self.make_clips()
        self.start_course()
        self.assertEqual(self.data(self.ledger / "01.json")["extraction"], "original_subtitle")
        silent_sound = self.data(self.ledger / "02.json")
        self.assertTrue(silent_sound["audio_processed"])
        self.assertGreater(silent_sound["audio_scope"][0]["samples"], 47000)
        self.assertEqual(silent_sound["original_cues"], silent_sound["cues"])
        self.assertEqual(silent_sound["asr_cues"], [])
        scan = self.data(self.work / "媒体补查/L02.json")
        self.assertEqual(scan["audio"]["speech_windows"], [])
        self.assertTrue(any(event["kind"] == "change" and event["outside_speech"] and not event["outside_cues"]
                            for event in scan["picture"]["scene_probes"]))
        self.command("window", self.package, 2, .9, 1.65, "--label", "短比例菜单", "--step", .1,
                     "--crop", 400, 70, 550, 155)
        window = self.data(self.work / "源片段复核/短比例菜单/片段.json")
        self.assertTrue(any(1 <= row["pts"] < 1.6 and not row["encoded_key_frame"] for row in window["frames"]))
        self.pick([{"name": "菜单初选", "lesson": 1, "time": 1.2},
                   {"name": "静默片段里的菜单", "lesson": 2, "time": 1.2}])
        note = self.work / "教材.md"
        body = ("# 画板操作\n\n## 设好画板比例\n\n### 打开比例菜单\n\n"
                "我先准备空白画板，打开比例菜单，选 3:4。{{01|00:00–00:03}} {{D003|第 1 页}}\n\n"
                "{{img:菜单初选|450}}\n\n练习中我保存预览。如果设置错了，我重新选择比例。\n\n"
                "## 检查静默录屏设置\n\n我在无口播的界面展开比例列表。{{02|00:00–00:03}}\n\n"
                "{{img:静默片段里的菜单|450}}\n\n我等面板收起，再检查 3:4 已应用。{{02|00:01.60–00:03}}\n")
        note.write_text(body, encoding="utf-8")
        point_dir = self.work / "逐段要点"
        point_dir.mkdir()
        for lesson in (1, 2):
            self.save(point_dir / f"{lesson:02}.json", [
                {"range_s": [0, 3], "kind": "画面", "source_note": "空白画板经过短暂菜单，最后显示 3:4 已应用"}])
        self.command("preview", self.package)
        review = self.data(self.work / "验收填写模板.json")
        review_path = self.work / "验收记录.json"
        settings = [("打开比例菜单", "菜单初选"), ("展开比例列表", "静默片段里的菜单")]
        for row, (anchor, image) in zip(review["episodes"], settings):
            row.update(watched=[{"range_s": [0, row["duration_s"]], "kind": "教学",
                                 "note": "从空白画板到短暂菜单，再看到比例已应用"}],
                       visual_basis="原片", visual_note="原片比例菜单可见",
                       key_steps=[{"range_s": [1, 1.6], "anchor": anchor,
                                   "figures": [image + ".png"]}])
            row["channel_review"] = {"opening": "0 秒从画板开场", "closing": "末段菜单收起",
                                     "screen": "界面四边仅有画板和菜单",
                                     "finding": "原片没有作者公开渠道"}
            row["source_points"][0]["anchor"] = anchor
        for doc in review["documents"]:
            if doc["source"].endswith(".srt"):
                doc.update(use="用作转写", inspected_at="字幕时间轴与首尾字幕", note="对照第一讲的课程原字幕")
            else:
                doc.update(inspected_at="PDF 第 1 页文字、比例示意和页边",
                           note="说明页展示导出前的比例确认")
        review["follow_along"] = [self.follow_along(
            "设好画板比例", ["第 1 讲 00:00–00:03"], ["菜单初选.png"])]
        for episode in review["source_coverage"]["videos"]:
            for window in episode["windows"]:
                window.update(outcome="正文已覆盖", note="窗口从空白画板展示到 3:4 比例菜单消失")
            for zone in episode["channels"]:
                zone.update(outcome="已查", note="原片从空白画板到比例应用完成，四周没有作者联系入口")
        for document in review["source_coverage"]["documents"]:
            for unit in document["units"]:
                if document["id"] == "D003":
                    unit.update(outcome="正文已覆盖", note="PDF 画板比例和预览标签", anchor="选 3:4")
                else:
                    unit.update(outcome="无独立教学", note="字幕为对应录屏的操作文字，已经并入视频原件复核")
        self.complete_claims(review)
        complete = review["episodes"]
        review["episodes"] = complete[:1]
        self.save(review_path, review)
        self.assertIn("每个原视频", self.command("accept", self.package, okay=False))
        review["episodes"] = complete
        close = complete[0]["channel_review"].pop("closing")
        self.save(review_path, review)
        self.assertIn("片头、片尾、画面署名及作者渠道", self.command("accept", self.package, okay=False))
        complete[0]["channel_review"]["closing"] = close
        self.save(review_path, review)
        displaced = body.replace("{{img:菜单初选|450}}\n\n练习中我保存预览。如果设置错了，我重新选择比例。",
                                 "练习中我保存预览。如果设置错了，我重新选择比例。\n\n{{img:菜单初选|450}}")
        note.write_text(displaced, encoding="utf-8")
        self.command("preview", self.package)
        initial_snapshot = review["snapshot"]
        review["snapshot"] = self.data(self.work / "验收填写模板.json")["snapshot"]
        self.save(review_path, review)
        self.assertIn("关键图要紧接", self.command("accept", self.package, okay=False))
        review["snapshot"] = initial_snapshot
        self.save(review_path, review)
        note.write_text(body + "\n我最后核对预览。\n", encoding="utf-8")
        self.assertIn("验收记录与当前预览", self.command("accept", self.package, okay=False))
        note.write_text(body, encoding="utf-8")
        proof_path = self.work / "确认回看.txt"
        proof_path.write_text("原片菜单 3:4 出现", encoding="utf-8")
        decision_path = self.work / "补识决定.json"
        self.save(decision_path, {
            "metadata_sha256": hashlib.sha256((self.ledger / "01.json").read_bytes()).hexdigest(),
            "windows": [{"range_s": [.5, 1.5], "reason": "对照录屏原菜单",
                         "proofs": [{"path": proof_path.name, "sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest()}],
                         "cues": [{"start": .5, "end": 1.5, "text": "打开比例菜单，选 3:4。"}]}],
        })
        self.command("adopt", self.package, 1, decision_path)
        adopted = self.data(self.work / "采用转写/01.json")
        self.assertEqual(adopted["confirmed_windows"], [[.5, 1.5]])
        self.assertIn("选 3:4", adopted["cues"][0]["text"])
        self.assertIn("验收记录与当前预览", self.command("accept", self.package, okay=False))
        proof_path.write_text("证据已变", encoding="utf-8")
        self.assertIn("采用依据字节已变动", self.command("read", self.package, 1, okay=False))
        confirmed_spec = self.data(decision_path)
        self.save(decision_path, {**self.data(decision_path), "windows": []})
        self.command("adopt", self.package, 1, decision_path)
        self.publish(expected_videos=2)

        introduction, first, second = body.split("\n## ")
        reordered = (introduction + "\n## 第 1 讲 " + second
                     + "\n## 第 2 讲 " + first)
        note.write_text(reordered, encoding="utf-8")
        order = self.work / "讲次顺序.json"
        self.save(order, [2, 1])
        self.command("preview", self.package)
        reordered_note = (self.work / "阅读预览" / ENTRY).read_text(encoding="utf-8")
        first_chapter = reordered_note.split("## 第 2 讲", 1)[0]
        self.assertIn("<!-- 段落出处 第 1 讲 00:00–00:03 -->", first_chapter)
        self.assertIn("<!-- 配图出处 原课第 1 讲 ", first_chapter)
        self.assertIn("> - 第 1 讲 · [10 录屏二.mp4]", reordered_note)
        self.assertIn("> - 第 2 讲 · [02 录屏 \\[一\\].mp4]", reordered_note)
        reordered_template = self.data(self.work / "验收填写模板.json")
        episodes = reordered_template["episodes"]
        self.assertEqual([(row["lesson"], row["display_number"], row["chapter"]) for row in episodes],
                         [(2, 1, "第 1 讲 检查静默录屏设置"), (1, 2, "第 2 讲 设好画板比例")])
        previous_review = self.data(review_path)
        by_source = {row["lesson"]: row for row in previous_review["episodes"]}
        reordered_review = {
            **previous_review,
            "snapshot": reordered_template["snapshot"],
            "episodes": [
                {**by_source[row["lesson"]], "chapter": row["chapter"], "display_number": row["lesson"]}
                for row in episodes
            ],
        }
        self.save(review_path, reordered_review)
        self.assertIn("第 1 讲的原件身份或时长不一致",
                      self.command("accept", self.package, okay=False))
        self.save(review_path, previous_review)
        self.save(order, [1, 2])
        note.write_text(body, encoding="utf-8")

        raw_mtime = (self.package / "原始资料" / "02 录屏 [一].mp4").stat().st_mtime_ns
        evidence_mtime = (self.work / "媒体补查/L01.json").stat().st_mtime_ns
        self.start_course()
        self.assertEqual(raw_mtime, (self.package / "原始资料" / "02 录屏 [一].mp4").stat().st_mtime_ns)
        self.assertEqual(evidence_mtime, (self.work / "媒体补查/L01.json").stat().st_mtime_ns)
        proof_path.write_text("原片菜单 3:4 出现", encoding="utf-8")
        self.save(decision_path, confirmed_spec)
        self.command("adopt", self.package, 1, decision_path)
        self.save(self.work / "媒体设置.json", {"1": {"voiced_reference": "10 录屏二.mp4"}})
        self.assertIn("采用决定已过期", self.command("prepare", self.package, okay=False))
        aligned = self.data(self.ledger / "01.json")
        self.assertEqual(aligned["audio_source"], "10 录屏二.mp4")
        self.assertEqual(len(aligned["audio_alignment"]), 13)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要本地 FFmpeg")
    def test_hard_captions_and_dark_silent_operation(self):
        from PIL import Image, ImageDraw

        for index, label in enumerate(("Ready", "ROI 2.3", "ROI 23", "Saved")):
            frame = Image.new("RGB", (640, 360), "#0c0c0c")
            draw = ImageDraw.Draw(frame)
            draw.text((230, 300), label, fill="white", font_size=30)
            if index == 1:
                draw.rectangle((250, 100, 365, 160), outline="red", width=4)
            frame.save(self.root / f"input-{index}.png")
        video = self.source / "暗色界面.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-framerate", "2", "-i",
                        str(self.root / "input-%d.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
                       check=True, timeout=30)
        deps = Path(self.env.get("WORLDANALYSIS_COURSE_RUNTIME", str(ROOT / ".runtime"))) / "site-packages"
        paths = [str(deps), self.env.get("PYTHONPATH", "")]
        self.env["PYTHONPATH"] = os.pathsep.join(filter(None, paths))
        self.env["WORLDANALYSIS_COURSE_RUNTIME"] = str(self.root / "空运行目录")
        self.start_course()
        self.assertFalse(self.data(self.ledger / "01.json")["audio_processed"])
        scan = self.data(self.work / "媒体补查/L01.json")
        self.assertTrue(any(event.get("dark_background") for event in scan["picture"]["scene_probes"]))
        path = Path(self.command("observe", self.package, 1, 0, 2, "--step", .25,
                                 "--crop", 160, 270, 460, 352, "--ocr").splitlines()[-1])
        seen = self.data(path)
        self.assertEqual(seen["ocr_unreadable_frames"], [])
        self.assertTrue(any(cue["start"] == .5 and "2.3" in cue["text"] for cue in seen["text_cues"]))
        self.assertTrue(any(cue["start"] == 1 and "23" in cue["text"] for cue in seen["text_cues"]))

    def test_short_uncovered_speech_and_differences_inside_a_cue(self):
        sys.path.insert(0, str(CLI.parent))
        try:
            from review_media import missing_ranges
            from transcript import compare_cues
        finally:
            sys.path.pop(0)
        self.assertEqual(missing_ranges(10.0, 10.8, [], []), [(10.0, 10.8)])
        self.assertEqual(missing_ranges(10.0, 10.2, [], []), [])
        self.assertEqual(missing_ranges(10.0, 10.2, [], [], minimum=0), [(10.0, 10.2)])
        captions = [{"start": 0, "end": 12, "text": "开启快投，目标 ROI 23。"}]
        audio = [{"start": 0, "end": 12, "text": "不要开启快投，目标 ROI 2.3。"}]
        differences, = compare_cues(captions, audio, 12)
        self.assertEqual(differences["kind"], "both")
        self.assertTrue(differences["critical_words_differ"])


if __name__ == "__main__":
    unittest.main()
