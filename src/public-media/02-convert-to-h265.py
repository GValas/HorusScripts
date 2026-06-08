import subprocess
import json
import shutil
import importlib.util
from datetime import datetime
from pathlib import Path

# Codes couleur ANSI : succès en vert, erreurs en rouge, avertissements en jaune.
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def log(msg: str = "") -> None:
    """Affiche un message en préfixant chaque ligne d'un timestamp.

    Colore automatiquement chaque ligne : vert pour les succès ([✓]),
    rouge pour les erreurs ([✗]), jaune pour les avertissements ([!]).
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for line in str(msg).split("\n"):
        if "[✗]" in line:
            line = f"{RED}{line}{RESET}"
        elif "[✓]" in line:
            line = f"{GREEN}{line}{RESET}"
        elif "[!]" in line:
            line = f"{YELLOW}{line}{RESET}"
        print(f"{ts} | {line}")


# ── Configuration : tout est dans 00-config.py (COMMUN + CONVERT_*) ───────────
_spec = importlib.util.spec_from_file_location(
    "pipeline_config", Path(__file__).with_name("00-config.py")
)
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)

INPUT_FOLDERS = config.INPUT_FOLDERS  # dossiers scannés récursivement
CQ = config.CONVERT_CQ  # qualité NVENC (+ bas = mieux)
PRESET = config.CONVERT_PRESET  # préréglage NVENC (p1 → p7)
EXTENSIONS = config.CONVERT_EXTENSIONS  # conteneurs vidéo scannés
SKIP_SUFFIX = config.CONVERT_SKIP_SUFFIX  # fichiers déjà convertis (ignorés)
DRY_RUN = config.DRY_RUN  # True = simulation seule
# Cache de scan (codec/dimensions par fichier) ; None = désactivé.
SCAN_CACHE_PATH = (
    Path(__file__).with_name(config.CONVERT_SCAN_CACHE)
    if getattr(config, "CONVERT_SCAN_CACHE", None)
    else None
)

# Résolution max de sortie -> boîte (largeur, hauteur) à ne pas dépasser.
# CONVERT_MAX_RESOLUTION (00-config.py) vaut une de ces clés, ou None (pas de
# downscale). MAX_WIDTH/MAX_HEIGHT = None -> needs_downscale() renvoie toujours False.
RESOLUTION_LIMITS = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2160p": (3840, 2160),
    "4k": (3840, 2160),
}
if config.CONVERT_MAX_RESOLUTION is None:
    MAX_WIDTH = MAX_HEIGHT = None
else:
    _key = str(config.CONVERT_MAX_RESOLUTION).lower()
    if _key not in RESOLUTION_LIMITS:
        raise SystemExit(
            f"CONVERT_MAX_RESOLUTION invalide : {config.CONVERT_MAX_RESOLUTION!r} "
            f"(attendu : None ou {', '.join(RESOLUTION_LIMITS)})"
        )
    MAX_WIDTH, MAX_HEIGHT = RESOLUTION_LIMITS[_key]
# ─────────────────────────────────────────────────────────────────────────────


def needs_downscale(width, height) -> bool:
    """True si la vidéo dépasse MAX_WIDTH/MAX_HEIGHT (donc à réduire)."""
    return bool(
        (MAX_WIDTH and width and width > MAX_WIDTH)
        or (MAX_HEIGHT and height and height > MAX_HEIGHT)
    )


def get_video_info(filepath: Path) -> dict:
    """Use ffprobe to detect all streams in a file."""
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        str(filepath),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        streams = data.get("streams", [])

        codec = None
        width = height = None
        pix_fmt = color_trc = color_primaries = color_space = None
        audio_count = 0
        sub_codecs = []  # codec_name of each subtitle stream, in order
        all_streams = []  # {index, type, codec} for every stream, in order

        for stream in streams:
            t = stream.get("codec_type", "")
            c = stream.get("codec_name", "").lower()
            all_streams.append({"index": stream.get("index"), "type": t, "codec": c})
            if t == "video" and codec is None:
                # Main video stream: also grab geometry + HDR signalling.
                codec = c
                width = stream.get("width")
                height = stream.get("height")
                pix_fmt = stream.get("pix_fmt")
                color_trc = stream.get("color_transfer")
                color_primaries = stream.get("color_primaries")
                color_space = stream.get("color_space")
            elif t == "audio":
                audio_count += 1
            elif t == "subtitle":
                sub_codecs.append(c)

        return {
            "codec": codec,
            "width": width,
            "height": height,
            "pix_fmt": pix_fmt,
            "color_trc": color_trc,
            "color_primaries": color_primaries,
            "color_space": color_space,
            "audio_tracks": audio_count,
            "subtitle_tracks": len(sub_codecs),
            "subtitle_codecs": sub_codecs,
            "streams": all_streams,
        }

    except Exception as e:
        log(f"  [!] ffprobe error on {filepath.name}: {e}")
        return {
            "codec": None,
            "width": None,
            "height": None,
            "pix_fmt": None,
            "color_trc": None,
            "color_primaries": None,
            "color_space": None,
            "audio_tracks": 0,
            "subtitle_tracks": 0,
            "subtitle_codecs": [],
            "streams": [],
        }


def get_duration(filepath: Path) -> float | None:
    """Durée du fichier en secondes (ffprobe format.duration) ; None si indéterminée."""
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_entries",
        "format=duration",
        str(filepath),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        dur = (data.get("format") or {}).get("duration")
        return float(dur) if dur is not None else None
    except Exception:
        return None


def load_scan_cache(path: Path | None) -> dict:
    """Charge le cache de scan JSON (clé = chemin) ; {} si absent/désactivé."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_scan_cache(path: Path | None, cache: dict) -> None:
    """Écrit le cache de scan (best-effort : une erreur d'écriture est ignorée)."""
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError:
        pass


