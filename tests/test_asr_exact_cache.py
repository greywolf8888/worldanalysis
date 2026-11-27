"""Check exact cross-project recognition reuse without model calls or production writes."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

TOOLS = Path(__file__).resolve().parents[1] / "_tools/course_production"
with patch.object(sys, "path", [str(TOOLS), *sys.path]):
    import gpu_asr
    from project import put, read, sha


class ExactAsrCacheTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.a, self.b = self.root / "第一课程.mp4", self.root / "第二课程.mp4"
        for source in (self.a, self.b):
            source.write_bytes(b"identical source bytes")
        self.decoder = self.root / "ffmpeg.exe"
        self.decoder.write_bytes(b"decoder version")
        self.pcm = b"\x01\x00" * gpu_asr.RATE
        self.context = {"raw_sha256": sha(self.a), "audio_sha256": sha(self.a), "settings": {},
                        "subtitle_sha256": None, "kind": "video", "recovery": None, "embedded": None}
        self.gpu = gpu_asr.VulkanASR.__new__(gpu_asr.VulkanASR)
        self.gpu.identity = {"backend": "sensevoice", "runtime_sha256": "a" * 64,
                             "model_sha256": "b" * 64, "device": {"index": 0}, "stream_revision": 1}

        def recognize(duration, language, windows):
            start, seconds, offset, chunks = windows[0]
            count = sum(map(len, chunks)) // 2
            self.gpu.decoded_windows = [{"requested_start": start, "requested_length": seconds,
                                         "timeline_start": offset, "samples": count, "sample_rate": gpu_asr.RATE}]
            return [[{"start": offset, "end": min(duration, offset + seconds), "text": "原声"}]]

        self.gpu._transcribe_pcm = Mock(side_effect=recognize)
        for mocked in (patch.object(gpu_asr, "RUNTIME", self.root),
                       patch.object(gpu_asr.shutil, "which", return_value=str(self.decoder)),
                       patch.object(gpu_asr, "audio_windows", side_effect=lambda *args: (item for item in [(0, self.pcm)]))):
            mocked.start()
            self.addCleanup(mocked.stop)

    def run_asr(self, source, name, *, context=None, track=0, language="zh", offset=0):
        return self.gpu.transcribe(source, 3, track, language, offset, cache=self.root / name,
                                   shared_context=context or self.context)

    def test_identical_bytes_and_renamed_project_use_one_recognition(self):
        first = self.run_asr(self.a, "课程甲")
        timeline = list(self.gpu.decoded_windows)
        self.assertEqual(self.gpu.shared_cache_stats["fresh_groups"], 1)
        self.assertEqual(self.run_asr(self.b, "课程乙"), first)
        self.assertEqual(self.gpu.decoded_windows, timeline)
        self.assertEqual(self.gpu.shared_cache_stats["shared_hits"], 1)
        self.assertEqual(self.gpu._transcribe_pcm.call_count, 1)
        self.assertEqual(self.run_asr(self.b, "课程乙"), first)
        self.assertEqual(self.gpu.shared_cache_stats["project_checkpoints"], 1)

    def test_source_track_model_config_and_pcm_changes_are_cache_misses(self):
        self.run_asr(self.a, "baseline")
        variations = [({"subtitle_sha256": "c" * 64}, {}),
                      ({"raw_sha256": "d" * 64}, {}), ({"settings": {"extra": 1}}, {}),
                      ({}, {"track": 1}), ({}, {"language": "en"}), ({}, {"offset": .2})]
        for index, (changes, kwargs) in enumerate(variations):
            self.run_asr(self.b, f"variant-{index}", context={**self.context, **changes}, **kwargs)
            self.assertEqual(self.gpu.shared_cache_stats["fresh_groups"], 1)
        self.gpu.identity["model_sha256"] = "e" * 64
        self.run_asr(self.b, "different-model")
        self.assertEqual(self.gpu.shared_cache_stats["fresh_groups"], 1)
        self.gpu.identity["model_sha256"] = "b" * 64
        self.pcm = b"\x02\x00" * gpu_asr.RATE
        self.run_asr(self.b, "different-pcm-but-same-length")
        self.assertEqual(self.gpu.shared_cache_stats["fresh_groups"], 1)
        self.b.write_bytes(b"changed sound bytes")
        with self.assertRaisesRegex(ValueError, "音源指纹发生变化"):
            self.run_asr(self.b, "wrong-expected-source")

    def test_corrupt_shared_payload_cannot_be_reused(self):
        self.run_asr(self.a, "甲")
        path, = (self.root / "cache/course-asr-exact").rglob("*.json")
        corrupt = read(path)
        corrupt["part"]["cues"][0]["text"] = "被篡改"
        put(path, corrupt)
        result = self.run_asr(self.b, "乙")
        self.assertEqual(result[0]["text"], "原声")
        self.assertEqual(self.gpu.shared_cache_stats["fresh_groups"], 1)

    def test_legacy_project_checkpoint_is_not_promoted_to_global_cache(self):
        checkpoint = self.root / "旧课" / "000000000000.json"
        put(checkpoint, {"cues": [{"start": 0, "end": 1, "text": "历史底稿"}],
                         "window": {"requested_start": 0, "requested_length": 1,
                                    "timeline_start": 0, "samples": gpu_asr.RATE, "sample_rate": gpu_asr.RATE}})
        self.assertEqual(self.run_asr(self.a, "旧课")[0]["text"], "历史底稿")
        self.assertFalse(list((self.root / "cache/course-asr-exact").rglob("*.json")))
        self.assertEqual(self.run_asr(self.b, "新课")[0]["text"], "原声")
        self.assertEqual(self.gpu.shared_cache_stats["fresh_groups"], 1)

    def test_disabling_shared_cache_keeps_existing_project_exact_checkpoint(self):
        first = self.run_asr(self.a, "原项目")
        existing = self.gpu.transcribe(self.a, 3, 0, "zh", cache=self.root / "原项目",
                                       shared_context=self.context, use_shared_cache=False)
        self.assertEqual(existing, first)
        self.assertEqual(self.gpu.shared_cache_stats["project_checkpoints"], 1)
        self.gpu.transcribe(self.b, 3, 0, "zh", cache=self.root / "新项目",
                            shared_context=self.context, use_shared_cache=False)
        self.assertEqual(self.gpu.shared_cache_stats["shared_hits"], 0)
        self.assertEqual(self.gpu.shared_cache_stats["fresh_groups"], 1)


if __name__ == "__main__":
    unittest.main()
