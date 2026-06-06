##################################################################
## Compresse récursivement le share photos/vidéos perso du NAS vers
## un dossier local, en recopiant l'arborescence à l'identique.
##   - photos (.jpg) : redimensionnées (max MAX_PHOTO_SIZE) + JPEG, EXIF conservé
##   - vidéos (.mkv) : ré-encodées 720p H.265 (hevc_nvenc / NVENC) / AAC en .mkv
##
## En amont, 00 normalise toutes les vidéos en .mkv et 01 toutes les photos en
## .jpg : ce script ne traite donc QUE le .jpg et le .mkv ; tout autre fichier
## est ignoré.
##
## L'arborescence du dossier de sortie est ensuite consommée par le
## script d'upload Google Photos (un album par sous-dossier).
##
## Dépendances non déclarées (à installer à la main, cf. CLAUDE.md) :
##   - Pillow            (photos)        pip install Pillow
##   - ffmpeg            (vidéos)        binaire sur le PATH, build NVENC
##   - GPU NVIDIA + nvidia-container-toolkit exposé au conteneur (hevc_nvenc).
##     Le devcontainer le fait déjà (runArgs --gpus all + caps "video").
##################################################################

import os
import sys
import json
import shutil
import logging
import subprocess
import importlib.util
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageFile

# Tolère les JPEG légèrement tronqués (quelques octets manquants en fin de
# fichier) : Pillow complète au lieu d'échouer. Sans ça, ces fichiers sont
# perdus à la compression.
ImageFile.LOAD_TRUNCATED_IMAGES = True

##################################################################
## Configuration : tout est dans 00-config.py (COMPRESS_*)
##################################################################

_spec = importlib.util.spec_from_file_location(
    "pipeline_config", Path(__file__).with_name("00-config.py"))
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)

SOURCE = config.NAS_PHOTOS
OUTPUT = config.COMPRESS_OUTPUT
DRY_RUN = config.DRY_RUN
MAX_PHOTO_SIZE = config.COMPRESS_MAX_PHOTO_SIZE
JPEG_QUALITY = config.COMPRESS_JPEG_QUALITY
VIDEO_HEIGHT = config.COMPRESS_VIDEO_HEIGHT
VIDEO_CQ = config.VIDEO_CQ
VIDEO_PRESET = config.VIDEO_PRESET
PHOTO_EXTS = config.PHOTO_EXT
VIDEO_EXTS = config.VIDEO_EXT

##################################################################


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


def setup_logging() -> logging.Logger:
    _use_paris_timezone()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger(__name__)


def copy_mtime(src: Path, dst: Path) -> None:
    """Reporte la date de modif. de la source sur la cible (chronologie GPhotos)."""
    try:
        st = src.stat()
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass


def probe_creation_time(src: Path) -> str | None:
    """
    Lit le tag creation_time (métadonnée lue par Google Photos) de la source
    via ffprobe, pour le réinjecter dans la vidéo ré-encodée. None si absent.
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_entries",
                "format_tags=creation_time:stream_tags=creation_time",
                str(src),
            ],
            capture_output=True, text=True, timeout=120,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    tags = (data.get("format") or {}).get("tags") or {}
    if tags.get("creation_time"):
        return tags["creation_time"]
    for stream in data.get("streams") or []:
        stags = stream.get("tags") or {}
        if stags.get("creation_time"):
            return stags["creation_time"]
    return None


def compress_photo(src: Path, dst: Path, logger: logging.Logger) -> bool:
    """Redimensionne (max MAX_PHOTO_SIZE) et ré-encode en JPEG, EXIF conservé."""
    try:
        with Image.open(src) as img:
            exif = img.info.get("exif")  # bytes EXIF d'origine (orientation, date…)
            img = img.convert("RGB")  # JPEG n'accepte ni alpha ni palette

            # Ne réduit que si nécessaire ; conserve le ratio. On NE corrige PAS
            # l'orientation au niveau pixel : on garde le tag EXIF tel quel pour
            # que Google Photos l'applique (évite la double rotation).
            img.thumbnail((MAX_PHOTO_SIZE, MAX_PHOTO_SIZE), Image.LANCZOS)

            save_kwargs = {
                "quality": JPEG_QUALITY,
                "optimize": True,
                "progressive": True,
            }
            if exif:
                save_kwargs["exif"] = exif

            if DRY_RUN:
                logger.info("[DRY RUN] PHOTO  %s  →  %s", src.name, dst.name)
                return True

            img.save(dst, "JPEG", **save_kwargs)
    except Exception as e:  # noqa: BLE001 — on logue et on continue le batch
        logger.error("ERREUR photo  %s  |  %s", src, e)
        return False

    copy_mtime(src, dst)
    return True


def compress_video(src: Path, dst: Path, logger: logging.Logger) -> bool:
    """Ré-encode en 720p H.265 NVENC / AAC, conteneur MKV."""
    if DRY_RUN:
        logger.info("[DRY RUN] VIDEO  %s  →  %s", src.name, dst.name)
        return True

    # Date de prise de vue : on la propage à la sortie pour que Google Photos
    # range la vidéo au bon endroit dans la chronologie (le ré-encodage perd
    # sinon le creation_time de la source).
    creation_time = probe_creation_time(src)

    # scale : borne la hauteur à VIDEO_HEIGHT sans jamais agrandir, largeur
    # auto en multiple de 2 (-2) comme l'exige le codec.
    vf = f"scale=-2:'min({VIDEO_HEIGHT},ih)'"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-map_metadata", "0",       # conserve les métadonnées globales de la source
        "-vf", vf,
        "-c:v", "hevc_nvenc",       # encodage GPU NVIDIA (NVENC)
        "-rc", "vbr",
        "-cq", str(VIDEO_CQ),
        "-qmin", str(VIDEO_CQ),
        "-qmax", str(VIDEO_CQ),
        "-b:v", "0",
        "-preset", VIDEO_PRESET,
        "-tag:v", "hvc1",          # lecture H.265 sur Apple / web
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
    ]
    if creation_time:
        cmd += ["-metadata", f"creation_time={creation_time}"]
    cmd += ["-loglevel", "warning", "-stats", str(dst)]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        logger.error("ERREUR vidéo  %s  |  ffmpeg code %s", src, e.returncode)
        if dst.exists():
            dst.unlink()  # ne pas laisser de sortie tronquée
        return False
    except FileNotFoundError:
        logger.error("ffmpeg introuvable sur le PATH — vidéos impossibles à traiter")
        return False

    copy_mtime(src, dst)
    return True


def in_excluded_folder(src: Path, source_root: Path) -> bool:
    """
    Vrai si le fichier est sous un dossier dont le nom commence par « _ »
    (ex. _to_be_sorted/). Ces dossiers de travail/brouillon ne doivent pas
    partir vers Google Photos. Seuls les composants de dossier SOUS la racine
    sont testés (la racine elle-même est ignorée).
    """
    rel = src.relative_to(source_root)
    return any(part.startswith("_") for part in rel.parts[:-1])


def target_for(src: Path, source_root: Path, output_root: Path):
    """(cible, type) pour un fichier source ; (None, 'skip') si non géré."""
    ext = src.suffix.lower()
    rel = src.relative_to(source_root)

    if ext == PHOTO_EXTS:
        return output_root / rel.with_suffix(".jpg"), "photo"
    if ext == VIDEO_EXTS:
        return output_root / rel.with_suffix(".mkv"), "video"
    return None, "skip"


def main() -> int:
    logger = setup_logging()
    source_root = Path(SOURCE)
    output_root = Path(OUTPUT)

    mode = "DRY RUN (simulation)" if DRY_RUN else "COMPRESSION RÉELLE"
    logger.info("=" * 64)
    logger.info("Mode    : %s", mode)
    logger.info("Source  : %s", source_root)
    logger.info("Sortie  : %s", output_root)
    logger.info("Photos  : .jpg, max %dpx, JPEG q%d", MAX_PHOTO_SIZE, JPEG_QUALITY)
    logger.info("Vidéos  : .mkv -> %dp, hevc_nvenc CQ %d, preset %s",
                VIDEO_HEIGHT, VIDEO_CQ, VIDEO_PRESET)
    logger.info("=" * 64)

    if not source_root.is_dir():
        logger.error("Dossier source introuvable : %s", source_root)
        return 1

    photos = videos = skipped = errors = already = collisions = excluded = 0
    seen_targets: set[str] = set()

    for src in sorted(source_root.rglob("*")):
        if not src.is_file():
            continue

        # Ignore les dossiers de travail (nom commençant par « _ »).
        if in_excluded_folder(src, source_root):
            logger.debug("Ignoré (dossier « _ ») : %s", src)
            excluded += 1
            continue

        dst, kind = target_for(src, source_root, output_root)

        if kind == "skip":
            logger.debug("Ignoré (ni .jpg ni .mkv) : %s", src.name)
            skipped += 1
            continue

        # Deux sources -> même cible (ex. a.png et a.jpg). On garde la première.
        key = str(dst).lower()
        if key in seen_targets:
            logger.warning("COLLISION : cible déjà produite, ignoré : %s", src)
            collisions += 1
            continue
        seen_targets.add(key)

        # Incrémental : si la cible existe déjà, on ne refait pas le travail.
        if dst.exists():
            already += 1
            continue

        if not DRY_RUN:
            dst.parent.mkdir(parents=True, exist_ok=True)

        if kind == "photo":
            ok = compress_photo(src, dst, logger)
            if ok:
                photos += 1
            else:
                errors += 1
        else:
            ok = compress_video(src, dst, logger)
            if ok:
                videos += 1
            else:
                errors += 1

    logger.info("\n" + "=" * 64)
    logger.info("BILAN")
    logger.info("  Photos traitées      : %d", photos)
    logger.info("  Vidéos traitées      : %d", videos)
    logger.info("  Déjà présentes       : %d  (cible existante, sautées)", already)
    logger.info("  Collisions           : %d", collisions)
    logger.info("  Dossiers « _ » exclus: %d", excluded)
    logger.info("  Non gérées / ignorées: %d", skipped)
    logger.info("  Erreurs              : %d", errors)
    if DRY_RUN:
        logger.info("  → Mettez DRY_RUN=false (ou env) pour écrire réellement.")
    logger.info("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
