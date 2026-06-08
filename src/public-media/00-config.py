#!/usr/bin/env python3
"""
00-config.py — Paramètres centralisés du pipeline public (public-media).

Tout se règle ici, en variables Python (plus aucune variable d'environnement
ni env/.env), exactement comme le pipeline perso :
  - un bloc COMMUN partagé par les deux scripts (NAS_MOUNT, INPUT_FOLDERS,
    DRY_RUN) ;
  - un bloc par script :
      01-clean-names.py      -> CLEAN_*
      02-convert-to-h265.py  -> CONVERT_*

Les deux scripts chargent ce fichier via importlib (le nom « 00-config.py »,
avec chiffres et tiret, n'est pas importable directement) ; le lanceur
run-public-media-pipeline.sh lit NAS_MOUNT / DRY_RUN via un petit appel python3.
"""

# ══════════════════════════════════════════════════════════════════════════════
# COMMUN  (partagé par 01 et 02)
# ══════════════════════════════════════════════════════════════════════════════

# Racine du montage NAS. En container/local : /mnt/wsl/horus (cf. CLAUDE.md :
# utiliser /mnt/wsl/horus, JAMAIS /mnt/horus, invisible des conteneurs).
# Le lanceur l'exporte pour que docker-compose monte ${NAS_MOUNT}:${NAS_MOUNT}.
# Astuce : pour traiter des fichiers Windows, pointer NAS_MOUNT/INPUT_FOLDERS sur
# leur chemin WSL (ex. "/mnt/c/Users/.../Downloads") — Docker Desktop partage
# nativement les disques Windows, pas besoin de /mnt/wsl dans ce cas.
NAS_MOUNT = "/mnt/wsl/horus"

# Dossiers parcourus récursivement, l'un après l'autre (total global en fin de run).
INPUT_FOLDERS = [
    f"{NAS_MOUNT}/tvshows",
    f"{NAS_MOUNT}/movies",
    f"{NAS_MOUNT}/cartoons",
]

# Interrupteur UNIQUE de simulation, commun aux deux scripts :
#   True  = simulation (aucun renommage ni conversion, rien d'écrit/supprimé) ;
#   False = exécution réelle — ATTENTION : 02 SUPPRIME les originaux après
#           conversion réussie.
DRY_RUN = False

# ══════════════════════════════════════════════════════════════════════════════
# 01 — clean-names
#   Renomme dossiers/fichiers : retire les jetons techniques (codecs, résolutions,
#   teams de release…) et applique les conventions de nommage. Détecte les
#   collisions (deux noms -> même cible) avant d'appliquer.
# ══════════════════════════════════════════════════════════════════════════════

# Mots techniques retirés des noms de fichiers : tout ce qui suit le 1er mot
# reconnu (insensible à la casse) est coupé du titre.
CLEAN_TECH_WORDS = set(
    (
        "1080p 2160p 4k 576p 720p aac ac3 bdrip bluray brrip "
        "divx dts dvdrip fastsub french h264 x265 hdlight hdr hdtv hq "
        "imax multi multitruefrench proper repack subfrench truefrench vff vostfr web "
        "webdl webdl1080p webrip x264 x265 xvid "
        "hevc h265 h.265 h.264 remux 10bit hc vf vo nf amzn yify "
    ).split()
)

# ══════════════════════════════════════════════════════════════════════════════
# 02 — convert-to-h265
#   Ré-encode en HEVC via NVENC (GPU NVIDIA obligatoire, aucun repli CPU), tous
#   les flux audio/sous-titres décodables préservés. Vérifie le nombre de flux
#   en sortie avant de remplacer l'original.
# ══════════════════════════════════════════════════════════════════════════════

CONVERT_CQ = 26  # qualité NVENC : + bas = mieux (24–28 conseillé)
CONVERT_PRESET = "p4"  # préréglage NVENC : p1 (rapide) → p7 (qualité)
CONVERT_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov"}  # conteneurs scannés
CONVERT_SKIP_SUFFIX = "_x265"  # fichiers déjà convertis (ignorés au scan)

# Cache de scan : mémorise (codec, dimensions) par fichier, validé par (mtime,
# taille), pour éviter de re-sonder via ffprobe TOUTE la bibliothèque à chaque
# run (gain majeur sur les runs répétés). Fichier JSON à côté des scripts
# (gitignoré) ; mettre None pour désactiver le cache.
CONVERT_SCAN_CACHE = ".scan-cache.json"

# Downscale optionnel : résolution MAX de sortie. Tout fichier dépassant cette
# résolution est ré-encodé (MÊME s'il est déjà HEVC) en le réduisant pour tenir
# dans la boîte correspondante (aspect conservé, jamais d'upscale). Le HDR 10-bit
# (bt2020/PQ) est préservé ; le Dolby Vision dynamique est perdu (NVENC).
#   Valeurs acceptées : "480p", "720p", "1080p", "1440p", "2160p" / "4k", ou None.
#   None = pas de downscale -> comportement d'origine (on saute ce qui est HEVC).
CONVERT_MAX_RESOLUTION = None
