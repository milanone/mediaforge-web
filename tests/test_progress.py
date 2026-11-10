"""Parsing of ffmpeg's progress lines and the data behind the live numbers and charts (no ffmpeg needed)."""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_tmp = tempfile.mkdtemp(prefix="mediaforge_web_test_")
os.environ.setdefault("MEDIAFORGE_FFMPEG", shutil.which("ffmpeg") or "ffmpeg")
os.environ.setdefault("MEDIAFORGE_FFPROBE", shutil.which("ffprobe") or "ffprobe")
os.environ["MEDIAFORGE_LIBRARY"] = _tmp
os.environ["MEDIAFORGE_UPLOADS"] = os.path.join(_tmp, "uploads")
os.environ["MEDIAFORGE_OUTPUT"] = os.path.join(_tmp, "output")
sys.path.insert(0, str(ROOT))
import app as web  # noqa: E402

# lines copied from a real run (QSV on gaia)
REAL = "frame=23183 fps= 54 q=-0.0 size=  257792KiB time=00:16:05.79 bitrate=2186.6kbits/s speed=2.27x"


class Parse(unittest.TestCase):
    def test_a_real_line(self):
        st = web.parse_progress_line(REAL)
        self.assertEqual(st["frame"], 23183)
        self.assertEqual(st["fps"], 54.0)
        self.assertEqual(st["size_bytes"], 257792 * 1024)
        self.assertAlmostEqual(st["time_s"], 16 * 60 + 5.79)
        self.assertAlmostEqual(st["bitrate_kbps"], 2186.6)
        self.assertAlmostEqual(st["speed"], 2.27)

    def test_spaces_after_the_equal_sign_and_other_units(self):
        st = web.parse_progress_line("frame=   12 fps=0.0 q=0.0 size=    3MiB time=00:00:00.50 bitrate=N/A speed=N/A")
        self.assertEqual(st["size_bytes"], 3 * 1024 ** 2)
        self.assertIsNone(st["bitrate_kbps"])
        self.assertIsNone(st["speed"])

    def test_not_available_fields(self):
        st = web.parse_progress_line("frame=  100 fps= 25 q=-1.0 size=N/A time=00:00:04.00 bitrate=N/A speed=1x")
        self.assertIsNone(st["size_bytes"])
        self.assertEqual(st["speed"], 1.0)
        self.assertIsNone(web.parse_progress_line("frame=N/A time=N/A")["frame"])

    def test_final_line_with_lsize(self):
        st = web.parse_progress_line("frame= 2700 fps= 31 q=-0.0 Lsize=   19379KiB time=00:01:30.00 bitrate=1763.7kbits/s speed=1.03x")
        self.assertEqual(st["size_bytes"], 19379 * 1024)

    def test_other_lines_are_not_progress(self):
        for line in ("Press [q] to stop", "[mp4 @ 0x1] Starting second pass", "Stream mapping:", "time=00:00:01 only"):
            self.assertIsNone(web.parse_progress_line(line), line)


class History(unittest.TestCase):
    def setUp(self):
        self.job = {"status": "running", "log": [], "started_at": time.time() - 100}

    def sample(self, time_s, size, t_offset, fps=54.0):
        """Record one sample as if HISTORY_STEP seconds had passed."""
        self.job["_hist_t"] = time.time() - web.HISTORY_STEP - 1
        web.record_progress(self.job, {"frame": 1, "fps": fps, "size_bytes": size, "time_s": time_s,
                                       "bitrate_kbps": 2000.0, "speed": 2.0})

    def test_latest_numbers_are_always_kept(self):
        web.record_progress(self.job, web.parse_progress_line(REAL))
        self.assertEqual(self.job["stats"]["frame"], 23183)

    def test_samples_are_throttled(self):
        web.record_progress(self.job, web.parse_progress_line(REAL))
        web.record_progress(self.job, web.parse_progress_line(REAL))   # same instant: no second sample
        self.assertEqual(len(self.job["history"]), 1)

    def test_instantaneous_bitrate_is_measured_on_the_recent_samples(self):
        # 1 MiB per 4 s of media; measured against the sample two steps back: 2 MiB in 8 s = 2097.152 kb/s
        for i in range(5):
            self.sample(time_s=100 + 4 * i, size=(10 + i) * 1024 ** 2, t_offset=i)
        self.assertAlmostEqual(self.job["history"][-1]["inst_kbps"], 2097.152, places=2)
        self.assertIsNone(self.job["history"][0]["inst_kbps"])   # the first sample has nothing to compare with

    def test_the_two_passes_are_not_mixed(self):
        self.sample(time_s=100, size=10 * 1024 ** 2, t_offset=0)
        self.job["pass_no"] = 2
        self.sample(time_s=5, size=1024, t_offset=1)          # pass 2 starts again from zero
        last = self.job["history"][-1]
        self.assertEqual(last["pass"], 2)
        self.assertIsNone(last["inst_kbps"])                   # no reference sample of the same pass

    def test_a_sample_without_time_does_not_break_the_next_one(self):
        """ffmpeg prints time=N/A in the first lines after -ss: the next sample used it as the reference
        (TypeError: float - None) and the whole job failed."""
        self.sample(time_s=None, size=1024, t_offset=0)
        self.sample(time_s=5.0, size=2048, t_offset=1)
        self.sample(time_s=9.0, size=4096, t_offset=2)
        self.sample(time_s=13.0, size=8192, t_offset=3)
        self.assertEqual(len(self.job["history"]), 4)
        self.assertIsNone(self.job["history"][1]["inst_kbps"])    # its reference had no time

    def test_history_is_capped(self):
        self.job["history"] = [{"pass": 1, "time_s": 0, "size": 0}] * web.HISTORY_MAX
        self.job["_hist_t"] = 0
        web.record_progress(self.job, web.parse_progress_line(REAL))
        self.assertEqual(len(self.job["history"]), web.HISTORY_MAX)


class StatusRoute(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        web.jobs.clear()
        web.jobs["a"] = {"status": "running", "progress": 10, "log": [], "duration": 8848.0,
                         "stats": web.parse_progress_line(REAL),
                         "history": [{"t": 10.0 * i, "fps": 54.0} for i in range(5)]}

    def tearDown(self):
        web.jobs.clear()

    def test_status_carries_stats_and_duration(self):
        d = self.client.get("/api/status/a").get_json()
        self.assertEqual(d["duration"], 8848.0)
        self.assertEqual(d["stats"]["frame"], 23183)

    def test_history_is_sent_incrementally(self):
        full = self.client.get("/api/status/a").get_json()
        self.assertEqual(len(full["history"]), 5)
        self.assertEqual(full["history_total"], 5)
        tail = self.client.get("/api/status/a?h=3").get_json()
        self.assertEqual(len(tail["history"]), 2)
        self.assertEqual(self.client.get("/api/status/a?h=5").get_json()["history"], [])


class Page(unittest.TestCase):
    def test_page_has_the_stats_line_and_the_charts(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        for needle in ('id="progressStats"', 'id="chartsBox"', 'id="chartFps"', 'id="chartSpeed"',
                       'id="chartBitrate"', "/api/status/' + jobId + '?h='"):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()
