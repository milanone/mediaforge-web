"""Version endpoint and the "new version available" logic (GitHub is never contacted: fetch_latest is replaced)."""
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

LATEST = {"commit": "b" * 40, "date": "2026-10-18T10:00:00Z", "message": "Add things"}


class VersionApi(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        self._saved = (web.APP_COMMIT, web.fetch_latest, dict(web._latest))
        web._latest.update(checked=0.0, ok=False, data=None, running=False)

    def tearDown(self):
        web.APP_COMMIT, web.fetch_latest = self._saved[0], self._saved[1]
        web._latest.update(self._saved[2])

    def ask(self):
        """First call starts the background refresh; wait for it, then ask again."""
        self.client.get("/api/version")
        for _ in range(100):
            if not web._latest["running"]:
                break
            time.sleep(0.02)
        return self.client.get("/api/version").get_json()

    def test_update_available_when_commits_differ(self):
        web.APP_COMMIT = "a" * 40
        web.fetch_latest = lambda: LATEST
        v = self.ask()
        self.assertTrue(v["update_available"])
        self.assertEqual(v["short"], "aaaaaaa")
        self.assertEqual(v["latest"]["message"], "Add things")

    def test_up_to_date(self):
        web.APP_COMMIT = LATEST["commit"]
        web.fetch_latest = lambda: LATEST
        self.assertFalse(self.ask()["update_available"])

    def test_unknown_build_never_claims_to_be_outdated(self):
        web.APP_COMMIT = "unknown"
        web.fetch_latest = lambda: LATEST
        self.assertFalse(self.ask()["update_available"])

    def test_github_failure_is_silent(self):
        web.APP_COMMIT = "a" * 40

        def boom():
            raise OSError("offline")
        web.fetch_latest = boom
        v = self.ask()
        self.assertFalse(v["update_available"])
        self.assertIsNone(v["latest"])

    def test_result_is_cached(self):
        web.APP_COMMIT = "a" * 40
        calls = []
        web.fetch_latest = lambda: calls.append(1) or LATEST
        self.ask()
        self.client.get("/api/version")
        self.client.get("/api/version")
        self.assertEqual(len(calls), 1)

    def test_failed_check_is_retried_sooner_than_a_good_one(self):
        self.assertLess(web.LATEST_RETRY, web.LATEST_TTL)


class Page(unittest.TestCase):
    def test_page_has_footer_and_banner(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        for needle in ('id="versionInfo"', 'id="updateBanner"', "/api/version", "./deploy.sh"):
            self.assertIn(needle, html)


class UpdateButtons(unittest.TestCase):
    """Forced check and the in-page update request (the host watcher itself is a shell script, not tested here)."""

    def setUp(self):
        self.client = web.app.test_client()
        self.tmp = Path(tempfile.mkdtemp(prefix="mediaforge_updates_"))
        self._saved = (web.APP_COMMIT, web.fetch_latest, dict(web._latest), web.UPDATES_DIR, dict(web.jobs))
        web.UPDATES_DIR = self.tmp
        web.APP_COMMIT = "a" * 40
        web.jobs.clear()
        web._latest.update(checked=time.time(), ok=True, data=None, running=False)

    def tearDown(self):
        web.APP_COMMIT, web.fetch_latest = self._saved[0], self._saved[1]
        web._latest.update(self._saved[2])
        web.UPDATES_DIR = self._saved[3]
        web.jobs.clear()
        web.jobs.update(self._saved[4])
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_now_bypasses_the_cache(self):
        web.fetch_latest = lambda: LATEST
        v = self.client.post("/api/version/check").get_json()
        self.assertTrue(v["update_available"])
        self.assertTrue(v["checked"])
        self.assertIsNone(v["error"])

    def test_check_now_reports_a_github_failure(self):
        def boom():
            raise OSError("offline")
        web.fetch_latest = boom
        v = self.client.post("/api/version/check").get_json()
        self.assertFalse(v["checked"])
        self.assertIn("offline", v["error"])

    def test_update_refused_without_a_watcher(self):
        r = self.client.post("/api/update")
        self.assertEqual(r.status_code, 409)
        self.assertFalse((self.tmp / "request").exists())

    def test_update_writes_the_request_when_the_watcher_is_alive(self):
        (self.tmp / "alive").write_text("")
        self.assertTrue(self.client.get("/api/version").get_json()["updater_available"])
        self.assertEqual(self.client.post("/api/update").status_code, 200)
        self.assertTrue((self.tmp / "request").exists())

    def test_stale_heartbeat_means_no_watcher(self):
        alive = self.tmp / "alive"
        alive.write_text("")
        old = time.time() - web.WATCHER_MAX_AGE - 60
        os.utime(alive, (old, old))
        self.assertFalse(self.client.get("/api/version").get_json()["updater_available"])

    def test_update_refused_while_a_job_runs(self):
        (self.tmp / "alive").write_text("")
        web.jobs["x"] = {"status": "running"}
        self.assertEqual(self.client.post("/api/update").status_code, 409)
        self.assertFalse((self.tmp / "request").exists())

    def test_update_status_is_exposed(self):
        (self.tmp / "status").write_text("aggiornamento in corso (10:00:00)\n", encoding="utf-8")
        self.assertIn("in corso", self.client.get("/api/version").get_json()["update_status"])


if __name__ == "__main__":
    unittest.main()
