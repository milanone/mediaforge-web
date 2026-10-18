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


if __name__ == "__main__":
    unittest.main()
