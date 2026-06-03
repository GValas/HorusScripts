#!/usr/bin/env python3
"""
scan_media_dates.py
--------------------
Scanne récursivement un répertoire, détecte les tags de date manquants,
et les corrige automatiquement :
  - photos JPEG : tags EXIF via piexif (DateTimeOriginal, lu par Google Photos)
  - vidéos MKV  : tag creation_time via ffmpeg/ffprobe (lu par Google Photos)

Côté vidéo, seul le conteneur MKV est traité : le pipeline amont
(00-convert-to-mkv+h265.py) garantit que toutes les vidéos sont déjà en .mkv.

Stratégie de date de remplacement (par ordre de priorité) :
  1. Un autre tag de date présent dans le même fichier
  2. La date d'une photo du même dossier qui possède déjà un tag valide
  3. Abandon (fichier signalé comme non corrigeable)

Les vidéos sont écrites en remux (`-c copy`, sans réencodage) : ffmpeg produit
un MKV valide, la date relue est vérifiée, et l'original n'est remplacé
qu'en cas de succès.

Le script tourne dans le même contexte que 02-compress-for-gphotos.py
(dev/prod container ou local), contre le share monté du NAS.

Dépendances:
    pip install piexif
    ffmpeg + ffprobe disponibles sur le PATH
"""

import os
import sys
import csv
import json
import shutil
import logging
import subprocess
from pathlib import Path
from datetime import datetime

import piexif

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION (surchargeable par variables d'environnement)
# ══════════════════════════════════════════════════════════════════════════════


def _env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


# Répertoire racine à scanner : le share photos du NAS (même var que
# 02-compress-for-gphotos.py pour partager la configuration).
SOURCE       = os.environ.get("PHOTOS_SOURCE", "/mnt/horus/photos")

# Rapports : chemin vide ("") ou "none" pour désactiver.
EXPORT_CSV   = os.environ.get("EXPORT_CSV", "rapport.csv")
EXPORT_JSON  = os.environ.get("EXPORT_JSON", "")
if EXPORT_CSV.lower() in ("", "none"):
    EXPORT_CSV = None
if EXPORT_JSON.lower() in ("", "none"):
    EXPORT_JSON = None

# DRY_RUN : True = simulation, aucun fichier modifié. Défaut sûr.
DRY_RUN      = _env_bool("DRY_RUN", "true")
FIX_PHOTOS   = _env_bool("FIX_PHOTOS", "true")   # Corriger les photos JPEG
FIX_VIDEOS   = _env_bool("FIX_VIDEOS", "true")   # Corriger les vidéos

# ══════════════════════════════════════════════════════════════════════════════

NULL_DATE = b"0000:00:00 00:00:00"

PHOTO_EXTENSIONS = {".jpg", ".jpeg"}

# Vidéos : uniquement le MKV (le pipeline amont normalise tout en .mkv).
VIDEO_EXTENSIONS = {".mkv"}

# Clés de tags de date reconnues dans la sortie ffprobe (insensible à la casse).
# Tags pertinents pour le conteneur MKV (Matroska) : creation_time injecté par
# ffmpeg, et les champs natifs date / date_recorded.
DATE_TAG_KEYS = {
    "creation_time",
    "date",
    "date_recorded",
}

# Bornes de plausibilité : rejette les dates epoch (1970 Unix, et 1904 hérité
# d'anciens conteneurs) que certains encodeurs écrivent au lieu d'une vraie date.
MIN_PLAUSIBLE_YEAR = 1990
MAX_PLAUSIBLE_YEAR = 2100

# Cache : pour chaque dossier, la première date valide trouvée dans le dossier
_folder_date_cache: dict[Path, bytes | None] = {}


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# UTILITAIRES DE DATE
# ══════════════════════════════════════════════════════════════════════════════

def _is_plausible_date(dt: datetime) -> bool:
    """Écarte les dates epoch (1904/1970) et les valeurs aberrantes."""
    return MIN_PLAUSIBLE_YEAR <= dt.year <= MAX_PLAUSIBLE_YEAR


def _parse_any_date(value) -> datetime | None:
    """
    Parse une date dans les formats courants (EXIF, ISO 8601, sortie ffprobe)
    en datetime naïf. Tolère 'T', fraction de seconde et suffixe de fuseau.
    Retourne None si aucun format ne correspond.
    """
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    s = str(value).strip()
    if not s:
        return None

    # Normalise : 'T' -> espace, retire la fraction de seconde
    core = s.replace("T", " ").split(".")[0].strip()
    # Retire un suffixe de fuseau éventuel ('Z' ou '±hh:mm')
    if core.endswith("Z"):
        core = core[:-1].strip()
    if len(core) >= 6 and core[-6] in "+-" and core[-3] == ":":
        core = core[:-6].strip()

    for candidate in (core, core[:19]):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S",
                    "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y:%m:%d"):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


