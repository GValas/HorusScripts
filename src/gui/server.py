#!/usr/bin/env python3
"""
server.py — Interface web locale pour lancer et piloter les pipelines horus.

Stdlib uniquement (aucune dépendance pip sur l'hôte, comme le reste du repo) :
  - sert une page unique (index.html) ;
  - lance run-public-media-pipeline.sh / run-perso-media-pipeline.sh en
    sous-processus (les lanceurs restent la SEULE logique métier) ;
  - streame les logs en direct (Server-Sent Events) ;
  - permet d'arrêter un run (SIGINT au groupe -> escalade SIGKILL) ;
  - lit les réglages whitelistés des deux 00-config.py et écrit les valeurs
    modifiées dans une SURCOUCHE 00-config.local.py (gitignorée).

Un seul run à la fois (les pipelines pilotent le GPU/Docker en série).

Usage : python3 src/gui/server.py [PORT] [HOST]   (défaut 8765, 0.0.0.0)
        …ou via ./run-gui.sh
"""

import collections
import http.server
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

GUI_DIR = Path(__file__).resolve().parent
ROOT = GUI_DIR.parent.parent  # …/HorusScripts

# ──────────────────────────────────────────────────────────────────────────────
# Définition des pipelines : lanceur, config éditable, étapes.
# type ∈ {bool, int, float, str, str_or_none, list, choice}
# Une étape s'écrit [id, description] ou [id, description, cochée_par_défaut].
# Un champ marqué "secret": True s'affiche masqué (clés d'API).
# ──────────────────────────────────────────────────────────────────────────────
PRESET_CHOICES = ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]

