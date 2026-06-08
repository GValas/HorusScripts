#!/usr/bin/env python3
"""
any_to_mkv_h265.py — Met TOUTES les vidéos en MKV
- Recherche récursive et insensible à la casse
- Objectif : TOUTE vidéo finit en H.265 + MKV, quel que soit le format d'origine.
- Détecte le codec via ffprobe et choisit l'action :
    • déjà H.265 + MKV           → ignoré (déjà au bon format)
    • déjà H.265, autre conteneur→ remux -c copy vers MKV (sans perte, rapide)
    • tout autre codec, en .mkv  → ré-encodage H.265 SUR PLACE (fichier temporaire)
    • tout autre codec, non-.mkv → ré-encodage H.265 vers MKV
- Écrit le fichier converti au même endroit que l'original
- N'écrase jamais une cible .mkv déjà existante
- Conserve les tags existants : -map_metadata 0 recopie les métadonnées
  globales (dont un creation_time déjà présent et plausible) dans le MKV, et la
  mtime du fichier de sortie est réalignée sur celle de l'original. AUCUNE
  inférence de date ici (nom de fichier, mtime, voisins) : c'est le rôle de
  02-enrich-movies-photos-with-date.py, exécuté ensuite sur le MKV produit.
- Supprime l'original si la conversion réussit
- Normalise aussi l'extension des photos : .jpeg -> .jpg (RENAME_JPEG), simple
  renommage sans écrasement — pour que toute la bibliothèque soit en .jpg/.mkv.
- Bilan final par extension et par codec
Prérequis : ffmpeg + ffprobe installés et accessibles dans le PATH
"""

import os
import json
import shutil
import subprocess
import sys
import logging
import importlib.util
from collections import defaultdict
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

# ── Configuration : tout est dans 00-config.py (CONVERT_*) ──────────────────
_spec = importlib.util.spec_from_file_location(
    "pipeline_config", Path(__file__).with_name("00-config.py")
)
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)

SOURCE_DIR = Path(config.PHOTOS_SRC)
DRY_RUN = config.DRY_RUN
RENAME_JPEG = config.CONVERT_RENAME_JPEG
HEIC_TO_JPG = config.CONVERT_HEIC_TO_JPG
HEIC_EXTENSIONS = config.CONVERT_HEIC_EXTENSIONS
JPEG_QUALITY = config.CONVERT_JPEG_QUALITY
EXTENSIONS = config.CONVERT_EXTENSIONS
OUTPUT_SUFFIX = config.CONVERT_OUTPUT_SUFFIX
MIN_PLAUSIBLE_YEAR = config.MIN_PLAUSIBLE_YEAR
CQ = config.VIDEO_CQ
NVENC_PRESET = config.VIDEO_PRESET
AUDIO_CODEC = config.CONVERT_AUDIO_CODEC
AUDIO_BITRATE = config.CONVERT_AUDIO_BITRATE
# Cache de scan (codec par fichier) ; None = désactivé.
SCAN_CACHE_PATH = (
    Path(__file__).with_name(config.CONVERT_SCAN_CACHE)
    if getattr(config, "CONVERT_SCAN_CACHE", None)
    else None
)
# ───────────────────────────────────────────────────────────


def _use_paris_timezone() -> None:
    """Force l'heure de Paris dans les timestamps de log, quel que soit le
    fuseau du système/conteneur (sinon UTC -> décalage de 1-2h)."""
    try:
        paris = ZoneInfo("Europe/Paris")
        logging.Formatter.converter = staticmethod(
            lambda ts: datetime.fromtimestamp(ts, paris).timetuple()
        )
    except Exception:
        pass  # base de fuseaux indisponible -> heure système par défaut


def setup_logging(log_dir: Path, dry_run: bool) -> logging.Logger:
    _use_paris_timezone()
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


def nvenc_available() -> bool:
    """Vrai si l'encodeur GPU hevc_nvenc est présent dans ce build ffmpeg.

    Ne garantit pas qu'un GPU est exposé au runtime, mais permet d'échouer tôt
    et clairement plutôt que de tomber en plein batch (politique GPU-only).
    """
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return False
    return "hevc_nvenc" in (out.stdout or "")


def get_video_codec(path: Path) -> str | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() or None


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


