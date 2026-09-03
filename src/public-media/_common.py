#!/usr/bin/env python3
"""
_common.py — Briques partagées par les scripts du pipeline public-media.

Regroupe ce qui était copié-collé entre 01 et 02 : chargement de la config
(avec surcouche locale), garde-fou d'espace disque, tailles lisibles et cache
de scan ffprobe.

Chaque pipeline garde son propre _common.py : les conteneurs ne montent qu'un
seul dossier (`src/public-media` ou `src/perso-media`) sur /work, un module
commun à la racine de src/ n'y serait pas visible.

Utilisable aussi en ligne de commande par le lanceur shell :
    python3 _common.py NAS_MOUNT   -> affiche la valeur effective
"""

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path


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


if __name__ == "__main__":
    cfg = load_config(__file__)
    print(getattr(cfg, sys.argv[1]))
