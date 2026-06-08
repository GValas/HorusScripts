#!/usr/bin/env bash
# PUBLIC — nettoyage des noms + conversion HEVC (NVENC) du NAS (movies/tvshows…).
# Lance le service compose convert-h265 (src/public-media : 01-clean-names.py
# puis 02-convert-to-h265.py).
#
# TOUS les réglages sont dans src/public-media/00-config.py (NAS_MOUNT,
# INPUT_FOLDERS, DRY_RUN, CLEAN_*, CONVERT_*) — comme le pipeline perso. Plus
# de variables d'environnement ni d'env/.env.
#   DRY_RUN : True = simulation (aucun renommage ni conversion), False = réel
#             (02 SUPPRIME les originaux après conversion).
#
# Usage :
#   ./run-public-media-pipeline.sh         # demande confirmation si DRY_RUN=False
#   ./run-public-media-pipeline.sh -y      # sans confirmation
#   ./run-public-media-pipeline.sh --dry-run  # force la simulation (ignore 00-config)
#   ./run-public-media-pipeline.sh --real     # force l'exécution réelle
#   ./run-public-media-pipeline.sh -d      # args supplémentaires transmis à compose (ex: -d)
set -euo pipefail
cd "$(dirname "$0")"

CONFIG="src/public-media/00-config.py"
read_cfg() {
  python3 -c "import importlib.util,pathlib; \
s=importlib.util.spec_from_file_location('c', pathlib.Path('${CONFIG}')); \
m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(getattr(m, '$1'))"
}

# docker-compose.yml monte ${NAS_MOUNT}:${NAS_MOUNT} -> il faut l'exporter pour
# que compose l'interpole (la source de vérité est 00-config.py).
export NAS_MOUNT="$(read_cfg NAS_MOUNT)"
DRY_RUN="$(read_cfg DRY_RUN)"

# Sépare -y / --dry-run / --real du reste (transmis tel quel à `docker compose up`).
ASSUME_YES=0
DRY_OVERRIDE=""
PASS_ARGS=()
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    --dry-run) DRY_OVERRIDE=1 ;;
    --real) DRY_OVERRIDE=0 ;;
    *) PASS_ARGS+=("$arg") ;;
  esac
done

# --dry-run/--real priment sur 00-config.py. La valeur effective est exportée en
# PIPELINE_DRY_RUN (1/0) : docker-compose.yml la transmet au conteneur, où les
# scripts l'honorent au-dessus de 00-config.py.
case "$DRY_OVERRIDE" in
  1) DRY_RUN=True ;;
  0) DRY_RUN=False ;;
esac
export PIPELINE_DRY_RUN=$([ "$DRY_RUN" = "True" ] && echo 1 || echo 0)

echo "════════════════════════════════════════════════════════════"
echo " Pipeline public-media (clean-names -> convert-h265)"
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