def get_video_codec_cached(path: Path, cache: dict) -> str | None:
    """Codec vidéo avec cache validé par (mtime, taille) : évite un ffprobe par
    fichier déjà connu et inchangé. Sur miss/changement, sonde et mémorise."""
    try:
        st = path.stat()
    except OSError:
        return get_video_codec(path)
    key = str(path)
    ent = cache.get(key)
    if ent and ent.get("mtime") == int(st.st_mtime) and ent.get("size") == st.st_size:
        return ent.get("codec")
    codec = get_video_codec(path)
    cache[key] = {"mtime": int(st.st_mtime), "size": st.st_size, "codec": codec}
    return codec


def enough_space(target_dir: Path, needed: int) -> bool:
    """True s'il reste au moins `needed` octets libres sur le volume de target_dir.

    Indéterminé (erreur OS) -> True : on ne bloque pas une conversion par excès
    de prudence si l'espace libre n'a pas pu être lu.
    """
    try:
        return shutil.disk_usage(target_dir).free >= needed
    except OSError:
        return True


def get_duration(path: Path) -> float | None:
    """Durée du fichier en secondes (ffprobe format.duration) ; None si indéterminée."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return float(result.stdout.strip())
    except (ValueError, subprocess.SubprocessError, OSError):
        return None


def get_creation_time(path: Path) -> str | None:
    """Lit le tag creation_time exposé par ffprobe (tags de format) ; None si absent."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format_tags=creation_time",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() or None


def _parse_iso(value: str) -> datetime | None:
    """Parse une date ffprobe (ISO 8601, éventuellement avec 'T', 'Z' ou fraction)."""
    core = value.strip().replace("T", " ").split(".")[0].strip()
    if core.endswith("Z"):
        core = core[:-1].strip()
    if len(core) >= 6 and core[-6] in "+-" and core[-3] == ":":  # suffixe ±hh:mm
        core = core[:-6].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(core, fmt)
        except ValueError:
            continue
    return None


def existing_creation_iso(path: Path) -> str | None:
    """Tag creation_time DÉJÀ présent dans le fichier, à reporter dans le MKV.

    Retourne la date au format ISO 8601, ou None si le fichier n'a pas de tag
    plausible. 00 se contente de CONSERVER une date existante : aucune inférence
    (nom de fichier, mtime, photo voisine) — celle-ci est faite ensuite par
    02-enrich-movies-photos-with-date.py sur le MKV produit. Un tag aberrant
    (epoch 1904/1970, année < 1990) est ignoré (sera complété par 01).
    """
    embedded = get_creation_time(path)
    if not embedded:
        return None
    dt = _parse_iso(embedded)
    if dt and dt.year >= MIN_PLAUSIBLE_YEAR:
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    return None


