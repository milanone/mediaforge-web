"""Codec "Copia originale": -c:v copy, every video option ignored (command built without ffmpeg, plus one real run)."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_tmp = tempfile.mkdtemp(prefix="mediaforge_web_test_")
os.environ.setdefault("MEDIAFORGE_FFMPEG", shutil.which("ffmpeg") or "ffmpeg")
os.environ.setdefault("MEDIAFORGE_FFPROBE", shutil.which("ffprobe") or "ffprobe")
os.environ.setdefault("MEDIAFORGE_LIBRARY", _tmp)
os.environ.setdefault("MEDIAFORGE_UPLOADS", os.path.join(_tmp, "uploads"))
os.environ.setdefault("MEDIAFORGE_OUTPUT", os.path.join(_tmp, "output"))
sys.path.insert(0, str(ROOT))
import app as web  # noqa: E402

PROBE = {"streams": [{"codec_type": "video", "index": 0, "codec_name": "hevc", "width": 3840, "height": 2160,
                      "pix_fmt": "yuv420p10le", "r_frame_rate": "24/1"},
                     {"codec_type": "audio", "index": 1, "codec_name": "eac3"}],
         "format": {"duration": "100"}}
OPTS = {"codec": "copy", "quality": 16, "resolution_cap": 1080, "fps": "25", "flip": "h", "ar_mode": "crop",
        "ar_ratio": "16:9", "bitrate_kbps": 2000, "preset": "slow", "look_ahead": True}


class Command(unittest.TestCase):
    def build(self, dst="out.mkv", **extra):
        return web.build_cmd(Path("in.mkv"), Path(dst), dict(OPTS, **extra), PROBE)

    def test_video_is_copied_and_no_video_option_survives(self):
        cmd = self.build()
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        for flag in ("-vf", "-crf", "-global_quality", "-b:v", "-preset", "-look_ahead", "-hwaccel"):
            self.assertNotIn(flag, cmd)

    def test_the_stream_statistics_stay_valid_unless_trimmed(self):
        plain = " ".join(self.build())
        self.assertNotIn("BPS=", plain)            # the tags are left as they are
        self.assertIn("BPS=", " ".join(self.build(start="00:01:00")))   # a trim changes the real duration

    def test_hevc_in_mp4_gets_the_hvc1_tag(self):
        cmd = self.build("out.mp4")
        self.assertEqual(cmd[cmd.index("-tag:v") + 1], "hvc1")

    def test_the_note_says_the_video_is_copied(self):
        note = next(a for a in self.build() if a.startswith("ENCODING_SETTINGS="))
        self.assertEqual(note, "ENCODING_SETTINGS=video copy")

    def test_predict_refuses_copy(self):
        web.app.test_client()
        r = web.app.test_client().post("/api/predict", json={"source": "library", "path": "x.mkv", "codec": "copy"})
        self.assertIn(r.status_code, (400, 404))


def have_ffmpeg():
    return shutil.which(web.FFMPEG) is not None or os.path.exists(web.FFMPEG)


@unittest.skipUnless(have_ffmpeg(), "ffmpeg not available")
class RealRun(unittest.TestCase):
    def test_copy_keeps_the_video_stream_untouched(self):
        name = "copy_test_clip.mkv"
        subprocess.run([web.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
                        "-t", "5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(web.LIBRARY_DIR / name)], check=True)
        c = web.app.test_client()
        job = c.post("/api/encode", json={"source": "library", "path": name, "codec": "copy",
                                          "container": "mkv"}).get_json()["job_id"]
        end = time.time() + 60
        while time.time() < end:
            st = c.get(f"/api/status/{job}").get_json()
            if st["status"] != "running":
                break
            time.sleep(0.3)
        self.assertEqual(st["status"], "done", st["log"][-5:])
        probe = web.ffprobe_json(Path(web.jobs[job]["dst"]))
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["codec_name"], video["width"], video["height"]), ("h264", 320, 240))


if __name__ == "__main__":
    unittest.main()