def probe_for_scan(f: Path, cache: dict) -> tuple:
    """(codec, width, height) pour la décision de scan, avec cache (mtime+size).

    Sur hit de cache valide, on évite l'appel ffprobe ; sinon on sonde et on
    mémorise. La conversion elle-même re-sonde toujours le fichier en entier.
    """
    try:
        st = f.stat()
    except OSError:
        info = get_video_info(f)
        return info["codec"], info["width"], info["height"]

    key = str(f)
    ent = cache.get(key)
    if ent and ent.get("mtime") == int(st.st_mtime) and ent.get("size") == st.st_size:
        return ent.get("codec"), ent.get("width"), ent.get("height")

    info = get_video_info(f)
    cache[key] = {
        "mtime": int(st.st_mtime),
        "size": st.st_size,
        "codec": info["codec"],
        "width": info["width"],
        "height": info["height"],
    }
    return info["codec"], info["width"], info["height"]


def enough_space(target_dir: Path, needed: int) -> bool:
    """True s'il reste au moins `needed` octets libres sur le volume de target_dir.

    Indéterminé (erreur OS) -> True : on ne bloque pas une conversion par excès
    de prudence si l'espace libre n'a pas pu être lu.
    """
    try:
        return shutil.disk_usage(target_dir).free >= needed
    except OSError:
        return True


