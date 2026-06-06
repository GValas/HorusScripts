#!/usr/bin/env bash
# Pipeline complet perso-photo-videos : enchaîne 01 -> 02 -> 03 -> 04 dans le
# conteneur. Arrêt à la PREMIÈRE erreur (set -e).
#
# Tous les réglages (dont DRY_RUN commun) sont dans
# src/perso-photo-videos/00-config.py — monté en LIVE, donc pris en compte sans
# rebuild. Les fichiers produits t'appartiennent (--user).
#
# Étapes :
#   01 convert  : tout en H.265 + MKV (GPU NVENC) + .jpeg->.jpg     [NAS lecture/écriture]
#   02 enrich   : complète les dates manquantes (EXIF / creation_time) [NAS lecture/écriture]
#   03 compress : copies 2048/720p -> output/gphotos (GPU NVENC)    [NAS ro, output rw]
#   04 upload   : output/gphotos -> Google Photos (rclone)          [output ro, rclone.conf]
#
# Usage :
#   ./run-perso-pipeline.sh           # demande confirmation si DRY_RUN=False
#   ./run-perso-pipeline.sh -y        # sans confirmation
#   ./run-perso-pipeline.sh 03 04     # ne lance que ces étapes (sous-ensemble)
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="horus-convert-h265"
NAS="/mnt/wsl/horus"
SRC="$PWD/src/perso-photo-videos"
OUT="$PWD/output"
RCLONE_CONF="$PWD/env/rclone.conf"
USERSPEC="$(id -u):$(id -g)"

# Étapes demandées (défaut : toutes).
ASSUME_YES=0
STEPS=()
for a in "$@"; do
  case "$a" in
    -y|--yes) ASSUME_YES=1 ;;
    01|02|03|04) STEPS+=("$a") ;;
    *) echo "Argument inconnu : $a (attendu: -y, ou 01/02/03/04)" >&2; exit 2 ;;
  esac
done
[ ${#STEPS[@]} -eq 0 ] && STEPS=(01 02 03 04)

# Lit DRY_RUN depuis 00-config.py (source de vérité commune).
DRY_RUN=$(python3 -c "import importlib.util,pathlib; \
s=importlib.util.spec_from_file_location('c', pathlib.Path('${SRC}/00-config.py')); \
m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.DRY_RUN)")

echo "════════════════════════════════════════════════════════════"
echo " Pipeline perso-photo-videos"
echo "   Étapes : ${STEPS[*]}"
echo "   Mode   : $([ "$DRY_RUN" = "True" ] && echo 'DRY RUN (simulation)' || echo '*** EXÉCUTION RÉELLE ***')"
echo "════════════════════════════════════════════════════════════"

# Prérequis upload.
if printf '%s\n' "${STEPS[@]}" | grep -qx 04; then
  [ -f "$RCLONE_CONF" ] || { echo "Erreur : env/rclone.conf manquant (requis pour 04)." >&2; exit 1; }
fi
mkdir -p "$OUT"

# Confirmation si exécution réelle (01 supprime des originaux, 04 envoie sur le net).
if [ "$DRY_RUN" != "True" ] && [ "$ASSUME_YES" -ne 1 ]; then
  read -r -p "DRY_RUN=False : exécution RÉELLE. Continuer ? [y/N] " ans
  case "$ans" in y|Y|o|O) ;; *) echo "Annulé."; exit 0;; esac
fi

# Bases de lancement docker communes.
BASE=(docker run --rm --user "$USERSPEC" -v "$NAS:$NAS" -v "$SRC:/work" -w /work)
GPU=(--gpus all -e NVIDIA_DRIVER_CAPABILITIES=all)

run_step() {  # $1 = libellé
  echo; echo "──────── $1 ────────"
}

for step in "${STEPS[@]}"; do
  case "$step" in
    01) run_step "01 convert-to-mkv+h265"
        "${BASE[@]}" "${GPU[@]}" "$IMAGE" python3 "01-convert-to-mkv+h265.py" ;;
    02) run_step "02 enrich-movies-photos-with-date"
        "${BASE[@]}" "$IMAGE" python3 "02-enrich-movies-photos-with-date.py" ;;
    03) run_step "03 compress-for-gphotos"
        "${BASE[@]}" "${GPU[@]}" -v "$OUT:/output" "$IMAGE" python3 "03-compress-for-gphotos.py" ;;
    04) run_step "04 upload-to-gphotos (rclone)"
        # --entrypoint bash : on court-circuite l'entrypoint de l'image nvidia/cuda
        # (bannière CUDA + « NVIDIA Driver was not detected »). 04 n'utilise pas le
        # GPU, ce check est un faux positif ici.
        "${BASE[@]}" --entrypoint bash -v "$OUT:/output:ro" -v "$RCLONE_CONF:/cfg/rclone.conf" \
          -e RCLONE_CONFIG=/cfg/rclone.conf "$IMAGE" "04-upload-to-gphotos.sh" ;;
  esac
done

echo; echo "════════════════════════════════════════════════════════════"
echo " Pipeline terminé (${STEPS[*]})."
echo "════════════════════════════════════════════════════════════"
