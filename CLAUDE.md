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
Until a deploy script exists, gaia gets files by hand; the repo was imported from there on 2026-10-18
(commit "Import the version running on gaia"). Never edit on gaia without bringing the change back here.
The GPU path can only be verified on gaia (Linux `-hwaccel qsv`), not on Windows.

## Relationship with the desktop app

Engine rules must match: default CRF curve (`crf` points in `index.html`), QSV preset levels, GPU decode gating
(`HW_DECODE_*`), FPS detection (`mpdecimate` on 5 clips), `STATS_TAGS_MKV` clearing, `ENCODING_SETTINGS` note.
Deliberately different: GPU path (`-hwaccel qsv` here, `d3d11va` + `hwmap` on Windows), encoders (QSV/x265 only here),
no Mux/Chapters tabs, jobs/library/upload/download.

## Conventions

- Code and the repo in English; the existing code still has Italian comments and UI text (translate only when asked)
- Surgical, non-destructive edits; do not rename existing functions without a request
- Tests: `py -m unittest discover -s tests -v` (none yet)
