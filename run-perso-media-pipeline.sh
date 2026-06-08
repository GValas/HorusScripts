#!/usr/bin/env bash
# Pipeline complet perso-media : enchaîne 01 -> 02 -> 03 -> 04 dans le
# conteneur. Arrêt à la PREMIÈRE erreur (set -e).
#
# Tous les réglages (dont DRY_RUN commun) sont dans
# src/perso-media/00-config.py — monté en LIVE, donc pris en compte sans
# rebuild. Les fichiers produits t'appartiennent (--user).
#
# Étapes :
#   01 convert  : tout en H.265 + MKV (GPU NVENC) + .jpeg->.jpg     [NAS lecture/écriture]
#   02 enrich   : complète les dates manquantes (EXIF / creation_time) [NAS lecture/écriture]
#   03 compress : copies 2048/720p -> output/gphotos (GPU NVENC)    [NAS ro, output rw]
#   04 upload   : output/gphotos -> Google Photos (rclone)          [output ro, rclone.conf]
#
# Usage :
#   ./run-perso-media-pipeline.sh           # demande confirmation si DRY_RUN=False
#   ./run-perso-media-pipeline.sh -y        # sans confirmation
#   ./run-perso-media-pipeline.sh 03 04     # ne lance que ces étapes (sous-ensemble)
#   ./run-perso-media-pipeline.sh --dry-run # force la simulation (ignore 00-config)
#   ./run-perso-media-pipeline.sh --real    # force l'exécution réelle
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="horus-convert-h265"
SCRIPTS_DIR="$PWD/src/perso-media"
RCLONE_CONF="$PWD/env/rclone.conf"
USERSPEC="$(id -u):$(id -g)"

# Tous les réglages viennent de 00-config.py (source de vérité commune).
read_cfg() {
  python3 -c "import importlib.util,pathlib; \
s=importlib.util.spec_from_file_location('c', pathlib.Path('${SCRIPTS_DIR}/00-config.py')); \
m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(getattr(m, '$1'))"
}

# Racine scannée par 01/02/03 : le NAS (/mnt/wsl/horus/photos) OU un dossier
# Windows (ex. /mnt/c/Users/.../photos-a-trier — Docker Desktop partage les
# disques C:). On la lit dans la config et on la monte telle quelle.
PHOTOS_SRC="$(read_cfg PHOTOS_SRC)"

# Dossier de sortie gphotos : SEULE source de vérité = COMPRESS_OUTPUT (00-config).
# Le lanceur tourne sur l'hôte -> chemin hôte absolu (= $PWD/output/gphotos) ;
# monté tel quel sur /output/gphotos (= COMPRESS_OUTPUT côté conteneur).
OUT="$(read_cfg COMPRESS_OUTPUT)"

# Étapes demandées (défaut : toutes).
ASSUME_YES=0
DRY_OVERRIDE=""
STEPS=()
for a in "$@"; do
  case "$a" in
    -y|--yes) ASSUME_YES=1 ;;
    --dry-run) DRY_OVERRIDE=1 ;;
    --real) DRY_OVERRIDE=0 ;;
    01|02|03|04) STEPS+=("$a") ;;
    *) echo "Argument inconnu : $a (attendu: -y, --dry-run, --real, ou 01/02/03/04)" >&2; exit 2 ;;
  esac
done
[ ${#STEPS[@]} -eq 0 ] && STEPS=(01 02 03 04)

DRY_RUN="$(read_cfg DRY_RUN)"
# --dry-run/--real priment sur 00-config.py. On calcule la valeur effective et on
# la transmet aux conteneurs via PIPELINE_DRY_RUN (1/0), source de vérité unique
# pour ce run (les scripts l'honorent au-dessus de 00-config.py).
case "$DRY_OVERRIDE" in
  1) DRY_RUN=True ;;
  0) DRY_RUN=False ;;
esac
EFFECTIVE_DRY=$([ "$DRY_RUN" = "True" ] && echo 1 || echo 0)

echo "════════════════════════════════════════════════════════════"
echo " Pipeline perso-media"
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
BASE=(docker run --rm --user "$USERSPEC" -e "PIPELINE_DRY_RUN=$EFFECTIVE_DRY" -v "$PHOTOS_SRC:$PHOTOS_SRC" -v "$SCRIPTS_DIR:/work" -w /work)
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
        "${BASE[@]}" "${GPU[@]}" -v "$OUT:/output/gphotos" "$IMAGE" python3 "03-compress-for-gphotos.py" ;;
    04) run_step "04 upload-to-gphotos (rclone)"
        # --entrypoint bash : on court-circuite l'entrypoint de l'image nvidia/cuda
        # (bannière CUDA + « NVIDIA Driver was not detected »). 04 n'utilise pas le
        # GPU, ce check est un faux positif ici.
        "${BASE[@]}" --entrypoint bash -v "$OUT:/output/gphotos:ro" -v "$RCLONE_CONF:/cfg/rclone.conf" \
          -e RCLONE_CONFIG=/cfg/rclone.conf "$IMAGE" "04-upload-to-gphotos.sh" ;;
  esac
done

echo; echo "════════════════════════════════════════════════════════════"
echo " Pipeline terminé (${STEPS[*]})."
echo "════════════════════════════════════════════════════════════"
