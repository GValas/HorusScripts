#!/usr/bin/env python3
"""
00-config.py — Paramètres centralisés du pipeline perso-media.

Tout se règle ici, en variables Python (aucune variable d'environnement) :
  - un bloc COMMUN partagé par plusieurs scripts (DRY_RUN, extensions cibles,
    réglages NVENC, racine NAS, bornes de dates) ;
  - un bloc par script, préfixé :
      01-convert-to-mkv+h265.py            -> CONVERT_*
      02-enrich-movies-photos-with-date.py -> ENRICH_*
      03-compress-for-gphotos.py           -> COMPRESS_*
  L'upload (04-upload-to-gphotos.sh, basé sur rclone) ne lit que DRY_RUN ;
  son authentification est gérée par rclone, hors de ce fichier.

Les scripts Python chargent ce fichier via importlib (les noms numérotés avec
tirets/« + » ne sont pas importables directement) ; le script .sh lit DRY_RUN
via un petit appel python3.
"""

from pathlib import Path

# ══════════════════════════════════════════════════════════════════════════════
# COMMUN  (partagé par plusieurs scripts)
# ══════════════════════════════════════════════════════════════════════════════

# Racine du share photos du NAS, scannée par 01/02/03. En container/local :
# /mnt/wsl/horus/photos (cf. CLAUDE.md : utiliser /mnt/wsl/horus, JAMAIS
# /mnt/horus, invisible des conteneurs).
# Astuce : pour traiter un dossier Windows, pointer sur son chemin WSL
# (ex. "/mnt/c/Users/.../photos-a-trier") — Docker Desktop partage les disques C:.
PHOTOS_SRC = "/mnt/c/Users/valas/Downloads/photos-a-trier"

# Dossier du projet (…/HorusScripts), déduit de l'emplacement de ce fichier —
# sert à construire le chemin de sortie de 03 (output/gphotos).
_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent

# Interrupteur UNIQUE de simulation, commun aux 4 scripts :
#   True  = simulation (rien n'est écrit / supprimé / envoyé) — défaut sûr ;
#   False = exécution réelle.
DRY_RUN = False

# Extensions finales de la bibliothèque : 1 seule pour les photos, 1 pour les
# vidéos. 01 normalise TOUT vers ces deux extensions (vidéos -> .mkv, .jpeg ->
# .jpg) ; 02/03/04 ne traitent qu'elles.
PHOTO_EXT = ".jpg"
VIDEO_EXT = ".mkv"

# Encodage vidéo H.265 sur GPU NVIDIA (NVENC), commun à 01 (conversion) et
# 03 (compression).
VIDEO_CQ = 28  # qualité NVENC : + bas = mieux (24–30 conseillé)
VIDEO_PRESET = "p4"  # préréglage NVENC : p1 (rapide) → p7 (qualité)

# Bornes d'années pour juger une date « plausible » : écarte les dates epoch
# (1904/1970) et les valeurs aberrantes. Utilisé par 01 (tag existant) et 02
# (toutes les sources de date).
MIN_PLAUSIBLE_YEAR = 1990
MAX_PLAUSIBLE_YEAR = 2100

# ══════════════════════════════════════════════════════════════════════════════
# 01 — convert-to-mkv+h265
#   Met TOUTE vidéo en H.265 + MKV (NVENC obligatoire) et normalise les photos
#   .jpeg -> .jpg. Conserve les tags existants, sans inférer de date (rôle de 02).
# ══════════════════════════════════════════════════════════════════════════════

CONVERT_RENAME_JPEG = True  # normaliser aussi les photos : .jpeg -> .jpg
CONVERT_HEIC_TO_JPG = True  # convertir les photos HEIC/HEIF (iPhone) -> .jpg
CONVERT_HEIC_EXTENSIONS = {".heic", ".heif"}  # extensions HEIC reconnues
CONVERT_JPEG_QUALITY = 95  # qualité JPEG du HEIC décodé (0–100)
# Conteneurs d'entrée à scanner (le .mkv est ajouté au scan par le script lui-
# même, pour ré-encoder un .mkv non-H.265 ou ignorer un .mkv déjà H.265).
CONVERT_EXTENSIONS = {
    ".mov",
    ".mp4",
    ".webm",
    ".wmv",
    ".mpeg",
    ".mpg",
    ".rm",
    ".rmvb",
    ".3gp",
    ".avi",
    ".divx",
    ".asf",
    ".vob",
    ".m2ts",
    ".mts",
    ".flv",
    ".f4v",
    ".m4v",
}
CONVERT_OUTPUT_SUFFIX = VIDEO_EXT  # conteneur de sortie (= .mkv commun)
CONVERT_AUDIO_CODEC = "aac"  # ré-encodage audio (archivage)
CONVERT_AUDIO_BITRATE = "192k"

