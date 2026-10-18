import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, abort, Response

# Overridable by environment so the module can be imported and tested outside
# the container (the defaults are the paths inside the Docker image).
FFMPEG = os.environ.get("MEDIAFORGE_FFMPEG", "/usr/lib/jellyfin-ffmpeg/ffmpeg")
FFPROBE = os.environ.get("MEDIAFORGE_FFPROBE", "/usr/lib/jellyfin-ffmpeg/ffprobe")

LIBRARY_DIR = Path(os.environ.get("MEDIAFORGE_LIBRARY", "/library"))
UPLOAD_DIR = Path(os.environ.get("MEDIAFORGE_UPLOADS", "/data/uploads"))
OUTPUT_DIR = Path(os.environ.get("MEDIAFORGE_OUTPUT", "/data/output"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".wmv", ".flv"}

# Tag statistici che mkvmerge scrive per traccia (BPS, DURATION,
# NUMBER_OF_FRAMES, NUMBER_OF_BYTES, entrambi anche in versione "-eng"):
# "-map_metadata 0" li copia alla cieca dal sorgente anche quando lo stream
# viene ricodificato a un peso/bitrate completamente diverso, ingannando
# MediaInfo/mkvmerge stessi (che senza un BPS esplicito calcolano il bitrate
# da NUMBER_OF_BYTES/DURATION, tornando comunque al valore stantio del
# sorgente) - vanno quindi svuotati esplicitamente sugli stream ricodificati.
# Stesso fix della versione desktop (github.com/milanone/MediaForge).
STATS_TAGS_MKV = ("BPS", "BPS-eng", "DURATION", "DURATION-eng",
                   "NUMBER_OF_FRAMES", "NUMBER_OF_FRAMES-eng",
                   "NUMBER_OF_BYTES", "NUMBER_OF_BYTES-eng")

app = Flask(__name__, static_folder="static", static_url_path="")

jobs = {}


# ---------------------------------------------------------------------------
# Encoder detection: a 1-frame real encode, not just "ffmpeg -encoders"
# (hevc_qsv/h264_qsv show up there even without the matching GPU present).
# ---------------------------------------------------------------------------
def encoder_works(encoder: str) -> bool:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "color=c=black:s=256x144:d=0.1",
           "-frames:v", "1", "-c:v", encoder, "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25, stdin=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


CANDIDATE_ENCODERS = [
    ("hevc_qsv", "HEVC (QSV - hardware)"),
    ("h264_qsv", "H.264 (QSV - hardware)"),
    ("libx265", "HEVC (CPU - software)"),
]
AVAILABLE_ENCODERS = [
    {"value": v, "label": l} for v, l in CANDIDATE_ENCODERS
    if v == "libx265" or encoder_works(v)
]


def resolve_bin_path(rel_path: str, source_type: str) -> Path:
    base = UPLOAD_DIR if source_type == "upload" else LIBRARY_DIR
    p = (base / rel_path).resolve()
    if not str(p).startswith(str(base.resolve())):
        abort(403)
    return p


def ffprobe_json(path: Path):
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
    )
    return json.loads(r.stdout or "{}")


def real_video_bitrate(path: Path, duration):
    """Bitrate reale del flusso video: somma i byte dei singoli pacchetti
    (non una stima dal bitrate del contenitore, spesso assente/impreciso su mkv)."""
    if not duration or duration <= 0:
        return None
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "packet=size", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        total = sum(int(l) for l in r.stdout.splitlines() if l.strip().isdigit())
        return round(total * 8 / duration) if total > 0 else None
    except Exception:
        return None


def declared_video_bitrate(video):
    """Bitrate video GIA dichiarato nel file (senza ricalcolarlo): campo bit_rate
    genuino di ffprobe, o in mancanza i tag BPS/BPS-eng (comuni su file mkv gia
    passati da mkvpropedit/ffmpeg -map_metadata). None se non c'e nulla di dichiarato."""
    if not video:
        return None
    br = video.get("bit_rate")
    if br:
        try:
            return int(br)
        except (TypeError, ValueError):
            pass
    tags = video.get("tags", {}) or {}
    for key in ("BPS", "BPS-eng"):
        if tags.get(key):
            try:
                return int(tags[key])
            except (TypeError, ValueError):
                pass
    return None


def simplify_streams(probe):
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    duration = float(probe.get("format", {}).get("duration") or 0) or None
    return video, audio, subs, duration


# ---------------------------------------------------------------------------
# ffmpeg command construction
# ---------------------------------------------------------------------------
PRESETS = ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]

# Stesse 4 scelte audio di MediaForge desktop, stessi bitrate DI DEFAULT
# (l'utente puo comunque cambiarli, vedi opts["audio_bitrate"])
AUDIO_ONLY_EXT = {"copy": ".mka", "aac": ".aac", "ac3": ".ac3", "mp3": ".mp3"}
AUDIO_DEFAULT_BITRATE = {"aac": 128, "ac3": 384, "mp3": 128}


def audio_codec_args(mode: str, bitrate=None) -> list:
    if mode == "copy":
        return ["-c:a", "copy"]
    if mode not in AUDIO_DEFAULT_BITRATE:
        return ["-c:a", "copy"]
    try:
        br = int(bitrate) if bitrate else AUDIO_DEFAULT_BITRATE[mode]
    except (TypeError, ValueError):
        br = AUDIO_DEFAULT_BITRATE[mode]
    if mode == "aac":
        return ["-c:a", "aac", "-b:a", f"{br}k"]
    if mode == "ac3":
        return ["-c:a", "ac3", "-b:a", f"{br}k"]
    if mode == "mp3":
        return ["-c:a", "mp3", "-b:a", f"{br}k", "-ar", "44100"]