# ══════════════════════════════════════════════════════════════════════════════
# LECTURE DES TAGS — PHOTOS
# ══════════════════════════════════════════════════════════════════════════════

def check_photo_tags(filepath: Path) -> dict:
    result = {
        "type": "photo",
        "tags_found": [],
        "tags_missing": [],
        "has_critical_tag": False,
        "primary_date": None,
        "pic_dict": None,   # conservé pour la correction
        "error": None,
    }

    TAG_DEFS = [
        ("0th",  piexif.ImageIFD.DateTime,          "Image DateTime",         False),
        ("Exif", piexif.ExifIFD.DateTimeOriginal,   "EXIF DateTimeOriginal",  True),
        ("Exif", piexif.ExifIFD.DateTimeDigitized,  "EXIF DateTimeDigitized", False),
    ]

    try:
        pic_dict = piexif.load(str(filepath))
        result["pic_dict"] = pic_dict

        for ifd, tag_id, tag_name, is_critical in TAG_DEFS:
            ifd_dict = pic_dict.get(ifd, {})
            val = ifd_dict.get(tag_id)
            if val and val != NULL_DATE:
                date_str = val.decode("utf-8", errors="replace") if isinstance(val, bytes) else str(val)
                result["tags_found"].append({"tag": tag_name, "value": date_str, "raw": val})
                if result["primary_date"] is None:
                    result["primary_date"] = date_str
                if is_critical:
                    result["has_critical_tag"] = True
            else:
                result["tags_missing"].append({"tag": tag_name})

    except Exception as e:
        result["error"] = str(e)
        for _, _, tag_name, _ in TAG_DEFS:
            result["tags_missing"].append({"tag": tag_name})

    return result


# ══════════════════════════════════════════════════════════════════════════════
# LECTURE DES TAGS — VIDÉOS (ffprobe)
# ══════════════════════════════════════════════════════════════════════════════

