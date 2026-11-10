"""/api/predict end to end on a small synthetic video (real ffmpeg, software x265; the QSV path needs the GPU).

Skipped when ffmpeg or the libx265 encoder is not available.
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


@unittest.skipUnless(have_x265(), "ffmpeg with libx265 not available")
class PredictRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = "predict_test_clip.mkv"
        subprocess.run([web.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
                        "-t", "40", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(web.LIBRARY_DIR / cls.name)],
                       check=True)
        cls.client = web.app.test_client()

    def post(self, **extra):
        body = {"source": "library", "path": self.name, "codec": "libx265", "quality": 28, "look_ahead": False}
        body.update(extra)
        return self.client.post("/api/predict", json=body)

    def wait(self, job_id, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            st = self.client.get(f"/api/status/{job_id}").get_json()
            if st["status"] != "running":
                return st
            time.sleep(0.5)
        self.fail("prediction did not finish")

    def test_selected_times_give_one_bitrate_per_clip_and_a_summary(self):
        r = self.post(n=2, clip_min=0.1, times="0:05, 0:25")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        job = r.get_json()["job_id"]
        # a prediction must not show up as an encode to re-attach to (and must not look like one to deploy.sh)
        self.assertNotIn(job, [j["job_id"] for j in self.client.get("/api/jobs").get_json()])
        st = self.wait(job)
        self.assertEqual(st["status"], "done", st["log"][-5:])
        p = st["predict"]
        self.assertEqual([s["start"] for s in p["samples"]], [5.0, 25.0])
        self.assertTrue(all(s["kbps"] > 0 for s in p["samples"]))
        res = p["result"]
        self.assertLessEqual(res["min"], res["mean"])
        self.assertLessEqual(res["mean"], res["max"])
        self.assertAlmostEqual(res["duration"], 40, delta=1)

    def test_random_times_stay_inside_the_film(self):
        r = self.post(n=3, clip_min=0.1)
        starts = r.get_json()["starts"]
        self.assertEqual(len(starts), 3)
        self.assertTrue(all(2 <= s <= 40 - 6 for s in starts), starts)
        self.assertEqual(self.wait(r.get_json()["job_id"])["status"], "done")

    def test_new_points_route_gives_a_fresh_spread_set_each_time(self):
        url = f"/api/predict_points?source=library&path={self.name}&n=3&clip_min=0.1"
        sets = [self.client.get(url).get_json()["starts"] for _ in range(5)]
        for starts in sets:
            self.assertEqual(len(starts), 3)
            self.assertEqual(starts, sorted(starts))
            self.assertTrue(all(2 <= s <= 40 - 6 for s in starts), starts)
        self.assertGreater(len({tuple(s) for s in sets}), 1)        # not always the same
        self.assertEqual(self.client.get("/api/predict_points?source=library&path=nope.mkv").status_code, 404)
        self.assertEqual(self.client.get(f"/api/predict_points?source=library&path={self.name}&n=0").status_code, 400)

    def test_job_status_carries_the_chosen_points_from_the_start(self):
        r = self.post(n=2, clip_min=0.1, times="0:05, 0:25")
        st = self.client.get(f"/api/status/{r.get_json()['job_id']}").get_json()
        self.assertEqual(st["predict"]["starts"], [5.0, 25.0])
        self.wait(r.get_json()["job_id"])

    def test_bad_requests(self):
        self.assertEqual(self.post(bitrate_kbps=1000).status_code, 400)
        self.assertEqual(self.post(audio_only=True).status_code, 400)
        self.assertEqual(self.post(n=0).status_code, 400)
        self.assertEqual(self.post(clip_min=0.1, times="5, 99").status_code, 400)
        self.assertEqual(self.client.post("/api/predict", json={"source": "library", "path": "nope.mkv"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
