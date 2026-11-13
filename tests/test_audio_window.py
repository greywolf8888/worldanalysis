"""Short, real-wave synthetic tests; no real course inputs, models, ASR or DB."""
from fractions import Fraction
import hashlib
import os
from pathlib import Path
import struct
import tempfile
import types
import unittest
from unittest.mock import patch
import wave

import av
import numpy as np

from worldanalysis import audio_window as aw
from worldanalysis import media
from worldanalysis.media_store import identity


def pcm(start, count):
    return struct.pack("<" + "h" * count, *((i % 20000) - 10000 for i in range(start, start + count)))


def make_wave(path, samples, *, rate=16000, silence=False):
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(rate)
        for start in range(0, samples, 4096):
            count = min(4096, samples - start)
            sink.writeframesraw(bytes(count * 2) if silence else pcm(start, count))


def make_mixed(path, *, audio=True):
    """Generate a tiny local container, including noisy video bytes if mixed."""
    rng = np.random.default_rng(5)
    with av.open(str(path), "w", format="matroska") as sink:
        video = sink.add_stream("ffv1", rate=10)
        video.width = video.height = 32
        video.pix_fmt = "yuv420p"
        sound = sink.add_stream("pcm_s16le", rate=16000) if audio else None
        if sound is not None:
            sound.layout = "mono"
        for index in range(10):
            frame = av.VideoFrame.from_ndarray(rng.integers(0, 255, (32, 32, 3), dtype=np.uint8), format="rgb24")
            frame.pts, frame.time_base = index, Fraction(1, 10)
            for packet in video.encode(frame):
                sink.mux(packet)
            if sound is not None:
                frame = av.AudioFrame(format="s16", layout="mono", samples=1600)
                frame.planes[0].update(pcm(index * 1600, 1600))
                frame.sample_rate, frame.pts, frame.time_base = 16000, index * 1600, Fraction(1, 16000)
                for packet in sound.encode(frame):
                    sink.mux(packet)
        for packet in video.encode(None):
            sink.mux(packet)
        if sound is not None:
            for packet in sound.encode(None):
                sink.mux(packet)


class FakeContainer:
    """Timestamp/packet fault injection; real wave tests cover the actual decoder."""
    def __init__(self, reader, pieces):
        reader.read(44)
        self.pieces = pieces
        self.start_time = 0
        self.context = types.SimpleNamespace(sample_rate=16000, channels=1, thread_count=0, options={})
        self.audio = types.SimpleNamespace(codec_context=self.context, index=0)
        self.streams = types.SimpleNamespace(audio=[self.audio])
        self.seeks = []

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def seek(self, *args, **kwargs): self.seeks.append((args, kwargs))
    def demux(self, audio):
        for stamp, count in self.pieces:
            frame = av.AudioFrame(format="s16", layout="mono", samples=count)
            frame.planes[0].update(pcm(stamp or 0, count))
            frame.sample_rate, frame.pts, frame.time_base = 16000, stamp, Fraction(1, 16000)
            yield types.SimpleNamespace(size=count * 2, decode=lambda frame=frame: [frame])


