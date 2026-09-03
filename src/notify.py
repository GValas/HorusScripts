#!/usr/bin/env python3
"""
notify.py — Notification de fin de pipeline (ntfy / webhook générique).

Lit NOTIFY_WEBHOOK dans le 00-config.py indiqué (surcouche 00-config.local.py
comprise) et POST le message vers cette URL (corps = texte brut, compatible
ntfy.sh et la plupart des webhooks). No-op si NOTIFY_WEBHOOK vaut None, ou si
l'URL est injoignable : une notification ne doit JAMAIS faire échouer le
pipeline.

Un seul exemplaire pour les deux pipelines (le fichier était dupliqué à
l'identique) : le pipeline cible est désigné par le chemin de sa config. Tourne
sur l'hôte, appelé par les lanceurs — il lui suffit de python3 (stdlib) et d'un
accès réseau sortant.

Usage : python3 src/notify.py <chemin/vers/00-config.py> "message"
"""

import sys
import importlib.util
import urllib.request
from pathlib import Path


def _webhook(config_path: Path) -> str | None:
    spec = importlib.util.spec_from_file_location("c", config_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    overlay = config_path.with_name("00-config.local.py")
    if overlay.is_file():
        exec(
            compile(overlay.read_text(encoding="utf-8"), str(overlay), "exec"),
            m.__dict__,
        )
    return getattr(m, "NOTIFY_WEBHOOK", None)


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: notify.py <00-config.py> <message>", file=sys.stderr)
        return 0  # jamais bloquant
    config_path = Path(sys.argv[1])
    msg = sys.argv[2]
    try:
        url = _webhook(config_path)
    except Exception as e:  # noqa: BLE001 — config illisible : on n'échoue pas
        print(f"[notify] config illisible : {e}", file=sys.stderr)
        return 0
    if not url:
        return 0
    try:
        req = urllib.request.Request(url, data=msg.encode("utf-8"), method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001 — réseau/serveur : best-effort
        print(f"[notify] envoi impossible : {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
