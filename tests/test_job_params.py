"""The parameters summary of a job (what the page shows while it runs): describe_job()."""
import os
import shutil
import sys
import tempfile
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

VIDEO = {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160}
PROBE = {"streams": [VIDEO, {"codec_type": "subtitle", "codec_name": "subrip"}], "format": {"duration": "100"}}
PROBE_NO_SUBS = {"streams": [VIDEO], "format": {"duration": "100"}}
SRC = Path("/library/film.mkv")


def rows(data, out="film-libx265.mkv", probe=PROBE):
    return dict(web.describe_job(data, probe, SRC, out))


class DescribeJob(unittest.TestCase):
    def test_crf_encode_with_reencoded_audio(self):
        r = rows({"codec": "libx265", "quality": 18, "preset": "medium", "look_ahead": True, "audio_mode": "ac3",
                  "audio_bitrate": "384", "subs_mode": "mux", "container": "mkv", "resolution_cap": 1080,
                  "start": "00:10:00", "duration": "00:05:00"})
        self.assertEqual(r["Sorgente"], "film.mkv (hevc 3840x2160)")
        self.assertEqual(r["Uscita"], "film-libx265.mkv")
        self.assertEqual(r["Controllo"], "Qualità costante, CRF 18")
        self.assertEqual(r["Audio"], "AC3 384 kb/s")
        self.assertEqual(r["Sottotitoli"], "Muxa a fine codifica")
        self.assertEqual(r["Risoluzione massima"], "1080p")
        self.assertEqual(r["Taglio"], "da 00:10:00, durata 00:05:00")
        self.assertNotIn("Look-ahead", r)                      # QSV only

    def test_target_bitrate_two_pass_and_qsv_look_ahead(self):
        r = rows({"codec": "libx265", "bitrate_kbps": 2000, "two_pass": True})
        self.assertEqual(r["Controllo"], "Bitrate medio 2000 kb/s · 2 passate")
        self.assertEqual(rows({"codec": "hevc_qsv", "quality": 20, "look_ahead": True})["Look-ahead"], "sì")

    def test_default_audio_bitrate_and_chosen_tracks(self):
        self.assertEqual(rows({"codec": "libx265", "quality": 20, "audio_mode": "aac"})["Audio"], "AAC 128 kb/s")
        self.assertEqual(rows({"codec": "libx265", "quality": 20, "audio_indices": [0, 2]})["Audio"],
                         "Copia originale (2 tracce scelte)")

    def test_no_subtitle_row_for_a_file_without_subtitles(self):
        self.assertIn("Sottotitoli", rows({"codec": "libx265", "quality": 20}))
        self.assertNotIn("Sottotitoli", rows({"codec": "libx265", "quality": 20}, probe=PROBE_NO_SUBS))

    def test_copy_has_no_video_options(self):
        r = rows({"codec": web.COPY_VIDEO, "quality": 20, "preset": "slow"})
        self.assertIn("nessuna ricodifica", r["Codec video"])
        for k in ("Controllo", "Preset", "Look-ahead"):
            self.assertNotIn(k, r)

    def test_maintenance_and_audio_only(self):
        self.assertIn("Operazione", rows({"mode": "fix_hvc1"}))
        self.assertNotIn("Codec video", rows({"mode": "fix_bitrate"}))
        r = rows({"audio_only": True, "audio_mode": "mp3"}, "film-audio.mp3")
        self.assertEqual(r["Operazione"], "Solo audio")
        self.assertEqual(r["Audio"], "MP3 128 kb/s")
        self.assertNotIn("Codec video", r)


if __name__ == "__main__":
    unittest.main()
