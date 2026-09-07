##################################################################
## slim down audio tracks of movies already encoded in H.265
##   les pistes lossless (TrueHD, DTS-HD MA, PCM) pèsent souvent
##   PLUS que la vidéo. On les ré-encode en EAC3/Opus ; l'image
##   est copiée bit à bit, jamais ré-encodée.
##################################################################

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402

##################################################################
## Configuration : tout est dans 00-config.py (COMMUN + AUDIO_*)
##################################################################

config = _common.load_config(__file__)

ROOTS = config.AUDIO_FOLDERS
DRY_RUN = _common.resolve_dry_run(config)
MAX_BITRATE = config.AUDIO_MAX_BITRATE  # Mb/s ; 0/None = étape désactivée
TARGET_CODEC = config.AUDIO_TARGET_CODEC
TARGET_KBPS = int(config.AUDIO_TARGET_BITRATE_KBPS)
MAX_CHANNELS = config.AUDIO_MAX_CHANNELS
DROP_DUPLICATE_LANGUAGES = bool(config.AUDIO_DROP_DUPLICATE_LANGUAGES)
EXTENSIONS = {e.lower() for e in config.AUDIO_EXTENSIONS}
CACHE_PATH = (
    Path(__file__).with_name(config.AUDIO_SCAN_CACHE)
    if getattr(config, "AUDIO_SCAN_CACHE", None)
    else None
)
SCAN_WORKERS = int(getattr(config, "AUDIO_SCAN_WORKERS", 1))

CACHE_VERSION = 1
CACHE_FLUSH_EVERY = 50
# Fenêtre échantillonnée quand le débit d'une piste n'est pas déclaré (TrueHD,
# DTS-HD, PCM ne l'annoncent pas) : on somme les paquets sur SAMPLE_SECONDS.
SAMPLE_START = 600
SAMPLE_SECONDS = 30

##################################################################

_USE_COLOR = sys.stdout.isatty() or os.environ.get("FORCE_COLOR") == "1"
GREEN = "\033[92m" if _USE_COLOR else ""
RED = "\033[91m" if _USE_COLOR else ""
YELLOW = "\033[93m" if _USE_COLOR else ""
RESET = "\033[0m" if _USE_COLOR else ""


def log(msg: str = "") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for line in str(msg).split("\n"):
        colored = line
        if "[✗]" in line:
            colored = f"{RED}{line}{RESET}"
        elif "[✓]" in line:
            colored = f"{GREEN}{line}{RESET}"
        elif "[!]" in line:
            colored = f"{YELLOW}{line}{RESET}"
        print(f"{ts} | {colored}")


def human_gb(size: float) -> str:
    return f"{size / 1024 ** 3:.2f} Go"


# ── Décision : que faire de chaque piste ? ───────────────────────────────────
def target_bitrate_kbps(channels: int) -> int:
    """Débit cible d'une piste ré-encodée, selon son nombre de canaux.

    TARGET_KBPS vise le 5.1/7.1 ; en dessous on réduit à 112 kb/s par canal,
    inutile de dépenser 640 kb/s pour une piste stéréo.
    """
    return min(TARGET_KBPS, 112 * max(1, channels))


