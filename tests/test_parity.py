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

    def test_sar_and_original_ratio_rules(self):
        """SAR parsing and "Originale" must give the same number on both sides."""
        samples = [
            {}, {"sample_aspect_ratio": "1:1"}, {"sample_aspect_ratio": "0:1"}, {"sample_aspect_ratio": "N/A"},
            {"sample_aspect_ratio": "8:9", "display_aspect_ratio": "35:27", "width": 700, "height": 480},
            {"sample_aspect_ratio": "8:9", "display_aspect_ratio": "0:1", "width": 700, "height": 480},
            {"sample_aspect_ratio": "32:27", "width": 720, "height": 576},
            {"sample_aspect_ratio": "garbage", "width": 1920, "height": 1080},
        ]
        for v in samples:
            self.assertAlmostEqual(web.sar_numeric(v), self.desk.sar_numerico(v), msg=str(v))
            a, b = web.resolve_ratio("Originale", v), self.desk.risolvi_rapporto_ar("Originale", v)
            self.assertEqual(bool(a), bool(b), str(v))
            if a:
                self.assertAlmostEqual(a, b, msg=str(v))

    def test_even_size_correction_matches(self):
        for mode in ("crop", "pad", "stretch"):
            for ratio in (16 / 9, 4 / 3, 2.39, 35 / 27):
                for sar in (1.0, 8 / 9):
                    w = web.target_dims_even(1279, 719, ratio, mode, sar)
                    d = self.desk.dimensioni_target_pari(1279, 719, ratio, mode, sar)
                    self.assertEqual(w, d, (mode, ratio, sar))

    def test_stats_tags_cleared_on_video(self):
        self.assertEqual(web.STATS_TAGS_MKV, self.desk.STATS_TAGS_MKV)

    def test_default_crf_for_4k_source_scaled_down_matches_desktop(self):
        """Same default (14) for a 4K source scaled down; the web feeds the OUTPUT size (after the resolution
        cap) plus the source side, like the desktop."""
        if not shutil.which("node"):
            self.skipTest("node not available")
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        start = html.index("const CRF_PUNTI")
        end = html.index("// Ricalcola il CRF")
        # (source w, h, cap): output size is computed by effectiveDims / risoluzione_effettiva
        cases = [(3840, 2160, 1080), (3840, 2160, 720), (3840, 2160, 0), (1920, 1080, 0), (1920, 1080, 720),
                 (2560, 1440, 1080), (2160, 3840, 1080), (3840, 1608, 1080), (3000, 2000, 1080)]
        js = html[start:end] + """
const cases = %s;
console.log(JSON.stringify(cases.map(([w, h, cap]) => {
  const [ow, oh] = effectiveDims(w, h, cap);
  return [ow, oh, crfSuggerito(ow, oh, 'hevc_qsv', Math.max(w, h)), crfSuggerito(ow, oh, 'av1_qsv', Math.max(w, h))];
})));""" % json.dumps(cases)
        out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
        for (w, h, cap), (ow, oh, hevc, av1) in zip(cases, out):
            dw, dh = self.desk.risoluzione_effettiva(w, h, cap or None)
            self.assertEqual((ow, oh), (dw, dh), (w, h, cap))
            side = max(w, h)
            self.assertEqual(hevc, self.desk.crf_suggerito(dw, dh, "hevc_qsv", side), (w, h, cap))
            self.assertEqual(av1, self.desk.crf_suggerito(dw, dh, "av1_qsv", side), (w, h, cap))
        self.assertEqual(out[0][2], 14)   # 4K -> 1080p
        self.assertEqual(out[2][2], 20)   # 4K kept at 4K

    LIVE_LINES = [
        "frame=  240 fps= 48 q=-1.0 size=    1024KiB time=00:00:10.00 bitrate= 838.9kbits/s speed=1.98x",
        "frame=    1 fps=0.0 q=0.0 size=       0KiB time=N/A bitrate=N/A speed=N/A",
        "frame= 9000 fps= 25 q=-0.0 Lsize=   20MiB time=01:02:03.45 bitrate=2700.1kbits/s speed=1.5x",
        "Stream mapping:",
    ]

    def test_live_progress_parsing_matches_desktop(self):
        for line in self.LIVE_LINES:
            self.assertEqual(web.parse_progress_line(line), self.desk.parse_progress_line(line), line)

    def test_live_audio_subtraction_matches_desktop(self):
        v = {"codec_type": "video"}
        a1 = {"codec_type": "audio", "bit_rate": "640000"}
        a2 = {"codec_type": "audio", "tags": {"BPS": "384000"}}
        a3 = {"codec_type": "audio"}
        cases = [  # (streams, web opts, desktop opts)
            ([v, a1, a2], {"audio_mode": "copy"}, {"audio": "copy"}),
            ([v, a1, a2], {"audio_mode": "ac3"}, {"audio": "ac3"}),
            ([v, a1, a2], {"audio_mode": "aac"}, {"audio": "aac"}),
            ([v, a1, a2], {"audio_mode": "mp3"}, {"audio": "mp3"}),
            ([v, a1, a2], {"audio_mode": "copy", "audio_indices": [0]}, {"audio": "copy", "stream_map": [0, 1]}),
            ([v, a1, a2], {"audio_mode": "ac3", "audio_indices": [1]}, {"audio": "ac3", "stream_map": [0, 2]}),
            ([v, a1, a3], {"audio_mode": "copy"}, {"audio": "copy"}),               # not declared -> None
            ([v, a1, a2], {"audio_only": True}, {"audio": "copy", "mode": "audio"}),  # audio-only job -> None
            ([v], {"audio_mode": "copy"}, {"audio": "copy"}),
        ]
        for streams, wopts, dopts in cases:
            self.assertEqual(web.audio_output_kbps(wopts, {"streams": streams}),
                             self.desk.audio_output_kbps(dopts, {"streams": streams}), (wopts, dopts))

    def test_live_samples_match_desktop(self):
        """Same sequence of progress lines -> same samples (instantaneous and average video bitrate with the audio
        subtracted, fps, speed, time axis), including a pass change."""
        import queue
        from unittest import mock

        def line(t, kib):
            return f"frame= 1 fps=24 q=0 size={kib}KiB time=00:{t // 60:02d}:{t % 60:02d}.00 bitrate=999.0kbits/s speed=1.1x"
        feed = [(1000.0 + 10 * i, 1, line(10 + 10 * i, 500 + 600 * i)) for i in range(5)]
        feed += [(1000.0 + 50 + 10 * i, 2, line(10 + 10 * i, 100 + 300 * i)) for i in range(4)]
        feed.insert(1, (1003.0, 1, line(11, 520)))          # too soon after the previous one: both skip it
        job = {"audio_kbps": 384.0, "started_at": 1000.0, "pass_no": 1}
        live = self.desk.LiveStats(queue.Queue())
        live.t0 = 1000.0
        live.next_segment(384.0)
        seg = 1
        for now, pass_no, ln in feed:
            if pass_no != seg:
                seg = pass_no
                job["pass_no"] = pass_no
                live.next_segment(384.0)
            with mock.patch.object(web.time, "time", return_value=now):
                web.record_progress(job, web.parse_progress_line(ln))
            live.add(ln, now)
        keys = ("t", "pass", "time_s", "fps", "speed", "avg_kbps", "inst_kbps", "size")
        self.assertEqual(len(job["history"]), len(live.history))
        for w_s, d_s in zip(job["history"], live.history):
            self.assertEqual({k: w_s[k] for k in keys}, {k: d_s[k] for k in keys})

    def test_fps_ratio_rule_matches_desktop(self):
        """Repeats in a whole ratio can be dropped, any other ratio is left alone: same rule on both sides."""
        for d, real in ((60, 30), (50, 25), (59.94, 29.97), (120, 30), (30, 24), (29.97, 23.976), (25, 24), (60, 24),
                        (30, 30), (30, 0), (24, 25)):
            self.assertEqual(web.fps_ratio_regular(d, real), self.desk.rapporto_fps_regolare(d, real), (d, real))

    def test_default_container_matches_desktop(self):
        """The container proposed for a file (the source's own) is the same rule on both sides."""
        if not shutil.which("node"):
            self.skipTest("node not available")
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        start = html.index("function containerForSource")
        end = html.index("function setContainerFromSource")
        names = ["a.mkv", "a.MKV", "a.mp4", "a.m4v", "a.mov", "a.avi", "a.ts", "a.webm", "noext", "dir/film.2021.mp4"]
        js = html[start:end] + f"console.log(JSON.stringify({json.dumps(names)}.map(containerForSource)));"
        out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out, [self.desk.contenitore_predefinito(Path(n)) for n in names])

    def test_predict_bitrate_helpers_match_desktop(self):
        import random
        for seed in range(20):
            self.assertEqual(web.predict_start_times(6000, 3, 60, random.Random(seed)),
                             self.desk.predict_start_times(6000, 3, 60, random.Random(seed)))
        self.assertEqual(web.predict_start_times(30, 3, 60), self.desk.predict_start_times(30, 3, 60))
        for text in ("13, 20.5", "1:02:03; 5:30", "0"):
            self.assertEqual(web.parse_start_times(text, 6000, 60), self.desk.parse_start_times(text, 6000, 60))
        for bad in ("abc", "", "100", "-1"):
            with self.assertRaises(ValueError):
                web.parse_start_times(bad, 6000, 60)
            with self.assertRaises(ValueError):
                self.desk.parse_start_times(bad, 6000, 60)
        self.assertEqual(web.summarize_bitrates([483, 732, 505], 3000),
                         self.desk.summarize_bitrates([483, 732, 505], 3000))

    def test_predict_sample_command_is_the_real_encode_on_a_short_video_only_clip(self):
        video = video_stream(3840, 2160, "hevc", "yuv420p10le")
        opts = {"codec": "hevc_qsv", "quality": 16, "preset": "medium", "look_ahead": True, "resolution_cap": 1080,
                "audio_mode": "aac", "subs_mode": "copy", "container": "mp4", "bitrate_kbps": 2000}
        cmd = web.build_cmd(Path("in.mkv"), Path("clip.mkv"), web.predict_sample_opts(opts, 824, 60),
                            probe_of(video), hw=True)
        self.assertEqual(arg_after(cmd, "-ss"), "824.00")
        self.assertEqual(arg_after(cmd, "-t"), "60.00")
        self.assertEqual(arg_after(cmd, "-global_quality"), "16")       # CRF, not the bitrate that was set
        self.assertNotIn("-b:v", cmd)
        self.assertEqual(arg_after(cmd, "-vf"), "scale_qsv=w=1920:h=1080")   # same GPU scaling as the real job
        self.assertEqual([cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"], ["0:v:0"])
        self.assertNotIn("-c:s", cmd)
        self.assertEqual(cmd[-1], "clip.mkv")


class WebOnly(unittest.TestCase):
    """Behaviour of the web engine on its own (no desktop needed)."""

    def test_non_square_pixels_detection(self):
        self.assertEqual(web.non_square_pixels({"sample_aspect_ratio": "8:9", "display_aspect_ratio": "35:27"}),
                         ("8:9", "35:27"))
        for sar in ("1:1", "0:1", "N/A", "", None):
            self.assertIsNone(web.non_square_pixels({"sample_aspect_ratio": sar}))

    def test_preview_warnings(self):
        v = video_stream(701, 479, "h264", "yuv420p")
        self.assertEqual(web.ar_warnings({"ar_mode": "none"}, v), [])
        self.assertIn("non valido", web.ar_warnings({"ar_mode": "crop", "ar_ratio": "boh"}, v)[0])
        self.assertIn("non disponibile", web.ar_warnings({"ar_mode": "dar", "ar_ratio": "Originale"}, None)[0])
        self.assertIn("numeri pari", web.ar_warnings({"ar_mode": "crop", "ar_ratio": "16:9"}, v)[0])
        self.assertEqual(web.ar_warnings({"ar_mode": "dar", "ar_ratio": "16:9"}, v), [])
        # a size that only rounds to itself (622.2 -> 622) is not a "correction"
        hd = video_stream(1920, 1080, "h264", "yuv420p")
        self.assertEqual(web.ar_warnings({"ar_mode": "crop", "ar_ratio": "16:9"}, hd), [])

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
