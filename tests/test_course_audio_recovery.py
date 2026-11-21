"""Verify that a damaged audio packet leaves a reviewable visual timeline."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


CLI = Path(__file__).resolve().parents[1] / "_tools/course_production/course.py"


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要本地 FFmpeg")
class CourseAudioRecoveryTest(unittest.TestCase):
    def test_decodable_container_with_corrupt_audio_can_resume_and_recover(self):
        with TemporaryDirectory() as scratch:
            folder = Path(scratch)
            source = folder / "原始来源"
            source.mkdir()
            package = folder / "课程"
            work = package / ".course/work"
            ledger = package / ".course/ledger"
            recovery = work / "媒体恢复"
            damaged = source / "01 损坏音轨.mp4"
            later = source / "02 正常无声录屏.mp4"
            clean = folder / "同版可用音轨.mp4"
            ffmpeg = ["ffmpeg", "-v", "error", "-nostdin"]
            subprocess.run([*ffmpeg, "-f", "lavfi", "-i", "color=navy:s=96x96:d=2", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-c:a", "aac", str(clean)],
                           check=True, timeout=20)
            subprocess.run([*ffmpeg, "-i", str(clean), "-map", "0:v:0", "-c:v", "copy", str(later)],
                           check=True, timeout=20)
            packet_data = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "a:0",
                                                  "-show_entries", "packet=pos,size", "-of", "json", str(clean)])
            packet = json.loads(packet_data)["packets"][35]
            start, length = int(packet["pos"]), int(packet["size"])
            pristine = clean.read_bytes()
            damaged.write_bytes(pristine[:start] + b"\xff" * length + pristine[start+length:])
            failed = subprocess.run([*ffmpeg, "-xerror", "-i", str(damaged), "-map", "0:a:0", "-f", "null", "-"],
                                    capture_output=True, timeout=20)
            self.assertNotEqual(failed.returncode, 0)

            def course(action, *arguments):
                return subprocess.run([sys.executable, "-X", "utf8", str(CLI), action, *map(str, arguments)],
                                      cwd=folder, capture_output=True, text=True, encoding="utf-8", timeout=160)

            def read(path):
                return json.loads(path.read_text(encoding="utf-8"))

            initial = course("start", source, "--output", package, "--workers", 1, "--cpu")
            self.assertNotEqual(initial.returncode, 0, initial.stdout)
            self.assertIn("仍有取材缺口", initial.stdout + initial.stderr)
            summary = read(ledger / "media-summary.json")
            self.assertEqual([row["lesson"] for row in summary["videos"]], [1, 2])
            unresolved, = summary["unreadable"]
            self.assertEqual(unresolved["stage"], "audio_decode")
            self.assertGreater(unresolved["decoded_pcm_s"], 0)
            first = read(work / "媒体补查/L01.json")
            self.assertIn("decode_error", first["audio"])
            self.assertGreater(first["picture"]["sample_count"], 0)
            self.assertTrue((work / "采用转写/02.json").is_file())
            self.assertFalse(read(ledger / "01.json")["audio_processed"])
            preview = course("preview", package)
            self.assertIn("仍有取材缺口", preview.stdout + preview.stderr)

            recovery.mkdir()
            restored = recovery / clean.name
            shutil.copy2(clean, restored)
            spec = {"original_sha256": hashlib.sha256(damaged.read_bytes()).hexdigest(), "file": restored.name,
                    "sha256": hashlib.sha256(pristine).hexdigest(), "method": "同源副本",
                    "scope": "原画不变。使用同一录制文件的可读音轨。"}
            (recovery / "01.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            (work / "媒体设置.json").write_text(json.dumps({"1": {"recovery": "01.json"}}), encoding="utf-8")
            retried = course("prepare", package, "--workers", 1)
            self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
            self.assertNotIn("unreadable", read(ledger / "media-summary.json"))
            self.assertTrue(read(ledger / "01.json")["audio_processed"])
            self.assertGreater(read(work / "媒体补查/L01.json")["audio"]["decoded_s"], 1.9)
            self.assertEqual(read(work / "采用转写/01.json")["recovery"]["scope"], spec["scope"])
            self.assertEqual((package / "原始资料" / damaged.name).read_bytes(), damaged.read_bytes())
