##################################################################
## rename movies by cleaning technical info & using name conventions
##################################################################

import os
import re
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402

##################################################################
## Configuration : tout est dans 00-config.py (COMMUN + CLEAN_*)
##################################################################

config = _common.load_config(__file__)

ROOTS = config.INPUT_FOLDERS  # dossiers parcourus récursivement
# DRY_RUN effectif : PIPELINE_DRY_RUN (exporté par le lanceur via --dry-run /
# --real) prime sur 00-config.py — pratique pour un run ponctuel sans éditer
# (ni risquer d'oublier de remettre) la config.
DRY_RUN = _common.resolve_dry_run(config)
TECH_WORDS = config.CLEAN_TECH_WORDS  # mots techniques retirés des noms
VIDEO_EXTENSIONS = config.CLEAN_VIDEO_EXTENSIONS
SUBTITLE_EXTENSIONS = config.CLEAN_SUBTITLE_EXTENSIONS
LANG_TOKENS = config.CLEAN_SUBTITLE_LANG_TOKENS
RENAMED_EXTENSIONS = VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS

##################################################################

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
    """Fichiers candidats au renommage : uniquement les vidéos et sous-titres.

    Les jaquettes, .nfo et autres fichiers annexes sont laissés intacts — les
    renommer n'apporte rien et risque de casser les références des médiathèques.
    """
    alls = set()
    for root in roots:
        for rootdir, dirnames, filenames in os.walk(root):
            for f in filenames:
                if os.path.splitext(f)[1].lower() in RENAMED_EXTENSIONS:
                    alls.add(os.path.join(rootdir, f))
    return alls


def clean_movies_dir(dir: str) -> str:
    dir_name = os.path.dirname(dir)
    base_name = os.path.basename(dir)

    cleaned = re.sub(r"[()\s\-_]", ".", base_name)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)

    words = cleaned.split(".")
    words = [w.capitalize() for w in words if w]
    cleaned = ".".join(words)
    cleaned = re.sub(r"^\.", "", cleaned)

    # Garde-fou : un nettoyage qui viderait le nom laisse le dossier tel quel.
    if not cleaned:
        return dir

    return os.path.join(dir_name, cleaned)


def clean_movie_title(title: str) -> str:
    dir_name = os.path.dirname(title)
    base_name = os.path.basename(title)

    # sépare nom et extension
    name, _, ext = base_name.rpartition(".")
    if not name:
        return title  # pas d'extension reconnue, on ne touche pas

    # Seuls les médias et leurs sous-titres sont renommés (garde-fou doublant
    # le filtrage de get_movies_titles) : une jaquette ou un .nfo reste intact.
    if f".{ext.lower()}" not in RENAMED_EXTENSIONS:
        return title

    # normalise séparateurs
    name = re.sub(r"[()\s\-_]", ".", name)
    name = re.sub(r"\.{2,}", ".", name)

    # reconstruit en s'arrêtant au premier mot technique
    words = name.split(".")
    result = []
    cut_at = None
    for i, word in enumerate(words):
        if word.lower() in TECH_WORDS:
            cut_at = i
            break
        # épisode de série : S01E01, S01E01E02, 1x01 ...
        if RE_EPISODE.match(word.lower()):
            result.append(word.upper())
        else:
            result.append(word.capitalize())

    # Garde-fou : un nom entièrement composé de jetons techniques (« 1080p.mkv »)
    # donnerait un nom VIDE, donc un fichier caché « .mkv ». On ne touche pas.
    if not result:
        return title

    new_name = ".".join(result)

    # année entre parenthèses
    new_name = re.sub(r"(?<!\()\b((?:19|20)\d{2})\b(?!\))", r"(\1)", new_name)

    # nettoie les points résiduels en début/fin
    new_name = new_name.strip(".")
    if not new_name:
        return title

    # Sous-titres : ré-attache les jetons de langue/variante situés APRÈS la
    # coupure technique. Sans ça « Film.2019.1080p.fr.srt » et
    # « Film.2019.1080p.en.srt » visent tous deux « Film.(2019).srt » : la
    # deuxième piste est perdue et la première n'est plus identifiable.
    if f".{ext.lower()}" in SUBTITLE_EXTENSIONS and cut_at is not None:
        seen = set()
        langs = []
        for word in words[cut_at:]:
            low = word.lower()
            if low in LANG_TOKENS and low not in seen:
                seen.add(low)
                langs.append(low)
        if langs:
            new_name = ".".join([new_name, *langs])

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
        if old == new:
            continue

        # Ne jamais écraser une cible déjà présente sur le disque : os.rename
        # remplace silencieusement le fichier de destination sous POSIX.
        # On tolère le simple changement de casse (même fichier sous-jacent sur
        # un système insensible à la casse) via os.path.samefile.
        try:
            if os.path.exists(new) and not os.path.samefile(old, new):
                logger.warning(
                    "CIBLE EXISTE : '%s' existe déjà → '%s' non renommé", new, old
                )
                skipped += 1
                continue
        except OSError as e:  # source disparue entre le scan et le renommage
            logger.error("ERREUR  %s  |  %s", old, e)
            errors += 1
            continue

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


