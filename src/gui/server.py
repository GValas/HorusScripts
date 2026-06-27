#!/usr/bin/env python3
"""
server.py — Interface web locale pour lancer et piloter les pipelines horus.

Stdlib uniquement (aucune dépendance pip sur l'hôte, comme le reste du repo) :
  - sert une page unique (index.html) ;
  - lance run-public-media-pipeline.sh / run-perso-media-pipeline.sh en
    sous-processus (les lanceurs restent la SEULE logique métier) ;
  - streame les logs en direct (Server-Sent Events) ;
  - permet d'arrêter un run (SIGINT au groupe -> escalade SIGKILL) ;
  - lit / réécrit les réglages whitelistés des deux 00-config.py.

Un seul run à la fois (les pipelines pilotent le GPU/Docker en série).

Usage : python3 src/gui/server.py [PORT] [HOST]   (défaut 8765, 0.0.0.0)
        …ou via ./run-gui.sh
"""

import http.server
import importlib.util
import json
import os
import re
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
# type ∈ {bool, int, str, str_or_none, list, choice}
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
            {"key": "DRY_RUN", "type": "bool", "label": "Simulation par défaut (DRY_RUN)",
             "help": "Défaut du pipeline ; le mode choisi au lancement le surcharge."},
            {"key": "PHOTOS_SRC", "type": "str", "label": "Dossier source (PHOTOS_SRC)",
             "help": "Racine scannée par 01/02/03 (NAS ou dossier Windows en /mnt/c/…)."},
            {"key": "VIDEO_CQ", "type": "int", "label": "Qualité NVENC vidéo (VIDEO_CQ)",
             "help": "Plus bas = meilleure qualité (24–30 conseillé)."},
            {"key": "VIDEO_PRESET", "type": "choice", "choices": PRESET_CHOICES,
             "label": "Préréglage NVENC (VIDEO_PRESET)", "help": "p1 (rapide) → p7 (qualité)."},
            {"key": "CONVERT_SCAN_WORKERS", "type": "int", "label": "Threads scan ffprobe (CONVERT_SCAN_WORKERS)"},
            {"key": "COMPRESS_MAX_PHOTO_SIZE", "type": "int", "label": "Côté max photo, px (COMPRESS_MAX_PHOTO_SIZE)"},
            {"key": "COMPRESS_VIDEO_HEIGHT", "type": "int", "label": "Hauteur vidéo compressée, px (COMPRESS_VIDEO_HEIGHT)"},
            {"key": "COMPRESS_JPEG_QUALITY", "type": "int", "label": "Qualité JPEG (COMPRESS_JPEG_QUALITY)"},
            {"key": "COMPRESS_PHOTO_WORKERS", "type": "int", "label": "Threads compression photo (COMPRESS_PHOTO_WORKERS)"},
            {"key": "UPLOAD_TRANSFERS", "type": "int", "label": "Uploads parallèles (UPLOAD_TRANSFERS)"},
            {"key": "UPLOAD_TPSLIMIT", "type": "int", "label": "Plafond requêtes/s (UPLOAD_TPSLIMIT)"},
            {"key": "UPLOAD_RETRIES", "type": "int", "label": "Passes rclone (UPLOAD_RETRIES)"},
            {"key": "NOTIFY_WEBHOOK", "type": "str_or_none", "label": "Webhook de fin de run (NOTIFY_WEBHOOK)",
             "help": "URL ntfy/webhook, ou vide pour aucune notification."},
        ],
    },
    "public": {
        "label": "Public (films/séries → noms propres + HEVC)",
        "launcher": "run-public-media-pipeline.sh",
        "config": "src/public-media/00-config.py",
        "steps": None,  # 01 puis 02, toujours enchaînés
        "fields": [
            {"key": "DRY_RUN", "type": "bool", "label": "Simulation par défaut (DRY_RUN)",
             "help": "Défaut du pipeline ; le mode choisi au lancement le surcharge. "
                     "02 SUPPRIME les originaux en mode réel."},
            {"key": "NAS_MOUNT", "type": "str", "label": "Racine NAS (NAS_MOUNT)",
             "help": "/mnt/wsl/horus (jamais /mnt/horus) — ou un /mnt/c/… Windows."},
            {"key": "INPUT_FOLDERS", "type": "list", "label": "Dossiers à traiter (INPUT_FOLDERS)",
             "help": "Un chemin par ligne. Ceux sous NAS_MOUNT sont réécrits en f\"{NAS_MOUNT}/…\"."},
            {"key": "CONVERT_CQ", "type": "int", "label": "Qualité NVENC (CONVERT_CQ)",
             "help": "Plus bas = meilleure qualité (24–28 conseillé)."},
            {"key": "CONVERT_PRESET", "type": "choice", "choices": PRESET_CHOICES,
             "label": "Préréglage NVENC (CONVERT_PRESET)", "help": "p1 (rapide) → p7 (qualité)."},
            {"key": "CONVERT_MAX_RESOLUTION", "type": "str_or_none", "label": "Downscale max (CONVERT_MAX_RESOLUTION)",
             "help": "480p / 720p / 1080p / 1440p / 2160p / 4k, ou vide pour aucun."},
            {"key": "NOTIFY_WEBHOOK", "type": "str_or_none", "label": "Webhook de fin de run (NOTIFY_WEBHOOK)",
             "help": "URL ntfy/webhook, ou vide pour aucune notification."},
        ],
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# Lecture / écriture de config (importlib pour lire, regex ciblée pour réécrire
# en préservant commentaires et mise en forme).
# ──────────────────────────────────────────────────────────────────────────────
def _config_path(pipeline):
    return ROOT / PIPELINES[pipeline]["config"]


def load_config(pipeline):
    """Charge le 00-config.py et renvoie {key: valeur} pour les champs whitelistés."""
    path = _config_path(pipeline)
    spec = importlib.util.spec_from_file_location("horus_cfg", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
    if t in ("str", "choice"):
        return json.dumps(value, ensure_ascii=False)
    if t == "str_or_none":
        return "None" if value is None else json.dumps(value, ensure_ascii=False)
    raise ValueError(f"littéral non géré: {t}")


def rewrite_config(pipeline, payload):
    """Réécrit en place les clés fournies dans le 00-config.py (validé, atomique)."""
    fields = {f["key"]: f for f in PIPELINES[pipeline]["fields"]}
    path = _config_path(pipeline)
    text = path.read_text(encoding="utf-8")
    # NAS_MOUNT courant (pour réécrire INPUT_FOLDERS en f-strings) : valeur reçue
    # si présente, sinon valeur actuelle du fichier.
    _, current = load_config(pipeline)
    nas_mount = current.get("NAS_MOUNT")
    if "NAS_MOUNT" in payload:
        nas_mount = coerce(fields["NAS_MOUNT"], payload["NAS_MOUNT"])

    for key, raw in payload.items():
        field = fields.get(key)
        if field is None:
            raise ValueError(f"clé non éditable: {key}")
        value = coerce(field, raw)

        if field["type"] == "list":
            lines = []
            for item in value:
                base = (nas_mount or "").rstrip("/")
                if base and item.startswith(base + "/"):
                    rest = item[len(base) + 1:]
                    lines.append(f'    f"{{NAS_MOUNT}}/{rest}",')
                else:
                    lines.append(f"    {json.dumps(item, ensure_ascii=False)},")
            block = key + " = [\n" + "\n".join(lines) + "\n]"
            pattern = re.compile(rf"^{key}\s*=\s*\[.*?\]", re.MULTILINE | re.DOTALL)
            text, n = pattern.subn(lambda m: block, text, count=1)
        else:
            literal = _literal(field, value)
            pattern = re.compile(rf"^(\s*{key}\s*=\s*)(.*?)(\s*#.*)?$", re.MULTILINE)
            text, n = pattern.subn(lambda m: m.group(1) + literal + (m.group(3) or ""), text, count=1)
        if n == 0:
            raise ValueError(f"clé introuvable dans le fichier: {key}")

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ──────────────────────────────────────────────────────────────────────────────
# Gestion d'un (seul) run : sous-processus + buffer de logs + notifications SSE.
# ──────────────────────────────────────────────────────────────────────────────
class RunManager:
    def __init__(self):
        self.cond = threading.Condition()
        self.lines = []
        self.proc = None
        self.running = False
        self.meta = {}

    def status(self):
        with self.cond:
            return {
                "running": self.running,
                "meta": dict(self.meta),
                "lines": len(self.lines),
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
            self.lines = []
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
            self.lines.append(line)
            self.cond.notify_all()

    def _pump(self):
        try:
            for line in self.proc.stdout:
                self._append(line.rstrip("\n"))
        finally:
            code = self.proc.wait()
            with self.cond:
                self.running = False
                self.meta["exit_code"] = code
                self.lines.append(f"=== Terminé (code de sortie {code}) ===")
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
                return self._send_json({"values": values})
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
                meta = MANAGER.start(body.get("pipeline"), body.get("mode"), body.get("steps"))
                return self._send_json({"ok": True, "meta": meta})
            if path == "/api/stop":
                return self._send_json({"ok": MANAGER.stop()})
            if path.startswith("/api/config/"):
                pipeline = path.rsplit("/", 1)[-1]
                if pipeline not in PIPELINES:
                    return self._send_json({"error": "pipeline inconnu"}, 404)
                rewrite_config(pipeline, self._read_body())
                _, values = load_config(pipeline)
                return self._send_json({"ok": True, "values": values})
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
                    while cursor >= len(MANAGER.lines) and MANAGER.running:
                        MANAGER.cond.wait(timeout=15)
                    new = MANAGER.lines[cursor:]
                    cursor = len(MANAGER.lines)
                    running = MANAGER.running
                if new:
                    for line in new:
                        payload = json.dumps({"line": line}, ensure_ascii=False)
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                if not running and cursor >= len(MANAGER.lines):
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