PIPELINES = {
    "perso": {
        "label": "Perso (photos/vidéos → Google Photos)",
        "launcher": "run-perso-media-pipeline.sh",
        "config": "src/perso-media/00-config.py",
        "steps": [
            ["01", "convert — tout en H.265 + MKV, HEIC→JPG"],
            ["02", "enrich — complète les dates manquantes"],
            ["03", "compress — copies 2048/720p → output/gphotos"],
            ["04", "upload — output/gphotos → Google Photos (rclone)"],
        ],
        "fields": [
            {
                "key": "DRY_RUN",
                "type": "bool",
                "label": "Simulation par défaut (DRY_RUN)",
                "help": "Défaut du pipeline ; le mode choisi au lancement le surcharge.",
            },
            {
                "key": "PHOTOS_SRC",
                "type": "str",
                "label": "Dossier source (PHOTOS_SRC)",
                "help": "Racine scannée par 01/02/03 (NAS ou dossier Windows en /mnt/c/…).",
            },
            {
                "key": "VIDEO_CQ",
                "type": "int",
                "label": "Qualité NVENC vidéo (VIDEO_CQ)",
                "help": "Plus bas = meilleure qualité (24–30 conseillé).",
            },
            {
                "key": "VIDEO_PRESET",
                "type": "choice",
                "choices": PRESET_CHOICES,
                "label": "Préréglage NVENC (VIDEO_PRESET)",
                "help": "p1 (rapide) → p7 (qualité).",
            },
            {
                "key": "CONVERT_SCAN_WORKERS",
                "type": "int",
                "label": "Threads scan ffprobe (CONVERT_SCAN_WORKERS)",
            },
            {
                "key": "COMPRESS_MAX_PHOTO_SIZE",
                "type": "int",
                "label": "Côté max photo, px (COMPRESS_MAX_PHOTO_SIZE)",
            },
            {
                "key": "COMPRESS_VIDEO_HEIGHT",
                "type": "int",
                "label": "Hauteur vidéo compressée, px (COMPRESS_VIDEO_HEIGHT)",
            },
            {
                "key": "COMPRESS_JPEG_QUALITY",
                "type": "int",
                "label": "Qualité JPEG (COMPRESS_JPEG_QUALITY)",
            },
            {
                "key": "COMPRESS_PHOTO_WORKERS",
                "type": "int",
                "label": "Threads compression photo (COMPRESS_PHOTO_WORKERS)",
            },
            {
                "key": "UPLOAD_TRANSFERS",
                "type": "int",
                "label": "Uploads parallèles (UPLOAD_TRANSFERS)",
            },
            {
                "key": "UPLOAD_TPSLIMIT",
                "type": "int",
                "label": "Plafond requêtes/s (UPLOAD_TPSLIMIT)",
            },
            {
                "key": "UPLOAD_RETRIES",
                "type": "int",
                "label": "Passes rclone (UPLOAD_RETRIES)",
            },
            {
                "key": "NOTIFY_WEBHOOK",
                "type": "str_or_none",
                "label": "Webhook de fin de run (NOTIFY_WEBHOOK)",
                "help": "URL ntfy/webhook, ou vide pour aucune notification.",
            },
        ],
    },
    "public": {
        "label": "Public (films → noms propres, HEVC, identification en ligne)",
        "launcher": "run-public-media-pipeline.sh",
        "config": "src/public-media/00-config.py",
        "steps": [
            ["01", "clean — nettoie les noms (jetons techniques)"],
            ["02", "convert — ré-encode en HEVC (supprime les originaux)"],
            [
                "03",
                "identify — identifie les films en ligne et les renomme "
                "(OpenSubtitles + TMDB)",
                False,
            ],
        ],
        "fields": [
            {
                "key": "DRY_RUN",
                "type": "bool",
                "label": "Simulation par défaut (DRY_RUN)",
                "help": "Défaut du pipeline ; le mode choisi au lancement le surcharge. "
                "02 SUPPRIME les originaux en mode réel.",
            },
            {
                "key": "NAS_MOUNT",
                "type": "str",
                "label": "Racine NAS (NAS_MOUNT)",
                "help": "/mnt/wsl/horus (jamais /mnt/horus) — ou un /mnt/c/… Windows.",
            },
            {
                "key": "INPUT_FOLDERS",
                "type": "list",
                "label": "Dossiers à traiter (INPUT_FOLDERS)",
                "help": "Un chemin par ligne (chemins absolus).",
            },
            {
                "key": "CONVERT_CQ",
                "type": "int",
                "label": "Qualité NVENC (CONVERT_CQ)",
                "help": "Plus bas = meilleure qualité (24–28 conseillé).",
            },
            {
                "key": "CONVERT_PRESET",
                "type": "choice",
                "choices": PRESET_CHOICES,
                "label": "Préréglage NVENC (CONVERT_PRESET)",
                "help": "p1 (rapide) → p7 (qualité).",
            },
            {
                "key": "CONVERT_MAX_RESOLUTION",
                "type": "str_or_none",
                "label": "Downscale max (CONVERT_MAX_RESOLUTION)",
                "help": "480p / 720p / 1080p / 1440p / 2160p / 4k, ou vide pour aucun.",
            },
            {
                "key": "CONVERT_MAX_BITRATE",
                "type": "float",
                "label": "Débit max, Mb/s (CONVERT_MAX_BITRATE)",
                "help": "Ré-encode tout fichier au-dessus, même déjà en HEVC — "
                "le seul réglage qui vise la taille. 0 = désactivé.",
            },
            {
                "key": "NOTIFY_WEBHOOK",
                "type": "str_or_none",
                "label": "Webhook de fin de run (NOTIFY_WEBHOOK)",
                "help": "URL ntfy/webhook, ou vide pour aucune notification.",
            },
            # ── Étape 03 : identification en ligne des films ──────────────
            {
                "key": "IDENTIFY_PATTERN",
                "type": "str",
                "label": "Motif de renommage (IDENTIFY_PATTERN)",
                "help": "Champs : {annee} (ou {yyyy}), {titre}, {titre_vo}, "
                "{ext}. {ext} est obligatoire.",
            },
            {
                "key": "IDENTIFY_FOLDERS",
                "type": "list",
                "label": "Dossiers de films à identifier (IDENTIFY_FOLDERS)",
                "help": "Un chemin par ligne. FILMS UNIQUEMENT (le motif n'a "
                "pas de sens pour une série).",
            },
            {
                "key": "IDENTIFY_OPENSUBTITLES_API_KEY",
                "type": "str_or_none",
                "secret": True,
                "label": "Clé API OpenSubtitles",
                "help": "opensubtitles.com/consumers — identification par "
                "empreinte du fichier. Vide = repli sur la recherche par titre.",
            },
            {
                "key": "IDENTIFY_TMDB_API_KEY",
                "type": "str_or_none",
                "secret": True,
                "label": "Clé API TMDB (v3)",
                "help": "themoviedb.org/settings/api — titre localisé et année. "
                "Sans elle, seule l'identification par empreinte fonctionne.",
            },
            {
                "key": "IDENTIFY_LANGUAGE",
                "type": "str",
                "label": "Langue des titres (IDENTIFY_LANGUAGE)",
                "help": "Code TMDB, ex. fr-FR ou en-US.",
            },
            {
                "key": "IDENTIFY_SPACE_REPLACEMENT",
                "type": "str_or_none",
                "label": "Remplacement des espaces (IDENTIFY_SPACE_REPLACEMENT)",
                "help": "« . » pour Le.Prénom ; vide pour garder les espaces.",
            },
            {
                "key": "IDENTIFY_FALLBACK_TITLE_SEARCH",
                "type": "bool",
                "label": "Repli recherche par titre (IDENTIFY_FALLBACK_TITLE_SEARCH)",
                "help": "Indispensable après 02 : le ré-encodage change "
                "l'empreinte, le fichier n'est plus reconnu par hash.",
            },
            {
                "key": "IDENTIFY_RENAME_SUBTITLES",
                "type": "bool",
                "label": "Renommer aussi les sous-titres (IDENTIFY_RENAME_SUBTITLES)",
            },
            {
                "key": "IDENTIFY_RENAME_FOLDER",
                "type": "bool",
                "label": "Renommer le dossier du film (IDENTIFY_RENAME_FOLDER)",
                "help": "Seulement s'il ne contient qu'un seul film.",
            },
            {
                "key": "IDENTIFY_REQUEST_DELAY",
                "type": "float",
                "label": "Délai entre appels d'API, s (IDENTIFY_REQUEST_DELAY)",
            },
            {
                "key": "IDENTIFY_MAX_FILES",
                "type": "int",
                "label": "Films max par run (IDENTIFY_MAX_FILES)",
                "help": "0 = illimité. Utile pour tester sans brûler le quota.",
            },
        ],
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# Lecture / écriture de config : importlib pour lire (00-config.py + surcouche),
# génération complète de 00-config.local.py pour écrire.
# ──────────────────────────────────────────────────────────────────────────────
def _config_path(pipeline):
    return ROOT / PIPELINES[pipeline]["config"]


def _overlay_path(pipeline):
    """Surcouche locale (gitignorée) où l'interface écrit ses modifications."""
    return _config_path(pipeline).with_name("00-config.local.py")


def load_config(pipeline):
    """Valeurs EFFECTIVES des champs whitelistés : 00-config.py + surcouche.

    Même ordre de chargement que les scripts et les lanceurs (_common.load_config)
    pour que l'interface affiche exactement ce qui sera exécuté.
    """
    path = _config_path(pipeline)
    spec = importlib.util.spec_from_file_location("horus_cfg", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    overlay = _overlay_path(pipeline)
    if overlay.is_file():
        exec(
            compile(overlay.read_text(encoding="utf-8"), str(overlay), "exec"),
            module.__dict__,
        )
    out = {}
    for field in PIPELINES[pipeline]["fields"]:
        out[field["key"]] = getattr(module, field["key"], None)
    return module, out


def coerce(field, raw):
    """Convertit une valeur reçue du navigateur vers son type Python."""
    t = field["type"]
    if t == "bool":
        return bool(raw)
    if t == "int":
        return int(raw)
    if t == "float":
        return float(raw)
    if t in ("str", "choice"):
        return str(raw)
    if t == "str_or_none":
        s = str(raw).strip() if raw is not None else ""
        return s or None
    if t == "list":
        if isinstance(raw, list):
            items = raw
        else:
            items = str(raw).splitlines()
        return [s.strip() for s in items if s.strip()]
    raise ValueError(f"type inconnu: {t}")


def _literal(field, value):
    t = field["type"]
    if t == "bool":
        return "True" if value else "False"
    if t == "int":
        return str(int(value))
    if t == "float":
        return repr(float(value))
    if t in ("str", "choice"):
        return json.dumps(value, ensure_ascii=False)
    if t == "str_or_none":
        return "None" if value is None else json.dumps(value, ensure_ascii=False)
    if t == "list":
        if not value:
            return "[]"
        items = "\n".join(f"    {json.dumps(v, ensure_ascii=False)}," for v in value)
        return "[\n" + items + "\n]"
    raise ValueError(f"littéral non géré: {t}")


OVERLAY_HEADER = """\
# 00-config.local.py — SURCOUCHE LOCALE, ENTIÈREMENT GÉNÉRÉE.
#
# Écrit par l'interface web (src/gui). Chargé APRÈS 00-config.py par les scripts
# et les lanceurs : les valeurs ci-dessous ÉCRASENT celles du fichier versionné.
# Gitignoré — c'est ici que vivent les réglages volatils (dossier en cours de
# traitement, DRY_RUN du moment), pour que 00-config.py ne bouge plus à chaque
# lancement et reste lisible dans git.
#
# Toute modification manuelle sera écrasée au prochain enregistrement depuis
# l'interface. Supprimer ce fichier suffit à revenir aux valeurs de 00-config.py.
"""


def write_overlay(pipeline, payload):
    """Écrit la surcouche 00-config.local.py à partir des champs whitelistés.

    On n'édite plus 00-config.py : le réécrire à chaque lancement produisait du
    bruit permanent dans git, et la réécriture par expression régulière abîmait
    les valeurs contenant un « # » (pris pour un début de commentaire).
    """
    fields = {f["key"]: f for f in PIPELINES[pipeline]["fields"]}
    for key in payload:
        if key not in fields:
            raise ValueError(f"clé non éditable: {key}")

    # On repart des valeurs effectives courantes pour que la surcouche reste
    # complète même si le formulaire n'envoie qu'une partie des champs.
    _, current = load_config(pipeline)
    values = dict(current)
    for key, raw in payload.items():
        values[key] = coerce(fields[key], raw)

    lines = [OVERLAY_HEADER]
    for field in PIPELINES[pipeline]["fields"]:
        key = field["key"]
        lines.append(f"{key} = {_literal(field, values.get(key))}")
    text = "\n".join(lines) + "\n"

    path = _overlay_path(pipeline)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ──────────────────────────────────────────────────────────────────────────────
# Gestion d'un (seul) run : sous-processus + buffer de logs + notifications SSE.
# ──────────────────────────────────────────────────────────────────────────────
# Nombre maximal de lignes de log conservées en mémoire. Un run de plusieurs
# heures (ffmpeg -stats) en produit des centaines de milliers : sans borne, la
# RAM du serveur croît indéfiniment et chaque nouveau client SSE rejoue tout.
MAX_LOG_LINES = 5000


class RunManager:
    def __init__(self):
        self.cond = threading.Condition()
        # deque bornée : les lignes les plus anciennes sont abandonnées.
        # `total` compte TOUTES les lignes émises depuis le début du run, ce qui
        # donne aux clients SSE un curseur absolu stable malgré la troncature.
        self.lines = collections.deque(maxlen=MAX_LOG_LINES)
        self.total = 0
        self.proc = None
        self.running = False
        self.meta = {}

    def status(self):
        with self.cond:
            return {
                "running": self.running,
                "meta": dict(self.meta),
                "lines": self.total,
            }

    def start(self, pipeline, mode, steps):
        if pipeline not in PIPELINES:
            raise ValueError("pipeline inconnu")
        if mode not in ("dry-run", "real"):
            raise ValueError("mode invalide")
        with self.cond:
            if self.running:
                raise RuntimeError("un run est déjà en cours")
            launcher = ROOT / PIPELINES[pipeline]["launcher"]
            cmd = [str(launcher), "-y", "--dry-run" if mode == "dry-run" else "--real"]
            if PIPELINES[pipeline]["steps"] is not None and steps:
                valid = {s[0] for s in PIPELINES[pipeline]["steps"]}
                cmd += [s for s in steps if s in valid]
            self.lines.clear()
            self.total = 0
            self.meta = {
                "pipeline": pipeline,
                "mode": mode,
                "steps": steps or [],
                "cmd": " ".join(cmd),
                "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                "exit_code": None,
            }
            self.running = True
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,  # groupe dédié -> arrêt propre du sous-arbre
                env=os.environ.copy(),
            )
            self.cond.notify_all()
        self._append(f"$ {' '.join(cmd)}")
        threading.Thread(target=self._pump, daemon=True).start()
        return dict(self.meta)

    def _append(self, line):
        with self.cond:
            self._append_locked(line)
            self.cond.notify_all()

    def _append_locked(self, line):
        """Ajoute une ligne (verrou déjà tenu) en gardant `total` cohérent."""
        self.lines.append(line)
        self.total += 1

    def _pump(self):
        try:
            for line in self.proc.stdout:
                self._append(line.rstrip("\n"))
        finally:
            code = self.proc.wait()
            with self.cond:
                self.running = False
                self.meta["exit_code"] = code
                self._append_locked(f"=== Terminé (code de sortie {code}) ===")
                self.cond.notify_all()

    def stop(self):
        with self.cond:
            if not self.running or self.proc is None:
                return False
            pid = self.proc.pid
        try:
            os.killpg(os.getpgid(pid), signal.SIGINT)
        except ProcessLookupError:
            return False
        self._append("=== Arrêt demandé (SIGINT)… ===")
        threading.Thread(target=self._escalate, args=(pid,), daemon=True).start()
        return True

    def _escalate(self, pid):
        for _ in range(120):  # ~12 s de grâce
            time.sleep(0.1)
            if not self.running:
                return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            self._append("=== Toujours actif : SIGKILL envoyé ===")
        except ProcessLookupError:
            pass


MANAGER = RunManager()


# ──────────────────────────────────────────────────────────────────────────────
# Serveur HTTP.
# ──────────────────────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "HorusGUI/1.0"

    def log_message(self, *args):  # silence le log par défaut
        pass

    # — helpers réponse —
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # — routage —
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/api/meta":
            return self._send_json(self._meta())
        if path == "/api/status":
            return self._send_json(MANAGER.status())
        if path.startswith("/api/config/"):
            pipeline = path.rsplit("/", 1)[-1]
            if pipeline not in PIPELINES:
                return self._send_json({"error": "pipeline inconnu"}, 404)
            try:
                _, values = load_config(pipeline)
                overlay = _overlay_path(pipeline)
                return self._send_json(
                    {
                        "values": values,
                        "overlay": str(overlay) if overlay.is_file() else None,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"error": str(exc)}, 500)
        if path == "/api/stream":
            return self._stream()
        return self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/run":
                body = self._read_body()
                meta = MANAGER.start(
                    body.get("pipeline"), body.get("mode"), body.get("steps")
                )
                return self._send_json({"ok": True, "meta": meta})
            if path == "/api/stop":
                return self._send_json({"ok": MANAGER.stop()})
            if path.startswith("/api/config/"):
                pipeline = path.rsplit("/", 1)[-1]
                if pipeline not in PIPELINES:
                    return self._send_json({"error": "pipeline inconnu"}, 404)
                write_overlay(pipeline, self._read_body())
                _, values = load_config(pipeline)
                return self._send_json(
                    {
                        "ok": True,
                        "values": values,
                        "overlay": str(_overlay_path(pipeline)),
                    }
                )
        except RuntimeError as exc:  # run déjà en cours
            return self._send_json({"error": str(exc)}, 409)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 400)
        return self._send_json({"error": "not found"}, 404)

    # — vues —
    def _meta(self):
        return {
            "pipelines": {
                key: {
                    "label": p["label"],
                    "steps": p["steps"],
                    "fields": p["fields"],
                }
                for key, p in PIPELINES.items()
            }
        }

    def _serve_index(self):
        try:
            body = (GUI_DIR / "index.html").read_bytes()
        except FileNotFoundError:
            return self._send_json({"error": "index.html manquant"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        cursor = 0
        try:
            while True:
                with MANAGER.cond:
                    while cursor >= MANAGER.total and MANAGER.running:
                        MANAGER.cond.wait(timeout=15)
                    # Index absolu de la plus ancienne ligne encore en mémoire.
                    first = MANAGER.total - len(MANAGER.lines)
                    if cursor < first:
                        dropped = first - cursor
                        new = [
                            f"… {dropped} ligne(s) plus anciennes tronquées …",
                            *MANAGER.lines,
                        ]
                    else:
                        new = list(MANAGER.lines)[cursor - first :]
                    cursor = MANAGER.total
                    running = MANAGER.running
                if new:
                    for line in new:
                        payload = json.dumps({"line": line}, ensure_ascii=False)
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                if not running and cursor >= MANAGER.total:
                    end = json.dumps(MANAGER.status(), ensure_ascii=False)
                    self.wfile.write(f"event: end\ndata: {end}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass


class ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    host = sys.argv[2] if len(sys.argv) > 2 else "0.0.0.0"
    server = ThreadingServer((host, port), Handler)
    ip = _lan_ip()
    print("════════════════════════════════════════════════════════════")
    print(" Interface web horus — lancement et pilotage des pipelines")
    print(f"   Local :  http://127.0.0.1:{port}")
    if host == "0.0.0.0" and ip:
        print(f"   LAN   :  http://{ip}:{port}   (PC / téléphone du réseau)")
    print("   Ctrl-C pour arrêter le serveur.")
    print("════════════════════════════════════════════════════════════")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du serveur.")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
