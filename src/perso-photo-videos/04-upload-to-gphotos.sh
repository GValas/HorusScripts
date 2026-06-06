#!/usr/bin/env bash
# Upload de output/gphotos vers Google Photos via rclone (étape 04 du pipeline).
#   - 1 dossier de 1er niveau de output/gphotos = 1 album du même nom
#     (tout le contenu du dossier, sous-dossiers inclus, va dans cet album).
#   - S'exécute normalement DANS le conteneur (rclone y est installé) via
#     run-perso-pipeline.sh. Remplace l'ancien 04-upload-to-gphotos.py (archivé
#     sous src/perso-photo-videos/archive/).
#
# ── PRÉREQUIS (une seule fois) ────────────────────────────────────────────────
#   cp env/rclone.conf.example env/rclone.conf  puis remplir client_id /
#   client_secret / token (cf. env/rclone.conf.example). Le token s'obtient via
#   `rclone authorize "google photos"` sur une machine avec navigateur + rclone
#   (ex. rclone.exe sous Windows). Aucune install rclone requise sur l'hôte.
#
# ── RÉGLAGES (tous dans src/perso-photo-videos/00-config.py) ──────────────────
#   DRY_RUN (commun)  : True -> rclone --dry-run (simulation) ; False -> réel.
#   UPLOAD_REMOTE / UPLOAD_TRANSFERS / UPLOAD_TPSLIMIT ; source = COMPRESS_OUTPUT.
# Arguments additionnels transmis tels quels à rclone (ex. -v).
set -euo pipefail

# 00-config.py est TOUJOURS à côté de ce script (en local comme dans l'image,
# où ils sont copiés ensemble). TOUS les réglages viennent de là -> chemins
# corrects en local (output/gphotos absolu) ET en conteneur (/output/gphotos),
# sans hypothèse sur le répertoire courant.
CONFIG="$(cd "$(dirname "$0")" && pwd)/00-config.py"

read_cfg() {
  python3 -c "import importlib.util,pathlib; \
s=importlib.util.spec_from_file_location('c', pathlib.Path('${CONFIG}')); \
m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(getattr(m, '$1'))"
}

SRC="$(read_cfg COMPRESS_OUTPUT)"   # = dossier de sortie produit par 03
DRY_RUN="$(read_cfg DRY_RUN)"
REMOTE="$(read_cfg UPLOAD_REMOTE)"
TRANSFERS="$(read_cfg UPLOAD_TRANSFERS)"
TPSLIMIT="$(read_cfg UPLOAD_TPSLIMIT)"
RCLONE_DRY=""
[ "$DRY_RUN" = "True" ] && RCLONE_DRY="--dry-run"

# ── Vérifications ─────────────────────────────────────────────────────────────
command -v rclone >/dev/null 2>&1 || {
  echo "Erreur : rclone introuvable. Installe-le : https://rclone.org/install/" >&2
  exit 1
}
rclone listremotes 2>/dev/null | grep -qx "${REMOTE}:" || {
  echo "Erreur : remote '${REMOTE}:' absent. Lance 'rclone config' (cf. en-tête)." >&2
  exit 1
}
[ -d "$SRC" ] || { echo "Erreur : dossier source introuvable : $SRC" >&2; exit 1; }

echo "Mode    : $([ -n "$RCLONE_DRY" ] && echo 'DRY RUN (simulation)' || echo 'UPLOAD RÉEL')"
echo "Remote  : ${REMOTE}:album/<dossier>"
echo "Source  : $SRC"
echo "========================================================================"

# ── Un dossier de 1er niveau = un album ───────────────────────────────────────
shopt -s nullglob
count=0
for d in "$SRC"/*/; do
  album="$(basename "$d")"
  # Cohérence avec le reste du pipeline : on ignore les dossiers en « _ ».
  case "$album" in _*) echo "ignoré (dossier « _ ») : $album"; continue;; esac
  echo "── Album : $album ──"
  rclone copy "$d" "${REMOTE}:album/${album}" \
    $RCLONE_DRY --stats 30s --stats-one-line \
    --transfers "$TRANSFERS" --tpslimit "$TPSLIMIT" "$@"
  count=$((count + 1))
done

echo "========================================================================"
echo "Terminé — $count album(s) traité(s)."
[ -n "$RCLONE_DRY" ] && echo "(DRY RUN : passe DRY_RUN=False dans ${CONFIG} pour envoyer réellement.)"
