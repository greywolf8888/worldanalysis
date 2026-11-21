"""Representative ZIP, 7z and audio routing tests independent of course data."""
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "_tools/course_production/course.py"
sys.path.insert(0, str(CLI.parent))

from archive_media import extract_member, list_members, readable_members
from media_inventory import DECISIONS, MEDIA_INVENTORY, collect_course_media, verify_course_media
from prepare_documents import document_ids
from project import CourseProject, put, read, sha


class CourseMediaInventoryTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.source = self.root / "原始课件"
        self.source.mkdir()
        self.package = self.root / "课程包"

    def test_nested_media_roles_and_versioned_evidence(self):
        loose_video = self.source / "主课.mp4"
        loose_video.write_bytes(b"test loose video bytes")
        ambiguous = "补充.zip!目录/再次打包.zip!操作录制.mp4"
        duplicate = "补充.zip!目录/再次打包.zip!主课备份.mp4"
        asset = "补充.zip!目录/再次打包.zip!世界音效/动作.wav"
        inner = io.BytesIO()
        with ZipFile(inner, "w") as package:
            package.writestr("操作录制.mp4", b"distinct archived video bytes")
            package.writestr("主课备份.mp4", loose_video.read_bytes())
            package.writestr("软件示例/index.d.ts", b"declare const app: string;")
            package.writestr("世界音效/动作.wav", b"test sound-effect bytes")
            package.writestr("配套规则.txt", "补充规则")
        with ZipFile(self.source / "补充.zip", "w") as package:
            package.writestr("目录/再次打包.zip", inner.getvalue())

        project = CourseProject.initialize(self.source, self.package)
        raw = project.validate_raw()
        put(project.ledger / "inventory.json", {"sources": [dict(row, lesson=12) if row["file"] == loose_video.name
                                                           else row for row in raw]})
        first = collect_course_media(project, raw)
        self.assertEqual([x["source"] for x in first["unresolved"]], [ambiguous])
        roles = {item["file"]: item for item in first["candidates"]}
        self.assertEqual(roles[duplicate]["target"], loose_video.name)
        self.assertEqual(roles[duplicate]["sha256"], sha(loose_video))
        self.assertEqual(roles[asset]["role"], "参考")
        self.assertFalse(any("index.d.ts" in file for file in roles))
        self.assertEqual(first["selected"][0]["lesson"], 12)
        nested = next(item for item in first["archives"] if item["member_chain"])
        self.assertEqual(nested["member_count"], 5)
        self.assertIn({"path": "配套规则.txt", "size": len("补充规则".encode()), "status": "file"}, nested["members"])

        decisions = project.work / DECISIONS
        put(decisions, {ambiguous: {"role": "教学", "reason": "对照目录正文和录屏开头确认独立操作步骤"}})
        selected = collect_course_media(project, raw)["selected"]
        self.assertEqual([item["lesson"] for item in selected], [12, 13])
        embedded = selected[1]["embedded"]
        self.assertEqual(embedded["member_chain"], ["目录/再次打包.zip", "操作录制.mp4"])
        prepared = [dict(item, source=item["file"]) for item in selected]
        digest = sha(project.ledger / MEDIA_INVENTORY)
        verify_course_media(project, raw, prepared)
        routed = collect_course_media(project, raw)
        self.assertEqual(sha(project.ledger / MEDIA_INVENTORY), digest)
        self.assertEqual(routed["selected"], selected)

        put(decisions, {ambiguous: {"role": "参考", "reason": "对照原课核对后确认该文件是配套操作素材"}})
        with self.assertRaisesRegex(ValueError, "归类决定已变动"):
            verify_course_media(project, raw, prepared)
        self.assertEqual(len(collect_course_media(project, raw)["selected"]), 1)
        verify_course_media(project, raw, prepared, require_current=False, manifest_sha256=digest)
        (project.work / embedded["work_file"]).write_bytes(b"changed work member")
        with self.assertRaisesRegex(ValueError, "原视频身份已变"):
            verify_course_media(project, raw, prepared, require_current=False, manifest_sha256=digest)

    def test_libarchive_lists_and_reads_the_exact_7z_member(self):
        tar = shutil.which("bsdtar") or shutil.which("tar")
        if not tar or b"bsdtar" not in subprocess.check_output([tar, "--version"], timeout=10):
            self.skipTest("libarchive tar is required to generate the 7z fixture offline")
        lesson = self.root / "第1讲 [画面].mp4"
        lesson.write_bytes(b"first archive member")
        other = self.root / "第1讲 画.mp4"
        other.write_bytes(b"different media")
        package = self.source / "本课.7z"
        subprocess.run([tar, "-cf", str(package), "--format", "7zip", "-C", str(self.root),
                        lesson.name, other.name], check=True, timeout=20)
        members = readable_members(list_members(package))
        self.assertEqual(set(members), {lesson.name, other.name})
        target = self.root / "selected.mp4"
        extract_member(package, members[lesson.name], target)
        self.assertEqual(target.read_bytes(), lesson.read_bytes())

    def test_reclassification_preserves_document_citation_ids(self):
        meta = self.root / "ledger"
        meta.mkdir()
        put(meta / "documents-summary.json", {"records": [{"file": "旧讲义.txt", "id": "D002"},
                                                           {"file": "现在是正课.wav", "id": "D003"}]})
        rows = [{"file": "新配套.txt"}, {"file": "旧讲义.txt"}]
        result = document_ids(meta, rows)
        self.assertEqual(result, {"旧讲义.txt": "D002", "现在是正课.wav": "D003", "新配套.txt": "D004"})
        self.assertEqual(document_ids(meta, [{"file": "现在是正课.wav"}]), result)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
    def test_same_title_and_duration_need_a_review_with_different_digests(self):
        first = self.source / "课程视频教学常见排版修正方法.mp4"
        copy = self.source / "课程视频教学常见排版修正方法 (1).mp4"
        separate = self.source / "课程视频教学常见排版修正方法 (2).mp4"
        for file, color, seconds in [(first, "red", 2), (copy, "green", 2), (separate, "blue", 3)]:
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=160x100:d={seconds}",
                            "-pix_fmt", "yuv420p", str(file)], check=True, timeout=15)
        project = CourseProject.initialize(self.source, self.package)
        raw = project.validate_raw()
        # Preserve the previously established first lesson even when names sort differently.
        put(project.ledger / "inventory.json", {"sources": [dict(row, lesson=6) if row["file"] == first.name
                                                           else row for row in raw]})
        routed = collect_course_media(project, raw)
        candidates = {row["file"]: row for row in routed["candidates"]}
        self.assertEqual(candidates[copy.name]["role"], "待核")
        self.assertNotEqual(candidates[first.name]["sha256"], candidates[copy.name]["sha256"])
        self.assertEqual(candidates[copy.name]["possible_duplicate"]["target"], first.name)
        self.assertEqual(candidates[separate.name]["role"], "教学")

        put(project.work / DECISIONS, {copy.name: {"role": "教学", "reason": "逐帧确认同名的操作例子和输入不同"}})
        routed = collect_course_media(project, raw)
        self.assertFalse(routed["unresolved"])
        self.assertEqual([row["file"] for row in routed["selected"]], [first.name, separate.name, copy.name])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
    def test_teaching_audio_uses_asr_and_has_no_synthetic_video(self):
        lesson = self.source / "第1讲.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        str(lesson)], check=True, timeout=15)
        extras = self.source / "音效"
        extras.mkdir()
        shutil.copyfile(lesson, extras / "提示.wav")
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(CLI), "start", str(self.source),
                                 "--output", str(self.package), "--cpu", "--workers", "1"],
                                env={**os.environ, "PYTHONUTF8": "1"}, capture_output=True,
                                text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        project = CourseProject.open(self.package)
        media = read(project.ledger / "media-summary.json")["videos"]
        self.assertEqual([(row["source"], row.get("kind")) for row in media], [(lesson.name, "audio")])
        metadata = read(project.ledger / "01.json")
        self.assertTrue(metadata["audio_processed"])
        self.assertTrue(metadata["audio_scope"])
        self.assertTrue(read(project.work / "媒体补查/L01.json")["picture"]["no_video"])
        self.assertFalse((project.work / "媒体补查/L01.jpg").exists())
        (project.work / "教材.md").write_text(
            "# 音频课\n\n## 第 1 讲 实际顺序\n\n依次执行。{{1|00:00.00–00:01.00}}\n", encoding="utf-8")
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(CLI), "preview", str(self.package)],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(read(project.work / "验收填写模板.json")["episodes"][0]["kind"], "audio")
        rendered = (project.preview / "课件/00 开始学习.md").read_text(encoding="utf-8")
        self.assertIn("独立音频", rendered)


if __name__ == "__main__":
    unittest.main()
