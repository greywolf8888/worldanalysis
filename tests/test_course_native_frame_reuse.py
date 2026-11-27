"""Exact sparse/nearby video frame selection and publication pixel checks."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_tools/course_production"))

import av
from PIL import Image
import frames
from project import put, read, sha
from publish import check_video_figures


def fingerprints(images):
    return [(pts, image.size, hashlib.sha256(image.convert("RGB").tobytes()).hexdigest())
            for image, pts in images]


@unittest.skipUnless(shutil.which("ffmpeg"), "需要本机 FFmpeg")
class NativeFrameReuseTest(unittest.TestCase):
    def setUp(self):
        parent = ROOT.parent / "tmp" if ROOT.parent.name == ".runtime" else ROOT / ".runtime/tmp"
        parent.mkdir(parents=True, exist_ok=True)
        scratch = TemporaryDirectory(dir=parent, prefix="native-frame-test-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.project = self.root / "课程包"
        self.raw = self.project / "原始资料"
        self.raw.mkdir(parents=True)
        self.clip = self.raw / "变帧率.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=128x72:rate=20:duration=4",
                        "-vf", "select=not(mod(n\\,2))+not(mod(n\\,7))", "-fps_mode", "vfr", "-c:v", "libx264",
                        "-g", "8", "-pix_fmt", "yuv420p", "-output_ts_offset", "5", str(self.clip)], check=True)
        with av.open(str(self.clip)) as container:
            video = container.streams.video[0]
            decoded = list(container.decode(video))
            self.origin = decoded[0].time
            self.times = [frame.time - self.origin for frame in decoded]
            self.tail = self.times[-1] + float((decoded[-1].duration or 0) * decoded[-1].time_base)
        self.assertGreater(self.origin, 0)
        self.assertGreater(len({round(b-a, 6) for a,b in zip(self.times, self.times[1:])}), 1)

    def assert_same(self, times):
        expected = fingerprints(frames.get_native_frame(self.clip, when, self.origin) for when in times)
        actual = fingerprints(frames.get_native_frames(self.clip, times, self.origin))
        self.assertEqual(actual, expected)

    def test_variable_rate_origin_keyframe_boundaries_and_tail(self):
        requests = list(self.times[2:14]) + [self.times[-1], (self.times[-1]+self.tail)/2]
        self.assert_same(requests)
        self.assert_same([self.times[4], self.times[4], self.times[4] + .012])
        self.assert_same([self.times[7], self.times[3], self.times[2]])
        with self.assertRaises(ValueError):
            list(frames.get_native_frames(self.clip, [self.tail+1, self.tail+1.1], self.origin))

    def test_sparse_uses_original_seek_and_uncertain_probe_falls_back(self):
        with patch.object(frames, "seek_anchor", side_effect=AssertionError("sparse seek was probed")):
            self.assert_same([self.times[0], self.times[-1]])
            self.assert_same(self.times[:2])
            self.assert_same([self.times[2], self.times[2]])
        with patch.object(frames, "seek_anchor", return_value=None):
            self.assert_same(self.times[2:8])
        with patch.object(frames, "decode_shared_anchor", side_effect=ValueError("decode fixture failed")):
            self.assert_same(self.times[2:8])

    def test_exact_crops_kept_and_bad_pixel_rejected(self):
        course_state = self.project / ".course"
        ledger = course_state / "ledger"
        art = course_state / "work/选用素材"
        put(ledger / "01.json", {"source": self.clip.name, "sha256": sha(self.clip),
                                 "kind": "video", "timeline_origin_s": self.origin,
                                 "duration_s": self.tail + .5})
        picks = [{"lesson": 1, "name": f"图{i}", "time": when, "crop": [2, 3, 80, 60]}
                 for i,when in enumerate(self.times[2:8])]
        frames.keep(self.project, picks, ledger=ledger, output_root=art)
        selected = read(ledger / "figures.json")
        for item, pick in zip(selected, picks):
            image, pts = frames.get_native_frame(self.clip, pick["time"], self.origin)
            self.assertEqual(item["pts"], pts)
            with Image.open(art / item["name"]) as actual:
                self.assertEqual(actual.convert("RGB").tobytes(), image.crop(pick["crop"]).convert("RGB").tobytes())
        check_video_figures(self.project, selected, ledger=ledger, art_root=art)
        path = art / selected[3]["name"]
        with Image.open(path) as opened:
            image = opened.convert("RGB")
        pixel = image.getpixel((0,0))
        image.putpixel((0,0), tuple((value+1)%256 for value in pixel))
        image.save(path)
        with self.assertRaisesRegex(ValueError, "裁图不符"):
            check_video_figures(self.project, selected, ledger=ledger, art_root=art)

    def test_invalid_project_timing_keeps_guard(self):
        ledger = self.project / ".course/ledger"
        put(ledger / "01.json", {"source": self.clip.name, "kind": "video", "duration_s": self.tail})
        for when in (-1, self.tail, float("nan")):
            with self.assertRaisesRegex(ValueError, "超出第"):
                list(frames.orig_many(self.project, 1, [when], ledger=ledger))


if __name__ == "__main__":
    unittest.main()
