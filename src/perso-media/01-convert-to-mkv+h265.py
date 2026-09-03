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
import subprocess
import sys
import logging
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402

# ── Configuration : tout est dans 00-config.py (CONVERT_*) ──────────────────
config = _common.load_config(__file__)

SOURCE_DIR = Path(config.PHOTOS_SRC)
DRY_RUN = _common.resolve_dry_run(config)  # surcharge CLI (--dry-run / --real)
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
SCAN_WORKERS = getattr(config, "CONVERT_SCAN_WORKERS", 1)  # sondes // au scan
# Schéma des entrées du cache (on y stocke désormais pix_fmt / colorimétrie /
# codecs audio) : à incrémenter pour invalider les caches d'une version antérieure.
SCAN_CACHE_VERSION = 2
# Sauvegarde intermédiaire du cache tous les N fichiers sondés (un run
# interrompu ne doit pas perdre tout le travail de scan).
SCAN_CACHE_FLUSH_EVERY = 200

# Codecs audio déjà compressés et muxables tels quels en MKV : on les RECOPIE
# au lieu de les ré-encoder en AAC. Ré-encoder un AAC en AAC est une perte
# générationnelle gratuite. Tout le reste (PCM des vieux caméscopes, etc.) est
# ré-encodé en AAC comme avant.
AUDIO_COPY_OK = {
    "aac",
    "ac3",
    "eac3",
    "mp3",
    "opus",
    "flac",
    "vorbis",
    "alac",
    "dts",
}
# ───────────────────────────────────────────────────────────


def setup_logging(log_dir: Path, dry_run: bool) -> logging.Logger:
    _common.use_paris_timezone()
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


EMPTY_PROPS = {
    "codec": None,
    "pix_fmt": None,
    "color_trc": None,
    "color_primaries": None,
    "color_space": None,
    "audio_codecs": [],
}


def get_video_props(path: Path) -> dict:
    """Codec vidéo + colorimétrie + codecs audio, en un seul appel ffprobe.

    La colorimétrie sert à préserver le 10 bits / HDR à l'encodage (les iPhone
    et Pixel récents filment en HLG 10 bits) ; la liste des codecs audio sert à
    décider si on peut recopier l'audio au lieu de le ré-encoder.
    """
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        streams = (json.loads(result.stdout) or {}).get("streams", [])
    except Exception:  # noqa: BLE001 — fichier illisible : propriétés vides
        return dict(EMPTY_PROPS, audio_codecs=[])

    props = dict(EMPTY_PROPS, audio_codecs=[])
    for stream in streams:
        kind = stream.get("codec_type", "")
        name = (stream.get("codec_name") or "").lower()
        if kind == "video" and props["codec"] is None:
            props["codec"] = name or None
            props["pix_fmt"] = stream.get("pix_fmt")
            props["color_trc"] = stream.get("color_transfer")
            props["color_primaries"] = stream.get("color_primaries")
            props["color_space"] = stream.get("color_space")
        elif kind == "audio":
            props["audio_codecs"].append(name)
    return props


def save_scan_cache(cache: dict) -> None:
    """Sauvegarde intermédiaire/finale du cache de scan (best-effort)."""
    _common.save_scan_cache(SCAN_CACHE_PATH, cache, SCAN_CACHE_VERSION)


def get_video_props_cached(path: Path, cache: dict) -> dict:
    """Propriétés vidéo avec cache validé par (mtime, taille) : évite un ffprobe
    par fichier déjà connu et inchangé. Sur miss/changement, sonde et mémorise."""
    try:
        st = path.stat()
    except OSError:
        return get_video_props(path)
    key = str(path)
    ent = cache.get(key)
    if _common.cache_entry_valid(ent, st):
        return ent.get("props") or dict(EMPTY_PROPS, audio_codecs=[])
    props = get_video_props(path)
    cache[key] = {"mtime": int(st.st_mtime), "size": st.st_size, "props": props}
    return props


def prewarm_codecs(candidates: list, cache: dict) -> None:
    """Pré-sonde en parallèle (ffprobe) les fichiers absents du cache.

    ffprobe est I/O-bound : les threads se recouvrent. Les écritures du cache se
    font dans le thread principal (ex.map yield séquentiel). L'encodage qui suit
    reste séquentiel (GPU partagé)."""
    if SCAN_WORKERS <= 1 or not candidates:
        return
    misses = []
    for f in candidates:
        try:
            st = f.stat()
        except OSError:
            continue
        if not _common.cache_entry_valid(cache.get(str(f)), st):
            misses.append((f, st))
    if not misses:
        return

    def work(item):
        f, st = item
        return str(f), st, get_video_props(f)

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        for i, (key, st, props) in enumerate(ex.map(work, misses), 1):
            cache[key] = {"mtime": int(st.st_mtime), "size": st.st_size, "props": props}
            # Flush périodique : sonder une grosse bibliothèque prend des
            # dizaines de minutes ; une interruption ne doit pas tout perdre.
            if i % SCAN_CACHE_FLUSH_EVERY == 0:
                save_scan_cache(cache)
    save_scan_cache(cache)


