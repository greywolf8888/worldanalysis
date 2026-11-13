"""P3 bounded-worker tests; synthetic inputs do not accept real course quality."""
from __future__ import annotations
from importlib import metadata
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
import wave

from worldanalysis import media

FIXTURES = Path(__file__).parent / "media_fixtures"


def make_silence(path, seconds):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        second = b"\0" * 32000
        for _ in range(seconds):
            out.writeframesraw(second)


class FakeASR:
    calls = []
    def __init__(self, cfg): pass
    def transcribe(self, pcm, start, end):
        self.calls.append((start, end, len(pcm)))
        return {"status": "extracted", "reason": "SYNTHETIC_TEST_DOUBLE_NOT_REAL_ASR", "elapsed_seconds": 0.01,
                "provider_id": "faster-whisper", "provider_version": "1.2.1", "runtime": "local_cpu",
                "segments": [{"start_ms": start, "end_ms": end, "text": "synthetic test double", "words": []}]}


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="media-test-", dir=FIXTURES)
        self.root = Path(self.temp.name)
        self.source = self.root / "synthetic.wav"
        make_silence(self.source, 12)
        self.cfg = media._limits({"asr_enabled": False, "ocr_enabled": False,
                                  "window_seconds": 5, "overlap_seconds": 2, "synthetic": True})
        FakeASR.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def process(self, name="output", cfg=None):
        output = self.root / name
        output.mkdir(exist_ok=True)
        return media._process(self.source.resolve(), output.resolve(), cfg or self.cfg)

    def test_defaults_and_hard_queue_reservations(self):
        cfg = media._limits()
        self.assertEqual((cfg["window_seconds"], cfg["overlap_seconds"]), (300, 2))
        for options in ({"pending_audio_byte_cap": 1024}, {"cache_byte_cap": 1024}, {"final_evidence_byte_cap": 1024}):
            result = media.extract_media(self.source, self.root / "must-not-create", options=options)
            self.assertEqual(result["processing_status"], "blocked")
            self.assertIn("RESOURCE", result["gaps"][0]["reason"])
            self.assertFalse((self.root / "must-not-create").exists())

    def test_capped_encoder_stops_before_overflow(self):
        buffer = media.CappedBuffer(5)
        buffer.write(b"1234")
        with self.assertRaises(media.MediaLimit):
            buffer.write(b"67")
        self.assertEqual(buffer.getvalue(), b"1234")

    def test_cpu_only_admission_rejects_unmeasured_gpu(self):
        result = media.extract_media(self.source, self.root / "gpu", options={"device": "cuda"})
        self.assertEqual(result["processing_status"], "blocked")
        self.assertIn("CPU_CALIBRATION_ONLY", result["gaps"][0]["reason"])
        self.assertFalse((self.root / "gpu").exists())

    def test_actual_reader_bytes_include_repeated_seeks_and_no_unbounded_read(self):
        reader = media.CountingReader(self.source, byte_cap=10, chunk=4)
        self.assertEqual(len(reader.read(1000)), 4)
        reader.seek(0)
        self.assertEqual(len(reader.read(1000)), 4)
        self.assertEqual(len(reader.read(1000)), 2)
        with self.assertRaises(media.MediaLimit):
            reader.read(1)
        with self.assertRaises(media.MediaLimit):
            reader.read()
        self.assertEqual(reader.received, 10)
        self.assertLessEqual(reader.max_read, 4)
        reader.close()

    def test_local_metadata_probe_is_not_asr_acceptance(self):
        result = media.probe_media(self.source)
        self.assertEqual(result["processing_status"], "extracted", result)
        self.assertEqual(result["duration_ms"], 12000)
        self.assertLessEqual(result["input_bytes_received"], 1024**2)
        self.assertFalse(result["source_capabilities"]["network_saving_claim"])
        self.assertEqual(result["source_capabilities"]["http_range"], "unsupported_not_probed")

    def test_network_url_ads_and_parent_traversal_rejected(self):
        for path in ("https://example.invalid/private.mp4?token=not-a-real-token", "file:///remote.wav", "C:/test.wav:private", self.root / ".." / self.root.name / "synthetic.wav"):
            result = media.extract_media(path, self.root / "never")
            self.assertEqual(result["processing_status"], "blocked")
            self.assertFalse(result["blocks"])
        outside = media.extract_media(self.source, self.root / "never", options={"allowed_root": self.root / "absent"})
        self.assertEqual(outside["processing_status"], "blocked")

    def test_local_playlist_with_network_reference_not_a_reader(self):
        playlist = self.root / "untrusted.m3u8"
        playlist.write_text("#EXTM3U\n#EXTINF:10,\nhttps://example.invalid/segment.ts\n", encoding="utf-8")
        result = media.probe_media(playlist)
        self.assertEqual(result["processing_status"], "unsupported")
        self.assertFalse(result["streams"])

    def test_long_pcm_is_window_bounded_and_not_full_source_hashed(self):
        make_silence(self.source, 650)
        cfg = media._limits({"asr_enabled": False, "ocr_enabled": False, "synthetic": True})
        real_sha = media._sha
        def safe_sha(path):
            self.assertNotEqual(Path(path).resolve(), self.source.resolve(), "full source hash prohibited")
            return real_sha(path)
        with patch.object(media, "_sha", side_effect=safe_sha):
            result = self.process(cfg=cfg)
        windows = result["metadata"]["audio_windows"]
        self.assertEqual([w["start_ms"] for w in windows], [0, 298000, 596000])
        self.assertTrue(all(w["pcm_bytes"] <= 300 * 16000 * 2 for w in windows))
        self.assertEqual(result["metadata"]["peak_pcm_bytes"], 9_600_000)
        self.assertEqual(result["metrics"][0]["observed"], 650000)
        self.assertEqual(result["metrics"][0]["processed"], 0)
        self.assertEqual(result["metadata"]["runs"][0]["mixed_source_demux_passes"], 1)
        self.assertFalse(result["metadata"]["full_input_hash_performed"])
        self.assertEqual(sum(w["input_bytes_received"] for w in windows), result["metadata"]["input_bytes_received"])

    def test_checkpoint_resume_seeks_only_pending_and_does_not_repeat_windows(self):
        cfg = {**self.cfg, "max_windows": 1}
        first = self.process(cfg=cfg)
        self.assertEqual(first["enumeration_state"], "partial")
        self.assertEqual([w["start_ms"] for w in first["metadata"]["audio_windows"]], [0])
        second = self.process()
        starts = [w["start_ms"] for w in second["metadata"]["audio_windows"]]
        self.assertEqual(starts, [0, 3000, 6000, 9000])
        self.assertEqual(second["metadata"]["runs"][-1]["resume_seek_ms"], 3000)
        self.assertEqual(second["enumeration_state"], "complete")
        run_count = len(second["metadata"]["runs"])
        with patch.object(media, "CountingReader", side_effect=AssertionError("completed checkpoint must not re-read source")):
            third = self.process()
        self.assertEqual(len(third["metadata"]["runs"]), run_count)
        self.assertEqual([w["start_ms"] for w in third["metadata"]["audio_windows"]], starts)

    def test_backpressure_stops_real_reader_while_sink_waits(self):
        entered, release = threading.Event(), threading.Event()
        readers, results = [], []
        original_reader = media.CountingReader
        def reader_factory(*args, **kwargs):
            instance = original_reader(*args, **kwargs)
            readers.append(instance)
            return instance
        class SlowSink(FakeASR):
            def transcribe(inner, pcm, start, end):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test release missing")
                return super().transcribe(pcm, start, end)
        with patch.object(media, "ASR", SlowSink), patch.object(media, "CountingReader", side_effect=reader_factory):
            thread = threading.Thread(target=lambda: results.append(self.process(cfg={**self.cfg, "asr_enabled": True, "max_windows": 1})))
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                snapshot = (readers[0].received, readers[0].read_calls)
                self.assertTrue(thread.is_alive())
                self.assertEqual((readers[0].received, readers[0].read_calls), snapshot)
            finally:
                release.set()
                thread.join(15)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results[0]["metadata"]["audio_windows"]), 1)

    def test_exact_eof_pause_does_not_emit_overlap_only_window(self):
        make_silence(self.source, 11)
        first = self.process(cfg={**self.cfg, "max_windows": 3})
        self.assertEqual(len(first["metadata"]["audio_windows"]), 3)
        resumed = self.process()
        self.assertEqual([w["start_ms"] for w in resumed["metadata"]["audio_windows"]], [0, 3000, 6000])
        self.assertEqual(resumed["metrics"][0]["observed"], 11000)
        self.assertEqual(resumed["enumeration_state"], "complete")

    def test_prepared_transcript_reconciles_crash_without_reinference(self):
        real_write = media.Store.write
        def crash(store, relative, *args):
            if relative.startswith("transcripts/"):
                raise RuntimeError("synthetic crash between prepared receipt and artifact publication")
            return real_write(store, relative, *args)
        with patch.object(media, "ASR", FakeASR), patch.object(media.Store, "write", crash):
            first = self.process(cfg={**self.cfg, "asr_enabled": True, "max_windows": 1})
        self.assertEqual(len(FakeASR.calls), 1)
        self.assertEqual(first["enumeration_state"], "partial")
        with patch.object(media, "ASR", FakeASR):
            recovered = self.process(cfg={**self.cfg, "asr_enabled": True, "max_windows": 1})
        self.assertEqual(len(FakeASR.calls), 1, "durable prepared result reused, not ASR submitted twice")
        self.assertEqual([w["start_ms"] for w in recovered["metadata"]["audio_windows"]], [0])
        self.assertEqual(len([b for b in recovered["blocks"] if b["origin"] == "asr"]), 1)
        derivation = recovered["metadata"]["derivations"][0]
        self.assertEqual(derivation["block_indexes"], [1])
        self.assertEqual(derivation["source_block_indexes"], [0])

    def test_changed_source_and_changed_artifact_fail_closed(self):
        self.process(cfg={**self.cfg, "max_windows": 1})
        with self.source.open("ab") as out:
            out.write(b"changed")
        with self.assertRaisesRegex(ValueError, "BINDING_MISMATCH"):
            self.process()
        # Independent output binding for artifact integrity test.
        result = self.process(name="integrity")
        file = self.root / "integrity" / result["files"][0]["relative_path"]
        file.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "ARTIFACT_MISMATCH"):
            self.process(name="integrity")

    def test_missing_weights_yields_no_fake_asr(self):
        result = self.process(cfg={**self.cfg, "asr_enabled": True, "model_dir": str(self.root / "missing-model")})
        self.assertFalse(any(b["origin"] == "asr" for b in result["blocks"]))
        self.assertTrue(any("WEIGHTS_MISSING" in g["reason"] for g in result["gaps"]))
        self.assertEqual(result["metrics"][0]["processed"], 0)

    def test_wrong_weights_not_admitted(self):
        root = self.root / "wrong-weights"
        root.mkdir()
        for name in ("model.bin", "tokenizer.json", "config.json", "vocabulary.txt"):
            (root / name).write_bytes(b"synthetic invalid model")
        backend = media.ASR({**self.cfg, "asr_enabled": True, "model_dir": str(root)})
        self.assertIsNone(backend.model)
        self.assertIn("WEIGHTS_NOT_ADMITTED", backend.reason)

    def test_float32_conversion_only_current_window_and_lazy_inference_consumed(self):
        import numpy as np
        calls = []
        class Backend:
            def transcribe(self, audio, **kwargs):
                calls.append((type(audio), audio.dtype, audio.size))
                def inference():
                    calls.append("generator-executed")
                    if False: yield None
                return inference(), types.SimpleNamespace(language="en")
        backend = media.ASR.__new__(media.ASR)
        backend.model, backend.reason, backend.version, backend.cfg = Backend(), None, "1.2.1", {"language": "en"}
        result = backend.transcribe(b"\0\0" * 1600, 1000, 1100)
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["segments"], [])
        self.assertEqual(calls[0], (np.ndarray, np.dtype("float32"), 1600))
        self.assertEqual(calls[-1], "generator-executed")

    def test_failure_signature_disables_backend_not_retried_for_each_window(self):
        calls = []
        class BadBackend:
            def transcribe(self, *args, **kwargs):
                calls.append(1)
                raise RuntimeError("deterministic test failure")
        backend = media.ASR.__new__(media.ASR)
        backend.model, backend.reason, backend.version, backend.cfg = BadBackend(), None, "1.2.1", {"language": None}
        first = backend.transcribe(b"\0\0" * 1600, 0, 100)
        second = backend.transcribe(b"\0\0" * 1600, 100, 200)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "unsupported")
        self.assertEqual(calls, [1])

    def test_overlap_dedup_uses_time_and_words_preserves_nonoverlap_repeat(self):
        previous = [{"start_ms": 1000, "end_ms": 2000, "text": "hello world", "words": [{"start_ms": 1500, "end_ms": 2000, "text": " world"}]}]
        current = [{"start_ms": 1600, "end_ms": 2500, "text": "world again", "words": [{"start_ms": 1600, "end_ms": 1950, "text": " world"}, {"start_ms": 2100, "end_ms": 2500, "text": " again"}]}]
        kept = media._dedupe_segments(current, previous)
        self.assertEqual(kept[0]["text"], "again")
        self.assertEqual(kept[0]["start_ms"], 2100)
        repeated = [{"start_ms": 3000, "end_ms": 4000, "text": "hello world", "words": []}]
        self.assertEqual(media._dedupe_segments(repeated, previous), repeated)
        self.assertEqual(current[0]["text"], "world again", "raw hypotheses not mutated")

    def test_silent_visual_changes_periodic_floor_and_no_complete_visual_claim(self):
        source = FIXTURES / "synthetic_silent_changes.mp4"
        if not source.exists():
            self.skipTest("explicit synthetic fixture not generated")
        result = media.extract_media(source, self.root / "mixed", options={**self.cfg, "visual_period_seconds": 2})
        self.assertEqual(result["enumeration_state"], "complete", result)
        frames = result["metadata"]["visual_frames"]
        self.assertGreaterEqual(len(frames), 6)
        self.assertTrue(any("sampled_local_pixel_change_including_silent_activity" in f["triggers"] for f in frames))
        self.assertTrue(any("periodic_floor" in f["triggers"] for f in frames))
        self.assertEqual(result["processing_status"], "partial")
        self.assertEqual(result["metrics"][0]["observed"], 12000, "AAC tail padding must not inflate coverage")
        self.assertIsNone(result["metrics"][1]["expected"])
        self.assertTrue(any(g["kind"] == "semantic_gap" for g in result["gaps"]))
        self.assertEqual(result["metadata"]["runs"][0]["mixed_source_demux_passes"], 1)
        self.assertLessEqual(result["metadata"]["ram"]["peak_worker_commit_bytes"], result["metadata"]["ram"]["hard_cap_bytes"])
        for file in result["files"]:
            self.assertTrue((self.root / "mixed" / file["relative_path"]).is_file())
        schema = json.loads((Path(__file__).parents[1] / "worldanalysis_agent_devpack_20261110/contracts/bundle.schema.json").read_text(encoding="utf-8"))
        import jsonschema
        validator = jsonschema.Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/coverageMetric"})
        for metric in result["metrics"]:
            validator.validate(metric)
            self.assertLessEqual(metric["processed"], metric["observed"])

    def test_input_budget_is_actual_bytes_and_explicit_resource_gap(self):
        result = self.process(cfg={**self.cfg, "input_byte_cap": 65536})
        self.assertEqual(result["enumeration_state"], "partial")
        self.assertLessEqual(result["metadata"]["input_bytes_received"], 65536)
        self.assertTrue(any(g["kind"] == "resource_block" for g in result["gaps"]), result)

    def test_hard_ram_failure_does_not_silently_fallback(self):
        result = media.extract_media(self.source, self.root / "tiny-memory", options={**self.cfg, "ram_byte_cap": 16 * 1024**2})
        self.assertEqual(result["processing_status"], "blocked")
        self.assertIn("RAM_", result["gaps"][0]["reason"])
        self.assertFalse(result["blocks"])

    def test_real_asr_requires_actual_synthetic_speech_output(self):
        source = FIXTURES / "synthetic_tts.wav"
        if not source.exists() or not (media._DEFAULT_MODEL / "model.bin").exists():
            self.skipTest("reviewed offline weights/synthetic system-TTS fixture unavailable")
        result = media.extract_media(source, self.root / "real-asr", options={"ocr_enabled": False, "language": "en", "synthetic": True})
        texts = " ".join(b.get("text", "") for b in result["blocks"] if b["origin"] == "asr")
        self.assertIn("synthetic", texts.lower(), result)
        self.assertIn("42", texts, result)
        self.assertTrue(result["metadata"]["audio_windows"][0]["asr_elapsed_seconds"] > 0)
        self.assertEqual(result["metadata"]["production_acceptance"], "pending_real_authorized_course_input")
        self.assertTrue(result["metadata"]["synthetic"])
        self.assertEqual(result["metrics"][0]["processed"], result["metrics"][0]["observed"])
        self.assertLessEqual(result["metadata"]["peak_pcm_bytes"], 300 * 16000 * 2)


if __name__ == "__main__":
    unittest.main()
