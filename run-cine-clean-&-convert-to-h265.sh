#!/usr/bin/env bash
# Lance le conteneur de prod convert-h265 (ré-encodage HEVC via NVENC).
# Réglages dans env/.env (NAS_MOUNT, INPUT_FOLDERS, CQ, PRESET, DRY_RUN).
# DRY_RUN : true = simulation (aucun renommage ni conversion), false = réel.
# Défaut false (comportement historique). Deux façons de le régler :
#   ./run-convert-h265.sh --DRY_RUN=true       (flag, prioritaire)
#   DRY_RUN=true ./run-convert-h265.sh         (variable d'env)
# Les autres arguments sont transmis tels quels à `docker compose up`
#   ex: ./run-convert-h265.sh --DRY_RUN=true -d   (en arrière-plan)
set -euo pipefail

# Valeur initiale depuis l'env (ou défaut historique), surchargée par --DRY_RUN=.
DRY_RUN="${DRY_RUN:-false}"

# Sépare le flag --DRY_RUN du reste des arguments (passés à docker compose).
PASS_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --DRY_RUN=*) DRY_RUN="${arg#*=}" ;;
    *) PASS_ARGS+=("$arg") ;;
  esac
done

# Exporté pour que docker-compose.yml l'interpole (prioritaire sur env/.env).
export DRY_RUN

cd "$(dirname "$0")"

# Service explicite : depuis l'ajout de `compress-gphotos`, un `up` sans nom
# de service les lancerait tous les deux. On ne démarre que convert-h265.
docker compose --env-file env/.env up --build --no-log-prefix \
  "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}" convert-h265
