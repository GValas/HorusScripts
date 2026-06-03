#!/usr/bin/env python3
"""
any_to_mkv_h265.py — Met TOUTES les vidéos en MKV
- Recherche récursive et insensible à la casse
- Détecte le codec via ffprobe et choisit l'action :
    • codec legacy            → ré-encodage H.265 / HEVC (avec perte, lent)
    • codec récent, non-MKV   → remux -c copy vers MKV (sans perte, quasi instantané)
    • déjà en .mkv            → ignoré (rien à faire)
- Écrit le fichier converti au même endroit que l'original
- N'écrase jamais une cible .mkv déjà existante
- Préserve la date d'origine : le tag creation_time est recopié dans le MKV
  (à défaut, la date de modification du fichier), et la mtime du fichier de
  sortie est réalignée sur celle de l'original
- Supprime l'original si la conversion réussit
- Bilan final par extension et par codec
Prérequis : ffmpeg + ffprobe installés et accessibles dans le PATH
"""

import os
import subprocess
import sys
import logging
from collections import defaultdict
from pathlib import Path
from datetime import datetime

# ── Configuration ──────────────────────────────────────────
SOURCE_DIR    = Path(r"\\horus\photos")  # ← à modifier

DRY_RUN       = False    # True = simulation sans conversion, False = conversion réelle

# Conteneurs candidats : tout sauf .mkv (la cible). Les .mp4/.webp récents
# seront simplement remuxés en .mkv (sans ré-encodage) ; les codecs legacy
# (quel que soit le conteneur) seront ré-encodés en H.265.
EXTENSIONS    = {".mov", ".mp4", ".webm", ".wmv", ".mpeg", ".mpg", ".rm", ".rmvb",
                 ".3gp", ".avi", ".divx", ".asf", ".vob",
                 ".m2ts", ".mts", ".flv", ".f4v", ".m4v"}

# Codecs considérés comme legacy → seront convertis
LEGACY_CODECS = {
    "mpeg1video", "mpeg2video",
    "msmpeg4v3", "msmpeg4v2", "msmpeg4v1",
    "wmv1", "wmv2", "wmv3",
    "rv10", "rv20", "rv30", "rv40",
    "flashsv", "flashsv2", "flv1",
    "h263", "h263p",
    "dvvideo",
    "indeo3", "indeo4", "indeo5",
    "cinepak", "msvideo1",
    "theora", "vp6", "vp6f", "vp6a",
    "rawvideo", "mjpeg", "mpeg4",
}

OUTPUT_SUFFIX = ".mkv"
# Année plancher : un tag creation_time antérieur (epoch 1904/1970) est ignoré
# lors du calcul de la date d'origine à préserver.
MIN_PLAUSIBLE_YEAR = 1990
CRF           = 28       # Qualité H.265 : 0 (max) → 51 (min), 28 = bon équilibre
PRESET        = "medium" # ultrafast / fast / medium / slow / veryslow
AUDIO_CODEC   = "aac"
AUDIO_BITRATE = "192k"
# ───────────────────────────────────────────────────────────


