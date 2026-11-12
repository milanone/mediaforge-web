# CLAUDE.md

Web sibling of the desktop app `../MediaForge` (GitHub `milanone/MediaForge`). Same ffmpeg encoding rules,
served by Flask in Docker on `gaia` (home server, Intel N150 iGPU).

## Layout

- `app.py` — Flask backend, one file: encoder probing, `build_filters()`, `build_cmd()` (+ `hw_plan()` for the
  QSV decode/scale path), `run_encode()` / `run_ffmpeg_watchdog()` (jobs in memory, stall watchdog, software
  retry), `detect_fps()`, maintenance remux, REST routes under `/api/`
- `static/index.html` — the whole UI (vanilla JS, no build step)
- `Dockerfile` (ubuntu 24.04 + `jellyfin-ffmpeg7`), `docker-compose.yaml` (paths of the author's server; the library shown in the page is `/library`, with the mounts `hdd-live`, `gaia` (`/home/francesco/mediaforge-library`) and `torrent-downloads` (`/home/francesco/torrent-downloads`, written by the Transmission container: `complete/` and `incomplete/`). Mounts are read-write because the output is written next to the source file)

## Deployment

`/home/francesco/docker/mediaforge-web` on gaia is a clone of this repo (container `mediaforge-web`, port 5050,
image `mediaforge-web:local`); it was converted from a plain folder on 2026-10-18 (old files kept as `*.bak*`,
`data/` untouched). Deploy = push here, then on gaia (ask the user first: it restarts a service in use):

```bash
ssh gaia "cd /home/francesco/docker/mediaforge-web && ./deploy.sh"
```

`deploy.sh` refuses to run while an encode is in progress (jobs live in memory, a rebuild kills them; `--force`
overrides), pulls, and rebuilds with the commit as build argument: the page footer shows that version and
`/api/version` compares it with the latest commit of `main` on GitHub (asked at most once an hour, in the
background, failures are silent) to show a "nuova versione disponibile" banner. A build without the commit
(`docker compose up` by hand, a local run) shows "versione non nota" and never claims to be outdated.

In-page buttons: the footer says "aggiornata" / "nuova versione disponibile" and has "Controlla aggiornamenti" (asks
GitHub now, `/api/version/check`). The banner has "Aggiorna ora" only when the host watcher is alive: the container
has no Docker access, so `/api/update` just writes `updates/request` (folder `./updates` shared with the host) and
`update-watcher.sh`, run every minute by cron on gaia, runs `deploy.sh` and writes `updates/status`; it also touches
`updates/alive`, which is how the page knows the button can work. Cron line (installed by hand, with the user's OK):
`* * * * * /home/francesco/docker/mediaforge-web/update-watcher.sh`. The endpoint is unauthenticated like the rest
of the app: it can only trigger an update to what is on GitHub `main`, and it refuses while a conversion runs.

Installed on gaia (2026-10-18): a nightly cron, `0 4 * * * /home/francesco/docker/mediaforge-web/auto-update.sh`, which
runs `deploy.sh` only when GitHub `main` differs from the commit the container was built from (`deploy.sh` refuses
while a conversion runs: it just tries again the next night; log in `updates/auto-update.log`). The per-minute
`update-watcher.sh` for the in-page "Aggiorna ora" button is NOT installed (polling every minute was rejected), so
that button stays hidden and the banner shows the manual command.

Live numbers and charts: `parse_progress_line()` reads ffmpeg's progress line (`frame= fps= q= size= time= bitrate= speed=`) in
`run_ffmpeg_watchdog`; the latest values go to `job["stats"]` and every 10 s a sample to `job["history"]` (instantaneous bitrate
over the last ~20 s, since ffmpeg's `bitrate` is the average since the start; `pass` tells 2-pass runs apart). Progress lines no
longer fill the log. `/api/status/<id>?h=N` returns only the samples after the first N, so the polling stays light. The page shows
frame / time / size / ETA next to the bar and three canvas charts (fps, speed vs real time, bitrate), no library. `q` is not shown:
it is `-0.0` (not reported) with QSV and x265.

Predict bitrate: `POST /api/predict` (options of the page + `n`, `clip_min`, `times`) creates a job with `kind: "predict"`
that `run_predict()` fills: it encodes N short clips with `build_cmd()` (same GPU path, video stream only, `mkv` in a temp
folder; if the GPU path fails it retries the clip with software decoding) and puts the per-clip bitrates and the summary
(`summarize_bitrates()`) in `job["predict"]`, which `/api/status/<id>` returns. Progress and Cancel use the normal
`/api/status` and `/api/stop`. `GET /api/predict_points` (the page's "Nuovi punti" button) draws a fresh set of random
start times with the same `predict_start_times()` and the page writes them, as h:mm:ss, in "Punti scelti"; the status
poll carries `predict.starts` from the start, so the page shows which minute is being encoded. The job is hidden from `/api/jobs` so the page does not offer to re-attach to it; the
consequence is that `deploy.sh` does not see it and would restart the container during a prediction (it only costs the
prediction). Tried with `hevc_qsv` on a Windows PC (the local copy of the page on the Loki file gave 1870/1757/1247 kbps,
as the desktop); not tried on gaia. `record_progress()` had a latent bug found by this: a sample with `time=N/A` made the
next one fail with `float - None`; fixed, test added.

The live bitrate chart shows the VIDEO bitrate: ffmpeg's `size=` and `bitrate=` are those of the whole output file (measured on
a 30 s Loki clip, video QSV + two E-AC3 tracks copied: ffmpeg said 3245 kbps, video 1786 + audio 1455 from the packets;
a real encode showed "media 3628" in the chart against a true video bitrate of 2093), so `audio_output_kbps()` computes the
audio that ends up in the file (declared bitrate of each copied track, chosen bitrate of each re-encoded one; None if a copied
track has no declared bitrate or the job is audio-only) and `record_progress()` subtracts it from both series (never below 0;
pass 1 of a 2-pass run writes no audio, so it is 0 there). The page title says "audio da N kb/s sottratto", or "video + audio"
when it is unknown. It assumes constant-bitrate audio (AC-3, E-AC-3, AAC): not right for TrueHD, FLAC or DTS-HD. Early samples
of a short job are off because ffmpeg writes the audio before the video; on a film it fades. The chart's "media" is also the mean
of samples taken every 10 s of wall time, not of film time, so slow (complex) scenes weigh more: it is an indication, and the real
video bitrate is the one in the final message ("Bitrate output").

Bitrate over time (exact): when a job is `done`, `GET /api/bitrate_chart/<id>?step=60` returns the video bitrate per interval from
the packet sizes of the output (`read_video_packets()` / `bucket_bitrates()`, the same functions as the desktop; the packets are
cached in the job), `&source=1` adds the source (reads the whole source file: slow, off by default), `&csv=1` downloads it
(comma-separated `time_s,kbps[,source_kbps]`, one time column = the middle of each interval). The page draws it under the result (`#minuteBox`, step 30 s / 1 / 2 / 5 min, hover, mean
weighted by interval length = the real video bitrate). After every encode a thread also writes `<output>_bitrate.csv` (1-minute
step) next to the output file (`save_bitrate_csv()`, silent on failure). Tests: `tests/test_bitrate_chart.py`. Checked with a
synthetic 3 min clip on a local server; not tried on gaia nor on a real film.

Running view (page only; the desktop has its own Encode tab and log): while a conversion runs the page shows ONLY the job card
(`#jobCard`: title, the parameters, progress bar, stats, "Interrompi" / "Ferma e tieni il file parziale", GPU warning, live
charts, log); the library, options and tools cards are hidden (`setRunView()`). When it ends the card keeps the result (download,
preview, bitrate chart, CSV path) and shows "Nuova conversione", which brings the other cards back. The parameters come from the
server (`describe_job()`, stored in `job["params"]`, returned by `/api/encode`, `/api/status` and `/api/jobs`), so a page opened
during an encode (another PC, reload) reattaches with the same view (`/api/jobs` at load). The bitrate chart box (`#minuteBox`)
moves between the job card and `#fileChartCard` (the per-file button).

The jobs live in memory, so a restart of the server (every deploy) makes `/api/bitrate_chart/<id>` answer 404 for jobs already
finished: found out the hard way on 2026-11-12, when a deploy right after a Loki encode left the page with "not found" for the
chart and the CSV link. Two remedies: `GET /api/file_bitrate_chart?source=library|upload&path=..&step=60[&csv=1]` gives the same
chart/CSV for any file with no job (the page button "Bitrate nel tempo del file", `loadFileChart()`; the packets of the last 3
files are cached in `_file_jobs`; no source comparison), and the page now says so instead of "not found". The autosaved CSV
survives anyway (it is a file next to the output): `csv_path` in the answer, and the end-of-encode message, tell where it is
(`display_path()`: relative to the library, e.g. `/torrent-downloads/...`, or `/output/...`). Before a deploy, remember that
finished results open in someone's page are lost.

GPU decoder warning: if ffmpeg prints a line of `GPU_DECODE_ERRORS` (the decoder is dropping frames but ffmpeg would
still exit with 0), `run_ffmpeg_watchdog(watch_gpu=True)` raises `job["gpu_warning"]` and does NOT stop anything: software
encoding on gaia is very slow, so the page asks the user (continue / stop / `/api/retry_software` = redo everything
without GPU). The desktop app instead stops the attempt and falls back to software by itself. The Linux QSV decode path
never showed the problem (AV1 1080p, also the real 2h27 film: 1800/1800 and 2700/2700 frames), so no `-extra_hw_frames` here.

Safer first time for a big change: build a copy as `mediaforge-web:test` on port 5051 with its own library folder,
try it, then deploy. Rollback: `docker tag mediaforge-web:backup-20261018 mediaforge-web:local && docker compose up -d`
in that folder (that tag and `/home/francesco/docker/mediaforge-web.bak-20261018` hold the pre-clone version).
Never edit files on gaia: change them here and pull. The GPU path (Linux `-hwaccel qsv`) can only be verified on gaia.

## Relationship with the desktop app

Engine rules must match: default CRF curve (`crf` points in `index.html`), QSV preset levels, GPU decode gating
(`HW_DECODE_*`), FPS detection (`mpdecimate` on 5 clips), `STATS_TAGS_MKV` clearing, `ENCODING_SETTINGS` note.
Deliberately different: GPU path (`-hwaccel qsv` here, `d3d11va` + `hwmap` on Windows), encoders (QSV/x265 only here),
no Mux/Chapters tabs, jobs/library/upload/download.

## Parity with the sibling app (desktop <-> web)

`MediaForge` (desktop, `../MediaForge`) and `mediaforge-web` (`../mediaforge-web`, Docker on gaia) are separate
codebases that must keep the same encoding rules. Status as of 2026-10-18; `mediaforge-web/tests/test_parity.py`
enforces the "same" rows.

| Rule | Desktop | Web | Status |
|---|---|---|---|
| Default CRF by resolution (QSV) | `QSV_CRF_POINTS` | `CRF_PUNTI` in `index.html` | same (tested through node) |
| QSV presets | 3 levels, `hevc_qsv` only | 3 levels | same |
| GPU decode gating | `HW_DECODE_*` | `HW_DECODE_*` | same, web also allows `mjpeg` (Linux QSV decoder) |
| FPS check (mpdecimate on 5 clips) | `fps_contenuto()` | `detect_fps()` | same method; desktop adds a timestamp (VFR) check |
| Stats tags cleared, `ENCODING_SETTINGS` | yes | yes | same |
| Target bitrate, stop-and-keep (clean stop, partial file kept) | yes | yes | same (web: UI + `/api/finish`) |
| 2-pass | libx265, libsvtav1 | libx265 only | same idea; QSV has no 2nd pass |
| Per-job GPU decode switch, output bitrate/size after encode | `gpu_decode`, final log line | `hw_accel`, status message | same |
| Aspect ratio: crop / pad / DAR / stretch, "Originale", SAR/DAR rules | yes | yes | same (SAR parsing, DAR first, even-size rounding tested) |
| Non-square pixels warning with one-click fix (Deforma + Originale), warnings in the command preview | yes | yes | same |
| Real bitrate, declared bitrate, fix of a stale bitrate tag | yes | yes | same method; the web computes the real bitrate on every probe, the desktop only when nothing is declared or on request |
| GPU decoder drops frames (error lines in the ffmpeg log) | stops the attempt, software fallback by itself | warning in the page: continue / stop / redo in software | different on purpose (software is very slow on gaia) |
| GPU path | direct QSV first (`-hwaccel qsv`, as the web), then `d3d11va` + `hwmap`, then software | `-hwaccel qsv`, then software | same first choice; the extra d3d11va step exists only on Windows |
| Default CRF for a 4K source scaled down (QSV) | 14 (`QSV_CRF_DOWNSCALED_4K`, via `crf_suggerito(source_side=)`) | 14 (`QSV_CRF_DOWNSCALED_4K` in `index.html`; the page now also feeds the OUTPUT size after the resolution cap, as the desktop does) | same (tested through node) |
| Live charts while encoding (fps, speed, video bitrate instantaneous + average, audio subtracted) | `LiveStats`, `parse_progress_line()`, `audio_output_kbps()`, `LiveChartWindow` (a separate window) | `record_progress()`, `parse_progress_line()`, `audio_output_kbps()`, `drawChart()` (inside the page) | same numbers and rules (tested against each other); the desktop shows them in a window, the web in the page |
| Predict bitrate (N clips of X min, random or typed times, mean/min/max, predicted size) | `predict_bitrate()` | `/api/predict`, `run_predict()` | same method (start-time and summary functions tested against each other); the web runs it as a hidden job and has no software fallback beyond software decoding |
| Bitrate over time of the finished file (exact, per interval, CSV) | `BitrateChartWindow`, `read_video_packets()`, `bucket_bitrates()`, autosave `<output>_bitrate.csv` ("Bitrate CSV" checkbox, h:mm:ss times) | `/api/bitrate_chart`, same two functions, autosave `<output>_bitrate.csv` (seconds) | same method; CSV time columns differ (h:mm:ss vs seconds). Chart of any file with no job: desktop button "Bitrate chart", web button "Bitrate nel tempo del file" (`/api/file_bitrate_chart`); both say where the CSV was saved |
| Video "copy" (no re-encode) | `vcodec == "copy"` (already there; the DAR fix still works with copy) | codec "Copia originale" (`COPY_VIDEO`, `-c:v copy`); the page disables quality, bitrate, 2-pass, Prevedi bitrate, resolution, FPS, flip, preset, look-ahead and aspect ratio (the DAR fix is not offered with copy here); container, trim, audio, subtitles stay | same idea; web is stricter on purpose (all video options off) |
| Encoders | QSV, AMF, NVENC, VideoToolbox, x265, SVT-AV1 | QSV, x265 | different on purpose |
| Mux, Chapters, batch folder / library, upload, background jobs | desktop only / web only | | different on purpose |

When you change a "same" rule on one side, change the other and run both test suites.

## Conventions

- Code and the repo in English; the existing code still has Italian comments and UI text (translate only when asked)
- Surgical, non-destructive edits; do not rename existing functions without a request
- Tests: `py -m unittest discover -s tests -v` (parity with the desktop app; needs `pip install flask`, node for the CRF check)