def parse_ratio(ratio_str):
    """Accetta '16:9', '1.85:1' o un decimale puro '1.85' (come in MediaForge)."""
    if not ratio_str:
        return None
    s = ratio_str.strip()
    try:
        if ":" in s or "/" in s:
            num, den = s.replace(":", "/").split("/")
            return float(num) / float(den)
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


def sar_numeric(video):
    sar = (video or {}).get("sample_aspect_ratio") or "1:1"
    try:
        n, d = sar.split(":")
        n, d = float(n), float(d)
        return n / d if d else 1.0
    except (ValueError, AttributeError):
        return 1.0


def resolve_ratio(text, video):
    """'Originale' usa il DAR attuale del file (w*sar/h); altrimenti parse_ratio."""
    text = (text or "").strip()
    if text.lower() == "originale":
        w, h = (video or {}).get("width"), (video or {}).get("height")
        if not w or not h:
            return None
        return w * sar_numeric(video) / h
    return parse_ratio(text)


def build_filters(opts, video, bake_ar=False):
    """bake_ar=True e usato SOLO dall'anteprima fotogramma. Crop/pad/stretch
    ridisegnano gia i pixel per davvero, quindi usano la stessa espressione sia
    per la conversione vera che per l'anteprima. 'dar' invece e solo un tag per
    i player (setdar, non ridisegna nulla) - un JPEG estratto non lo
    mostrerebbe, quindi SOLO in questo caso l'anteprima usa un filtro diverso
    che 'cuoce' fisicamente il rapporto ridimensionando la larghezza."""
    filters = []
    cap = opts.get("resolution_cap")
    if cap and video:
        w, h = video.get("width"), video.get("height")
        if w and h:
            portrait = h > w
            if portrait and w > cap:
                filters.append(f"scale={cap}:-2")
            elif not portrait and h > cap:
                filters.append(f"scale=-2:{cap}")
    fps = opts.get("fps")
    if fps and not bake_ar:
        filters.append(f"fps={fps}")
    flip = opts.get("flip")
    if flip == "h":
        filters.append("hflip")
    elif flip == "v":
        filters.append("vflip")

    ar_mode = opts.get("ar_mode")
    if ar_mode and ar_mode != "none":
        ratio = resolve_ratio(opts.get("ar_ratio", ""), video)
        if ratio:
            r = f"({ratio})"
            if ar_mode == "crop":
                # Ritaglia dal centro fino al rapporto scelto; "sar" e una
                # variabile nativa di ffmpeg (pixel non quadrati/anamorfico).
                filters.append(f"crop=w='2*trunc((min(iw*sar,ih*{r})/sar)/2)':"
                                f"h='2*trunc(min(ih,iw*sar/{r})/2)'")
            elif ar_mode == "pad":
                # Barre nere centrate fino al rapporto scelto, arrotonda per eccesso
                # (pad non puo mai scendere sotto le dimensioni sorgente).
                filters.append(f"pad=w='2*ceil((max(iw*sar,ih*{r})/sar)/2)':"
                                f"h='2*ceil(max(ih,iw*sar/{r})/2)':"
                                "x='(ow-iw)/2':y='(oh-ih)/2'")
            elif ar_mode == "dar":
                if bake_ar:
                    filters.append(f"scale=trunc(ih*{ratio}/2)*2:ih")
                else:
                    filters.append(f"setdar={ratio}")
            elif ar_mode == "stretch":
                # Come 'dar' ma cambia davvero i pixel: setsar=1 e fondamentale,
                # altrimenti ffmpeg ricalcola da solo un sar compensativo che
                # manterrebbe il DAR originale, vanificando la deformazione.
                filters.append(f"scale=w='2*trunc((ih*{r})/2)':h='2*trunc(ih/2)'")
                filters.append("setsar=1")
    return filters


def build_maintenance_cmd(src: Path, dst: Path, opts: dict, probe: dict):
    """Remux puro (nessuna ricodifica) per le due utility 'fix tag' di MediaForge:
    fix_hvc1 corregge il tag HEVC (hev1 -> hvc1) per compatibilita Apple/QuickTime,
    fix_bitrate riscrive il tag BPS col bitrate REALE del video (packet-size, non stimato)."""
    mode = opts["mode"]
    cmd = [FFMPEG, "-y", "-i", str(src), "-c", "copy", "-map_metadata", "0"]
    if mode == "fix_hvc1":
        cmd += ["-tag:v", "hvc1"]
    elif mode == "fix_bitrate":
        _, _, _, duration = simplify_streams(probe)
        bitrate = real_video_bitrate(src, duration)
        # bitrate assente -> il tag viene solo svuotato (nessun valore
        # attendibile da scrivere). Svuota anche gli altri tag statistici
        # mkvmerge stantii (STATS_TAGS_MKV): senza, "-map_metadata 0" li
        # copierebbe tali e quali da src a dst, e con BPS ormai corretto
        # MediaInfo/mkvmerge tornerebbero comunque a calcolare il bitrate da
        # NUMBER_OF_BYTES/DURATION stantii.
        valore = str(bitrate) if bitrate else ""
        cmd += ["-metadata:s:v:0", f"BPS={valore}", "-metadata:s:v:0", f"BPS-eng={valore}"]
        for tag in STATS_TAGS_MKV:
            if tag not in ("BPS", "BPS-eng"):
                cmd += ["-metadata:s:v:0", f"{tag}="]
    cmd += [str(dst)]
    return cmd


# Decodifica hardware QSV: codec e formati pixel supportati dalla iGPU dell'N150
# (4:2:0 a 8/10 bit). Altro (es. 4:2:2, 4:4:4, 12 bit) resta in software.
HW_DECODE_CODECS = {"h264", "hevc", "av1", "vp9", "mpeg2video", "vc1", "mjpeg"}
HW_DECODE_PIXFMTS = {"yuv420p", "yuvj420p", "yuv420p10le", "nv12", "p010le"}


