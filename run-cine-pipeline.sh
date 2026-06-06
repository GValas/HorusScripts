#!/usr/bin/env bash
# CINÉ — nettoyage des noms + conversion HEVC (NVENC) du NAS (movies/tvshows…).
# Lance le service compose convert-h265 (src/cine-videos : 01-clean-names.py
# puis 02-convert-to-h265.py).
#
# Réglages dans env/.env (NAS_MOUNT, INPUT_FOLDERS, CQ, PRESET, DRY_RUN).
# DRY_RUN : true = simulation (aucun renommage ni conversion), false = réel.
# Défaut false (comportement historique). Deux façons de le régler :
#   ./run-cine-pipeline.sh --DRY_RUN=true       (flag, prioritaire)
#   DRY_RUN=true ./run-cine-pipeline.sh         (variable d'env)
# Les autres arguments sont transmis tels quels à `docker compose up`
#   ex: ./run-cine-pipeline.sh --DRY_RUN=true -d   (en arrière-plan)
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

docker compose --env-file env/.env up --build --no-log-prefix \
  "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}" convert-h265
