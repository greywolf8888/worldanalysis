"""Integration coverage for fresh course folders, independent of the archived batch."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "_tools/course_production/course.py"
ENTRY = Path("课件/00 开始学习.md")
OBSIDIAN = os.environ.get("WORLDANALYSIS_TEST_OBSIDIAN")


class CourseProductionTest(unittest.TestCase):
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

    def publish(self, expected_videos):
        self.assertFalse((self.package / ENTRY).exists())
        if OBSIDIAN:
            self.command("accept", self.package, "--obsidian-exe", OBSIDIAN)
            verified = self.command("verify", self.package)
            self.assertIn(f'"videos": {expected_videos}', verified)
            self.assertEqual(list((self.package / "课件").rglob("*.md")), [self.package / ENTRY])
            self.assertIn("<!-- 段落出处", (self.package / ENTRY).read_text(encoding="utf-8"))
        else:
            missing = self.root / "不存在的 Obsidian.exe"
            self.assertIn(missing.name, self.command("accept", self.package, "--obsidian-exe", missing, okay=False))
            self.assertFalse((self.package / ENTRY).exists())

    def test_document_only_intake_preview_and_publication(self):
        guide = self.make_guide()
        self.start_course()
        self.assertEqual(guide.read_bytes(), (self.package / "原始资料" / guide.name).read_bytes())
        self.pick([{"name": "比例图", "document": "D001", "page": 1, "scale": 1.2}])
        draft = self.work / "教材.md"
        draft.write_text(
            "# 画板导出\n\n## 设好画板比例\n\n"
            "我先准备空白画板，打开比例菜单，选 3:4。{{D001|第 1 页，比例和预览设置}}\n\n"
            "{{img:比例图|450}}\n\n我保存预览。如果比例显示错误，我重新选择比例。\n\n"
            "~~~text\nratio={{current_ratio}}\n~~~\n",
            encoding="utf-8",
        )
        self.command("preview", self.package)
        self.assertIn("{{current_ratio}}", (self.work / "阅读预览" / ENTRY).read_text(encoding="utf-8"))
        docs = self.data(self.ledger / "documents-summary.json")["records"]
        self.assertGreater(docs[0]["text_blocks"], 0)
        review = self.data(self.work / "验收填写模板.json")
        self.assertEqual(review["episodes"], [])
        review["documents"][0]["note"] = "已查看第 1 页的尺寸标签和预览示意"
        review["follow_along"] = [self.follow_along(
            "设好画板比例", ["第 1 页，比例和预览设置"], ["比例图.png"])]
        self.save(self.work / "验收记录.json", review)
        self.publish(expected_videos=0)
        if OBSIDIAN:
            archived = self.package / "原始资料" / guide.name
            with archived.open("ab") as stream:
                stream.write(b"\n")
            self.assertIn("原件有增删或变更", self.command("verify", self.package, okay=False))

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

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要本地 FFmpeg")
    def test_mixed_course_checks_every_video_and_rejects_stale_review(self):
        self.make_guide()
        self.make_clips()
        self.start_course()
        self.assertEqual(self.data(self.ledger / "01.json")["extraction"], "original_subtitle")
        self.assertEqual(self.data(self.ledger / "02.json")["extraction"], "sensevoice_cpu")
        self.command("window", self.package, 2, .9, 1.65, "--label", "短比例菜单", "--step", .1,
                     "--crop", 400, 70, 550, 155)
        window = self.data(self.work / "源片段复核/短比例菜单/片段.json")
        self.assertTrue(any(1 <= row["pts"] < 1.6 and not row["encoded_key_frame"] for row in window["frames"]))
        self.pick([{"name": "菜单初选", "lesson": 1, "time": 1.2},
                   {"name": "静默片段里的菜单", "lesson": 2, "time": 1.2}])
        note = self.work / "教材.md"
        body = ("# 画板操作\n\n## 设好画板比例\n\n"
                "我先准备空白画板，打开比例菜单，选 3:4。{{01|00:00–00:03}} {{D002|第 1 页}}\n\n"
                "{{img:菜单初选|450}}\n\n我保存预览。如果设置错了，我重新选择比例。\n\n"
                "## 检查静默录屏设置\n\n我在无口播的界面展开比例列表。{{02|00:00–00:03}}\n\n"
                "{{img:静默片段里的菜单|450}}\n\n我等面板收起，再检查 3:4 已应用。\n")
        note.write_text(body, encoding="utf-8")
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
        for doc in review["documents"]:
            if doc["source"].endswith(".srt"):
                doc.update(use="用作转写", note="对照第一讲的课程原字幕")
            else:
                doc["note"] = "说明页展示导出前的比例确认"
        review["follow_along"] = [self.follow_along(
            "设好画板比例", ["第 1 讲 00:00–00:03"], ["菜单初选.png"])]
        complete = review["episodes"]
        review["episodes"] = complete[:1]
        self.save(review_path, review)
        self.assertIn("每个原视频", self.command("accept", self.package, okay=False))
        review["episodes"] = complete
        self.save(review_path, review)
        displaced = body.replace("{{img:菜单初选|450}}\n\n我保存预览。如果设置错了，我重新选择比例。",
                                 "我保存预览。如果设置错了，我重新选择比例。\n\n{{img:菜单初选|450}}")
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
        self.publish(expected_videos=2)
        raw_mtime = (self.package / "原始资料" / "02 录屏 [一].mp4").stat().st_mtime_ns
        evidence_mtime = (self.work / "媒体补查/L01.json").stat().st_mtime_ns
        self.start_course()
        self.assertEqual(raw_mtime, (self.package / "原始资料" / "02 录屏 [一].mp4").stat().st_mtime_ns)
        self.assertEqual(evidence_mtime, (self.work / "媒体补查/L01.json").stat().st_mtime_ns)
        self.save(self.work / "媒体设置.json", {"1": {"voiced_reference": "10 录屏二.mp4"}})
        self.command("prepare", self.package)
        aligned = self.data(self.ledger / "01.json")
        self.assertEqual(aligned["audio_source"], "10 录屏二.mp4")
        self.assertEqual(len(aligned["audio_alignment"]), 13)


if __name__ == "__main__":
    unittest.main()
