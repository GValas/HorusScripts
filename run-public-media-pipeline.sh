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
# Lecture déléguée à src/public-media/_common.py : même chargement que les
# scripts (00-config.py PUIS la surcouche 00-config.local.py écrite par
# l'interface web), donc aucune divergence possible entre l'affiché et l'exécuté.
read_cfg() { python3 src/public-media/_common.py "$1"; }

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

# UID/GID de l'hôte : docker-compose.yml lance le conteneur avec, comme le
# pipeline perso. Sans ça les fichiers générés dans src/public-media
# (conversion.log, .scan-cache.json) appartiennent à root et ne sont plus
# éditables ni supprimables sans sudo.
export HOST_UID="$(id -u)"
export HOST_GID="$(id -g)"

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

# Notification de fin (no-op si NOTIFY_WEBHOOK=None). Sur erreur, le trap envoie
# l'échec avant de quitter (set -e).
notify() { python3 src/notify.py "$CONFIG" "$1" >/dev/null 2>&1 || true; }
MODE_LABEL="$([ "$DRY_RUN" = "True" ] && echo DRY-RUN || echo RÉEL)"
trap 'notify "❌ Pipeline public-media ÉCHEC (mode ${MODE_LABEL})"' ERR

docker compose up --build --no-log-prefix \
  "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}" convert-h265

trap - ERR  # succès : on désarme le trap d'échec avant la notification finale
notify "✅ Pipeline public-media terminé (mode ${MODE_LABEL})"
