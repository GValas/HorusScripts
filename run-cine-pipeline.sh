#!/usr/bin/env bash
# CINÉ — nettoyage des noms + conversion HEVC (NVENC) du NAS (movies/tvshows…).
# Lance le service compose convert-h265 (src/cine-videos : 01-clean-names.py
# puis 02-convert-to-h265.py).
#
# TOUS les réglages sont dans src/cine-videos/00-config.py (NAS_MOUNT,
# INPUT_FOLDERS, DRY_RUN, CLEAN_*, CONVERT_*) — comme le pipeline perso. Plus
# de variables d'environnement ni d'env/.env.
#   DRY_RUN : True = simulation (aucun renommage ni conversion), False = réel
#             (02 SUPPRIME les originaux après conversion).
#
# Usage :
#   ./run-cine-pipeline.sh        # demande confirmation si DRY_RUN=False
#   ./run-cine-pipeline.sh -y     # sans confirmation
#   ./run-cine-pipeline.sh -d     # args supplémentaires transmis à compose (ex: -d)
set -euo pipefail
cd "$(dirname "$0")"

CONFIG="src/cine-videos/00-config.py"
read_cfg() {
  python3 -c "import importlib.util,pathlib; \
s=importlib.util.spec_from_file_location('c', pathlib.Path('${CONFIG}')); \
m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(getattr(m, '$1'))"
}

# docker-compose.yml monte ${NAS_MOUNT}:${NAS_MOUNT} -> il faut l'exporter pour
# que compose l'interpole (la source de vérité est 00-config.py).
export NAS_MOUNT="$(read_cfg NAS_MOUNT)"
DRY_RUN="$(read_cfg DRY_RUN)"

# Sépare -y du reste (transmis tel quel à `docker compose up`).
ASSUME_YES=0
PASS_ARGS=()
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    *) PASS_ARGS+=("$arg") ;;
  esac
done

echo "════════════════════════════════════════════════════════════"
echo " Pipeline cine-videos (clean-names -> convert-h265)"
echo "   NAS    : $NAS_MOUNT"
echo "   Mode   : $([ "$DRY_RUN" = "True" ] && echo 'DRY RUN (simulation)' || echo '*** EXÉCUTION RÉELLE ***')"
echo "════════════════════════════════════════════════════════════"

# 02 SUPPRIME les originaux après conversion : confirmation si exécution réelle.
if [ "$DRY_RUN" != "True" ] && [ "$ASSUME_YES" -ne 1 ]; then
  read -r -p "DRY_RUN=False : exécution RÉELLE. Continuer ? [y/N] " ans
  case "$ans" in y|Y|o|O) ;; *) echo "Annulé."; exit 0;; esac
fi

docker compose up --build --no-log-prefix \
  "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}" convert-h265