def _ffprobe_date(filepath: Path) -> datetime | None:
    """
    Lit la première date de création plausible exposée par ffprobe
    (tags format + tags de chaque flux). Retourne un datetime ou None.
    Lève FileNotFoundError si ffprobe n'est pas sur le PATH.
    """
    proc = subprocess.run(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(filepath),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

    tag_dicts = []
    fmt = data.get("format") or {}
    if isinstance(fmt.get("tags"), dict):
        tag_dicts.append(fmt["tags"])
    for stream in data.get("streams") or []:
        if isinstance(stream.get("tags"), dict):
            tag_dicts.append(stream["tags"])

    for tags in tag_dicts:
        for key, val in tags.items():
            if key.lower() in DATE_TAG_KEYS:
                dt = _parse_any_date(val)
                if dt and _is_plausible_date(dt):
                    return dt
    return None


def check_video_tags(filepath: Path) -> dict:
    result = {
        "type": "video",
        "tags_found": [],
        "tags_missing": [],
        "has_critical_tag": False,
        "primary_date": None,
        "error": None,
    }

    try:
        dt = _ffprobe_date(filepath)
    except FileNotFoundError:
        result["error"] = "ffprobe introuvable sur le PATH"
        result["tags_missing"].append({"tag": "creation_time"})
        return result
    except (subprocess.SubprocessError, OSError) as e:
        result["error"] = f"ffprobe: {e}"
        result["tags_missing"].append({"tag": "creation_time"})
        return result

    if dt:
        date_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        result["tags_found"].append({"tag": "creation_time", "value": date_str})
        result["primary_date"] = date_str
        result["has_critical_tag"] = True
    else:
        result["tags_missing"].append({"tag": "creation_time"})

    return result


# ══════════════════════════════════════════════════════════════════════════════
# RECHERCHE DE DATE DE REMPLACEMENT
# ══════════════════════════════════════════════════════════════════════════════

def _best_date_from_tags(info: dict) -> bytes | None:
    """Retourne la meilleure date trouvée dans les tags existants du fichier."""
    for tag in info.get("tags_found", []):
        raw = tag.get("raw")
        if raw and isinstance(raw, bytes) and raw != NULL_DATE:
            return raw
    return None


def _date_from_folder(filepath: Path) -> bytes | None:
    """
    Cherche la date d'une autre photo JPEG dans le même dossier
    qui possède déjà un tag DateTimeOriginal valide.
    Résultat mis en cache par dossier.
    """
    folder = filepath.parent
    if folder in _folder_date_cache:
        return _folder_date_cache[folder]

    date_found = None
    for sibling in sorted(folder.iterdir()):
        if sibling == filepath:
            continue
        if sibling.suffix.lower() not in PHOTO_EXTENSIONS:
            continue
        try:
            pic = piexif.load(str(sibling))
            val = pic.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
            if val and val != NULL_DATE:
                date_found = val
                break
        except Exception:
            continue

    _folder_date_cache[folder] = date_found
    return date_found


def resolve_date(filepath: Path, info: dict) -> tuple[bytes | None, str]:
    """
    Retourne (date_bytes, source) où source décrit l'origine de la date.
    Retourne (None, "introuvable") si aucune date n'est disponible.
    """
    # Priorité 1 : autre tag dans le même fichier
    date = _best_date_from_tags(info)
    if date:
        return date, "tag existant dans le fichier"

    # Priorité 2 : date d'une photo voisine dans le même dossier
    date = _date_from_folder(filepath)
    if date:
        return date, f"photo voisine dans {filepath.parent.name}/"

    return None, "introuvable"


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTION DES FICHIERS — PHOTOS
# ══════════════════════════════════════════════════════════════════════════════

def fix_photo(filepath: Path, info: dict, date: bytes, dry_run: bool) -> str:
    """Injecte les tags de date manquants dans une photo JPEG."""
    pic_dict = info.get("pic_dict")
    if pic_dict is None:
        return "erreur: EXIF non chargé"

    oth_dict  = pic_dict.setdefault("0th", {})
    exif_dict = pic_dict.setdefault("Exif", {})

    changed = False
    if not oth_dict.get(piexif.ImageIFD.DateTime) or oth_dict.get(piexif.ImageIFD.DateTime) == NULL_DATE:
        oth_dict[piexif.ImageIFD.DateTime] = date
        changed = True
    if not exif_dict.get(piexif.ExifIFD.DateTimeOriginal) or exif_dict.get(piexif.ExifIFD.DateTimeOriginal) == NULL_DATE:
        exif_dict[piexif.ExifIFD.DateTimeOriginal] = date
        changed = True
    if not exif_dict.get(piexif.ExifIFD.DateTimeDigitized) or exif_dict.get(piexif.ExifIFD.DateTimeDigitized) == NULL_DATE:
        exif_dict[piexif.ExifIFD.DateTimeDigitized] = date
        changed = True

    if not changed:
        return "déjà complet"

    if dry_run:
        return "DRY RUN — serait mis à jour"

    try:
        piexif.remove(str(filepath))
        exif_bytes = piexif.dump(pic_dict)
        piexif.insert(exif_bytes, str(filepath))
        return "corrigé ✅"
    except Exception as e:
        return f"erreur écriture: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTION DES FICHIERS — VIDÉOS (ffmpeg, remux sans réencodage)
# ══════════════════════════════════════════════════════════════════════════════

def _ffmpeg_set_creation_time(filepath: Path, dt: datetime) -> None:
    """
    Réécrit la vidéo en remux (`-c copy`, sans réencodage) en injectant
    le tag creation_time, vers un fichier temporaire. La date est ensuite
    relue (ffprobe) et vérifiée avant de remplacer l'original.
    Lève FileNotFoundError si ffmpeg est absent, RuntimeError sinon.
    """
    iso = dt.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = filepath.with_name(f"{filepath.stem}.datetmp{filepath.suffix}")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(filepath),
        "-map", "0", "-c", "copy",
        "-map_metadata", "0",
        "-metadata", f"creation_time={iso}",
        str(tmp),
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            err = (proc.stderr or "").strip()
            raise RuntimeError(err.splitlines()[-1] if err else "échec ffmpeg")

        written = _ffprobe_date(tmp)
        if written is None:
            raise RuntimeError("ce conteneur ne conserve pas creation_time")
        if abs((written - dt).total_seconds()) > 48 * 3600:
            raise RuntimeError(f"date relue incohérente ({written:%Y-%m-%d})")

        os.replace(str(tmp), str(filepath))
    finally:
        if tmp.exists():
            try:
                os.remove(str(tmp))
            except OSError:
                pass


def fix_video(filepath: Path, date: bytes, dry_run: bool) -> str:
    """Injecte la date de création dans une vidéo MKV via ffmpeg (remux)."""
    dt = _parse_any_date(date)
    if dt is None:
        return "erreur: date source illisible"

    if dry_run:
        return f"DRY RUN — serait mis à jour (ffmpeg creation_time={dt:%Y-%m-%d %H:%M:%S})"

    try:
        _ffmpeg_set_creation_time(filepath, dt)
        return "corrigé ✅ (ffmpeg)"
    except FileNotFoundError:
        return "erreur: ffmpeg introuvable sur le PATH"
    except Exception as e:
        return f"erreur écriture vidéo: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# SCAN PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

def scan_and_fix(root: Path, logger: logging.Logger, dry_run: bool,
                 fix_photos: bool, fix_videos: bool) -> list[dict]:
    results = []
    all_files = sorted(root.rglob("*"))
    media_files = [
        f for f in all_files
        if f.is_file() and f.suffix.lower() in (PHOTO_EXTENSIONS | VIDEO_EXTENSIONS)
    ]

    total = len(media_files)
    mode  = "🧪 DRY RUN" if dry_run else "✏️  ÉCRITURE"
    logger.info("%s — %d fichier(s) média dans « %s »", mode, total, root)

    for i, filepath in enumerate(media_files, 1):
        is_video = filepath.suffix.lower() in VIDEO_EXTENSIONS
        info     = check_video_tags(filepath) if is_video else check_photo_tags(filepath)

        fix_result = None
        date_source = None

        if not info["has_critical_tag"]:
            date, date_source = resolve_date(filepath, info)

            if date:
                if is_video and fix_videos:
                    fix_result = fix_video(filepath, date, dry_run)
                elif not is_video and fix_photos:
                    fix_result = fix_photo(filepath, info, date, dry_run)
                else:
                    fix_result = "correction désactivée"
            else:
                fix_result = "⛔ aucune date disponible"

            logger.warning("[%d/%d] %s", i, total, filepath)
            logger.warning("       source date : %s", date_source or "introuvable")
            logger.warning("       action      : %s", fix_result)

        record = {
            "fichier":              str(filepath),
            "type":                 info["type"],
            "tag_critique_present": info["has_critical_tag"],
            "date_principale":      info.get("primary_date") or "",
            "tags_presents":        "; ".join(t["tag"] for t in info.get("tags_found", [])),
            "tags_manquants":       "; ".join(t["tag"] for t in info.get("tags_missing", [])),
            "date_source":          date_source or "",
            "action":               fix_result or "",
            "erreur":               info.get("error") or "",
        }
        results.append(record)

    return results


# ══════════════════════════════════════════════════════════════════════════════
# RAPPORT
# ══════════════════════════════════════════════════════════════════════════════

def print_summary(results: list[dict], logger: logging.Logger, dry_run: bool) -> None:
    ok       = sum(1 for r in results if r["tag_critique_present"])
    ko       = sum(1 for r in results if not r["tag_critique_present"])
    fixed    = sum(1 for r in results if "corrigé" in r["action"] or "DRY RUN" in r["action"])
    unfixable= sum(1 for r in results if "introuvable" in r["action"] or "⛔" in r["action"])
    errors   = sum(1 for r in results if r["erreur"] or "erreur" in r["action"])
    total    = len(results)

    logger.info("=" * 60)
    logger.info("RÉSUMÉ %s", "(DRY RUN)" if dry_run else "")
    logger.info("  Total analysé        : %d", total)
    logger.info("  ✅ Tag déjà OK       : %d", ok)
    logger.info("  ⚠️  Tag manquant      : %d", ko)
    logger.info("  🔧 Corrigé%s  : %d", "(simulé)" if dry_run else "", fixed)
    logger.info("  ⛔ Non corrigeable   : %d", unfixable)
    if errors:
        logger.info("  ❌ Erreurs          : %d", errors)
    logger.info("=" * 60)

    if dry_run and fixed > 0:
        logger.info("💡  C'est un DRY RUN. Passez DRY_RUN=false (env) pour appliquer les corrections.")


def save_csv(results: list[dict], output_path: str, logger: logging.Logger) -> None:
    fieldnames = [
        "fichier", "type", "tag_critique_present", "date_principale",
        "tags_presents", "tags_manquants", "date_source", "action", "erreur",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    logger.info("📄  Rapport CSV enregistré : %s", output_path)


# ══════════════════════════════════════════════════════════════════════════════
# POINT D'ENTRÉE
# ══════════════════════════════════════════════════════════════════════════════

def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def main() -> int:
    logger = setup_logging()
    root = Path(SOURCE)

    mode = "DRY RUN (simulation)" if DRY_RUN else "ÉCRITURE RÉELLE"
    logger.info("=" * 64)
    logger.info("Mode    : %s", mode)
    logger.info("Source  : %s", root)
    logger.info("Cibles  : photos=%s  vidéos=%s", FIX_PHOTOS, FIX_VIDEOS)
    logger.info("=" * 64)

    if not root.is_dir():
        logger.error("Répertoire source introuvable : %s", root)
        return 1

    if FIX_VIDEOS and not _ffmpeg_available():
        logger.warning(
            "ffmpeg/ffprobe introuvables sur le PATH : "
            "la lecture et la correction des vidéos échoueront."
        )

    results = scan_and_fix(root, logger, dry_run=DRY_RUN,
                           fix_photos=FIX_PHOTOS, fix_videos=FIX_VIDEOS)
    print_summary(results, logger, dry_run=DRY_RUN)

    if EXPORT_CSV:
        save_csv(results, EXPORT_CSV, logger)

    if EXPORT_JSON:
        with open(EXPORT_JSON, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        logger.info("📄  Rapport JSON enregistré : %s", EXPORT_JSON)

    return 0


if __name__ == "__main__":
    sys.exit(main())
