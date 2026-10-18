# MediaForge Web

Browser front end for the HEVC/H.264 transcoding of [MediaForge](https://github.com/milanone/MediaForge),
running in Docker on a home server. Pick a file from the library (or upload one), choose quality,
resolution, FPS, aspect ratio, audio and subtitles, and the server re-encodes it with ffmpeg,
using the Intel iGPU (QuickSync) when available. It is the sibling of the desktop app: same
encoding rules, different plumbing (files on the server, jobs in the background, download of the result).

## Features

- Source from a mounted library or upload; output to a mounted folder, plus download and in-browser playback
- Encoders probed at startup with a real 1-frame encode: `hevc_qsv`, `h264_qsv`, `libx265`
- QSV: hardware decoding and `scale_qsv` when the source allows it (codec/pixel-format gating), automatic
  retry in software if the GPU path fails or stalls; 3 real preset levels (fast / normal / slow)
- Default CRF follows the output resolution (480p=16, 720p=18, 1080p=20, 4K=22), measured with VMAF on 4K films
- "Verifica FPS originali": counts the real (non-duplicated) frames on 5 short clips and sets the FPS accordingly
- Resolution cap, aspect ratio (crop / pad / DAR tag / stretch), flip, trim, audio re-encode or copy, subtitles
  (copy / SRT / none / mux afterwards), audio-only mode
- Rate control: constant quality (CRF) or a target average bitrate, with optional 2-pass for `libx265`
- "Ferma e tieni il file parziale": closes the running encode cleanly (ffmpeg `q`) and keeps a playable partial file
- Version in the footer and a "new version available" banner (compares with the latest commit on GitHub)
- Background jobs that survive closing the page (reattach from any browser)
- Maintenance remux (no re-encode): fix the `hvc1` tag, fix a stale bitrate tag

## Run

```bash
docker compose up -d --build     # http://<server>:5050
```

`docker-compose.yaml` is the one used on the author's server: adapt the library/output paths, the user and
the `render` group id (`group_add`) to your machine; `/dev/dri` must be passed through for QuickSync.
ffmpeg comes from the `jellyfin-ffmpeg7` package installed by the `Dockerfile`.

## Update

`./deploy.sh` in the project folder on the server pulls from GitHub and rebuilds the container (it refuses to run
while a conversion is in progress).

## Limits

- Only tested on an Intel N150 (QuickSync) with the Jellyfin ffmpeg build; AMD/NVIDIA are not supported here
- No authentication: expose it only on a trusted network
- Target bitrate, 2-pass and the clean stop were added on 2026-10-18 and tested with `libx265` on Windows only;
  not yet verified with QSV on the server

## License

MIT, see `LICENSE`.
