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
| GPU path | `d3d11va` + `hwmap` | `-hwaccel qsv` | different on purpose (platform) |
| Encoders | QSV, AMF, NVENC, VideoToolbox, x265, SVT-AV1 | QSV, x265 | different on purpose |
| Mux, Chapters, batch folder / library, upload, background jobs | desktop only / web only | | different on purpose |

When you change a "same" rule on one side, change the other and run both test suites.

## Conventions

- Code and the repo in English; the existing code still has Italian comments and UI text (translate only when asked)
- Surgical, non-destructive edits; do not rename existing functions without a request
- Tests: `py -m unittest discover -s tests -v` (parity with the desktop app; needs `pip install flask`, node for the CRF check)