# Cache de scan : mémorise le codec par fichier, validé par (mtime, taille), pour
# éviter de re-sonder via ffprobe TOUTE la bibliothèque à chaque run (gain majeur
# sur les runs répétés). Fichier JSON à côté des scripts (gitignoré) ; None pour
# désactiver.
CONVERT_SCAN_CACHE = ".scan-cache.json"

# ══════════════════════════════════════════════════════════════════════════════
# 02 — enrich-movies-photos-with-date
#   Complète les dates de prise de vue manquantes (EXIF photos, creation_time
#   vidéos). Piloté uniquement par DRY_RUN. La normalisation d'extensions est
#   faite en amont par 01.
# ══════════════════════════════════════════════════════════════════════════════

ENRICH_EXPORT_CSV = "rapport.csv"  # rapport des fichiers scannés ; None = aucun
ENRICH_EXPORT_JSON = None  # rapport JSON optionnel ; None = aucun
# Clés de tags de date reconnues dans la sortie ffprobe (conteneur MKV),
# insensible à la casse.
ENRICH_DATE_TAG_KEYS = {
    "creation_time",
    "date",
    "date_recorded",
}

# ══════════════════════════════════════════════════════════════════════════════
# 03 — compress-for-gphotos
#   Copie compressée de la bibliothèque vers ./output/gphotos (photos
#   redimensionnées, vidéos ré-encodées 720p), prête pour l'upload. Ne traite
#   que PHOTO_EXT / VIDEO_EXT ; ignore les dossiers dont le nom commence par « _ ».
# ══════════════════════════════════════════════════════════════════════════════

COMPRESS_OUTPUT = str(_PROJECT_DIR / "output" / "gphotos")  # arborescence de sortie
COMPRESS_MAX_PHOTO_SIZE = 2048  # côté le plus long borné à cette valeur (px)
COMPRESS_JPEG_QUALITY = 95  # qualité JPEG (0–100)
COMPRESS_VIDEO_HEIGHT = 720  # hauteur max des vidéos (px) ; qualité = VIDEO_CQ

# ══════════════════════════════════════════════════════════════════════════════
# 04 — upload-to-gphotos (rclone)
#   Upload via rclone : src/perso-media/04-upload-to-gphotos.sh
#   (1 dossier de niveau 1 = 1 album). Le script lit ces réglages + DRY_RUN.
#   L'authentification (client OAuth + jeton) est dans env/rclone.conf
#   (gitignoré, hors de ce fichier). L'ancien script Python est archivé/.
# ══════════════════════════════════════════════════════════════════════════════

UPLOAD_REMOTE = "gphotos"  # nom du remote rclone (défini dans env/rclone.conf)
UPLOAD_TRANSFERS = 8  # uploads parallèles (rclone --transfers) ; gain principal
UPLOAD_TPSLIMIT = 15  # plafond de requêtes/s vers l'API (rclone --tpslimit) ;
#   au-delà de ~15-20, Google renvoie des 429 (rate limit) -> rclone back-off,
#   souvent plus lent. La vraie limite est le quota Google Photos
#   (~10 000 requêtes API/jour par projet OAuth) + le throttling serveur.
UPLOAD_RETRIES = 1  # rclone --retries : nb de passes globales. 1 = échec
#   RAPIDE quand le quota journalier Google (~10 000 req/jour) est atteint, au
#   lieu de reboucler 3× pendant des heures pour rien. La reprise se fait au run
#   suivant (rclone saute ce qui est déjà uploadé). Monter à 3 pour plus de
#   résilience aux erreurs transitoires si le quota n'est pas un souci.
UPLOAD_BATCH_MODE = "sync"  # rclone --gphotos-batch-mode : regroupe jusqu'à 50
#   créations de médias par appel API au lieu d'un batchCreate (lent) par fichier
#   -> upload bien plus rapide. "sync" (sûr, erreurs remontées par fichier),
#   "async" (plus rapide, erreurs différées), "off" (ancien comportement 1/1).
#   Nécessite rclone >= 1.63 (cf. Dockerfile : binaire officiel, pas le paquet apt).