def main() -> int:
    logger = setup_logging()

    mode = "DRY RUN (simulation)" if DRY_RUN else "RENOMMAGE RÉEL"
    logger.info("=" * 64)
    logger.info("Mode   : %s", mode)
    logger.info("Roots  : %s", ROOTS)
    if getattr(config, "_OVERLAY_PATH", None):
        logger.info("Config : surcouche active — %s", config._OVERLAY_PATH)
    logger.info("=" * 64)

    total_renamed = total_skipped = total_errors = total_collisions = 0

    # ── Dossiers ────────────────────────────────────────────
    logger.info("\n── Dossiers ──")
    # Renommage du PLUS PROFOND au moins profond : clean_movies_dir ne touche que
    # le basename, donc renommer un enfant avant son parent garde des chemins
    # valides (le parent existe encore). À l'inverse, renommer le parent d'abord
    # rendrait périmé le chemin stocké de l'enfant -> os.rename échouerait
    # (« old n'existe plus »). Cas typique : tvshows/Série/Season 01/.
    olds = sorted(get_movies_dirs(ROOTS), key=lambda p: p.count(os.sep), reverse=True)
    news = clean_movies_dirs(olds)
    col = detect_collisions(olds, news, logger)
    r, sk, e = apply_renames(olds, news, logger, DRY_RUN)
    total_renamed += r
    total_skipped += sk
    total_errors += e
    total_collisions += col
    logger.info(
        "Dossiers : %d renommé(s), %d collision(s), %d ignoré(s) (cible existe), %d erreur(s)",
        r,
        col,
        sk,
        e,
    )

    # ── Fichiers ────────────────────────────────────────────
    logger.info("\n── Fichiers ──")
    olds = sorted(get_movies_titles(ROOTS))
    news = clean_movies_titles(olds)
    col = detect_collisions(olds, news, logger)
    r, sk, e = apply_renames(olds, news, logger, DRY_RUN)
    total_renamed += r
    total_skipped += sk
    total_errors += e
    total_collisions += col
    logger.info(
        "Fichiers : %d renommé(s), %d collision(s), %d ignoré(s) (cible existe), %d erreur(s)",
        r,
        col,
        sk,
        e,
    )

    # ── Bilan ────────────────────────────────────────────────
    logger.info("\n" + "=" * 64)
    logger.info("BILAN FINAL")
    logger.info("  Total renommés   : %d", total_renamed)
    logger.info("  Total ignorés    : %d  (cible déjà existante)", total_skipped)
    logger.info("  Total collisions : %d", total_collisions)
    logger.info("  Total erreurs    : %d", total_errors)
    if DRY_RUN:
        logger.info("  → Mettez DRY_RUN = False pour appliquer les renommages.")
    logger.info("=" * 64)

    # Code de sortie parlant : le lanceur (set -e + trap ERR) doit pouvoir
    # distinguer un run propre d'un run en échec, et ne pas notifier « ✅ »
    # quand rien n'a fonctionné. Les collisions sont signalées mais ne sont pas
    # des erreurs : rien n'est écrasé, le fichier est simplement laissé en place.
    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main())