def convert_to_x265(input_path: Path) -> bool:
    """Convert a file to x265 using NVENC, preserving all streams."""
    output_path = input_path.with_name(input_path.stem + "_x265.mkv")

    if output_path.exists():
        log(f"  [~] Output already exists, skipping: {output_path.name}")
        return True

    # Probe source streams
    info = get_video_info(input_path)
    log(
        f"  [i] Streams: video ({info['codec']}) | "
        f"{info['audio_tracks']} audio track(s) | "
        f"{info['subtitle_tracks']} subtitle track(s)"
    )

    # Decide downscale + colour handling from the main video stream.
    do_scale = needs_downscale(info["width"], info["height"])
    is_hdr = (
        info["color_trc"] in ("smpte2084", "arib-std-b67")
        or info["color_primaries"] == "bt2020"
    )
    is_10bit = "10" in (info["pix_fmt"] or "")
    if do_scale:
        log(
            f"  [↓] Downscale {info['width']}x{info['height']} → fit {MAX_WIDTH}x{MAX_HEIGHT}"
            + (" (HDR 10-bit kept, Dolby Vision lost)" if is_hdr else "")
        )
    log(f"  [→] Converting: {input_path.name}")

    if DRY_RUN:
        log(f"  [DRY-RUN] Would convert to: {output_path.name}")
        return True

    # Disk-space guard: the new output coexists with the original until it is
    # replaced, so require at least the source size (HEVC is usually smaller)
    # plus a margin. Skip the file cleanly rather than fill the volume mid-batch.
    needed = input_path.stat().st_size + 200 * 1024 * 1024
    if not enough_space(output_path.parent, needed):
        log(f"  [✗] Not enough free disk space to convert {input_path.name} — skipped")
        return False

    # Build an explicit stream map instead of "-map 0". Real-world files carry
    # streams that break the conversion: subtitle streams ffmpeg can't identify
    # (codec_name "unknown" → "Subtitle codec 0 is not supported" on copy), and
    # embedded cover-art images (mjpeg "video" streams) that would be fed to
    # hevc_nvenc. So we keep: the main video, every audio track, and only the
    # decodable subtitle streams — dropping unknown subs, extra video and
    # attachments. Text subtitles ffmpeg can decode but Matroska can't remux by
    # copy (WebVTT/mov_text) are transcoded to SubRip.
    UNDECODABLE_SUB = {"unknown", "none", ""}
    SUB_TRANSCODE = {"webvtt", "mov_text"}  # → srt instead of copy

    map_args = ["-map", "0:v:0", "-map", "0:a?"]  # main video + all audio
    sub_args = []
    kept_subs = 0
    dropped = []
    for s in info["streams"]:
        if s["type"] != "subtitle":
            continue
        if s["codec"] in UNDECODABLE_SUB:
            dropped.append(f"#{s['index']} ({s['codec'] or '?'})")
            continue
        map_args += ["-map", f"0:{s['index']}"]
        enc = "srt" if s["codec"] in SUB_TRANSCODE else "copy"
        sub_args += [f"-c:s:{kept_subs}", enc]
        kept_subs += 1

    if dropped:
        log(
            f"  [!] Dropping {len(dropped)} unsupported subtitle stream(s): {', '.join(dropped)}"
        )
    expected_subs = kept_subs

    # Downscale filter (aspect kept, never upscales) — only when oversized.
    vf_args = []
    if do_scale:
        vf_args = [
            "-vf",
            f"scale={MAX_WIDTH}:{MAX_HEIGHT}"
            ":force_original_aspect_ratio=decrease:force_divisible_by=2",
        ]

    # HDR / 10-bit: encode 10-bit (main10) and carry the bt2020/PQ signalling so
    # the 1080p copy stays HDR. Dolby Vision dynamic metadata is not preserved.
    depth_args = []
    color_args = []
    if is_hdr or is_10bit:
        depth_args = ["-pix_fmt", "p010le", "-profile:v", "main10"]
    if is_hdr:
        color_args = [
            "-color_primaries",
            info["color_primaries"] or "bt2020",
            "-color_trc",
            info["color_trc"] or "smpte2084",
            "-colorspace",
            info["color_space"] or "bt2020nc",
        ]

    cmd = [
        "ffmpeg",
        "-i",
        str(input_path),
        *map_args,
        *vf_args,
        "-c:v",
        "hevc_nvenc",
        "-rc",
        "vbr",
        "-cq",
        str(CQ),
        "-qmin",
        str(CQ),
        "-qmax",
        str(CQ),
        "-preset",
        PRESET,
        "-b:v",
        "0",
        *depth_args,
        *color_args,
        "-c:a",
        "copy",  # copy all audio tracks as-is
        *sub_args,  # per-stream: copy, or transcode WebVTT/mov_text to srt
        "-tag:v",
        "hvc1",
        "-loglevel",
        "warning",
        str(output_path),
    ]

    try:
        subprocess.run(cmd, check=True, stderr=subprocess.PIPE, text=True)

        # Verify output streams match what we intended to keep
        out_info = get_video_info(output_path)
        audio_ok = out_info["audio_tracks"] == info["audio_tracks"]
        sub_ok = out_info["subtitle_tracks"] == expected_subs

        old_mb = input_path.stat().st_size / 1024 / 1024
        new_mb = output_path.stat().st_size / 1024 / 1024
        saving = 100 - (new_mb / old_mb * 100)

        log(f"  [✓] Done: {old_mb:.1f}MB → {new_mb:.1f}MB " f"(saved {saving:.1f}%)")

        if not audio_ok:
            log(
                f"  [!] WARNING: audio tracks mismatch! "
                f"expected {info['audio_tracks']}, got {out_info['audio_tracks']}"
            )
        if not sub_ok:
            log(
                f"  [!] WARNING: subtitle tracks mismatch! "
                f"expected {expected_subs}, got {out_info['subtitle_tracks']}"
            )

        if not (audio_ok and sub_ok):
            log(
                f"  [✗] Stream count mismatch — kept for inspection: {output_path.name}"
            )
            return False

        # Verify output DURATION matches the source: a correct stream count does
        # not guarantee a complete encode (a truncated output keeps all streams
        # but loses time). Refuse to delete the original when durations diverge.
        in_dur = get_duration(input_path)
        out_dur = get_duration(output_path)
        if in_dur and out_dur and abs(in_dur - out_dur) > max(1.0, 0.01 * in_dur):
            log(
                f"  [✗] Duration mismatch ({in_dur:.0f}s → {out_dur:.0f}s) — "
                f"likely truncated, original kept: {output_path.name}"
            )
            return False

        # Success: replace the original with the converted file, keeping its name
        final_path = input_path.with_name(input_path.stem + ".mkv")
        if final_path.exists() and final_path != input_path:
            log(
                f"  [!] WARNING: cannot rename, target already exists: {final_path.name} "
                f"— kept both originals and {output_path.name}"
            )
            return True

        input_path.unlink()
        output_path.rename(final_path)
        log(f"  [✓] Replaced original → {final_path.name}")

        return True

    except subprocess.CalledProcessError as e:
        log(f"  [✗] FFmpeg error — deleting incomplete output")
        stderr = (e.stderr or "").strip()
        if stderr:
            # Show the last lines of ffmpeg's output — that's where the cause is.
            for line in stderr.splitlines()[-15:]:
                log(f"      ffmpeg | {line}")
        if output_path.exists():
            output_path.unlink()
        return False