class AudioWindowTests(unittest.TestCase):
    def setUp(self):
        # All source and output IO stays in the authorized G workspace scope.
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-audio-window-", dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "synthetic.wav"
        make_wave(self.source, 6 * 16000)
        self.expected = identity(self.source)

    def extract(self, start=2940, end=4420, name="window", **kwargs):
        return aw.extract_audio_window(self.root, self.source, f"windows/{name}.wav", start, end,
                                       expected_identity=self.expected, **kwargs)

    def assert_no_final(self, name="window"):
        self.assertFalse((self.root / f"windows/{name}.wav").exists())
        self.assertFalse((self.root / f"windows/{name}.wav.manifest.json").exists())

    def test_real_wave_exact_half_open_samples_across_packet_boundaries(self):
        for index, (start, end) in enumerate(((0, 1), (1, 257), (127, 2049), (2940, 4420), (1200, 1201))):
            with self.subTest(start=start, end=end):
                result = self.extract(start, end, name=str(index))
                output = self.root / result["request"]["output_relative"]
                with wave.open(str(output), "rb") as check:
                    self.assertEqual((check.getnchannels(), check.getsampwidth(), check.getframerate()), (1, 2, 16000))
                    self.assertEqual(check.getnframes(), (end - start) * 16)
                    self.assertEqual(check.readframes(check.getnframes()), pcm(start * 16, (end - start) * 16))
                self.assertEqual(result["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
                self.assertEqual(result["byte_length"], output.stat().st_size)
                self.assertTrue(result["does_not_prove_whole_source_content_sha"])
                self.assertIsNone(result["source_content_sha256"])
                self.assertFalse(result["source_copy_retained"])
                self.assertFalse(result["asr_executed"])
                self.assertFalse(result["real_course_acceptance"])
                self.assertLessEqual(result["peak_frame_bytes"], 1024**2)
                self.assertNotIn("only_audio_transferred", str(result))

    def test_silence_is_retained_without_vad(self):
        make_wave(self.source, 6 * 16000, silence=True)
        self.expected = identity(self.source)
        result = self.extract()
        self.assertFalse(result["vad_applied"])
        with wave.open(str(self.root / result["request"]["output_relative"]), "rb") as check:
            self.assertEqual(check.readframes(result["sample_count"]), bytes(result["sample_count"] * 2))

    def test_invalid_range_types_bool_and_over_300_seconds_fail_before_io(self):
        for start, end in ((True, 10), (0, True), (0.0, 10), (0, "10"), (-1, 5), (5, 5), (5, 4), (0, 300001)):
            with self.subTest(start=start, end=end), patch.object(aw, "_Reader", side_effect=AssertionError("must not read")):
                with self.assertRaisesRegex(aw.AudioWindowError, "INVALID_WINDOW"):
                    self.extract(start, end)
        self.assert_no_final()

    def test_budget_types_and_output_cap_fail_before_read(self):
        for key in ("max_received_bytes", "max_output_bytes"):
            for value in (True, 0, -1, 1.5, 16 * 1024**3):
                with self.subTest(key=key, value=value), patch.object(aw, "_Reader", side_effect=AssertionError("must not read")):
                    with self.assertRaises(aw.AudioWindowError):
                        self.extract(**{key: value})
        with patch.object(aw, "_Reader", side_effect=AssertionError("must not read")):
            with self.assertRaises(media.MediaLimit):
                self.extract(max_output_bytes=1000)
        self.assert_no_final()

    def test_actual_received_budget_counts_and_stops_at_cap(self):
        readers, original = [], aw._Reader
        def reader(*args):
            result = original(*args)
            readers.append(result)
            return result
        with patch.object(aw, "_Reader", side_effect=reader):
            with self.assertRaises(media.MediaLimit):
                self.extract(max_received_bytes=8192)
        self.assertEqual(readers[0].received, 8192)
        self.assertLessEqual(readers[0].max_read, 1024**2)
        self.assert_no_final()
        self.assertFalse(list((self.root / "windows").glob(".audio-window-*")))

    def test_registration_identity_exact_match_and_change_outside_window(self):
        for field in self.expected:
            wrong = dict(self.expected)
            wrong[field] = "changed" if field == "basis" else self.expected[field] + 1
            with self.subTest(field=field), patch.object(aw, "_Reader", side_effect=AssertionError("must not read")):
                with self.assertRaisesRegex(aw.AudioWindowError, "REGISTRATION_IDENTITY"):
                    aw.extract_audio_window(self.root, self.source, "windows/window.wav", 100, 200, expected_identity=wrong)
        with self.source.open("ab") as handle:
            handle.write(b"outside requested time window changed")
        with self.assertRaisesRegex(aw.AudioWindowError, "REGISTRATION_IDENTITY"):
            self.extract()
        self.assert_no_final()

    def test_source_changes_during_read_no_success_or_final_wav(self):
        real_read = aw._Reader.read
        mutated = False
        def read(reader, size=-1):
            nonlocal mutated
            value = real_read(reader, size)
            if not mutated:
                mutated = True
                with self.source.open("ab") as handle:
                    handle.write(b"synthetic identity change")
            return value
        with patch.object(aw._Reader, "read", new=read):
            with self.assertRaisesRegex(aw.AudioWindowError, "IDENTITY_CHANGED"):
                self.extract()
        self.assert_no_final()

    def test_source_changes_after_decode_before_publication_is_rejected(self):
        real = aw._decode
        def decode(*args):
            result = real(*args)
            with self.source.open("ab") as handle:
                handle.write(b"synthetic post decode mutation")
            return result
        with patch.object(aw, "_decode", side_effect=decode):
            with self.assertRaisesRegex(aw.AudioWindowError, "IDENTITY_CHANGED"):
                self.extract()
        self.assert_no_final()

    def test_identical_repeat_does_not_open_or_decode_source(self):
        result = self.extract()
        with patch.object(aw, "_Reader", side_effect=AssertionError("source re-read prohibited")), patch.object(aw, "_decode", side_effect=AssertionError("re-decode prohibited")):
            self.assertEqual(result, self.extract())
        self.assertFalse(list((self.root / "windows").glob(".audio-window-*")))
        self.assertFalse((self.root / "windows/window.wav.prepared.json").exists())

    def test_changed_registered_source_still_fails_on_cached_repeat(self):
        self.extract()
        with self.source.open("ab") as handle:
            handle.write(b"changed")
        with patch.object(aw, "_Reader", side_effect=AssertionError("source re-read prohibited")):
            with self.assertRaisesRegex(aw.AudioWindowError, "REGISTRATION_IDENTITY"):
                self.extract()

    def test_existing_output_or_changed_request_is_not_overwritten(self):
        windows = self.root / "windows"
        windows.mkdir()
        final = windows / "occupied.wav"
        final.write_bytes(b"unrelated existing data")
        with self.assertRaisesRegex(aw.AudioWindowError, "WITHOUT_RECOVERY"):
            self.extract(name="occupied")
        self.assertEqual(final.read_bytes(), b"unrelated existing data")
        self.extract()
        with patch.object(aw, "_Reader", side_effect=AssertionError("must not re-decode")):
            with self.assertRaisesRegex(aw.AudioWindowError, "BINDING_MISMATCH"):
                self.extract(100, 200)

    def test_changed_output_sha_is_rejected_without_source_redecode(self):
        self.extract()
        final = self.root / "windows/window.wav"
        with final.open("r+b") as handle:
            handle.seek(60)
            handle.write(b"tampered")
        with patch.object(aw, "_Reader", side_effect=AssertionError("must not re-decode")):
            with self.assertRaisesRegex(aw.AudioWindowError, "SHA256_MISMATCH"):
                self.extract()

    def test_no_source_copy_pre_hash_or_whole_ndarray(self):
        source_bytes = self.source.read_bytes()
        real_hash = aw._wav_hash
        def derived_hash(path, size):
            self.assertNotEqual(Path(path).resolve(), self.source.resolve(), "source must not be hashed")
            return real_hash(path, size)
        with patch.object(aw, "_wav_hash", side_effect=derived_hash), patch("shutil.copyfile", side_effect=AssertionError("no copying")), patch("shutil.copy2", side_effect=AssertionError("no copying")), patch.object(media, "_sha", side_effect=AssertionError("no full source hash")):
            self.extract()
        self.assertEqual(self.source.read_bytes(), source_bytes)
        self.assertEqual(sorted(p.name for p in (self.root / "windows").iterdir()), [".media.lock", "window.wav", "window.wav.manifest.json"])

    def test_video_only_is_explicitly_unsupported(self):
        source = self.root / "synthetic-video.mkv"
        make_mixed(source, audio=False)
        self.source, self.expected = source, identity(source)
        with self.assertRaisesRegex(aw.AudioWindowError, "UNSUPPORTED_NO_AUDIO_TRACK"):
            self.extract(100, 300)
        self.assert_no_final()

    def test_mixed_container_counts_actual_bytes_not_audio_transfer_claim(self):
        source = self.root / "synthetic-mixed.mkv"
        make_mixed(source)
        self.source, self.expected = source, identity(source)
        readers, original = [], aw._Reader
        def reader(*args):
            instance = original(*args)
            readers.append(instance)
            return instance
        with patch.object(aw, "_Reader", side_effect=reader):
            result = self.extract(100, 500)
        self.assertEqual(result["sample_count"], 6400)
        self.assertEqual(result["received_bytes"], readers[0].received)
        self.assertGreater(result["received_bytes"], 6400 * 2)
        self.assertIn("mixed_streams", result["received_bytes_scope"])
        self.assertNotIn("only_audio_transferred", str(result))

    def test_long_seek_reads_only_bounded_nearby_chunks(self):
        make_wave(self.source, 650 * 16000, silence=True)
        self.expected = identity(self.source)
        result = self.extract(500000, 501000, max_received_bytes=512 * 1024)
        self.assertLessEqual(result["received_bytes"], 512 * 1024)
        self.assertLess(result["received_bytes"], self.source.stat().st_size // 10)
        self.assertGreaterEqual(result["actual_first_decoded_offset_ms"], 498000)
        self.assertEqual(result["sample_count"], 16000)
        self.assertLess(result["decoded_frames"], 64)

    def test_exact_300_second_limit_with_streamed_silent_output(self):
        make_wave(self.source, 301 * 16000, silence=True)
        self.expected = identity(self.source)
        result = self.extract(0, 300000)
        self.assertEqual(result["sample_count"], 4_800_000)
        self.assertEqual(result["byte_length"], aw.DEFAULT_MAX_OUTPUT_BYTES)
        self.assertLessEqual(result["peak_frame_bytes"], 1024**2)

    def test_eof_does_not_fabricate_tail_silence(self):
        with self.assertRaisesRegex(aw.AudioWindowError, "NOT_FULLY_COVERED"):
            self.extract(5900, 6100)
        self.assert_no_final()

    def test_arbitrary_small_frame_boundaries_do_not_lose_or_duplicate_samples(self):
        pieces = [(0, 3), (3, 2), (5, 8), (13, 19), (32, 31), (63, 100)]
        with patch.object(media, "_open", side_effect=lambda reader, cfg: FakeContainer(reader, pieces)):
            result = self.extract(1, 3)
        with wave.open(str(self.root / "windows/window.wav"), "rb") as check:
            self.assertEqual(check.readframes(1000), pcm(16, 32))
        self.assertEqual(result["sample_count"], 32)

    def test_missing_pts_gap_and_false_seek_max_pts_do_not_claim_coverage(self):
        cases = (([(None, 32)], 0, 1, "TIMESTAMP_MISSING"),
                 ([(0, 32), (16000, 32)], 0, 100, "DISCONTINUITY"),
                 ([(0, 1600)], 100000, 100100, "SEEK_NOT_NEAR"),
                 ([(3200, 1600)], 100, 400, "TIMESTAMP_GAP"))
        for index, (pieces, start, end, message) in enumerate(cases):
            with self.subTest(message=message), patch.object(media, "_open", side_effect=lambda reader, cfg, pieces=pieces: FakeContainer(reader, pieces)):
                with self.assertRaisesRegex(aw.AudioWindowError, message):
                    self.extract(start, end, name=str(index))
                self.assert_no_final(str(index))

    def test_unavailable_long_seek_is_not_retried_as_full_scan(self):
        with patch.object(FakeContainer, "seek", side_effect=RuntimeError("synthetic unsupported seek")), patch.object(media, "_open", side_effect=lambda reader, cfg: FakeContainer(reader, [(0, 1600)])):
            with self.assertRaisesRegex(aw.AudioWindowError, "UNSUPPORTED_BOUNDED_SEEK"):
                self.extract(100000, 100100)
        self.assert_no_final()

    def test_pipe_only_whitelist_reuses_existing_safe_open(self):
        real_open, calls = av.open, []
        def open(*args, **kwargs):
            calls.append((args, kwargs))
            self.assertIsInstance(args[0], media.CountingReader)
            self.assertEqual(kwargs["options"]["protocol_whitelist"], "pipe")
            self.assertEqual(kwargs["options"]["format_whitelist"], media._FORMATS)
            self.assertEqual(kwargs["options"]["enable_drefs"], "0")
            return real_open(*args, **kwargs)
        with patch.object(av, "open", side_effect=open):
            self.extract()
        self.assertEqual(len(calls), 1)

    def test_scope_output_paths_and_links_are_rejected(self):
        for value in ("https://example.invalid/a.wav", "../synthetic.wav", "file:///x.wav"):
            with self.subTest(value=value), self.assertRaises((ValueError, RuntimeError)):
                aw.extract_audio_window(self.root, value, "windows/x.wav", 0, 100, expected_identity=self.expected)
        for value in ("x.wav", "../x.wav", "windows/../x.wav", "windows\\x.wav", "windows/x.mp3", "windows/x.wav:stream"):
            with self.subTest(output=value), self.assertRaises((ValueError, RuntimeError)):
                aw.extract_audio_window(self.root, self.source, value, 0, 100, expected_identity=self.expected)
        other = self.root.parent / (self.root.name + "-outside.wav")
        make_wave(other, 1600)
        self.addCleanup(lambda: other.unlink(missing_ok=True))
        with self.assertRaisesRegex(ValueError, "OUTSIDE_AUTHORIZED_ROOT"):
            aw.extract_audio_window(self.root, other, "windows/x.wav", 0, 100, expected_identity=identity(other))
        link = self.root / "source-link.wav"
        try:
            link.symlink_to(self.source)
        except OSError:
            return  # Windows may lack Developer Mode symlink permission.
        with self.assertRaises((ValueError, RuntimeError)):
            aw.extract_audio_window(self.root, link, "windows/x.wav", 0, 100, expected_identity=self.expected)

    def test_publication_failure_removes_only_own_files(self):
        windows = self.root / "windows"
        windows.mkdir()
        other = windows / ".audio-window-unrelated.wav.part"
        other.write_bytes(b"not owned by this invocation")
        real = aw._write_json
        def fail(parent, destination, value, owned):
            if destination.name.endswith(".manifest.json"):
                raise RuntimeError("synthetic commit failure")
            return real(parent, destination, value, owned)
        with patch.object(aw, "_write_json", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "synthetic commit"):
                self.extract()
        self.assert_no_final()
        self.assertEqual(other.read_bytes(), b"not owned by this invocation")
        self.assertEqual(sorted(p.name for p in windows.iterdir()), [".audio-window-unrelated.wav.part", ".media.lock"])

    def test_interrupted_publication_recovers_without_source_read_or_decode(self):
        real = aw._write_json
        def crash(parent, destination, value, owned):
            if destination.name.endswith(".manifest.json"):
                raise RuntimeError("synthetic process died after prepared WAV publication")
            return real(parent, destination, value, owned)
        # A real killed process cannot run finally; retain only this test's temp tree.
        with patch.object(aw, "_write_json", side_effect=crash), patch.object(aw, "_cleanup"):
            with self.assertRaisesRegex(RuntimeError, "synthetic process died"):
                self.extract()
        self.assertTrue((self.root / "windows/window.wav.prepared.json").exists())
        self.assertFalse((self.root / "windows/window.wav.manifest.json").exists())
        with patch.object(aw, "_Reader", side_effect=AssertionError("no source re-read")), patch.object(aw, "_decode", side_effect=AssertionError("no second decode")):
            result = self.extract()
            self.assertEqual(result, self.extract())
        self.assertEqual(result["sample_count"], 23680)
        self.assertTrue((self.root / "windows/window.wav.manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