def setup_logging(log_dir: Path, dry_run: bool) -> logging.Logger:
    log_path = log_dir / ("dry_run.log" if dry_run else "conversion.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(__name__)


def check_tools() -> bool:
    ok = True
    for tool in ("ffmpeg", "ffprobe"):
        try:
            subprocess.run([tool, "-version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            print(f"Erreur : {tool} est introuvable.")
            ok = False
    return ok


def get_video_codec(path: Path) -> str | None:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() or None


def get_creation_time(path: Path) -> str | None:
    """Lit le tag creation_time exposé par ffprobe (tags de format) ; None si absent."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format_tags=creation_time",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() or None


def _parse_iso(value: str) -> datetime | None:
    """Parse une date ffprobe (ISO 8601, éventuellement avec 'T', 'Z' ou fraction)."""
    core = value.strip().replace("T", " ").split(".")[0].strip()
    if core.endswith("Z"):
        core = core[:-1].strip()
    if len(core) >= 6 and core[-6] in "+-" and core[-3] == ":":   # suffixe ±hh:mm
        core = core[:-6].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(core, fmt)
        except ValueError:
            continue
    return None


def source_creation_iso(path: Path) -> str:
    """Date d'origine à incruster dans le MKV (ISO 8601).

    Prend la PLUS ANCIENNE entre le tag creation_time du fichier et sa date de
    modification (mtime) — la plus proche de la prise de vue réelle. Un tag
    creation_time aberrant (epoch, année < 1990) est écarté.
    """
    candidates = [datetime.fromtimestamp(path.stat().st_mtime)]
    embedded = get_creation_time(path)
    if embedded:
        dt = _parse_iso(embedded)
        if dt and dt.year >= MIN_PLAUSIBLE_YEAR:
            candidates.append(dt)
    return min(candidates).strftime("%Y-%m-%dT%H:%M:%S")


def encode_h265(input_path: Path, output_path: Path,
                creation_iso: str, logger: logging.Logger) -> bool:
    """Ré-encode la vidéo en H.265 (avec perte) — pour les codecs legacy."""
    cmd = [
        "ffmpeg",
        "-i",      str(input_path),
        "-map_metadata", "0",        # recopie les métadonnées globales (dont creation_time)
        "-c:v",    "libx265",
        "-crf",    str(CRF),
        "-preset", PRESET,
        "-tag:v",  "hvc1",       # compatibilité Apple
        "-c:a",    AUDIO_CODEC,
        "-b:a",    AUDIO_BITRATE,
        "-metadata", f"creation_time={creation_iso}",  # garantit la date d'origine
        "-y",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("Erreur ffmpeg :\n%s", result.stderr[-800:])
        return False
    return True


def remux_to_mkv(input_path: Path, output_path: Path,
                 creation_iso: str, logger: logging.Logger) -> bool:
    """Change seulement le conteneur vers MKV (-c copy) — sans ré-encodage, sans perte.

    Tous les flux (vidéo/audio/sous-titres) sont recopiés tels quels, ainsi que
    les métadonnées globales (creation_time réinjecté pour garantir la date
    d'origine). Si un codec de sous-titres est incompatible avec le MKV, ffmpeg
    échoue : on conserve alors l'original (voir gestion de l'échec dans main()).
    """
    cmd = [
        "ffmpeg",
        "-i",   str(input_path),
        "-map", "0",
        "-c",   "copy",
        "-map_metadata", "0",        # recopie les métadonnées globales (dont creation_time)
        "-metadata", f"creation_time={creation_iso}",  # garantit la date d'origine
        "-y",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("Erreur ffmpeg (remux) :\n%s", result.stderr[-800:])
        return False
    return True


def human_size(path: Path) -> str:
    return human_size_bytes(path.stat().st_size)


def human_size_bytes(size: int) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} To"


def print_bilan(logger, title: str, stats: dict, label_width: int = 12):
    """Affiche un tableau de bilan trié par nombre de fichiers décroissant."""
    logger.info("  %s :", title)
    logger.info("    %-*s  %6s  %12s  %6s", label_width, "valeur", "fichiers", "taille", "conv.")
    logger.info("    %s", "-" * (label_width + 32))
    for key, data in sorted(stats.items(), key=lambda x: -x[1]["count"]):
        converted = f"{data['converted']}" if data['converted'] > 0 else "-"
        logger.info(
            "    %-*s  %6d  %12s  %6s",
            label_width,
            key,
            data["count"],
            human_size_bytes(data["size"]),
            converted,
        )


def main():
    if not SOURCE_DIR.is_dir():
        print(f"Erreur : dossier introuvable — {SOURCE_DIR}")
        sys.exit(1)

    logger = setup_logging(SOURCE_DIR, DRY_RUN)

    mode_label = "DRY RUN (simulation)" if DRY_RUN else "CONVERSION RÉELLE"
    logger.info("=" * 56)
    logger.info("Mode       : %s", mode_label)
    logger.info("Source     : %s", SOURCE_DIR.resolve())
    logger.info("Extensions : %s", ", ".join(sorted(EXTENSIONS)))
    logger.info("Sortie     : même dossier que l'original")
    if not DRY_RUN:
        logger.info("Originaux  : supprimés après conversion réussie")
    logger.info("=" * 56)

    if not check_tools():
        sys.exit(1)

    # Recherche récursive et insensible à la casse
    candidates = sorted(
        f for f in SOURCE_DIR.rglob("*")
        if f.is_file() and f.suffix.lower() in EXTENSIONS
    )

    if not candidates:
        logger.info("Aucun fichier trouvé pour les extensions ciblées.")
        sys.exit(0)

    logger.info("%d fichier(s) trouvé(s), analyse des codecs...", len(candidates))
    logger.info("")

    # Structures pour le bilan
    # { ".avi": {"count": N, "size": N, "converted": N}, ... }
    ext_stats   = defaultdict(lambda: {"count": 0, "size": 0, "converted": 0})
    codec_stats = defaultdict(lambda: {"count": 0, "size": 0, "converted": 0})

    total_size = 0
    skipped = encoded = remuxed = errors = 0
    start_total = datetime.now()

    for i, input_file in enumerate(candidates, 1):
        relative    = input_file.relative_to(SOURCE_DIR)
        output_file = input_file.with_suffix(OUTPUT_SUFFIX)
        ext         = input_file.suffix.lower()
        src_stat    = input_file.stat()
        size        = src_stat.st_size

        codec = get_video_codec(input_file)
        codec_label = codec or "inconnu"

        logger.info("[%d/%d] %s (%s)", i, len(candidates), relative, human_size(input_file))
        logger.info("    → codec détecté : %s", codec_label)

        # Comptabilise dans tous les cas
        ext_stats[ext]["count"] += 1
        ext_stats[ext]["size"]  += size
        codec_stats[codec_label]["count"] += 1
        codec_stats[codec_label]["size"]  += size

        # Choix de l'action : codec legacy (ou indéterminé) → ré-encodage H.265 ;
        # codec récent → simple remux vers MKV (sans perte). Aucun fichier n'est
        # « ignoré » : tout doit finir en MKV (les .mkv ne sont même pas scannés).
        legacy = (codec is None) or (codec in LEGACY_CODECS)
        action = "encode" if legacy else "remux"

        # Ne jamais écraser une cible .mkv déjà existante (cf. clean-names).
        if output_file.exists():
            logger.warning("    → ignoré : la cible existe déjà — %s", output_file.name)
            skipped += 1
            continue

        total_size += size

        # Date d'origine à préserver dans le MKV (tag creation_time, sinon mtime).
        creation_iso = source_creation_iso(input_file)

        if DRY_RUN:
            if action == "encode":
                logger.info("    → serait RÉ-ENCODÉ (H.265) en : %s", output_file.name)
            else:
                logger.info("    → serait REMUXÉ (-c copy) en : %s", output_file.name)
            logger.info("    → date préservée (creation_time) : %s", creation_iso)
            logger.info("    → original serait supprimé après conversion")
            ext_stats[ext]["converted"]           += 1
            codec_stats[codec_label]["converted"] += 1
            if action == "encode":
                encoded += 1
            else:
                remuxed += 1
            continue

        t0 = datetime.now()
        if action == "encode":
            success = encode_h265(input_file, output_file, creation_iso, logger)
        else:
            success = remux_to_mkv(input_file, output_file, creation_iso, logger)
        elapsed = (datetime.now() - t0).seconds

        if success:
            # Réaligne la date de modification du MKV sur celle de l'original
            # (le tag creation_time, lui, est déjà incrusté par ffmpeg).
            try:
                os.utime(output_file, (src_stat.st_atime, src_stat.st_mtime))
            except OSError as e:
                logger.warning("    ⚠️  mtime non restaurée : %s", e)

            # Vérifie que la date est bien présente dans le MKV produit.
            written = get_creation_time(output_file)
            if not written:
                logger.warning("    ⚠️  creation_time absent du MKV produit")

            verb = "RÉ-ENCODÉ" if action == "encode" else "REMUXÉ"
            logger.info(
                "    ✓ %s  →  %s (%s)  [%ds]  date=%s",
                verb, output_file.name, human_size(output_file), elapsed,
                written or creation_iso,
            )
            input_file.unlink()
            logger.info("    🗑  Original supprimé : %s", input_file.name)
            ext_stats[ext]["converted"]           += 1
            codec_stats[codec_label]["converted"] += 1
            if action == "encode":
                encoded += 1
            else:
                remuxed += 1
        else:
            logger.error("    ✗ ÉCHEC  →  %s (original conservé)", input_file.name)
            if output_file.exists():
                output_file.unlink()
                logger.warning("    Fichier partiel supprimé : %s", output_file.name)
            errors += 1

    elapsed_total = (datetime.now() - start_total).seconds

    # ── Bilan final ─────────────────────────────────────────
    logger.info("")
    logger.info("=" * 56)
    logger.info("BILAN GÉNÉRAL")
    logger.info("  Fichiers analysés     : %d", len(candidates))
    logger.info("  À ré-encoder (H.265)  : %d", encoded)
    logger.info("  À remuxer (-c copy)   : %d", remuxed)
    logger.info("  Ignorés (cible exist.): %d", skipped)
    logger.info("  Taille à convertir    : %s", human_size_bytes(total_size))
    logger.info("")

    print_bilan(logger, "Par extension", ext_stats, label_width=8)
    logger.info("")
    print_bilan(logger, "Par codec",     codec_stats, label_width=14)
    logger.info("")

    if DRY_RUN:
        logger.info(
            "  Dry run terminé  —  %d ré-encodage(s) + %d remux = %d fichier(s)",
            encoded, remuxed, encoded + remuxed,
        )
        logger.info("  Mettez DRY_RUN = False pour lancer la conversion.")
    else:
        logger.info(
            "  Terminé en %ds  —  %d ré-encodé(s), %d remuxé(s), %d erreur(s)",
            elapsed_total, encoded, remuxed, errors,
        )
        logger.info("  Log : %s", SOURCE_DIR / "conversion.log")
    logger.info("=" * 56)


if __name__ == "__main__":
    main()