def hw_cap_size(video, cap):
    """Dimensioni (pari) dopo il limite di risoluzione, come fa build_filters, oppure None."""
    w, h = video.get("width"), video.get("height")
    if not (cap and w and h):
        return None
    even = lambda v: max(2, int(round(v / 2.0)) * 2)
    if h > w:
        return (cap, even(h * cap / w)) if w > cap else None
    return (even(w * cap / h), cap) if h > cap else None


def hw_plan(opts: dict, video, filters):
    """'full' = decodifica+scaling su GPU (frame sempre in memoria video);
    'decode' = solo decodifica su GPU, filtri software (frame riportati in RAM);
    None = tutto software come prima."""
    if not video or opts.get("hw_accel") is False:
        return None
    if opts.get("codec", "hevc_qsv") not in ("hevc_qsv", "h264_qsv"):
        return None
    if video.get("codec_name") not in HW_DECODE_CODECS or video.get("pix_fmt") not in HW_DECODE_PIXFMTS:
        return None
    if not filters:
        return "full"
    if len(filters) == 1 and filters[0].startswith("scale=") and hw_cap_size(video, opts.get("resolution_cap")):
        return "full"
    return "decode"


def build_cmd(src: Path, dst: Path, opts: dict, probe: dict, hw: bool = True):
    if opts.get("mode") in ("fix_hvc1", "fix_bitrate"):
        return build_maintenance_cmd(src, dst, opts, probe)

    video, audio, subs, _ = simplify_streams(probe)
    cmd = [FFMPEG, "-y"]
    plan = hw_plan(opts, video, build_filters(opts, video)) if (hw and not opts.get("audio_only")) else None
    if plan == "full":
        cmd += ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
    elif plan == "decode":
        # fotogrammi decodificati dalla GPU, riportati in RAM (hwdownload) prima dei filtri software
        cmd += ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
    if opts.get("start"):
        cmd += ["-ss", opts["start"]]
    cmd += ["-i", str(src)]
    if opts.get("duration"):
        cmd += ["-t", opts["duration"]]

    # Un trim (-ss/-t) invalida i tag statistici mkvmerge (STATS_TAGS_MKV)
    # anche su uno stream lasciato in "copia": ffmpeg ricalcola da solo
    # DURATION in base al contenuto realmente scritto, ma NON tocca
    # BPS/NUMBER_OF_BYTES/NUMBER_OF_FRAMES, che restano quelli dell'INTERO
    # file sorgente (verificato sulla versione desktop: estraendo 4s da un
    # file di 12s con audio in copia, NUMBER_OF_BYTES/BPS restavano quelli
    # dei 12s originali, calcolando un bitrate dichiarato ~3 volte troppo
    # alto). Vanno quindi svuotati anche qui, non solo quando l'audio viene
    # ricodificato.
    trim_attivo = bool(opts.get("start")) or bool(opts.get("duration"))

    if opts.get("audio_only"):
        a_idx = opts.get("audio_index", 0)
        cmd += ["-map", f"0:a:{a_idx}", "-map_metadata", "0", "-vn"]
        audio_mode = opts.get("audio_mode", "aac")
        cmd += audio_codec_args(audio_mode, opts.get("audio_bitrate"))
        if audio_mode != "copy" or trim_attivo:
            # Vedi STATS_TAGS_MKV: "-map_metadata 0" copierebbe alla cieca i
            # tag statistici del sorgente anche col nuovo bitrate fisso scelto
            # qui, o anche in copia se un trim ha cambiato la durata reale.
            for tag in STATS_TAGS_MKV:
                cmd += ["-metadata:s:a:0", f"{tag}="]
        cmd += [str(dst)]
        return cmd

    cmd += ["-map", "0:v:0"]

    audio_indices = opts.get("audio_indices")
    if audio_indices is None:
        if audio:
            cmd += ["-map", "0:a?"]
        n_audio_out = len(audio)
    else:
        for i in audio_indices:
            cmd += ["-map", f"0:a:{i}"]
        n_audio_out = len(audio_indices)

    # "mux" (vedi build_postmux_subs_cmd/run_encode): i sottotitoli vanno
    # esclusi anche qui dalla codifica principale, come "no" - vengono
    # aggiunti dopo con un remux separato, non durante questo passaggio.
    subs_mode = opts.get("subs_mode", "copy")
    escludi_subs = subs_mode in ("no", "mux")
    sub_indices = opts.get("sub_indices")
    if not escludi_subs:
        if sub_indices is None:
            if subs:
                cmd += ["-map", "0:s?"]
        else:
            for i in sub_indices:
                cmd += ["-map", f"0:s:{i}"]

    filters = build_filters(opts, video)
    if plan == "decode":
        ten = "10" in str(video.get("pix_fmt"))
        filters = ["hwdownload", "format=p010le" if ten else "format=nv12"] + filters
    if plan == "full" and filters:
        # unico filtro = limite di risoluzione: scaling sulla GPU (scale_qsv)
        size = hw_cap_size(video, opts.get("resolution_cap"))
        filters = [f"scale_qsv=w={size[0]}:h={size[1]}"]
    if filters:
        cmd += ["-vf", ",".join(filters)]

    codec = opts.get("codec", "hevc_qsv")
    quality = str(opts.get("quality", 23))
    preset = opts.get("preset") if opts.get("preset") in PRESETS else None
    # Target average bitrate (single-pass VBR) instead of constant quality when
    # opts["bitrate_kbps"] is set; same behaviour as the desktop app.
    bitrate = opts.get("bitrate_kbps")
    rate = ["-b:v", f"{int(bitrate)}k"] if bitrate else None
    if codec in ("hevc_qsv", "h264_qsv"):
        cmd += ["-c:v", codec] + (rate or ["-global_quality", quality])
        if preset:
            cmd += ["-preset", preset]
        if opts.get("look_ahead", True):
            cmd += ["-look_ahead", "1"]
    elif codec == "libx265":
        cmd += ["-c:v", "libx265"] + (rate or ["-crf", quality]) + ["-preset", preset or "medium"]
    else:
        cmd += ["-c:v", codec] + (rate or ["-global_quality", quality])
    if opts.get("pass_num"):
        # 2-pass (libx265 only, see run_encode): same command for both passes
        cmd += ["-pass", str(opts["pass_num"]), "-passlogfile", str(opts["passlog"])]

    # Tag hvc1 = compatibilita Apple/QuickTime per HEVC dentro un contenitore mp4
    if codec in ("hevc_qsv",) and dst.suffix.lower() == ".mp4":
        cmd += ["-tag:v", "hvc1"]

    # Il video e' sempre ricodificato in questa app (nessuna opzione "copia"
    # per il codec video): vedi STATS_TAGS_MKV, "-map_metadata 0" copierebbe
    # alla cieca i tag statistici del sorgente anche al nuovo bitrate/peso.
    for tag in STATS_TAGS_MKV:
        cmd += ["-metadata:s:v:0", f"{tag}="]

    # Annota i parametri di codifica usati (encoder/CRF, look-ahead, ed ogni
    # filtro/impostazione applicata rispetto ai default - risoluzione, aspect
    # ratio, fps, flip, poi l'audio piu' sotto): utile per ricordarsi in
    # futuro come e' stato prodotto il file senza doverlo ririlevare da
    # MediaInfo. Stessa idea della versione desktop (github.com/milanone/MediaForge).
    nota_codifica = [f"{codec} " + (f"bitrate={int(bitrate)}k" + (" 2-pass" if opts.get("pass_num") else "")
                                    if bitrate else f"CRF={quality}")]
    if opts.get("look_ahead", True) and codec not in ("libx265",):
        nota_codifica.append("look-ahead")
    if opts.get("resolution_cap"):
        nota_codifica.append(f"max {opts['resolution_cap']}p")
    ar_mode = opts.get("ar_mode") or "none"
    if ar_mode != "none":
        nota_codifica.append(f"{ar_mode} {opts.get('ar_ratio', '')}".strip())
    if opts.get("fps"):
        nota_codifica.append(f"fps={opts['fps']}")
    if opts.get("flip") == "h":
        nota_codifica.append("hflip")
    elif opts.get("flip") == "v":
        nota_codifica.append("vflip")

    audio_mode = opts.get("audio_mode", "copy")
    cmd += audio_codec_args(audio_mode, opts.get("audio_bitrate"))
    if audio_mode != "copy" or trim_attivo:
        for i in range(n_audio_out):
            for tag in STATS_TAGS_MKV:
                cmd += [f"-metadata:s:a:{i}", f"{tag}="]
    if audio_mode != "copy":
        bitrate_audio = opts.get("audio_bitrate") or AUDIO_DEFAULT_BITRATE.get(audio_mode)
        nota_codifica.append(f"audio {audio_mode} {bitrate_audio}k".strip())

    cmd += ["-metadata:s:v:0", f"ENCODING_SETTINGS={', '.join(nota_codifica)}"]

    if not escludi_subs and subs:
        if dst.suffix.lower() == ".mp4":
            # Il muxer mp4 accetta SOLO sottotitoli testuali "mov_text": codec
            # come ass/ssa/subrip fanno fallire l'INTERA conversione (file di
            # 0 byte), non solo il flusso sottotitoli - quindi si forza sempre,
            # a prescindere da "copy"/"srt" (stesso comportamento di MediaForge desktop).
            cmd += ["-c:s", "mov_text"]
        elif subs_mode == "srt":
            cmd += ["-c:s", "srt"]
        else:
            cmd += ["-c:s", "copy"]

    cmd += ["-map_metadata", "0"]
    if opts.get("pass_num") == 1:
        # pass 1: analysis only, nothing is written (audio and subtitles are not needed)
        cmd += ["-an", "-sn", "-f", "null", os.devnull]
    else:
        cmd += [str(dst)]
    return cmd


