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

Chargement : les deux scripts et le lanceur passent par _common.load_config() —
ce fichier PUIS la surcouche 00-config.local.py si elle existe (générée par
l'interface web, gitignorée, et qui écrase les valeurs ci-dessous). Le lanceur
lit NAS_MOUNT / DRY_RUN avec `python3 src/public-media/_common.py CLE`.
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
    f"{NAS_MOUNT}/movies",
]

# Interrupteur UNIQUE de simulation, commun aux deux scripts :
#   True  = simulation (aucun renommage ni conversion, rien d'écrit/supprimé) ;
#   False = exécution réelle — ATTENTION : 02 SUPPRIME les originaux après
#           conversion réussie.
DRY_RUN = True

# Notification de fin de pipeline (les runs durent souvent des heures). URL d'un
# webhook recevant le message de bilan en POST (texte brut) — ex. ntfy.sh :
# "https://ntfy.sh/mon-canal-prive". None = aucune notification (défaut).
# Le lanceur appelle src/notify.py en fin de run (succès ET échec).
NOTIFY_WEBHOOK = None

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

# Extensions RENOMMÉES par 01. Tout le reste (jaquettes .jpg, .nfo, .txt…) est
# laissé strictement intact : le nettoyage vise les médias et leurs sous-titres,
# pas les fichiers annexes d'un dossier de film.
CLEAN_VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".m4v",
    ".mov",
    ".mpg",
    ".mpeg",
    ".wmv",
}
CLEAN_SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}

# Jetons de LANGUE / VARIANTE des sous-titres. La coupure au premier mot
# technique emporterait sinon le suffixe de langue (« Film.2019.1080p.fr.srt »
# -> « Film.(2019).srt »), ce qui fait entrer en collision les pistes fr et en
# et en fait perdre une. Ces jetons sont donc ré-ajoutés en fin de nom, dans
# leur ordre d'origine, pour les seuls fichiers de sous-titres.
CLEAN_SUBTITLE_LANG_TOKENS = set(
    (
        "fr fre fra fren french en eng english es spa spanish de ger deu german "
        "it ita italian pt por nl dut ned ja jpn jp ko kor zh chi cn ru rus ar "
        "forced sdh hi cc default "
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

# Nombre de sondes ffprobe parallèles pendant la phase de SCAN (I/O-bound : les
# threads se recouvrent bien). N'affecte pas l'encodage (séquentiel sur le GPU).
# 1 = scan séquentiel (comportement d'origine).
CONVERT_SCAN_WORKERS = 8

# Downscale optionnel : résolution MAX de sortie. Tout fichier dépassant cette
# résolution est ré-encodé (MÊME s'il est déjà HEVC) en le réduisant pour tenir
# dans la boîte correspondante (aspect conservé, jamais d'upscale). Le HDR 10-bit
# (bt2020/PQ) est préservé ; le Dolby Vision dynamique est perdu (NVENC).
#   Valeurs acceptées : "480p", "720p", "1080p", "1440p", "2160p" / "4k", ou None.
#   None = pas de downscale -> comportement d'origine (on saute ce qui est HEVC).
CONVERT_MAX_RESOLUTION = None
