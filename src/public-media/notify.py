#!/usr/bin/env python3
"""
notify.py — Notification de fin de pipeline (ntfy / webhook générique).

Lit NOTIFY_WEBHOOK dans le 00-config.py voisin et POST le message reçu en
argument vers cette URL (corps = texte brut, compatible ntfy.sh et la plupart
des webhooks). No-op si NOTIFY_WEBHOOK vaut None, ou si l'URL est injoignable :
une notification ne doit JAMAIS faire échouer le pipeline.

Tourne sur l'hôte (appelé par le lanceur), pas dans le conteneur — il lui suffit
de python3 (stdlib) et d'un accès réseau sortant.

Usage : python3 notify.py "message"
"""

import sys
import importlib.util
import urllib.request
from pathlib import Path


def _webhook() -> str | None:
    spec = importlib.util.spec_from_file_location(
        "c", Path(__file__).with_name("00-config.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return getattr(m, "NOTIFY_WEBHOOK", None)


def main() -> int:
    msg = sys.argv[1] if len(sys.argv) > 1 else "pipeline terminé"
    try:
        url = _webhook()
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
