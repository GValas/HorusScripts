#!/usr/bin/env bash
# Interface web locale pour lancer et piloter les pipelines horus.
# Sert src/gui (stdlib Python uniquement — aucune dépendance pip sur l'hôte).
# Appelle run-public-media-pipeline.sh / run-perso-media-pipeline.sh en
# sous-processus et streame leurs logs ; édite aussi les 00-config.py.
#
# Usage :
#   ./run-gui.sh            # http://127.0.0.1:8765 (et LAN)
#   ./run-gui.sh 9000       # autre port
#   ./run-gui.sh 8765 127.0.0.1   # local uniquement (pas d'accès LAN)
set -euo pipefail
cd "$(dirname "$0")"
exec python3 src/gui/server.py "$@"
