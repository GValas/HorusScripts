import os
import subprocess
import json
from datetime import datetime
from pathlib import Path


def log(msg: str = "") -> None:
    """Affiche un message en préfixant chaque ligne d'un timestamp."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for line in str(msg).split("\n"):
        print(f"{ts} | {line}")

# ── Configuration (surchargeable par variables d'environnement en prod) ───────
# INPUT_FOLDERS : liste séparée par des virgules
#   ex: INPUT_FOLDERS="/mnt/horus/tvshows,/mnt/horus/movies,/mnt/horus/cartoons"
INPUT_FOLDERS = [  # Root folders to scan recursively, one after another
    p.strip()
    for p in os.environ.get("INPUT_FOLDERS", "/mnt/horus/tvshows").split(",")
    if p.strip()
]
CQ = int(os.environ.get("CQ", "26"))  # Quality (lower = better, 24–28 recommended)
PRESET = os.environ.get("PRESET", "p4")  # p1 (fastest) → p7 (best quality)
EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov"}
SKIP_SUFFIX = "_x265"  # Files already converted (skips them)
# DRY_RUN : True = simulate only, no conversion / no file written
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes", "on")
# ─────────────────────────────────────────────────────────────────────────────


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
        audio_count = 0
        sub_count = 0

        for stream in streams:
            t = stream.get("codec_type", "")
            if t == "video" and codec is None:
                codec = stream.get("codec_name", "").lower()
            elif t == "audio":
                audio_count += 1
            elif t == "subtitle":
                sub_count += 1

        return {
            "codec": codec,
            "audio_tracks": audio_count,
            "subtitle_tracks": sub_count,
        }

    except Exception as e:
        log(f"  [!] ffprobe error on {filepath.name}: {e}")
        return {"codec": None, "audio_tracks": 0, "subtitle_tracks": 0}


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
    log(f"  [→] Converting: {input_path.name}")

    if DRY_RUN:
        log(f"  [DRY-RUN] Would convert to: {output_path.name}")
        return True

    cmd = [
        "ffmpeg",
        "-i",
        str(input_path),
        "-map",
        "0",  # keep ALL streams
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
        "-c:a",
        "copy",  # copy all audio tracks as-is
        "-c:s",
        "copy",  # copy all subtitle tracks as-is
        "-tag:v",
        "hvc1",
        "-loglevel",
        "warning",
        "-stats",
        str(output_path),
    ]

    try:
        subprocess.run(cmd, check=True)

        # Verify output streams match
        out_info = get_video_info(output_path)
        audio_ok = out_info["audio_tracks"] == info["audio_tracks"]
        sub_ok = out_info["subtitle_tracks"] == info["subtitle_tracks"]

        old_mb = input_path.stat().st_size / 1024 / 1024
        new_mb = output_path.stat().st_size / 1024 / 1024
        saving = 100 - (new_mb / old_mb * 100)

        log(
            f"\033[92m  [✓] Done: {old_mb:.1f}MB → {new_mb:.1f}MB "
            f"(saved {saving:.1f}%)\033[0m"
        )

        if not audio_ok:
            log(
                f"  [!] WARNING: audio tracks mismatch! "
                f"expected {info['audio_tracks']}, got {out_info['audio_tracks']}"
            )
        if not sub_ok:
            log(
                f"  [!] WARNING: subtitle tracks mismatch! "
                f"expected {info['subtitle_tracks']}, got {out_info['subtitle_tracks']}"
            )

        if not (audio_ok and sub_ok):
            log(
                f"  [✗] Stream count mismatch — kept for inspection: {output_path.name}"
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

    except subprocess.CalledProcessError:
        log(f"  [✗] FFmpeg error — deleting incomplete output")
        if output_path.exists():
            output_path.unlink()
        return False


def scan_and_convert(root: str) -> tuple[int, int]:
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
        info = get_video_info(f)
        codec = info["codec"]
        if codec is None:
            log(f"  [?] Could not detect codec: {f.name}")
        elif codec in ("hevc", "h265"):
            log(f"  [=] Already x265, skipping: {f.relative_to(root_path)}")
        else:
            log(f"  [!] Not x265 ({codec}): {f.relative_to(root_path)}")
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

    total_success, total_failed = 0, 0
    for folder in INPUT_FOLDERS:
        log("\n" + "═" * 60)
        log(f"[*] Folder: {folder}")
        log("═" * 60 + "\n")
        s, f = scan_and_convert(folder)
        total_success += s
        total_failed += f

    if len(INPUT_FOLDERS) > 1:
        log("\n" + "═" * 60)
        log(f"[*] GLOBAL TOTAL across {len(INPUT_FOLDERS)} folder(s)")
        log(f"[✓] Converted successfully : {total_success}")
        log(f"[✗] Failed                 : {total_failed}")
        log("═" * 60)