enough_space = _common.enough_space


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


def existing_creation_iso(path: Path) -> str | None:
    """Tag creation_time DÉJÀ présent dans le fichier, à reporter dans le MKV.

    Retourne la date en ISO 8601 UTC (suffixe 'Z'), ou None si le fichier n'a pas
    de tag plausible. 01 se contente de CONSERVER une date existante : aucune
    inférence (nom de fichier, mtime, photo voisine) — celle-ci est faite ensuite
    par 02-enrich-movies-photos-with-date.py sur le MKV produit. Un tag aberrant
    (epoch 1904/1970, année < 1990) est ignoré (sera complété par 02).

    Le fuseau porté par le tag source est CONVERTI, pas tronqué : réécrire une
    heure locale sans fuseau la ferait relire comme de l'UTC par ffmpeg, et la
    vidéo se retrouverait décalée de 1 à 2 h dans Google Photos.
    """
    embedded = get_creation_time(path)
    if not embedded:
        return None
    dt = _common.parse_any_date(embedded)  # -> heure de Paris, naïve
    if dt and dt.year >= MIN_PLAUSIBLE_YEAR:
        return _common.to_utc_iso(dt)
    return None


def encode_h265(
    input_path: Path,
    output_path: Path,
    creation_iso: str | None,
    props: dict,
    logger: logging.Logger,
) -> bool:
    """Ré-encode la vidéo en H.265 sur GPU NVIDIA (NVENC) — tout codec non-H.265.

    Encodage GPU UNIQUEMENT : aucun repli CPU (libx265). Si NVENC est
    indisponible, ffmpeg échoue et l'original est conservé (cf. main(), qui
    vérifie la présence de hevc_nvenc avant tout encodage réel).

    Trois garde-fous de fidélité :
      - `-map` explicite : sans lui, la sélection par défaut de ffmpeg ne garde
        qu'UNE piste audio. Toutes les pistes audio sont désormais conservées ;
      - profondeur/colorimétrie : une source 10 bits ou HDR (HLG des iPhone et
        Pixel récents) est encodée en main10 avec sa signalisation, au lieu
        d'être aplatie en 8 bits SDR par un `-pix_fmt yuv420p` inconditionnel ;
      - audio : recopié tel quel s'il est déjà dans un codec compressé muxable
        en MKV, plutôt que ré-encodé en AAC (perte générationnelle gratuite).
    """
    pix_fmt = props.get("pix_fmt") or ""
    is_10bit = "10" in pix_fmt or "p010" in pix_fmt
    is_hdr = (
        props.get("color_trc") in ("smpte2084", "arib-std-b67")
        or props.get("color_primaries") == "bt2020"
    )

    if is_hdr or is_10bit:
        depth_args = ["-pix_fmt", "p010le", "-profile:v", "main10"]
    else:
        depth_args = ["-pix_fmt", "yuv420p"]

    color_args = []
    if is_hdr:
        color_args = [
            "-color_primaries",
            props.get("color_primaries") or "bt2020",
            "-color_trc",
            props.get("color_trc") or "smpte2084",
            "-colorspace",
            props.get("color_space") or "bt2020nc",
        ]

    audio_codecs = props.get("audio_codecs") or []
    if audio_codecs and all(c in AUDIO_COPY_OK for c in audio_codecs):
        audio_args = ["-c:a", "copy"]
    else:
        audio_args = ["-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE]

    cmd = [
        "ffmpeg",
        "-i",
        str(input_path),
        # Vidéo principale + TOUTES les pistes audio (les sous-titres des
        # vidéos perso sont inexistants et leurs codecs exotiques feraient
        # échouer le mux : on ne les mappe pas).
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
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
        *depth_args,
        *color_args,
        *audio_args,
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


human_size_bytes = _common.human_size_bytes


def human_size(path: Path) -> str:
    return human_size_bytes(path.stat().st_size)


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
    jpegs = [
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() == ".jpeg"
        and not _common.in_excluded_folder(p, root)
    ]
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
        if p.is_file()
        and p.suffix.lower() in HEIC_EXTENSIONS
        and not _common.in_excluded_folder(p, root)
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

    # Le log va à côté du script (dossier monté, gitignoré), PAS dans la
    # bibliothèque photo : y écrire pollue le share et casse sur une source en
    # lecture seule.
    logger = setup_logging(Path(__file__).resolve().parent, DRY_RUN)

    mode_label = "DRY RUN (simulation)" if DRY_RUN else "CONVERSION RÉELLE"
    logger.info("=" * 56)
    logger.info("Mode       : %s", mode_label)
    logger.info("Source     : %s", SOURCE_DIR.resolve())
    if getattr(config, "_OVERLAY_PATH", None):
        logger.info("Config     : surcouche active — %s", config._OVERLAY_PATH)
    logger.info("Extensions : %s", ", ".join(sorted(EXTENSIONS)))
    logger.info("Sortie     : même dossier que l'original")
    logger.info(
        "Encodage   : hevc_nvenc (GPU NVIDIA), CQ %d, preset %s", CQ, NVENC_PRESET
    )
    if not DRY_RUN:
        logger.info("Originaux  : supprimés après conversion réussie")
    logger.info("=" * 56)

    # Un run précédent interrompu (Ctrl-C, bouton « Arrêter » de l'interface web)
    # a pu laisser des .h265tmp.mkv / .datetmp.mkv à demi écrits. Ils portent
    # l'extension de la bibliothèque : sans ce nettoyage ils seraient traités
    # comme de vraies vidéos et finiraient sur Google Photos.
    _common.purge_temp_artifacts(SOURCE_DIR, DRY_RUN, logger)

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
        # Dossiers de travail « _ » : hors pipeline, comme pour 03/04.
        and not _common.in_excluded_folder(f, SOURCE_DIR)
        # Temporaires d'un run concurrent/interrompu : jamais des médias.
        and not _common.is_temp_artifact(f)
    )

    if not candidates:
        logger.info("Aucun fichier trouvé pour les extensions ciblées.")
        return 0

    logger.info("%d fichier(s) trouvé(s), analyse des codecs...", len(candidates))
    logger.info("")

    # Structures pour le bilan
    # { ".avi": {"count": N, "size": N, "converted": N}, ... }
    ext_stats = defaultdict(lambda: {"count": 0, "size": 0, "converted": 0})
    codec_stats = defaultdict(lambda: {"count": 0, "size": 0, "converted": 0})

    total_size = 0
    skipped = encoded = remuxed = errors = already_h265 = 0
    saved_old = saved_new = 0  # cumul tailles avant/après (bilan « espace »)
    start_total = datetime.now()
    scan_cache = _common.load_scan_cache(SCAN_CACHE_PATH, SCAN_CACHE_VERSION)
    prewarm_codecs(candidates, scan_cache)  # pré-sonde // les codecs (cache-miss)

    for i, input_file in enumerate(candidates, 1):
        relative = input_file.relative_to(SOURCE_DIR)
        ext = input_file.suffix.lower()
        src_stat = input_file.stat()
        size = src_stat.st_size

        props = get_video_props_cached(input_file, scan_cache)
        codec = props.get("codec")
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
            success = encode_h265(input_file, target, creation_iso, props, logger)
        else:
            success = remux_to_mkv(input_file, target, creation_iso, logger)
        # .total_seconds() et non .seconds : ce dernier repart de 0 au-delà
        # de 24 h (un très gros encodage afficherait une durée absurde).
        elapsed = int((datetime.now() - t0).total_seconds())

        if success:
            # Garde-fou intégrité : la durée de la sortie doit correspondre à la
            # source. Un remux/ré-encodage peut renvoyer un code 0 tout en
            # produisant une vidéo tronquée (flux corrects mais durée amputée) ;
            # on refuse alors de remplacer/supprimer l'original.
            # Une durée de sortie ILLISIBLE compte comme un échec : c'est
            # exactement ce que renvoie ffprobe sur un fichier gravement
            # tronqué. La version précédente sautait alors le contrôle et
            # supprimait quand même l'original.
            in_dur = get_duration(input_file)
            out_dur = get_duration(target)
            if in_dur and out_dur is None:
                logger.error(
                    "    ✗ Durée de sortie illisible — sortie probablement "
                    "tronquée, original conservé"
                )
                if target.exists():
                    target.unlink()
                errors += 1
                continue
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
            # Cumul pour le bilan « espace » (conversion menée à terme).
            try:
                saved_old += size
                saved_new += final_file.stat().st_size
            except OSError:
                pass

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

    elapsed_total = int((datetime.now() - start_total).total_seconds())
    # Purge des entrées dont le fichier a disparu (converti, renommé, supprimé).
    _common.prune_scan_cache(scan_cache, [k for k in scan_cache if os.path.exists(k)])
    save_scan_cache(scan_cache)

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
    if saved_old > 0:
        saved = saved_old - saved_new
        pct = 100 * saved / saved_old
        logger.info(
            "  Espace économisé      : %s → %s  (gain %s, %.1f%%)",
            human_size_bytes(saved_old),
            human_size_bytes(saved_new),
            human_size_bytes(saved),
            pct,
        )
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
        logger.info("  Log : %s", Path(__file__).resolve().parent / "conversion.log")
    logger.info("=" * 56)

    # Code de sortie parlant : sans lui le lanceur (set -e + trap ERR) enchaîne
    # sur l'étape suivante et notifie « ✅ » même si tout a échoué.
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
