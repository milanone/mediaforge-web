"""/api/detect_fps, which the page now calls by itself when a file is selected: verdict and cache, and agreement with
the desktop's classifica_fps on the same file (real ffmpeg, a clip of 24 fps stored at 30 with repeated frames)."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT.parent / "MediaForge" / "MediaForge.pyw"
_tmp = tempfile.mkdtemp(prefix="mediaforge_web_test_")
os.environ.setdefault("MEDIAFORGE_FFMPEG", shutil.which("ffmpeg") or "ffmpeg")
os.environ.setdefault("MEDIAFORGE_FFPROBE", shutil.which("ffprobe") or "ffprobe")
os.environ.setdefault("MEDIAFORGE_LIBRARY", _tmp)
os.environ.setdefault("MEDIAFORGE_UPLOADS", os.path.join(_tmp, "uploads"))
os.environ.setdefault("MEDIAFORGE_OUTPUT", os.path.join(_tmp, "output"))
sys.path.insert(0, str(ROOT))
import app as web  # noqa: E402


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
class AutoFps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name, rate, vf in (("dup30.mkv", 24, "fps=30"), ("real24.mkv", 24, "null"), ("dup50.mkv", 25, "fps=50")):
            subprocess.run([web.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={rate}",
                            "-t", "60", "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            str(web.LIBRARY_DIR / name)], check=True)
        cls.client = web.app.test_client()

    def test_a_whole_ratio_is_regular_and_can_be_dropped(self):
        d = self.client.get("/api/detect_fps?source=library&path=dup50.mkv").get_json()
        self.assertEqual(d["suggestion"], "25")
        self.assertTrue(d["regular"])

    def test_24_stored_as_30_is_found_but_not_regular(self):
        d = self.client.get("/api/detect_fps?source=library&path=dup30.mkv").get_json()
        self.assertEqual(d["suggestion"], "24")
        self.assertFalse(d["regular"])          # one repeat every 5 frames: left alone

    def test_repeated_frames_give_a_suggestion_and_the_answer_is_cached(self):
        d = self.client.get("/api/detect_fps?source=library&path=dup30.mkv").get_json()
        self.assertEqual(d["suggestion"], "24")
        self.assertGreater(d["duplicates_pct"], 15)
        self.assertTrue(any(k[0].endswith("dup30.mkv") for k in web._fps_cache))
        self.assertEqual(self.client.get("/api/detect_fps?source=library&path=dup30.mkv").get_json(), d)

    def test_a_file_without_repeated_frames_has_no_suggestion(self):
        d = self.client.get("/api/detect_fps?source=library&path=real24.mkv").get_json()
        self.assertEqual(d["suggestion"], "")
        self.assertFalse(d["irregular"])

    def test_agrees_with_the_desktop_verdict(self):
        if not DESKTOP.exists():
            self.skipTest("desktop app not available")
        spec = importlib.util.spec_from_file_location("mediaforge_desktop", DESKTOP)
        desk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(desk)
        src = web.LIBRARY_DIR / "dup30.mkv"
        data = desk.ffprobe_json(src)
        video = next(s for s in data["streams"] if s["codec_type"] == "video")
        r = desk.classifica_fps(desk.fps_da_frazione(video["avg_frame_rate"]), desk.fps_contenuto(src, 60.0),
                                desk.analizza_fps(src, video))
        d = self.client.get("/api/detect_fps?source=library&path=dup30.mkv").get_json()
        self.assertEqual(r["azione"], "keep")
        self.assertEqual(desk.fps_da_frazione(r["std"]), float(d["suggestion"]))
        self.assertFalse(d["regular"])                       # both leave a 24-as-30 file alone


if __name__ == "__main__":
    unittest.main()
