#!/usr/bin/env python3
"""
_common.py — Briques partagées par les scripts du pipeline perso-media.

Regroupe ce qui était copié-collé d'un script à l'autre : chargement de la
config (avec surcouche locale), fuseau des logs, garde-fou d'espace disque,
tailles lisibles, cache de scan ffprobe, exclusion des dossiers « _ » et des
fichiers temporaires, et les conversions de dates (le pipeline raisonne en
heure de Paris, ffmpeg écrit en UTC).

Utilisable aussi en ligne de commande par les lanceurs shell :
    python3 _common.py DRY_RUN     -> affiche la valeur effective
"""

import importlib.util
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")

# Marqueurs des fichiers de travail produits par les étapes 01/02. Ils portent
# l'extension de la bibliothèque (.mkv) : sans exclusion explicite, un run
# interrompu laisserait des orphelins qui seraient ensuite traités comme de
# vraies vidéos (converties, datées, compressées, uploadées).
TEMP_MARKERS = (".h265tmp", ".datetmp")


# ── Configuration ────────────────────────────────────────────────────────────
def load_config(script_file: str):
    """Charge le 00-config.py voisin, puis la surcouche 00-config.local.py.

    La surcouche (gitignorée, écrite par l'interface web) est exécutée dans le
    même espace de noms : elle écrase les valeurs de base sans toucher au
    fichier versionné. `_OVERLAY_PATH` est posé quand elle existe, pour que les
    scripts puissent le signaler dans leurs logs.
    """
    base = Path(script_file).resolve().with_name("00-config.py")
    spec = importlib.util.spec_from_file_location("pipeline_config", base)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    overlay = base.with_name("00-config.local.py")
    if overlay.is_file():
        exec(
            compile(overlay.read_text(encoding="utf-8"), str(overlay), "exec"),
            module.__dict__,
        )
        module._OVERLAY_PATH = str(overlay)
    return module


def resolve_dry_run(config) -> bool:
    """DRY_RUN effectif : PIPELINE_DRY_RUN (lanceur) prime sur la config."""
    env = os.environ.get("PIPELINE_DRY_RUN")
    if env is not None:
        return env == "1"
    return bool(config.DRY_RUN)


# ── Logs ─────────────────────────────────────────────────────────────────────
def use_paris_timezone() -> None:
    """Force l'heure de Paris dans les timestamps de log, quel que soit le
    fuseau du système/conteneur (sinon UTC -> décalage de 1-2 h)."""
    import logging

    try:
        logging.Formatter.converter = staticmethod(
            lambda ts: datetime.fromtimestamp(ts, PARIS).timetuple()
        )
    except Exception:  # noqa: BLE001 — base de fuseaux indisponible
        pass


# ── Système de fichiers ──────────────────────────────────────────────────────
def enough_space(target_dir: Path, needed: int) -> bool:
    """True s'il reste au moins `needed` octets libres sur le volume de target_dir.

    Indéterminé (erreur OS) -> True : on ne bloque pas une conversion par excès
    de prudence si l'espace libre n'a pas pu être lu.
    """
    try:
        return shutil.disk_usage(target_dir).free >= needed
    except OSError:
        return True


def human_size_bytes(size: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} To"


def is_temp_artifact(path: Path) -> bool:
    """Vrai pour un fichier de travail laissé par une étape interrompue."""
    return any(marker in path.name for marker in TEMP_MARKERS)