def encode_h265(
    input_path: Path,
    output_path: Path,
    creation_iso: str | None,
    logger: logging.Logger,
) -> bool:
    """Ré-encode la vidéo en H.265 sur GPU NVIDIA (NVENC) — tout codec non-H.265.

    Encodage GPU UNIQUEMENT : aucun repli CPU (libx265). Si NVENC est
    indisponible, ffmpeg échoue et l'original est conservé (cf. main(), qui
    vérifie la présence de hevc_nvenc avant tout encodage réel).
    """
    cmd = [
        "ffmpeg",
        "-i",
        str(input_path),
        "-map_metadata",
        "0",  # recopie les métadonnées globales (dont creation_time)
        "-c:v",
        "hevc_nvenc",  # encodage GPU NVIDIA (NVENC) — pas de CPU
        "-rc",
        "vbr",
        "-cq",
        str(CQ),
        "-qmin",
        str(CQ),
        "-qmax",
        str(CQ),
        "-b:v",
        "0",
        "-preset",
        NVENC_PRESET,
        "-tag:v",
        "hvc1",  # compatibilité Apple
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        AUDIO_CODEC,
        "-b:a",
        AUDIO_BITRATE,
    ]
    if creation_iso:  # réaffirme un tag existant plausible (sinon -map_metadata suffit)
        cmd += ["-metadata", f"creation_time={creation_iso}"]
    cmd += ["-y", str(output_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("Erreur ffmpeg :\n%s", result.stderr[-800:])
        return False
    return True


def remux_to_mkv(
    input_path: Path,
    output_path: Path,
    creation_iso: str | None,
    logger: logging.Logger,
) -> bool:
    """Change seulement le conteneur vers MKV (-c copy) — sans ré-encodage, sans perte.

    Tous les flux (vidéo/audio/sous-titres) sont recopiés tels quels, ainsi que
    les métadonnées globales (-map_metadata 0, dont un creation_time existant).
    Si un codec de sous-titres est incompatible avec le MKV, ffmpeg échoue : on
    conserve alors l'original (voir gestion de l'échec dans main()).
    """
    cmd = [
        "ffmpeg",
        "-i",
        str(input_path),
        "-map",
        "0",
        "-map",
        "-0:d",  # exclut les flux de DONNÉES (ex. 'mett' des
        # vidéos Pixel) que le conteneur MKV ne sait
        # pas muxer -> sinon "Could not write header"
        "-c",
        "copy",
        "-map_metadata",
        "0",  # recopie les métadonnées globales (dont creation_time)
    ]
    if creation_iso:  # réaffirme un tag existant plausible (sinon -map_metadata suffit)
        cmd += ["-metadata", f"creation_time={creation_iso}"]
    cmd += ["-y", str(output_path)]
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
    logger.info(
        "    %-*s  %6s  %12s  %6s", label_width, "valeur", "fichiers", "taille", "conv."
    )
    logger.info("    %s", "-" * (label_width + 32))
    for key, data in sorted(stats.items(), key=lambda x: -x[1]["count"]):
        converted = f"{data['converted']}" if data["converted"] > 0 else "-"
        logger.info(
            "    %-*s  %6d  %12s  %6s",
            label_width,
            key,
            data["count"],
            human_size_bytes(data["size"]),
            converted,
        )


def rename_jpeg_to_jpg(root: Path, dry_run: bool, logger: logging.Logger) -> None:
    """Normalise l'extension des photos : .jpeg (ou .JPEG…) -> .jpg.

    Contenu identique (jpg == jpeg), c'est un simple renommage. Ne jamais
    écraser : si un .jpg de même nom existe déjà, le renommage est ignoré.
    """
    jpegs = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".jpeg"]
    if not jpegs:
        return

    logger.info("Renommage .jpeg -> .jpg : %d fichier(s) trouvé(s)", len(jpegs))
    renamed = collisions = errors = 0
    for src in sorted(jpegs):
        target = src.with_suffix(".jpg")
        if target.exists():
            logger.warning(
                "    collision, ignoré : %s (%s existe déjà)", src.name, target.name
            )
            collisions += 1
            continue
        if dry_run:
            logger.info("    [DRY RUN] %s -> %s", src.name, target.name)
            renamed += 1
            continue
        try:
            os.rename(str(src), str(target))
            renamed += 1
        except OSError as e:
            logger.error("    erreur %s : %s", src.name, e)
            errors += 1
    logger.info(
        "Renommage .jpeg -> .jpg : %d renommé(s)%s, %d collision(s), %d erreur(s)",
        renamed,
        " (simulé)" if dry_run else "",
        collisions,
        errors,
    )


def convert_heic_to_jpg(root: Path, dry_run: bool, logger: logging.Logger) -> None:
    """Convertit les photos HEIC/HEIF -> JPG (décodage Pillow + pillow-heif).

    Les iPhones récents enregistrent en HEIC, format ignoré par le reste du
    pipeline (02/03 ne traitent que .jpg). On décode en JPEG en CONSERVANT l'EXIF
    (orientation, date de prise de vue) pour que 02 puisse l'exploiter, sans
    jamais écraser un .jpg de même nom, puis on supprime l'original HEIC si la
    conversion a réussi. No-op (avec avertissement) si pillow-heif est absent.
    """
    heics = [
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in HEIC_EXTENSIONS
    ]
    if not heics:
        return

    try:
        from PIL import Image
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception as e:  # noqa: BLE001 — dépendance optionnelle
        logger.warning(
            "Conversion HEIC impossible (pillow-heif absent ?) : %s — "
            "%d fichier(s) laissé(s) tel quel",
            e,
            len(heics),
        )
        return

    logger.info("Conversion HEIC -> JPG : %d fichier(s) trouvé(s)", len(heics))
    converted = collisions = errors = 0
    for src in sorted(heics):
        target = src.with_suffix(".jpg")
        if target.exists():
            logger.warning(
                "    collision, ignoré : %s (%s existe déjà)", src.name, target.name
            )
            collisions += 1
            continue
        if dry_run:
            logger.info("    [DRY RUN] %s -> %s", src.name, target.name)
            converted += 1
            continue
        try:
            with Image.open(src) as img:
                exif = img.info.get("exif")  # EXIF d'origine (orientation, date…)
                img = img.convert("RGB")  # JPEG n'accepte ni alpha ni palette
                save_kwargs = {"quality": JPEG_QUALITY, "optimize": True}
                if exif:
                    save_kwargs["exif"] = exif
                img.save(target, "JPEG", **save_kwargs)
            # Reporte la mtime de l'original (chronologie) puis supprime le HEIC.
            st = src.stat()
            os.utime(target, (st.st_atime, st.st_mtime))
            src.unlink()
            converted += 1
        except Exception as e:  # noqa: BLE001 — on logue et on continue le batch
            logger.error("    erreur %s : %s", src.name, e)
            if target.exists():
                try:
                    target.unlink()  # ne pas laisser de JPEG partiel
                except OSError:
                    pass
            errors += 1
    logger.info(
        "Conversion HEIC -> JPG : %d converti(s)%s, %d collision(s), %d erreur(s)",
        converted,
        " (simulé)" if dry_run else "",
        collisions,
        errors,
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
    logger.info(
        "Encodage   : hevc_nvenc (GPU NVIDIA), CQ %d, preset %s", CQ, NVENC_PRESET
    )
    if not DRY_RUN:
        logger.info("Originaux  : supprimés après conversion réussie")
    logger.info("=" * 56)

    # Normalisation des photos (indépendante de ffmpeg) : HEIC -> .jpg puis
    # .jpeg -> .jpg, pour que toute la bibliothèque photo soit en .jpg.
    if HEIC_TO_JPG:
        convert_heic_to_jpg(SOURCE_DIR, DRY_RUN, logger)
    if RENAME_JPEG:
        rename_jpeg_to_jpg(SOURCE_DIR, DRY_RUN, logger)

    if not check_tools():
        sys.exit(1)

    # Politique GPU-only : on refuse de démarrer un encodage réel sans NVENC,
    # plutôt que de risquer un repli CPU ou un échec en plein batch.
    if not DRY_RUN and not nvenc_available():
        logger.error(
            "hevc_nvenc (NVENC) indisponible dans ce ffmpeg. Lancez via l'image "
            "GPU (docker compose, base CUDA + nvidia-container-toolkit). "
            "Encodage GPU obligatoire — abandon."
        )
        sys.exit(1)

    # Recherche récursive et insensible à la casse. On inclut les .mkv : s'ils
    # ne sont pas déjà en H.265, ils seront ré-encodés sur place.
    scan_exts = EXTENSIONS | {OUTPUT_SUFFIX}
    candidates = sorted(
        f
        for f in SOURCE_DIR.rglob("*")
        if f.is_file() and f.suffix.lower() in scan_exts
    )

    if not candidates:
        logger.info("Aucun fichier trouvé pour les extensions ciblées.")
        sys.exit(0)

    logger.info("%d fichier(s) trouvé(s), analyse des codecs...", len(candidates))
    logger.info("")

    # Structures pour le bilan
    # { ".avi": {"count": N, "size": N, "converted": N}, ... }
    ext_stats = defaultdict(lambda: {"count": 0, "size": 0, "converted": 0})
    codec_stats = defaultdict(lambda: {"count": 0, "size": 0, "converted": 0})

    total_size = 0
    skipped = encoded = remuxed = errors = already_h265 = 0
    start_total = datetime.now()
    scan_cache = load_scan_cache(SCAN_CACHE_PATH)

    for i, input_file in enumerate(candidates, 1):
        relative = input_file.relative_to(SOURCE_DIR)
        ext = input_file.suffix.lower()
        src_stat = input_file.stat()
        size = src_stat.st_size

        codec = get_video_codec_cached(input_file, scan_cache)
        codec_label = codec or "inconnu"

        logger.info(
            "[%d/%d] %s (%s)", i, len(candidates), relative, human_size(input_file)
        )
        logger.info("    → codec détecté : %s", codec_label)

        # Comptabilise dans tous les cas
        ext_stats[ext]["count"] += 1
        ext_stats[ext]["size"] += size
        codec_stats[codec_label]["count"] += 1
        codec_stats[codec_label]["size"] += size

        is_mkv = ext == OUTPUT_SUFFIX
        is_hevc = codec == "hevc"  # ffprobe nomme le H.265 « hevc »

        # Objectif : tout finir en H.265 + MKV.
        #   - déjà H.265 + MKV          → rien à faire.
        #   - déjà H.265, autre conteneur → remux -c copy vers .mkv (sans perte).
        #   - tout autre codec (mkv ou non) → ré-encodage H.265 vers .mkv.
        if is_hevc and is_mkv:
            logger.info("    → déjà en H.265 + MKV, ignoré")
            already_h265 += 1
            continue
        action = "remux" if is_hevc else "encode"

        # Cible : sur place pour un .mkv (sortie = entrée → fichier temporaire à
        # l'exécution), sinon même nom avec le suffixe .mkv.
        in_place = is_mkv
        output_file = input_file if in_place else input_file.with_suffix(OUTPUT_SUFFIX)

        # Ne jamais écraser une cible .mkv déjà existante (sauf le cas in-place,
        # où la cible EST le fichier d'entrée).
        if not in_place and output_file.exists():
            logger.warning("    → ignoré : la cible existe déjà — %s", output_file.name)
            skipped += 1
            continue

        total_size += size

        # Tag creation_time existant à conserver (None si absent : la date sera
        # complétée ensuite par 02-enrich-movies-photos-with-date.py).
        creation_iso = existing_creation_iso(input_file)

        if DRY_RUN:
            if action == "encode":
                place = " SUR PLACE" if in_place else f" en : {output_file.name}"
                logger.info("    → serait RÉ-ENCODÉ (H.265)%s", place)
            else:
                logger.info("    → serait REMUXÉ (-c copy) en : %s", output_file.name)
            logger.info(
                "    → date conservée (creation_time) : %s",
                creation_iso or "aucune (à compléter par 01)",
            )
            if not in_place:
                logger.info("    → original serait supprimé après conversion")
            ext_stats[ext]["converted"] += 1
            codec_stats[codec_label]["converted"] += 1
            if action == "encode":
                encoded += 1
            else:
                remuxed += 1
            continue

        # ffmpeg ne peut pas écrire dans le fichier qu'il lit : pour un
        # ré-encodage sur place, on passe par un fichier temporaire.
        target = (
            input_file.with_name(f"{input_file.stem}.h265tmp{OUTPUT_SUFFIX}")
            if in_place
            else output_file
        )

        # Garde-fou espace disque : le fichier converti coexiste avec l'original
        # (temp en sur-place, ou .mkv à côté) jusqu'au remplacement. On exige au
        # moins la taille de la source libre + une marge, sinon on saute proprement.
        if not enough_space(target.parent, size + 200 * 1024 * 1024):
            logger.error(
                "    ✗ Espace disque insuffisant — fichier ignoré : %s",
                input_file.name,
            )
            skipped += 1
            continue

        t0 = datetime.now()
        if action == "encode":
            success = encode_h265(input_file, target, creation_iso, logger)
        else:
            success = remux_to_mkv(input_file, target, creation_iso, logger)
        elapsed = (datetime.now() - t0).seconds

        if success:
            # Garde-fou intégrité : la durée de la sortie doit correspondre à la
            # source. Un remux/ré-encodage peut renvoyer un code 0 tout en
            # produisant une vidéo tronquée (flux corrects mais durée amputée) ;
            # on refuse alors de remplacer/supprimer l'original.
            in_dur = get_duration(input_file)
            out_dur = get_duration(target)
            if in_dur and out_dur and abs(in_dur - out_dur) > max(1.0, 0.01 * in_dur):
                logger.error(
                    "    ✗ Durée incohérente (%.0fs → %.0fs) — sortie tronquée, "
                    "original conservé",
                    in_dur,
                    out_dur,
                )
                if target.exists():
                    target.unlink()
                errors += 1
                continue

            # Cas in-place : on remplace l'original par la version ré-encodée.
            if in_place:
                try:
                    os.replace(target, input_file)
                except OSError as e:
                    logger.error("    ✗ ÉCHEC remplacement sur place : %s", e)
                    if target.exists():
                        target.unlink()
                    errors += 1
                    continue
                final_file = input_file
            else:
                final_file = output_file

            # Réaligne la date de modification de la sortie sur celle de l'original
            # (le tag creation_time, lui, est déjà incrusté par ffmpeg).
            try:
                os.utime(final_file, (src_stat.st_atime, src_stat.st_mtime))
            except OSError as e:
                logger.warning("    ⚠️  mtime non restaurée : %s", e)

            # Vérifie qu'un tag existant a bien survécu. L'absence de date est
            # normale ici si l'original n'en avait pas : 01 la complètera.
            written = get_creation_time(final_file)
            if creation_iso and not written:
                logger.warning(
                    "    ⚠️  creation_time existant non conservé dans le MKV"
                )

            verb = "RÉ-ENCODÉ" if action == "encode" else "REMUXÉ"
            place = " (sur place)" if in_place else ""
            logger.info(
                "    ✓ %s%s  →  %s (%s)  [%ds]  date=%s",
                verb,
                place,
                final_file.name,
                human_size(final_file),
                elapsed,
                written or "à compléter par 01",
            )
            # Pour un autre conteneur, l'original est distinct de la sortie :
            # on le supprime. Pour l'in-place, os.replace l'a déjà remplacé.
            if not in_place:
                try:
                    input_file.unlink()
                    logger.info("    🗑  Original supprimé : %s", input_file.name)
                except OSError as e:
                    # Sur /mnt/c (Windows), un fichier peut être verrouillé
                    # (Explorateur, aperçu, antivirus) -> suppression refusée. Le
                    # MKV est déjà créé : on garde l'original en doublon et on
                    # CONTINUE (au prochain run, la cible .mkv existante fera
                    # sauter ce fichier). Ne jamais planter tout le pipeline pour ça.
                    logger.warning(
                        "    ⚠️  Original NON supprimé (verrou/permission ?) : %s — %s",
                        input_file.name,
                        e,
                    )
            ext_stats[ext]["converted"] += 1
            codec_stats[codec_label]["converted"] += 1
            if action == "encode":
                encoded += 1
            else:
                remuxed += 1
        else:
            logger.error("    ✗ ÉCHEC  →  %s (original conservé)", input_file.name)
            if target.exists():
                target.unlink()
                logger.warning("    Fichier partiel supprimé : %s", target.name)
            errors += 1

    elapsed_total = (datetime.now() - start_total).seconds
    save_scan_cache(SCAN_CACHE_PATH, scan_cache)

    # ── Bilan final ─────────────────────────────────────────
    logger.info("")
    logger.info("=" * 56)
    logger.info("BILAN GÉNÉRAL")
    logger.info("  Fichiers analysés     : %d", len(candidates))
    logger.info("  À ré-encoder (H.265)  : %d", encoded)
    logger.info("  À remuxer (-c copy)   : %d", remuxed)
    logger.info("  Déjà H.265 (ignorés)  : %d", already_h265)
    logger.info("  Ignorés (cible exist.): %d", skipped)
    logger.info("  Taille à convertir    : %s", human_size_bytes(total_size))
    logger.info("")

    print_bilan(logger, "Par extension", ext_stats, label_width=8)
    logger.info("")
    print_bilan(logger, "Par codec", codec_stats, label_width=14)
    logger.info("")

    if DRY_RUN:
        logger.info(
            "  Dry run terminé  —  %d ré-encodage(s) + %d remux = %d fichier(s)",
            encoded,
            remuxed,
            encoded + remuxed,
        )
        logger.info("  Mettez DRY_RUN = False pour lancer la conversion.")
    else:
        logger.info(
            "  Terminé en %ds  —  %d ré-encodé(s), %d remuxé(s), %d erreur(s)",
            elapsed_total,
            encoded,
            remuxed,
            errors,
        )
        logger.info("  Log : %s", SOURCE_DIR / "conversion.log")
    logger.info("=" * 56)


if __name__ == "__main__":
    main()
