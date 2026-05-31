#!/usr/bin/env bash
# Lance le conteneur de prod convert-h265 (ré-encodage HEVC via NVENC).
# Réglages dans env/.env (NAS_MOUNT, INPUT_FOLDERS, CQ, PRESET, DRY_RUN).
# Tout argument passé est transmis tel quel à `docker compose up`
#   ex: ./run-convert-h265.sh -d        (en arrière-plan)
set -euo pipefail

cd "$(dirname "$0")"

docker compose --env-file env/.env up --build --no-log-prefix "$@"