def in_excluded_folder(path: Path, root: Path) -> bool:
    """Vrai si le fichier est sous un dossier dont le nom commence par « _ ».

    Ces dossiers de travail/brouillon sont hors pipeline : ni conversion, ni
    écriture de date, ni compression, ni upload. Seuls les composants SOUS la
    racine sont testés (la racine elle-même est ignorée).
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return any(part.startswith("_") for part in rel.parts[:-1])


def purge_temp_artifacts(root: Path, dry_run: bool, logger) -> int:
    """Supprime les orphelins .h265tmp/.datetmp laissés par un run interrompu.

    Le bouton « Arrêter » de l'interface web envoie un SIGINT au groupe de
    processus : ffmpeg peut mourir en laissant un temporaire à demi écrit. On
    nettoie au démarrage plutôt que de le laisser polluer la bibliothèque.
    """
    orphans = [p for p in root.rglob("*") if p.is_file() and is_temp_artifact(p)]
    if not orphans:
        return 0
    logger.warning(
        "%d fichier(s) temporaire(s) d'un run précédent détecté(s) — nettoyage",
        len(orphans),
    )
    removed = 0
    for p in orphans:
        if dry_run:
            logger.warning("    [DRY RUN] supprimerait %s", p)
            removed += 1
            continue
        try:
            p.unlink()
            logger.warning("    supprimé : %s", p)
            removed += 1
        except OSError as e:
            logger.error("    suppression impossible : %s — %s", p, e)
    return removed


# ── Cache de scan ffprobe ────────────────────────────────────────────────────
def load_scan_cache(path: Path | None, version: int) -> dict:
    """Charge le cache de scan JSON. {} si absent, illisible ou d'une version
    antérieure (le schéma des entrées a changé -> on re-sonde)."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != version:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def save_scan_cache(path: Path | None, cache: dict, version: int) -> None:
    """Écrit le cache de scan (best-effort, atomique). Appelé périodiquement :
    un run de plusieurs heures interrompu ne doit pas perdre tout le scan."""
    if not path:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": version, "entries": cache}, f)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def prune_scan_cache(cache: dict, live_keys) -> int:
    """Retire les entrées dont le fichier n'existe plus (le cache ne rétrécit
    jamais sinon). Retourne le nombre d'entrées supprimées."""
    live = set(live_keys)
    stale = [k for k in cache if k not in live]
    for k in stale:
        del cache[k]
    return len(stale)


def cache_entry_valid(entry, st) -> bool:
    """Entrée de cache encore valable pour ce fichier (mtime + taille)."""
    return bool(
        entry
        and entry.get("mtime") == int(st.st_mtime)
        and entry.get("size") == st.st_size
    )


# ── Dates ────────────────────────────────────────────────────────────────────
def parse_any_date(value) -> datetime | None:
    """Parse une date (EXIF, ISO 8601, sortie ffprobe) en datetime NAÏF exprimé
    en heure de Paris.

    Le pipeline raisonne partout en heure locale (c'est ce qu'attend l'EXIF et
    ce que Google Photos affiche). Un horodatage porteur d'un fuseau ('Z' ou
    '±hh:mm' — ce qu'écrit ffmpeg dans les MKV/MP4) est donc CONVERTI vers
    Paris, et non simplement tronqué : sans ça une vidéo se retrouve décalée de
    1 à 2 h dans la chronologie.
    """
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    s = str(value).strip()
    if not s:
        return None

    # Le suffixe de fuseau se détecte AVANT de retirer la fraction de seconde :
    # sur "2023-06-01T10:00:00.000000Z", un split('.') mangerait le 'Z' et la
    # date serait prise pour de l'heure locale (décalage de 1-2 h).
    core = s.replace("T", " ").strip()
    offset = None  # décalage du fuseau porté par la chaîne, en minutes
    if core.endswith(("Z", "z")):
        core = core[:-1].strip()
        offset = 0
    elif len(core) >= 6 and core[-6] in "+-" and core[-3] == ":":
        sign = 1 if core[-6] == "+" else -1
        try:
            offset = sign * (int(core[-5:-3]) * 60 + int(core[-2:]))
        except ValueError:
            offset = None
        core = core[:-6].strip()
    core = core.split(".")[0].strip()  # fraction de seconde

    dt = None
    for candidate in (core, core[:19]):
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y:%m:%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y:%m:%d",
        ):
            try:
                dt = datetime.strptime(candidate, fmt)
                break
            except ValueError:
                continue
        if dt:
            break
    if dt is None:
        return None

    if offset is not None:
        aware = dt.replace(tzinfo=timezone.utc) - _minutes(offset)
        return aware.astimezone(PARIS).replace(tzinfo=None)
    return dt  # déjà une heure locale (EXIF)


def _minutes(n: int):
    from datetime import timedelta

    return timedelta(minutes=n)


def to_utc_iso(dt_local: datetime) -> str:
    """Convertit un datetime naïf (heure de Paris) en ISO 8601 UTC suffixé 'Z'.

    C'est la forme que ffmpeg stocke telle quelle dans `creation_time` et que
    Google Photos interprète correctement. Écrire une heure locale sans fuseau
    la ferait relire comme de l'UTC -> décalage de 1 à 2 h.
    """
    return (
        dt_local.replace(tzinfo=PARIS)
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    )


def is_plausible_year(dt: datetime, config) -> bool:
    """Écarte les dates epoch (1904/1970) et les valeurs aberrantes."""
    return config.MIN_PLAUSIBLE_YEAR <= dt.year <= config.MAX_PLAUSIBLE_YEAR


if __name__ == "__main__":
    # Lecture d'une clé de config depuis les lanceurs shell (surcouche incluse).
    cfg = load_config(__file__)
    print(getattr(cfg, sys.argv[1]))
