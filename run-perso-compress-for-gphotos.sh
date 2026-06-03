#!/usr/bin/env bash
# Lance le conteneur de compression photos/vidéos perso -> ./output/gphotos
# (photos max 1024px JPEG ; vidéos 720p H.265 via NVENC). Un sous-dossier de
# sortie = un futur album Google Photos.
# Réglages dans env/.env (NAS_MOUNT, DRY_RUN, VIDEO_CQ, VIDEO_PRESET, ...) ;
# PHOTOS_SOURCE et OUTPUT_FOLDER sont fixés par le service compose.
# DRY_RUN : true = simulation (rien n'est écrit), false = compression réelle.
# Défaut true (sûr). Deux façons de le régler :
#   ./run-compress-gphotos.sh --DRY_RUN=false     (flag, prioritaire)
#   DRY_RUN=false ./run-compress-gphotos.sh       (variable d'env)
# Les autres arguments sont transmis tels quels à `docker compose up`
#   ex: ./run-compress-gphotos.sh --DRY_RUN=false -d   (en arrière-plan)
set -euo pipefail

# Valeur initiale depuis l'env (ou défaut sûr), surchargée par --DRY_RUN=... .
DRY_RUN="${DRY_RUN:-true}"

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

# Service explicite : ne démarre que la compression, jamais convert-h265.
docker compose --env-file env/.env up --build --no-log-prefix \
  "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}" compress-gphotos