def build_postmux_subs_cmd(encoded_path: Path, src: Path, probe: dict, opts: dict, dst: Path,
                            warnings: list = None) -> list:
    """Aggiunge (remux puro, "-c copy", nessuna ricodifica) i sottotitoli del
    sorgente originale a un file gia' codificato SENZA sottotitoli: secondo
    passaggio della modalita' "Muxa a fine codifica" (subs_mode=="mux", vedi
    build_cmd) - input 0 e' il file gia' codificato (video/audio), input 1 e'
    il sorgente originale, da cui si rimappano solo gli stream sottotitoli.
    Usato sia dalla scelta manuale dell'utente sia dal recupero automatico
    dopo uno stallo (vedi run_encode): tenere i sottotitoli fuori dal
    passaggio di codifica vero e proprio - piu' lento e piu' esposto a stalli
    su timestamp corrotti - e aggiungerli qui con un remux separato, piu'
    veloce perche' non deve interlacciare anche video/audio da ricodificare.
    Applica lo stesso -ss/-t della codifica principale (opts) anche qui, sul
    sorgente da cui vengono presi i sottotitoli: altrimenti, con un trim
    attivo, arriverebbero integrali (l'intero file) invece che tagliati
    quanto video/audio. Stesso approccio della versione desktop
    (github.com/milanone/MediaForge)."""
    _, _, subs, _ = simplify_streams(probe)
    sub_indices = opts.get("sub_indices")
    idxs = list(range(len(subs))) if sub_indices is None else sub_indices

    cmd = [FFMPEG, "-y", "-i", str(encoded_path)]
    if opts.get("start"):
        cmd += ["-ss", opts["start"]]
    cmd += ["-i", str(src)]
    if opts.get("duration"):
        cmd += ["-t", opts["duration"]]
    cmd += ["-map", "0", "-map_metadata", "0"]
    for i in idxs:
        cmd += ["-map", f"1:s:{i}"]
    cmd += ["-c", "copy"]

    if idxs:
        sub_codec = "copy"
        if dst.suffix.lower() == ".mp4":
            codecs = {subs[i].get("codec_name") for i in idxs if i < len(subs)} - {"mov_text"}
            if codecs:
                sub_codec = "mov_text"
                if warnings is not None:
                    nomi = ", ".join(sorted(codecs))
                    warnings.append(
                        f"Sottotitoli {nomi} non supportati dal contenitore mp4: "
                        "conversione forzata in mov_text (perde lo styling avanzato "
                        "ASS/SSA, mantiene testo e timing).")
        cmd += ["-c:s", sub_codec]

    cmd.append(str(dst))
    return cmd


TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+)\.(\d+)")

STALL_TIMEOUT = 8.0  # secondi senza che "time=" avanzi prima di considerare lo stallo


def run_ffmpeg_watchdog(cmd, job, duration) -> str:
    """Lancia UN tentativo ffmpeg e ne segue l'esito, inoltrando le righe al
    log del job. Oltre alla cancellazione manuale (job["cancel_requested"],
    vedi /api/stop) e al codice di uscita, rileva uno STALLO: se "time=" non
    avanza per STALL_TIMEOUT secondi il processo viene considerato bloccato e
    terminato - non e' un errore ffmpeg con un codice di uscita, il processo
    resta vivo (a volte continuando a stampare righe, ferme o incoerenti) ma
    non avanza piu'. Il caso tipico e' un flusso sottotitoli con timestamp
    corrotto che manda in crisi il buffer di interleaving del muxer (vedi
    run_encode, che in quel caso riprova escludendo i sottotitoli) - stesso
    problema e stessa diagnosi della versione desktop
    (github.com/milanone/MediaForge, esegui_ffmpeg_con_watchdog).
    La lettura di stderr avviene in un thread separato: leggerla riga per
    riga in questo stesso ciclo bloccherebbe il controllo periodico dello
    stallo proprio nel caso che deve rilevare, quello in cui non arriva piu'
    nessuna riga. Ritorna "ok"/"cancelled"/"stalled"/"error"/"finished".
    "finished" = chiusura pulita su richiesta (job["finish_requested"], vedi
    /api/finish): si manda "q" a ffmpeg, che finisce i fotogrammi in coda e
    scrive la chiusura del contenitore, cosi' il file parziale e' riproducibile
    (come "Stop and keep file" della versione desktop). stdin e' quindi una pipe."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, stdin=subprocess.PIPE,
    )
    job["proc"] = proc

    line_q = queue.Queue()

    def reader():
        for line in proc.stderr:
            line_q.put(line.rstrip())
        line_q.put(None)  # sentinella: stderr chiuso, il processo ha finito

    threading.Thread(target=reader, daemon=True).start()

    last_progress = time.monotonic()
    last_time_val = None
    outcome = "ok"
    finish_sent = None  # instant "q" was sent
    while True:
        if job.get("finish_requested") and finish_sent is None and not job.get("cancel_requested"):
            try:
                proc.stdin.write("q")
                proc.stdin.flush()
            except OSError:
                pass
            finish_sent = time.monotonic()
            outcome = "finished"
            job["log"].append("Chiusura pulita del file in corso...")
        if finish_sent is not None and time.monotonic() - finish_sent > 30:
            # ffmpeg did not close within 30 s: the file cannot be trusted
            proc.terminate()
            outcome = "cancelled"
            break
        if job.get("cancel_requested"):
            proc.terminate()
            outcome = "cancelled"
            break
        # Controllato ad OGNI giro, non solo quando la coda resta vuota: uno
        # stallo reale puo' comunque continuare a produrre un flusso fitto di
        # righe non di progresso, quindi il timeout della get() da solo non
        # basterebbe a rilevarlo.
        if finish_sent is None and time.monotonic() - last_progress > STALL_TIMEOUT:
            proc.terminate()
            outcome = "stalled"
            break
        try:
            line = line_q.get(timeout=1.0)
        except queue.Empty:
            continue
        if line is None:
            break
        if not line:
            continue
        m = TIME_RE.search(line)
        if m and m.group(0) != last_time_val:
            last_time_val = m.group(0)
            last_progress = time.monotonic()
            if duration:
                h, mnt, s, frac = m.groups()
                cur = int(h) * 3600 + int(mnt) * 60 + int(s) + float("0." + frac)
                job["progress"] = max(0, min(100, round(cur / duration * 100, 1)))
        job["log"].append(line)
        job["log"] = job["log"][-300:]

    proc.wait()
    if outcome == "finished" and proc.returncode not in (0, 255):
        outcome = "error"
    if outcome == "ok" and proc.returncode != 0:
        outcome = "error"
    return outcome


def run_encode(job_id, src_path: Path, dst_path: Path, opts: dict, probe: dict):
    job = jobs[job_id]
    _, _, _, duration = simplify_streams(probe)
    if opts.get("duration"):
        try:
            duration = float(opts["duration"])
        except ValueError:
            pass
    job["duration"] = duration

    # Solo gli encode "normali" (non audio-only, non maintenance) hanno
    # sottotitoli nel percorso principale - la logica "muxa dopo"/"riprova
    # senza sottotitoli in caso di stallo" ha senso solo li'.
    can_defer_subs = not opts.get("audio_only") and opts.get("mode") not in ("fix_hvc1", "fix_bitrate")
    subs_mode = opts.get("subs_mode", "copy")
    defer_subs = can_defer_subs and subs_mode == "mux"
    retried_for_stall = False
    use_hw = True

    # 2-pass: only libx265 (QSV has no 2nd pass), only with a target bitrate. Pass 1 is analysis
    # (no output, subtitles excluded); pass 2 then runs through the normal loop below.
    pass_dir = None
    if (opts.get("two_pass") and opts.get("bitrate_kbps") and opts.get("codec") == "libx265"
            and can_defer_subs):
        pass_dir = Path(tempfile.mkdtemp(prefix="mediaforge_pass_"))
        opts = dict(opts, passlog=str(pass_dir / "pass"))
        cmd1 = build_cmd(src_path, dst_path, dict(opts, pass_num=1, subs_mode="no"), probe, hw=False)
        job["log"].append("Passata 1/2 (analisi)")
        job["cmd"] = " ".join(cmd1)
        outcome1 = run_ffmpeg_watchdog(cmd1, job, duration)
        if outcome1 in ("cancelled", "finished"):
            job["status"] = "cancelled"
            shutil.rmtree(pass_dir, ignore_errors=True)
            return
        if outcome1 != "ok":
            job["status"] = "error"
            shutil.rmtree(pass_dir, ignore_errors=True)
            return
        job["log"].append("Passata 2/2 (codifica)")
        job["progress"] = 0
        opts = dict(opts, pass_num=2)

    try:
        while True:
            opts_attempt = dict(opts, subs_mode="no") if defer_subs else opts
            dst_attempt = (dst_path.with_name(f".{dst_path.stem}.novosub{dst_path.suffix}")
                            if defer_subs else dst_path)

            cmd = build_cmd(src_path, dst_attempt, opts_attempt, probe, hw=use_hw)
            job["cmd"] = " ".join(cmd)
            hw_used = "-hwaccel" in cmd
            if hw_used:
                job["log"].append("Accelerazione GPU: decodifica" +
                                   (" + scaling" if "scale_qsv" in " ".join(cmd) else "") + " su QSV" +
                                   (" (filtri in software)" if "hwdownload" in " ".join(cmd) else "") + ".")

            outcome = run_ffmpeg_watchdog(cmd, job, duration)

            if outcome == "cancelled":
                job["status"] = "cancelled"
                dst_attempt.unlink(missing_ok=True)
                return

            if hw_used and outcome in ("error", "stalled"):
                job["log"].append("Decodifica/scaling su GPU non riusciti: riprovo tutto in software.")
                dst_attempt.unlink(missing_ok=True)
                use_hw = False
                continue

            if outcome == "stalled":
                if can_defer_subs and not defer_subs and not retried_for_stall and subs_mode != "no":
                    job["log"].append(
                        f"Codifica bloccata da oltre {STALL_TIMEOUT:.0f}s (probabile problema "
                        "di timestamp nei sottotitoli): riprovo escludendoli, verranno "
                        "riaggiunti con un remux separato a fine codifica.")
                    dst_attempt.unlink(missing_ok=True)
                    defer_subs = True
                    retried_for_stall = True
                    continue
                job["log"].append(f"Codifica bloccata da oltre {STALL_TIMEOUT:.0f}s (stallo).")
                job["status"] = "error"
                dst_attempt.unlink(missing_ok=True)
                return

            if outcome == "error":
                job["status"] = "error"
                dst_attempt.unlink(missing_ok=True)
                return

            # outcome == "ok" or "finished" (clean stop on request: keep the partial file)
            partial = outcome == "finished"
            if partial and defer_subs:
                job["log"].append("Sottotitoli non aggiunti al file parziale.")
                os.replace(dst_attempt, dst_path)
            if defer_subs and not partial:
                warnings = []
                postmux_cmd = build_postmux_subs_cmd(dst_attempt, src_path, probe, opts, dst_path, warnings)
                for w in warnings:
                    job["log"].append(f"Avviso: {w}")
                job["log"].append("$ (aggiunta sottotitoli) " + " ".join(postmux_cmd))
                try:
                    r = subprocess.run(postmux_cmd, capture_output=True, text=True,
                                        timeout=600, stdin=subprocess.DEVNULL)
                except Exception as e:
                    job["status"] = "error"
                    job["log"].append(str(e))
                    dst_attempt.unlink(missing_ok=True)
                    return
                dst_attempt.unlink(missing_ok=True)
                if r.returncode != 0:
                    job["status"] = "error"
                    job["log"].append((r.stderr or "")[-2000:])
                    dst_path.unlink(missing_ok=True)
                    return

            if dst_path.exists():
                if partial:
                    job["partial"] = True
                    job["log"].append("Fermata su richiesta: file parziale conservato.")
                else:
                    job["progress"] = 100
                out_duration = float(ffprobe_json(dst_path).get("format", {}).get("duration") or 0) or duration
                job["output_bitrate"] = real_video_bitrate(dst_path, out_duration)
                job["output_size"] = dst_path.stat().st_size
                # last: the status poll must not see "done" before bitrate/size are set
                job["status"] = "done"
            else:
                job["status"] = "error"
            return
    except Exception as e:
        job["status"] = "error"
        job["log"].append(str(e))
    finally:
        if pass_dir:
            shutil.rmtree(pass_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/info")
def info():
    return jsonify({"encoders": AVAILABLE_ENCODERS, "presets": PRESETS,
                     "audio_default_bitrate": AUDIO_DEFAULT_BITRATE})


@app.route("/api/browse")
def browse():
    rel = request.args.get("path", "")
    target = (LIBRARY_DIR / rel).resolve()
    if not str(target).startswith(str(LIBRARY_DIR.resolve())):
        abort(403)
    if not target.exists():
        return jsonify({"dirs": [], "files": [], "current": rel})
    dirs, files, entries = [], [], []
    for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        entry_rel = str(entry.relative_to(LIBRARY_DIR))
        if rel == "" and entry.name == "output" and entry.is_dir():
            continue  # cartella di output dell'app stessa, non e una sorgente da convertire
        if entry.is_dir():
            dirs.append(entry_rel)
            entries.append({"name": entry.name, "path": entry_rel, "dir": True,
                            "type": "Cartella", "size": None})
        elif entry.suffix.lower() in VIDEO_EXT:
            files.append(entry_rel)
            try:
                size = entry.stat().st_size
            except OSError:
                size = None
            entries.append({"name": entry.name, "path": entry_rel, "dir": False,
                            "type": entry.suffix.lower().lstrip(".").upper(), "size": size})
    return jsonify({"dirs": dirs, "files": files, "entries": entries, "current": rel})


@app.route("/api/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "no file"}), 400
    import os
    safe_name = f"{uuid.uuid4().hex}_{os.path.basename(f.filename)}"
    dest = UPLOAD_DIR / safe_name
    f.save(dest)
    return jsonify({"path": safe_name, "source": "upload"})


@app.route("/api/probe")
def probe_route():
    source_type = request.args.get("source", "library")
    rel_path = request.args.get("path", "")
    src = resolve_bin_path(rel_path, source_type)
    if not src.exists():
        return jsonify({"error": "file not found"}), 404

    probe = ffprobe_json(src)
    video, audio, subs, duration = simplify_streams(probe)
    real_bitrate = real_video_bitrate(src, duration)

    def lang_of(s):
        return (s.get("tags", {}) or {}).get("language", "?")

    return jsonify({
        "duration": duration,
        "size": src.stat().st_size,
        "video": {
            "codec": video.get("codec_name") if video else None,
            "width": video.get("width") if video else None,
            "height": video.get("height") if video else None,
            "fps": eval(video["r_frame_rate"]) if video and "/" in str(video.get("r_frame_rate", "")) else None,
            "real_bitrate": real_bitrate,
            "declared_bitrate": declared_video_bitrate(video),
        } if video else None,
        "audio": [
            {"index": i, "codec": s.get("codec_name"), "language": lang_of(s),
             "channels": s.get("channels"), "title": (s.get("tags", {}) or {}).get("title", "")}
            for i, s in enumerate(audio)
        ],
        "subtitles": [
            {"index": i, "codec": s.get("codec_name"), "language": lang_of(s)}
            for i, s in enumerate(subs)
        ],
    })


STD_RATES = [(24000/1001, "24000/1001"), (24.0, "24"), (25.0, "25"), (30000/1001, "30000/1001"),
             (30.0, "30"), (50.0, "50"), (60000/1001, "60000/1001"), (60.0, "60")]


def parse_rate(text):
    try:
        a, b = str(text).split("/")
        return float(a) / float(b) if float(b) else None
    except (ValueError, AttributeError):
        return None


@app.route("/api/detect_fps")
def detect_fps():
    """Stima i fotogrammi realmente distinti (senza ripetizioni) su alcuni spezzoni."""
    src = resolve_bin_path(request.args.get("path", ""), request.args.get("source", "library"))
    if not src.exists():
        return jsonify({"error": "file not found"}), 404
    probe = ffprobe_json(src)
    video = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        return jsonify({"error": "nessun flusso video"}), 400
    declared = parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate"))
    duration = float(probe.get("format", {}).get("duration") or 0)
    win = 8.0
    if not declared or duration < win * 2:
        return jsonify({"error": "file troppo corto o fps non rilevabile"}), 400
    points = [duration * f for f in (0.15, 0.3, 0.5, 0.7, 0.85)]
    values = []
    deadline = time.time() + 150
    for t in points:
        if time.time() > deadline:
            break
        t = min(t, duration - win - 1)
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-nostats", "-ss", f"{t:.1f}", "-t", str(win), "-i", str(src),
             "-map", "0:v:0", "-an", "-sn", "-vf", "scale=320:-2,mpdecimate,showinfo", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        n = len(re.findall(r"Parsed_showinfo.*\bn:\s*\d+", r.stderr))
        if n:
            values.append(n / win)
    if not values:
        return jsonify({"error": "analisi non riuscita"}), 500
    values.sort()
    # le scene quasi ferme fanno scartare fotogrammi veri: il massimo e' la stima migliore
    effective = values[-1]
    match = min(STD_RATES, key=lambda x: abs(x[0] - effective))
    suggestion = match[1] if abs(match[0] - effective) / match[0] <= 0.04 else None
    ntsc = abs(declared * 1.001 - round(declared * 1.001)) < 0.01
    if suggestion and ntsc and suggestion in ("24", "30", "60"):
        suggestion = {"24": "24000/1001", "30": "30000/1001", "60": "60000/1001"}[suggestion]
    no_dups = effective >= declared * 0.93
    return jsonify({
        "declared": declared, "effective": round(effective, 2),
        "per_window": [round(v, 2) for v in values],
        "duplicates_pct": 0 if no_dups else round((1 - effective / declared) * 100),
        "suggestion": "" if no_dups else suggestion,
        "irregular": (not no_dups) and suggestion is None,
    })


@app.route("/api/preview", methods=["POST"])
def preview():
    data = request.get_json(force=True)
    src = resolve_bin_path(data.get("path", ""), data.get("source", "library"))
    if not src.exists():
        return jsonify({"error": "file not found"}), 404
    probe = ffprobe_json(src)
    dst = OUTPUT_DIR / ("preview" + container_ext(data, src))
    cmd = build_cmd(src, dst, data, probe)
    return jsonify({"cmd": " ".join(cmd)})


@app.route("/api/frame_preview", methods=["POST"])
def frame_preview():
    """Estrae UN fotogramma applicando la stessa catena di filtri (-vf) che
    verrebbe usata nella conversione reale (scale/DAR/flip), cosi l'anteprima
    coincide sempre con l'output vero - stessa logica di costruisci_filtri_video
    in MediaForge desktop, condivisa tra comando reale e frame di anteprima."""
    data = request.get_json(force=True)
    if data.get("mode") in ("fix_hvc1", "fix_bitrate") or data.get("audio_only"):
        abort(404)
    src = resolve_bin_path(data.get("path", ""), data.get("source", "library"))
    if not src.exists():
        return jsonify({"error": "file not found"}), 404

    probe = ffprobe_json(src)
    video, _, _, duration = simplify_streams(probe)
    if not video:
        return jsonify({"error": "nessuno stream video"}), 400

    try:
        ts = float(data["start"]) if data.get("start") else (duration or 10) / 3
    except (TypeError, ValueError):
        ts = (duration or 10) / 3

    filters = build_filters(data, video, bake_ar=True)
    cmd = [FFMPEG, "-y", "-ss", str(ts), "-i", str(src)]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-frames:v", "1", "-q:v", "3", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        if r.returncode != 0 or not r.stdout:
            return jsonify({"error": "impossibile estrarre il fotogramma"}), 500
        return Response(r.stdout, mimetype="image/jpeg")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def unique_output_name(stem: str, suffix: str, directory: Path = None) -> str:
    """Nome file pulito (senza prefissi tecnici tipo l'id del job) nella cartella
    di destinazione (di norma quella del sorgente): se esiste gia un file con quel
    nome, aggiunge (1), (2)... invece di sovrascrivere una conversione precedente."""
    directory = directory or OUTPUT_DIR
    candidate = f"{stem}{suffix}"
    if not (directory / candidate).exists():
        return candidate
    n = 1
    while (directory / f"{stem} ({n}){suffix}").exists():
        n += 1
    return f"{stem} ({n}){suffix}"


def container_ext(opts, src: Path = None):
    if opts.get("mode") in ("fix_hvc1", "fix_bitrate"):
        return src.suffix if src else ".mp4"
    if opts.get("audio_only"):
        return AUDIO_ONLY_EXT.get(opts.get("audio_mode", "aac"), ".mka")
    return ".mp4" if opts.get("container") == "mp4" else ".mkv"


@app.route("/api/stream")
def stream():
    """Serve il file sorgente (libreria o upload) per la riproduzione inline
    nel browser (<video>), non come download forzato - a differenza di
    /api/download supporta le richieste Range necessarie per il seek."""
    source_type = request.args.get("source", "library")
    rel_path = request.args.get("path", "")
    src = resolve_bin_path(rel_path, source_type)
    if not src.exists():
        abort(404)
    return send_from_directory(src.parent, src.name, as_attachment=False, conditional=True)


@app.route("/api/play/<job_id>")
def play(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        abort(404)
    dst = Path(job["dst"])
    return send_from_directory(dst.parent, dst.name, as_attachment=False, conditional=True)


@app.route("/api/encode", methods=["POST"])
def encode():
    data = request.get_json(force=True)
    source_type = data.get("source", "library")
    rel_path = data.get("path", "")
    src_path = resolve_bin_path(rel_path, source_type)
    if not src_path.exists():
        return jsonify({"error": "file not found"}), 404

    probe = ffprobe_json(src_path)

    if data.get("mode") == "fix_hvc1":
        video, _, _, _ = simplify_streams(probe)
        if not video or video.get("codec_name") != "hevc":
            return jsonify({"error": "Il file sorgente non è HEVC: il tag hvc1 non è applicabile."}), 400

    # Se la cartella di output e' stata rimossa da fuori (es. pulizia manuale
    # sul mount host), ricrearla al volo invece di far fallire l'encode.
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    job_id = uuid.uuid4().hex
    suffix = container_ext(data, src_path)
    mode = data.get("mode")
    if mode == "fix_hvc1":
        codec_tag = "hvc1fix"
    elif mode == "fix_bitrate":
        codec_tag = "bitratefix"
    elif data.get("audio_only"):
        codec_tag = "audio"
    else:
        codec_tag = data.get("codec", "hevc_qsv").replace("_qsv", "")
    # Output accanto al sorgente (suffisso nel nome, mai sovrascrive); per i file
    # caricati dal browser (cartella uploads) resta la cartella di output dedicata.
    out_dir = src_path.parent if source_type == "library" else OUTPUT_DIR
    out_name = unique_output_name(f"{src_path.stem}-{codec_tag}", suffix, out_dir)
    dst_path = out_dir / out_name

    jobs[job_id] = {
        "status": "running", "progress": 0, "log": [],
        "src": str(src_path), "out_name": out_name, "dst": str(dst_path),
        "output_bitrate": None, "output_size": None,
    }

    t = threading.Thread(target=run_encode, args=(job_id, src_path, dst_path, data, probe), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/api/jobs")
def list_jobs():
    """Lavori in corso (per riagganciare la pagina dopo una chiusura o da un altro PC)."""
    return jsonify([{"job_id": jid, "src": Path(j["src"]).name, "out_name": j["out_name"],
                     "progress": j["progress"]}
                    for jid, j in jobs.items() if j["status"] == "running"])


@app.route("/api/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "log": job["log"][-50:],
        "output_bitrate": job.get("output_bitrate"),
        "output_size": job.get("output_size"),
        "partial": bool(job.get("partial")),
    })


@app.route("/api/stop/<job_id>", methods=["POST"])
def stop_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    proc = job.get("proc")
    if not proc or job["status"] != "running":
        return jsonify({"error": "il job non e' in esecuzione"}), 400

    job["cancel_requested"] = True
    try:
        proc.terminate()
    except Exception:
        pass

    def _escalate():
        time.sleep(5)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    threading.Thread(target=_escalate, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/finish/<job_id>", methods=["POST"])
def finish_job(job_id):
    """Clean stop that keeps the partial output (see run_ffmpeg_watchdog, "finished")."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    if not job.get("proc") or job["status"] != "running":
        return jsonify({"error": "il job non e' in esecuzione"}), 400
    job["finish_requested"] = True
    return jsonify({"ok": True})


@app.route("/api/download/<job_id>")
def download(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        abort(404)
    dst = Path(job["dst"])
    return send_from_directory(dst.parent, dst.name, as_attachment=True, download_name=job["out_name"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050)
