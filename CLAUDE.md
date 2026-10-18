# CLAUDE.md

Web sibling of the desktop app `../MediaForge` (GitHub `milanone/MediaForge`). Same ffmpeg encoding rules,
served by Flask in Docker on `gaia` (home server, Intel N150 iGPU).

## Layout

- `app.py` — Flask backend, one file: encoder probing, `build_filters()`, `build_cmd()` (+ `hw_plan()` for the
  QSV decode/scale path), `run_encode()` / `run_ffmpeg_watchdog()` (jobs in memory, stall watchdog, software
  retry), `detect_fps()`, maintenance remux, REST routes under `/api/`
- `static/index.html` — the whole UI (vanilla JS, no build step)
- `Dockerfile` (ubuntu 24.04 + `jellyfin-ffmpeg7`), `docker-compose.yaml` (paths of the author's server)

## Deployment

The running copy lives in `/home/francesco/docker/mediaforge-web` on gaia (container `mediaforge-web`, port 5050).
Until a deploy script exists, gaia gets files by hand (ask the user before every deploy: it restarts a service in use); the repo was imported from there on 2026-10-18
(commit "Import the version running on gaia"). Never edit on gaia without bringing the change back here.
The GPU path can only be verified on gaia (Linux `-hwaccel qsv`), not on Windows.

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
| GPU path | `d3d11va` + `hwmap` | `-hwaccel qsv` | different on purpose (platform) |
| Encoders | QSV, AMF, NVENC, VideoToolbox, x265, SVT-AV1 | QSV, x265 | different on purpose |
| Mux, Chapters, batch folder / library, upload, background jobs | desktop only / web only | | different on purpose |

When you change a "same" rule on one side, change the other and run both test suites.

## Conventions

- Code and the repo in English; the existing code still has Italian comments and UI text (translate only when asked)
- Surgical, non-destructive edits; do not rename existing functions without a request
- Tests: `py -m unittest discover -s tests -v` (parity with the desktop app; needs `pip install flask`, node for the CRF check)
