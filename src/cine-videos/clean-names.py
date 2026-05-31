##################################################################
## rename movies by cleaning technical info & using name conventions
##################################################################

import os
import re
import sys
import logging
from pathlib import Path

##################################################################

# Mêmes variables d'environnement que convert-h265.py (partagées via env/.env).
# INPUT_FOLDERS : dossiers à parcourir, séparés par des virgules.
ROOTS = [
    p.strip()
    for p in os.environ.get("INPUT_FOLDERS", "/mnt/horus/tvshows").split(",")
    if p.strip()
]
# DRY_RUN : True = simulation sans renommage, False = renommage réel.
# Défaut sûr (true) si non défini ; env/.env le force explicitement dans le container.
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes", "on")

##################################################################


TECH_WORDS = set(
    (
        "1080p 2160p 4k 576p 720p aac ac3 bdrip bluray brrip "
        "divx dts dvdrip fastsub french h264 x265 hdlight hdr hdtv hq "
        "imax multi multitruefrench proper repack subfrench truefrench vff vostfr web "
        "webdl webdl1080p webrip x264 x265 xvid "
        "hevc h265 h.265 h.264 remux 10bit hc vf vo nf amzn yify "
    ).split()
)

RE_EPISODE = re.compile(r"^(s\d{1,2}e\d{1,2}|\d{1,2}x\d{1,2}$)")


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger(__name__)


def get_movies_dirs(roots):
    alls = set()
    for root in roots:
        for rootdir, dirnames, filenames in os.walk(root):
            dirs = [os.path.join(rootdir, d) for d in dirnames]
            alls.update(dirs)
    return alls


def get_movies_titles(roots):
    alls = set()
    for root in roots:
        for rootdir, dirnames, filenames in os.walk(root):
            filepath = [os.path.join(rootdir, f) for f in filenames]
            alls.update(filepath)
    return alls


def clean_movies_dir(dir: str) -> str:
    dir_name = os.path.dirname(dir)
    base_name = os.path.basename(dir)

    base_name = re.sub(r"[()\s\-_]", ".", base_name)
    base_name = re.sub(r"\.{2,}", ".", base_name)

    words = base_name.split(".")
    words = [w.capitalize() for w in words if w]
    base_name = ".".join(words)
    base_name = re.sub(r"^\.", "", base_name)

    return os.path.join(dir_name, base_name)


def clean_movie_title(title: str) -> str:
    dir_name = os.path.dirname(title)
    base_name = os.path.basename(title)

    # sépare nom et extension
    name, _, ext = base_name.rpartition(".")
    if not name:
        return title  # pas d'extension reconnue, on ne touche pas

    # normalise séparateurs
    name = re.sub(r"[()\s\-_]", ".", name)
    name = re.sub(r"\.{2,}", ".", name)

    # reconstruit en s'arrêtant au premier mot technique
    words = name.split(".")
    result = []
    for word in words:
        if word.lower() in TECH_WORDS:
            break
        # épisode de série : S01E01, S01E01E02, 1x01 ...
        if RE_EPISODE.match(word.lower()):
            result.append(word.upper())
        else:
            result.append(word.capitalize())

    new_name = ".".join(result)

    # année entre parenthèses
    new_name = re.sub(r"(?<!\()\b((?:19|20)\d{2})\b(?!\))", r"(\1)", new_name)

    # nettoie les points résiduels en début/fin
    new_name = new_name.strip(".")

    new_title = f"{new_name}.{ext}"
    return os.path.join(dir_name, new_title)


def clean_movies_dirs(dirs):
    return [clean_movies_dir(d) for d in dirs]


def clean_movies_titles(titles):
    return [clean_movie_title(t) for t in titles]


def detect_collisions(olds, news, logger):
    """Détecte les cas où deux anciens noms aboutissent au même nouveau nom."""
    seen = {}
    collisions = 0
    for old, new in zip(olds, news):
        if old == new:
            continue
        key = new.lower()
        if key in seen:
            logger.warning(
                "COLLISION : '%s' et '%s' → même cible '%s'", seen[key], old, new
            )
            collisions += 1
        else:
            seen[key] = old
    return collisions


def apply_renames(olds, news, logger, dry_run: bool):
    renamed = skipped = errors = 0
    for old, new in zip(olds, news):
        if old != new:
            if dry_run:
                logger.info("[DRY RUN]  %s\n           → %s", old, new)
                renamed += 1
            else:
                try:
                    os.rename(old, new)
                    logger.info("OK   %s\n  →  %s", old, new)
                    renamed += 1
                except OSError as e:
                    logger.error("ERREUR  %s  |  %s", old, e)
                    errors += 1
    return renamed, skipped, errors


if __name__ == "__main__":

    logger = setup_logging()

    mode = "DRY RUN (simulation)" if DRY_RUN else "RENOMMAGE RÉEL"
    logger.info("=" * 64)
    logger.info("Mode   : %s", mode)
    logger.info("Roots  : %s", ROOTS)
    logger.info("=" * 64)

    total_renamed = total_errors = 0

    # ── Dossiers ────────────────────────────────────────────
    logger.info("\n── Dossiers ──")
    olds = sorted(get_movies_dirs(ROOTS))
    news = clean_movies_dirs(olds)
    col = detect_collisions(olds, news, logger)
    r, _, e = apply_renames(olds, news, logger, DRY_RUN)
    total_renamed += r
    total_errors += e + col
    logger.info("Dossiers : %d renommé(s), %d collision(s), %d erreur(s)", r, col, e)

    # ── Fichiers ────────────────────────────────────────────
    logger.info("\n── Fichiers ──")
    olds = sorted(get_movies_titles(ROOTS))
    news = clean_movies_titles(olds)
    col = detect_collisions(olds, news, logger)
    r, _, e = apply_renames(olds, news, logger, DRY_RUN)
    total_renamed += r
    total_errors += e + col
    logger.info("Fichiers : %d renommé(s), %d collision(s), %d erreur(s)", r, col, e)

    # ── Bilan ────────────────────────────────────────────────
    logger.info("\n" + "=" * 64)
    logger.info("BILAN FINAL")
    logger.info("  Total renommés : %d", total_renamed)
    logger.info("  Total erreurs  : %d", total_errors)
    if DRY_RUN:
        logger.info("  → Mettez DRY_RUN = False pour appliquer les renommages.")
    logger.info("=" * 64)