def plan_audio_actions(
    tracks: list,
    max_bitrate=MAX_BITRATE,
    drop_duplicate_languages=DROP_DUPLICATE_LANGUAGES,
    max_channels=MAX_CHANNELS,
) -> list:
    """Action à appliquer à chaque piste audio : copy / transcode / drop.

    `tracks` : dicts {index (relatif audio), codec, channels, language, bitrate}
    (bitrate en Mb/s, None si indéterminable).

    Règles, dans l'ordre :
      1. doublons de langue (optionnel, désactivé par défaut) — deux pistes
         d'une même langue sont souvent DEUX DOUBLAGES DIFFÉRENTS, pas un
         doublon : on ne supprime que sur demande explicite ;
      2. toute piste au-dessus du plafond est ré-encodée (c'est là que sont
         les TrueHD/DTS-HD, qui pèsent jusqu'à deux tiers du fichier) ;
      3. un débit indéterminé est traité comme « sous le plafond » : on ne
         touche pas à une piste qu'on n'a pas su mesurer.

    Garde-fou : un plan qui ne laisserait AUCUNE piste est annulé (tout en
    copie). Un film muet n'est jamais le résultat attendu.
    """
    actions = []
    seen_languages = {}
    for track in tracks:
        index = track["index"]
        channels = track.get("channels") or 2
        language = (track.get("language") or "und").lower()
        bitrate = track.get("bitrate")

        if drop_duplicate_languages and language != "und":
            # Meilleure piste de la langue = le plus de canaux, puis le plus
            # léger à canaux égaux.
            rank = (-channels, bitrate if bitrate is not None else 0)
            previous = seen_languages.get(language)
            if previous is None or rank < previous[0]:
                if previous is not None:
                    actions[previous[1]]["action"] = "drop"
                    actions[previous[1]]["reason"] = f"doublon {language}"
                seen_languages[language] = (rank, len(actions))
            else:
                actions.append(
                    {"index": index, "action": "drop", "reason": f"doublon {language}"}
                )
                continue

        if max_bitrate and bitrate is not None and bitrate > max_bitrate:
            out_channels = channels
            if max_channels and channels > max_channels:
                out_channels = max_channels
            actions.append(
                {
                    "index": index,
                    "action": "transcode",
                    "channels": out_channels,
                    "kbps": target_bitrate_kbps(out_channels),
                    "reason": f"{bitrate:.2f} Mb/s > {max_bitrate} Mb/s",
                }
            )
        else:
            actions.append({"index": index, "action": "copy", "reason": ""})

    if all(a["action"] == "drop" for a in actions) and actions:
        for a in actions:
            a["action"] = "copy"
            a["reason"] = "annulé : ne laisserait aucune piste"
    return actions


def estimated_saving(tracks: list, actions: list, duration: float) -> float:
    """Octets économisés par le plan (estimation, pour le bilan du dry-run)."""
    by_index = {t["index"]: t for t in tracks}
    saved = 0.0
    for action in actions:
        track = by_index.get(action["index"], {})
        rate = track.get("bitrate")
        if rate is None or not duration:
            continue
        if action["action"] == "drop":
            saved += rate * 1e6 * duration / 8
        elif action["action"] == "transcode":
            new_rate = action["kbps"] / 1000
            saved += max(0.0, (rate - new_rate)) * 1e6 * duration / 8
    return saved


# ── Sondage ffprobe ──────────────────────────────────────────────────────────
def ffprobe_json(args: list):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", *args]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return json.loads(out.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def measure_track_bitrate(path: Path, audio_index: int):
    """Débit réel d'une piste, en Mb/s, par somme des paquets sur une fenêtre.

    Nécessaire pour TrueHD / DTS-HD MA / PCM, qui ne DÉCLARENT pas leur débit —
    précisément les pistes les plus lourdes. Sans ça, elles passeraient pour
    gratuites et l'étape ne servirait à rien.
    """
    data = ffprobe_json(
        [
            "-select_streams",
            f"a:{audio_index}",
            "-read_intervals",
            f"{SAMPLE_START}%+{SAMPLE_SECONDS}",
            "-show_entries",
            "packet=size",
            str(path),
        ]
    )
    packets = data.get("packets") or []
    total = sum(int(p.get("size", 0)) for p in packets)
    if not total:
        return None
    return total * 8 / SAMPLE_SECONDS / 1e6


