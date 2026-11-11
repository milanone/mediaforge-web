"""Exact bitrate over time of a finished encode: bucketing, CSV, route and the CSV saved next to the output.

The route test uses real ffmpeg with software x265 (skipped if not available).
"""
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


def have_x265():
    try:
        r = subprocess.run([web.FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20)
        return "libx265" in r.stdout
    except Exception:
        return False


class Bucketing(unittest.TestCase):
    def test_bytes_per_interval_become_kbps(self):
        # 125000 bytes per second = 1000 kb/s; 3 s with a step of 2 s -> intervals [0,2) and [2,3)
        packets = [(i * 0.5, 62500) for i in range(6)]
        out = web.bucket_bitrates(packets, 2.0, 3.0)
        self.assertEqual([s for s, _ in out], [0.0, 2.0])
        self.assertAlmostEqual(out[0][1], 1000.0)
        self.assertAlmostEqual(out[1][1], 1000.0)    # the last interval is divided by its real length (1 s)

    def test_empty_input(self):
        self.assertEqual(web.bucket_bitrates([], 60), [])

    def test_csv_text(self):
        rows = [(0, 60, 1234.56, None), (60, 90, 800.0, 2000.0)]
        self.assertEqual(web.bitrate_csv_text(rows), "start_s;end_s;kbps\n0;60;1234.6\n60;90;800.0\n")
        self.assertEqual(web.bitrate_csv_text(rows, True).splitlines()[2], "60;90;800.0;2000.0")


@unittest.skipUnless(have_x265(), "ffmpeg with libx265 not available")
class ChartRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = "chart_test_clip.mkv"
        subprocess.run([web.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
                        "-t", "20", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(web.LIBRARY_DIR / cls.name)],
                       check=True)
        cls.client = web.app.test_client()
        r = cls.client.post("/api/encode", json={"source": "library", "path": cls.name, "codec": "libx265",
                                                 "quality": 28, "look_ahead": False, "container": "mkv"})
        cls.job = r.get_json()["job_id"]
        end = time.time() + 120
        while time.time() < end:
            st = cls.client.get(f"/api/status/{cls.job}").get_json()
            if st["status"] != "running":
                break
            time.sleep(0.5)
        cls.status = st

    def test_points_cover_the_film_and_match_the_final_bitrate(self):
        self.assertEqual(self.status["status"], "done")
        d = self.client.get(f"/api/bitrate_chart/{self.job}?step=5").get_json()
        pts = d["points"]
        self.assertEqual([p["start"] for p in pts], [0.0, 5.0, 10.0, 15.0])
        self.assertTrue(all(p["kbps"] > 0 for p in pts))
        mean = sum(p["kbps"] * (p["end"] - p["start"]) for p in pts) / sum(p["end"] - p["start"] for p in pts)
        self.assertAlmostEqual(mean, self.status["output_bitrate"] / 1000, delta=self.status["output_bitrate"] / 1000 * 0.05)

    def test_source_overlay_and_csv_download(self):
        d = self.client.get(f"/api/bitrate_chart/{self.job}?step=10&source=1").get_json()
        self.assertTrue(all(p["source_kbps"] is not None for p in d["points"]))
        r = self.client.get(f"/api/bitrate_chart/{self.job}?step=10&source=1&csv=1")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertEqual(r.get_data(as_text=True).splitlines()[0], "start_s;end_s;kbps;source_kbps")

    def test_csv_is_saved_next_to_the_output(self):
        dst = Path(web.jobs[self.job]["dst"])
        csv = dst.with_name(dst.stem + "_bitrate.csv")
        end = time.time() + 20
        while not csv.exists() and time.time() < end:
            time.sleep(0.3)
        self.assertTrue(csv.exists())
        self.assertEqual(csv.read_text(encoding="utf-8").splitlines()[0], "start_s;end_s;kbps")

    def test_unknown_and_unfinished_jobs(self):
        self.assertEqual(self.client.get("/api/bitrate_chart/nope").status_code, 404)
        web.jobs["running_x"] = {"status": "running", "log": []}
        try:
            self.assertEqual(self.client.get("/api/bitrate_chart/running_x").status_code, 400)
        finally:
            web.jobs.pop("running_x")


if __name__ == "__main__":
    unittest.main()
