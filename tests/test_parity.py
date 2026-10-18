"""Parity between MediaForge web (this repo) and the desktop app (../MediaForge).

The two programs keep separate code, so these tests pin the engine rules they must share:
default CRF, QSV presets, GPU-decode gating, FPS standards and the video part of the ffmpeg
command. Differences that are deliberate are asserted too, so a change on one side breaks
a test instead of drifting silently. Tests that need the desktop file or node are skipped
when missing.

Run: py -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT.parent / "MediaForge" / "MediaForge.pyw"

# the module probes ffmpeg and creates its data folders at import: keep them in a temp dir
_tmp = tempfile.mkdtemp(prefix="mediaforge_web_test_")
os.environ.setdefault("MEDIAFORGE_FFMPEG", shutil.which("ffmpeg") or "ffmpeg")
os.environ.setdefault("MEDIAFORGE_FFPROBE", shutil.which("ffprobe") or "ffprobe")
os.environ["MEDIAFORGE_LIBRARY"] = _tmp
os.environ["MEDIAFORGE_UPLOADS"] = os.path.join(_tmp, "uploads")
os.environ["MEDIAFORGE_OUTPUT"] = os.path.join(_tmp, "output")
sys.path.insert(0, str(ROOT))
import app as web  # noqa: E402


def load_desktop():
    spec = importlib.util.spec_from_file_location("mediaforge_desktop", DESKTOP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def video_stream(w=3840, h=2160, codec="hevc", pix="yuv420p10le"):
    return {"index": 0, "codec_type": "video", "codec_name": codec, "width": w, "height": h,
            "pix_fmt": pix, "r_frame_rate": "24000/1001", "avg_frame_rate": "24000/1001"}


def probe_of(video):
    return {"streams": [video, {"index": 1, "codec_type": "audio", "codec_name": "aac",
                                 "channels": 2, "tags": {"language": "eng"}}],
            "format": {"duration": "100"}}


def arg_after(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


@unittest.skipUnless(DESKTOP.exists(), "desktop MediaForge.pyw not found next to this repo")
class Parity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.desk = load_desktop()

    def test_crf_curve_in_js_matches_desktop(self):
        """The web computes the default CRF in JavaScript: run that code with node."""
        if not shutil.which("node"):
            self.skipTest("node not available")
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        start = html.index("const CRF_PUNTI")
        end = html.index("// Ricalcola il CRF")
        sizes = [(640, 480), (854, 480), (1280, 720), (1920, 1080), (2560, 1440), (3840, 2160),
                 (2160, 3840), (7680, 4320)]
        js = html[start:end] + "\nconsole.log(JSON.stringify(%s.map(([w,h]) => [crfSuggerito(w,h,'hevc_qsv'), crfSuggerito(w,h,'av1_qsv')])));" % json.dumps(sizes)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
        web_values = json.loads(out)
        desk_values = [[self.desk.crf_suggerito(w, h, "hevc_qsv"), self.desk.crf_suggerito(w, h, "av1_qsv")]
                       for w, h in sizes]
        self.assertEqual(web_values, desk_values)

    def test_qsv_presets(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        web_levels = re.findall(r"\['(veryfast|medium|veryslow)',", html[html.index("const QSV_PRESETS"):])[:3]
        self.assertEqual(web_levels, [p for p, _ in self.desk.QSV_PRESETS])

    def test_gpu_decode_gating_lists(self):
        self.assertEqual(web.HW_DECODE_PIXFMTS, self.desk.HW_DECODE_PIXFMTS)
        # deliberate: the web (QSV decoder on Linux) keeps mjpeg, d3d11va on Windows does not
        self.assertEqual(web.HW_DECODE_CODECS - self.desk.HW_DECODE_CODECS, {"mjpeg"})
        self.assertEqual(self.desk.HW_DECODE_CODECS - web.HW_DECODE_CODECS, set())

    def test_fps_standards(self):
        self.assertEqual({r for _, r in web.STD_RATES},
                         {r.replace("/1", "") if r.endswith("/1") else r for r in self.desk.FPS_STANDARD}
                         - {"120"})

    def test_video_encoder_args_match(self):
        """Same quality settings -> same encoder arguments (software path, no GPU)."""
        video = video_stream(3840, 2160, "h264", "yuv420p")
        web_opts = {"codec": "hevc_qsv", "quality": 22, "preset": "veryslow", "look_ahead": True}
        desk_opts = {"mode": "video", "output_ext": "mkv", "vcodec": "hevc_qsv", "quality": 22,
                     "preset": "veryslow", "look_ahead": True, "audio": "copy", "subs": "copy",
                     "timestamp": "", "limite_res": None}
        wc = web.build_cmd(Path("in.mkv"), Path("out.mkv"), web_opts, probe_of(video), hw=False)
        dc = self.desk.build_ffmpeg_cmd(Path("in.mkv"), Path("out.mkv"), desk_opts, probe_of(video),
                                        hwaccel_decode=False)
        for flag in ("-c:v", "-global_quality", "-preset", "-look_ahead"):
            self.assertEqual(arg_after(wc, flag), arg_after(dc, flag), flag)

    def test_resolution_cap_filter_matches_software(self):
        video = video_stream(3840, 2160, "h264", "yuv420p")
        wc = web.build_cmd(Path("in.mkv"), Path("out.mkv"),
                           {"codec": "libx265", "quality": 22, "resolution_cap": 1080},
                           probe_of(video), hw=False)
        dc = self.desk.build_ffmpeg_cmd(
            Path("in.mkv"), Path("out.mkv"),
            {"mode": "video", "output_ext": "mkv", "vcodec": "libx265", "quality": 22, "look_ahead": False,
             "audio": "copy", "subs": "copy", "timestamp": "", "limite_res": 1080},
            probe_of(video), hwaccel_decode=False)
        self.assertEqual(arg_after(wc, "-vf"), arg_after(dc, "-vf"))

    def test_stats_tags_cleared_on_video(self):
        self.assertEqual(web.STATS_TAGS_MKV, self.desk.STATS_TAGS_MKV)


class WebOnly(unittest.TestCase):
    """Behaviour of the web engine on its own (no desktop needed)."""

    def test_hw_plan_gating(self):
        ok = video_stream(3840, 2160, "hevc", "yuv420p10le")
        bad = video_stream(3840, 2160, "prores", "yuv422p10le")
        opts = {"codec": "hevc_qsv"}
        self.assertEqual(web.hw_plan(opts, ok, []), "full")
        self.assertIsNone(web.hw_plan(opts, bad, []))
        self.assertIsNone(web.hw_plan({"codec": "libx265"}, ok, []))

    def test_fractional_fps_is_kept_in_filter(self):
        flt = web.build_filters({"fps": "30000/1001"}, video_stream())
        self.assertIn("fps=30000/1001", flt)


if __name__ == "__main__":
    unittest.main()
