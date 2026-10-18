"""The GPU decoder warning: the page lets the user choose (continue / stop / redo in software).

The watchdog is run on a tiny Python child process that prints the ffmpeg error line, no real ffmpeg needed.
"""
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

ERROR_LINE = "[AVHWFramesContext @ 0x1] Static surface pool size exceeded."


def child(seconds_after=0.0, lines=(ERROR_LINE,)):
    """argv of a process that prints the lines on stderr (like ffmpeg) and then lives for a while."""
    code = ("import sys,time\n"
            + "".join(f"print({l!r}, file=sys.stderr, flush=True)\n" for l in lines)
            + f"time.sleep({seconds_after})\n")
    return [sys.executable, "-c", code]


def new_job(**extra):
    return dict({"status": "running", "progress": 0, "log": []}, **extra)


class Watchdog(unittest.TestCase):
    def test_warning_is_raised_but_nothing_is_stopped(self):
        job = new_job()
        outcome = web.run_ffmpeg_watchdog(child(1.0), job, 10, watch_gpu=True)
        self.assertEqual(outcome, "ok")          # the encode is NOT killed: the user decides
        self.assertTrue(job["gpu_warning"])
        self.assertTrue(any("ATTENZIONE" in l for l in job["log"]))

    def test_no_warning_without_watch_gpu(self):
        job = new_job()
        web.run_ffmpeg_watchdog(child(0.0), job, 10, watch_gpu=False)
        self.assertFalse(job.get("gpu_warning"))

    def test_other_errors_do_not_raise_it(self):
        job = new_job()
        web.run_ffmpeg_watchdog(child(0.0, ("something harmless",)), job, 10, watch_gpu=True)
        self.assertFalse(job.get("gpu_warning"))

    def test_retry_in_software_stops_the_attempt(self):
        job = new_job(retry_software=True)
        t0 = time.time()
        outcome = web.run_ffmpeg_watchdog(child(30), job, 10, watch_gpu=True)
        self.assertEqual(outcome, "retry_software")
        self.assertLess(time.time() - t0, 15)


class Routes(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        web.jobs.clear()

    def tearDown(self):
        web.jobs.clear()

    def test_retry_needs_a_warning(self):
        web.jobs["a"] = new_job()
        self.assertEqual(self.client.post("/api/retry_software/a").status_code, 400)
        self.assertFalse(web.jobs["a"].get("retry_software"))

    def test_retry_is_requested_after_a_warning(self):
        web.jobs["a"] = new_job(gpu_warning=True)
        self.assertEqual(self.client.post("/api/retry_software/a").status_code, 200)
        self.assertTrue(web.jobs["a"]["retry_software"])

    def test_unknown_job(self):
        self.assertEqual(self.client.post("/api/retry_software/nope").status_code, 404)

    def test_status_exposes_the_warning(self):
        web.jobs["a"] = new_job(gpu_warning=True, output_bitrate=None, output_size=None)
        self.assertTrue(self.client.get("/api/status/a").get_json()["gpu_warning"])


class Page(unittest.TestCase):
    def test_page_has_the_three_choices(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        for needle in ('id="gpuWarning"', "gpuWarnContinue()", "retrySoftware()", "/api/retry_software/"):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()