def probe_audio(path: Path) -> dict:
    """{duration, subtitles, video, tracks[]} — débit déclaré, sinon mesuré."""
    data = ffprobe_json(["-show_streams", "-show_format", str(path)])
    streams = data.get("streams") or []
    video = None
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = None

    tracks = []
    subtitles = 0
    audio_index = 0
    for stream in streams:
        kind = stream.get("codec_type")
        if kind == "subtitle":
            subtitles += 1
            continue
        if kind == "video" and video is None:
            # Signature de la piste vidéo : elle est copiée telle quelle, donc
            # elle DOIT être identique en sortie. C'est le contrôle qui garantit
            # qu'aucun ré-encodage d'image n'a eu lieu par accident.
            video = {
                "codec": stream.get("codec_name"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "pix_fmt": stream.get("pix_fmt"),
            }
            continue
        if kind != "audio":
            continue
        declared = stream.get("bit_rate")
        try:
            bitrate = float(declared) / 1e6 if declared else None
        except ValueError:
            bitrate = None
        if bitrate is None:
            bitrate = measure_track_bitrate(path, audio_index)
        tags = stream.get("tags") or {}
        tracks.append(
            {
                "index": audio_index,
                "codec": stream.get("codec_name"),
                "channels": stream.get("channels"),
                "language": tags.get("language"),
                "title": tags.get("title"),
                "bitrate": bitrate,
            }
        )
        audio_index += 1
    return {
        "duration": duration,
        "subtitles": subtitles,
        "video": video,
        "tracks": tracks,
    }


def audio_cache_entry(st, info: dict) -> dict:
    """Entrée de cache — une seule définition pour tous les chemins d'écriture."""
    return {
        "mtime": int(st.st_mtime),
        "size": st.st_size,
        "duration": info["duration"],
        "subtitles": info["subtitles"],
        "video": info.get("video"),
        "tracks": info["tracks"],
    }


def probe_with_cache(path: Path, cache: dict) -> dict:
    try:
        st = path.stat()
    except OSError:
        return probe_audio(path)
    entry = cache.get(str(path))
    if _common.cache_entry_valid(entry, st):
        return {
            "duration": entry.get("duration"),
            "subtitles": entry.get("subtitles", 0),
            "video": entry.get("video"),
            "tracks": entry.get("tracks", []),
        }
    info = probe_audio(path)
    cache[str(path)] = audio_cache_entry(st, info)
    return info


def prewarm(paths: list, cache: dict) -> None:
    """Pré-sonde en parallèle les fichiers absents du cache (I/O-bound)."""
    if SCAN_WORKERS <= 1 or not paths:
        return
    misses = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        if not _common.cache_entry_valid(cache.get(str(path)), st):
            misses.append((path, st))
    if not misses:
        return
    log(f"[*] Sondage de {len(misses)} fichier(s) ({SCAN_WORKERS} en parallèle)…")

    def work(item):
        path, st = item
        return str(path), st, probe_audio(path)

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        for i, (key, st, info) in enumerate(pool.map(work, misses), 1):
            cache[key] = audio_cache_entry(st, info)
            if i % CACHE_FLUSH_EVERY == 0:
                _common.save_scan_cache(CACHE_PATH, cache, CACHE_VERSION)
    _common.save_scan_cache(CACHE_PATH, cache, CACHE_VERSION)


# ── Remux ────────────────────────────────────────────────────────────────────
def build_command(src: Path, dst: Path, actions: list) -> list:
    """Commande ffmpeg du remux : VIDÉO COPIÉE, seules les pistes visées bougent."""
    cmd = ["ffmpeg", "-v", "error", "-stats", "-y", "-i", str(src), "-map", "0:v"]
    out_index = 0
    codec_args = []
    for action in actions:
        if action["action"] == "drop":
            continue
        cmd += ["-map", f"0:a:{action['index']}"]
        if action["action"] == "transcode":
            codec_args += [f"-c:a:{out_index}", TARGET_CODEC]
            codec_args += [f"-b:a:{out_index}", f"{action['kbps']}k"]
            if action.get("channels"):
                codec_args += [f"-ac:a:{out_index}", str(action["channels"])]
        else:
            codec_args += [f"-c:a:{out_index}", "copy"]
        out_index += 1
    cmd += ["-map", "0:s?", "-map", "0:t?", "-c:v", "copy", "-c:s", "copy"]
    cmd += codec_args
    cmd += ["-map_metadata", "0", "-map_chapters", "0", str(dst)]
    return cmd


def slim_file(path: Path, info: dict, actions: list) -> str:
    """Applique le plan. Retourne 'ok' | 'skip' | 'error'."""
    kept = [a for a in actions if a["action"] != "drop"]
    tmp = path.with_name(path.stem + ".audiotmp" + path.suffix)
    old_size = path.stat().st_size

    if DRY_RUN:
        return "ok"

    if not _common.enough_space(path.parent, old_size):
        log("    [!] Espace disque insuffisant — fichier laissé tel quel")
        return "skip"

    cmd = build_command(path, tmp, actions)
    try:
        subprocess.run(cmd, check=True, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as exc:
        log("    [✗] Échec ffmpeg :")
        for line in (exc.stderr or "").strip().splitlines()[-10:]:
            log(f"        ffmpeg | {line}")
        tmp.unlink(missing_ok=True)
        return "error"
    except OSError as exc:
        log(f"    [✗] {exc}")
        tmp.unlink(missing_ok=True)
        return "error"

    # Vérifications AVANT de remplacer l'original : nombre de pistes, durée,
    # et gain réel. Même exigence que 02 — un remux raté ne doit jamais
    # écraser la source.
    out = probe_audio(tmp)

    # L'IMAGE d'abord : la vidéo est copiée (-c:v copy), sa signature doit être
    # rigoureusement identique. Le moindre écart (codec, dimensions, format de
    # pixels) signifie qu'un ré-encodage a eu lieu — on n'écrase pas.
    if info.get("video") and out.get("video") != info["video"]:
        log(
            f"    [✗] Signature vidéo modifiée : {info['video']} → "
            f"{out.get('video')} — original conservé"
        )
        tmp.unlink(missing_ok=True)
        return "error"

    # Les LANGUES ensuite : perdre une VF ou une VO serait irréversible et
    # passerait inaperçu avec un simple comptage de pistes.
    by_index = {t["index"]: t for t in info["tracks"]}
    expected_langs = [
        (by_index[a["index"]].get("language") or "und") for a in kept
    ]
    out_langs = [(t.get("language") or "und") for t in out["tracks"]]
    if not languages_preserved(expected_langs, out_langs):
        log(
            f"    [✗] Langues divergentes : attendu {expected_langs}, "
            f"obtenu {out_langs} — original conservé"
        )
        tmp.unlink(missing_ok=True)
        return "error"

    if len(out["tracks"]) != len(kept):
        log(
            f"    [✗] {len(out['tracks'])} piste(s) audio en sortie, "
            f"{len(kept)} attendue(s) — original conservé"
        )
        tmp.unlink(missing_ok=True)
        return "error"
    if out["subtitles"] != info["subtitles"]:
        log(
            f"    [✗] {out['subtitles']} sous-titre(s) en sortie, "
            f"{info['subtitles']} attendu(s) — original conservé"
        )
        tmp.unlink(missing_ok=True)
        return "error"
    in_dur, out_dur = info.get("duration"), out.get("duration")
    if in_dur:
        if out_dur is None:
            log("    [✗] Durée de sortie illisible — original conservé")
            tmp.unlink(missing_ok=True)
            return "error"
        if abs(in_dur - out_dur) > max(1.0, 0.01 * in_dur):
            log(
                f"    [✗] Durée divergente ({in_dur:.0f}s → {out_dur:.0f}s) — "
                "original conservé"
            )
            tmp.unlink(missing_ok=True)
            return "error"

    new_size = tmp.stat().st_size
    if new_size >= old_size:
        log(
            f"    [!] Sortie plus grosse ({human_gb(new_size)} ≥ "
            f"{human_gb(old_size)}) — original conservé"
        )
        tmp.unlink(missing_ok=True)
        return "skip"

    try:
        os.replace(tmp, path)
    except OSError as exc:
        log(f"    [✗] Remplacement impossible : {exc}")
        tmp.unlink(missing_ok=True)
        return "error"
    log(
        f"    [✓] {human_gb(old_size)} → {human_gb(new_size)} "
        f"(−{(old_size - new_size) * 100 / old_size:.1f} %)"
    )
    return "ok"


def collect(roots) -> list:
    files = []
    for root in roots:
        for dirpath, _dirs, names in os.walk(root):
            for name in sorted(names):
                path = Path(dirpath) / name
                if path.suffix.lower() in EXTENSIONS and ".audiotmp" not in path.name:
                    files.append(path)
    return sorted(files)


def purge_temps(roots) -> None:
    """Supprime les .audiotmp orphelins d'un run interrompu : ils portent
    l'extension de la bibliothèque et seraient pris pour de vrais films."""
    for root in roots:
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                if ".audiotmp" in name:
                    orphan = Path(dirpath) / name
                    log(f"[!] Temporaire orphelin supprimé : {orphan.name}")
                    if not DRY_RUN:
                        orphan.unlink(missing_ok=True)


def languages_preserved(expected: list, obtained: list) -> bool:
    """Aucune langue CONNUE n'a été perdue ni remplacée.

    Une piste non étiquetée en entrée (« und ») accepte n'importe quelle
    étiquette en sortie : ffmpeg en pose une au remux quand le conteneur
    d'origine n'en portait pas, et passer de « inconnue » à « fre » n'est pas
    une perte. Exiger l'égalité stricte faisait échouer des remux parfaitement
    valides (vécu sur « Les Rois Mages », piste DTS-HD sans étiquette).
    """
    if len(expected) != len(obtained):
        return False
    return all(
        want == "und" or want == got for want, got in zip(expected, obtained)
    )


def describe(track: dict, action: dict) -> str:
    rate = track.get("bitrate")
    rate_txt = f"{rate:.2f} Mb/s" if rate is not None else "débit inconnu"
    label = f"{track.get('codec')} {track.get('channels')}ch " \
            f"[{track.get('language') or 'und'}] {rate_txt}"
    if action["action"] == "copy":
        return f"      = {label}"
    if action["action"] == "drop":
        return f"      ✗ {label} → supprimée ({action['reason']})"
    return (
        f"      ↓ {label} → {TARGET_CODEC} {action['channels']}ch "
        f"{action['kbps']}k ({action['reason']})"
    )


def main() -> int:
    if not MAX_BITRATE:
        log("[!] AUDIO_MAX_BITRATE vaut 0/None : étape désactivée, rien à faire.")
        return 0

    mode = "DRY RUN (simulation)" if DRY_RUN else "REMUX RÉEL"
    log("=" * 64)
    log("04 — allègement des pistes audio (la vidéo n'est jamais ré-encodée)")
    log(f"Mode     : {mode}")
    log(f"Dossiers : {ROOTS}")
    log(f"Plafond  : {MAX_BITRATE} Mb/s → {TARGET_CODEC} {TARGET_KBPS}k max")
    if MAX_CHANNELS:
        log(f"Canaux   : {MAX_CHANNELS} max (l'encodeur eac3 ne va pas au-delà)")
    if getattr(config, "_OVERLAY_PATH", None):
        log(f"Config   : surcouche active — {config._OVERLAY_PATH}")
    log("=" * 64)

    purge_temps(ROOTS)
    cache = _common.load_scan_cache(CACHE_PATH, CACHE_VERSION)
    files = collect(ROOTS)
    log(f"{len(files)} fichier(s) à examiner.")
    prewarm(files, cache)

    touched = skipped = errors = 0
    saved_estimate = 0.0
    for i, path in enumerate(files, 1):
        info = probe_with_cache(path, cache)
        tracks = info["tracks"]
        if not tracks:
            continue
        actions = plan_audio_actions(tracks)
        if all(a["action"] == "copy" for a in actions):
            continue

        log(f"\n[{i}/{len(files)}] {path.name}")
        by_index = {t["index"]: t for t in tracks}
        for action in actions:
            log(describe(by_index[action["index"]], action))
        gain = estimated_saving(tracks, actions, info.get("duration") or 0)
        if DRY_RUN:
            log(f"    [DRY RUN] gain estimé : {human_gb(gain)}")
        result = slim_file(path, info, actions)
        if result == "ok":
            touched += 1
            saved_estimate += gain
        elif result == "skip":
            skipped += 1
        else:
            errors += 1

    _common.save_scan_cache(CACHE_PATH, cache, CACHE_VERSION)

    log("\n" + "=" * 64)
    log("BILAN FINAL")
    log(f"  Fichiers examinés : {len(files)}")
    log(f"  Allégés           : {touched}")
    log(f"  Ignorés           : {skipped}  (sortie plus grosse, ou disque plein)")
    log(f"  Erreurs           : {errors}")
    label = "estimé" if DRY_RUN else "réalisé"
    log(f"  Gain {label}       : {human_gb(saved_estimate)}")
    if DRY_RUN:
        log("  → Relance en mode réel (--real / interface web) pour appliquer.")
    log("=" * 64)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