def scan_and_convert(root: str, cache: dict) -> tuple[int, int]:
    root_path = Path(root)

    if not root_path.exists():
        log(f"[!] Folder not found: {root}")
        return 0, 0

    log(f"[*] Scanning {root_path} recursively...\n")
    candidates = [
        f
        for f in root_path.rglob("*")
        if f.suffix.lower() in EXTENSIONS and SKIP_SUFFIX not in f.stem
    ]

    log(f"[*] Found {len(candidates)} video files to check\n")

    to_convert = []
    for f in candidates:
        codec, width, height = probe_for_scan(f, cache)
        too_big = needs_downscale(width, height)
        if codec is None:
            log(f"  [?] Could not detect codec: {f.name}")
        elif codec in ("hevc", "h265") and not too_big:
            log(f"  [=] Already x265, skipping: {f.relative_to(root_path)}")
        else:
            reason = f"downscale {width}x{height}" if too_big else f"not x265 ({codec})"
            log(f"  [!] To convert ({reason}): {f.relative_to(root_path)}")
            to_convert.append(f)

    log(f"\n[*] {len(to_convert)} file(s) need conversion\n")
    if not to_convert:
        log("[✓] Nothing to do!")
        return 0, 0

    total_size_mb = sum(f.stat().st_size for f in to_convert) / 1024 / 1024
    log(f"[*] Total size to convert: {total_size_mb:.1f} MB\n")
    log("─" * 60)

    success, failed = 0, 0
    for i, filepath in enumerate(to_convert, 1):
        log(f"\n[{i}/{len(to_convert)}] {filepath.relative_to(root_path)}")
        if convert_to_x265(filepath):
            success += 1
        else:
            failed += 1

    log("\n" + "─" * 60)
    log(f"[✓] Converted successfully : {success}")
    log(f"[✗] Failed                 : {failed}")
    log(f"[*] Converted files saved alongside originals with '{SKIP_SUFFIX}' suffix")
    return success, failed


if __name__ == "__main__":
    if DRY_RUN:
        log("[*] DRY-RUN mode: simulation only, no file will be converted\n")

    scan_cache = load_scan_cache(SCAN_CACHE_PATH)

    total_success, total_failed = 0, 0
    for folder in INPUT_FOLDERS:
        log("\n" + "═" * 60)
        log(f"[*] Folder: {folder}")
        log("═" * 60 + "\n")
        s, f = scan_and_convert(folder, scan_cache)
        total_success += s
        total_failed += f

    save_scan_cache(SCAN_CACHE_PATH, scan_cache)

    if len(INPUT_FOLDERS) > 1:
        log("\n" + "═" * 60)
        log(f"[*] GLOBAL TOTAL across {len(INPUT_FOLDERS)} folder(s)")
        log(f"[✓] Converted successfully : {total_success}")
        log(f"[✗] Failed                 : {total_failed}")
        log("═" * 60)
