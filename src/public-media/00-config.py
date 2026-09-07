#!/usr/bin/env python3
"""
00-config.py — Paramètres centralisés du pipeline public (public-media).

Tout se règle ici, en variables Python (plus aucune variable d'environnement
ni env/.env), exactement comme le pipeline perso :
  - un bloc COMMUN partagé par les scripts (NAS_MOUNT, INPUT_FOLDERS,
    DRY_RUN) ;
  - un bloc par script :
      01-clean-names.py      -> CLEAN_*
      02-convert-to-h265.py  -> CONVERT_*
      03-identify-movies.py  -> IDENTIFY_*   (étape optionnelle)

Chargement : les scripts et le lanceur passent par _common.load_config() —
ce fichier PUIS la surcouche 00-config.local.py si elle existe (générée par
l'interface web, gitignorée, et qui écrase les valeurs ci-dessous). Le lanceur
lit NAS_MOUNT / DRY_RUN avec `python3 src/public-media/_common.py CLE`.
"""

# ══════════════════════════════════════════════════════════════════════════════
# COMMUN  (partagé par 01, 02 et 03)
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

# Interrupteur UNIQUE de simulation, commun à toutes les étapes :
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

# Plafond de DÉBIT, en Mb/s (mégabits par seconde), calculé sur le fichier
# entier (taille × 8 / durée). Tout fichier au-dessus est ré-encodé au CQ
# configuré — MÊME s'il est déjà en HEVC et à la bonne résolution.
#
# C'est le seul levier qui vise la TAILLE : un film peut peser 24 Go en 1080p
# simplement parce qu'il a été encodé à 29 Mb/s, et CONVERT_MAX_RESOLUTION ne le
# touchera jamais. Repère : une logithèque HEVC bien encodée tourne autour de
# 3 Mb/s en 1080p ; 8 Mb/s est déjà confortable.
#
# Garde-fou : quand le débit est la SEULE raison de ré-encoder (fichier déjà
# HEVC et dans la résolution voulue), l'original est conservé si la sortie n'est
# pas plus petite — sans quoi on dégraderait l'image pour rien.
#   None = désactivé (comportement d'origine).
CONVERT_MAX_BITRATE = None

# ══════════════════════════════════════════════════════════════════════════════
# 03 — identify-movies  (mode « identification en ligne », étape OPTIONNELLE)
#   Identifie chaque film via la base de sous-titres OpenSubtitles (empreinte
#   « moviehash » du fichier — la même clé qui sert à récupérer ses sous-titres),
#   complète les métadonnées (titre, année) via TMDB, puis renomme
#   le film ET ses sous-titres selon IDENTIFY_PATTERN.
#   Non lancée par défaut : `./run-public-media-pipeline.sh 03` ou la case « 03 »
#   de l'interface web.
# ══════════════════════════════════════════════════════════════════════════════

# Dossiers scannés par 03. NE DOIT CONTENIR QUE DES FILMS : le motif de
# nommage (titre + année) n'a pas de sens pour une série.
IDENTIFY_FOLDERS = [
    f"{NAS_MOUNT}/movies",
]

# Motif de renommage. Champs disponibles :
#   {annee} ({année}, {yyyy}) année de sortie (TMDB, sinon OpenSubtitles)
#   {titre}                     titre localisé (cf. IDENTIFY_LANGUAGE)
#   {titre_vo}                  titre original
#   {ext} (ou {extension})      extension SANS le point (« mkv »)
# Un fichier dont un champ du motif est introuvable n'est PAS renommé (mieux
# vaut un nom sale qu'un nom amputé : « 2019..mkv » n'est plus identifiable).
IDENTIFY_PATTERN = "{titre}.({yyyy}).{ext}"

# Remplacement des espaces dans les champs (« Le Prénom » -> « Le.Prénom »),
# pour rester cohérent avec le style « points » du reste de la bibliothèque.
# Mettre None (ou "") pour conserver les espaces.
IDENTIFY_SPACE_REPLACEMENT = "."

# Langue des titres demandés à TMDB (code ISO type « fr-FR », « en-US »).
IDENTIFY_LANGUAGE = "fr-FR"

# Clés d'API — NE JAMAIS LES ÉCRIRE ICI (fichier versionné) : les saisir dans
# l'interface web, qui les enregistre dans la surcouche 00-config.local.py
# (gitignorée), ou créer ce fichier à la main.
#   OpenSubtitles : compte gratuit sur https://www.opensubtitles.com/consumers
#                   -> « New consumer » -> Api Key.
#   TMDB          : https://www.themoviedb.org/settings/api (clé v3).
# Sans clé OpenSubtitles, 03 se rabat sur une recherche TMDB par titre/année
# déduits du nom de fichier (moins fiable). Sans clé TMDB, les titres ne sont
# pas localisés et seule l'identification par empreinte fonctionne.
IDENTIFY_OPENSUBTITLES_API_KEY = None
IDENTIFY_TMDB_API_KEY = None

# Jetons retirés du TITRE envoyé à TMDB, EN PLUS de CLEAN_TECH_WORDS. 01 ne
# coupe qu'au PREMIER mot technique rencontré : « Any.Given.Sunday.(1999).Dc »
# ou « Ducobu.L.Eleve.(2011).Vof » gardent leur queue, qui fait échouer la
# recherche. N'affecte QUE la requête — jamais le nom du fichier.
# Prudence : n'y mettre que des mots qui ne peuvent pas être un vrai titre
# (« final », « cut », « part » en feraient échouer de légitimes).
IDENTIFY_QUERY_NOISE_WORDS = set(
    (
        "vof vostf vfq dc dvdri uncut unrated hybrid tvrip muet integrale "
        "extended remastered bdrip dvd bluray 3d mhd avi mp4 "
    ).split()
)

# Repli par recherche TMDB sur le titre/l'année déduits du nom de fichier quand
# l'empreinte du fichier est inconnue d'OpenSubtitles (copie ré-encodée : le
# moviehash change à chaque conversion, donc après 02 la plupart des fichiers
# ne sont plus reconnus par empreinte).
IDENTIFY_FALLBACK_TITLE_SEARCH = True

# Renomme aussi les sous-titres voisins (« Film.fr.srt » -> « <nouveau nom>.fr.srt »)
# et le dossier du film quand il ne contient qu'un seul film.
IDENTIFY_RENAME_SUBTITLES = True
IDENTIFY_RENAME_FOLDER = True

# Conteneurs considérés comme des films par 03.
IDENTIFY_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov"}

# Cache des identifications, indexé par empreinte du fichier (gitignoré) : évite
# de re-consommer le quota d'API à chaque run. Les échecs sont mémorisés
# IDENTIFY_MISS_TTL_DAYS jours (la base s'enrichit avec le temps).
# None = pas de cache.
IDENTIFY_CACHE = ".identify-cache.json"
IDENTIFY_MISS_TTL_DAYS = 30

# Politesse / quotas : délai (s) entre deux appels d'API, et plafond de fichiers
# traités par run (0 = illimité) — utile pour tester sans brûler le quota.
IDENTIFY_REQUEST_DELAY = 0.5
IDENTIFY_MAX_FILES = 0

# ══════════════════════════════════════════════════════════════════════════════
# 04 — slim-audio  (allègement des pistes audio, étape OPTIONNELLE)
#   Sur une bibliothèque déjà en HEVC, les pistes LOSSLESS (TrueHD Atmos,
#   DTS-HD MA, PCM) pèsent souvent PLUS que la vidéo : jusqu'aux deux tiers du
#   fichier. Les ré-encoder en EAC3 divise le poids par 8 sans toucher à
#   l'image — la vidéo est copiée bit à bit, il n'y a aucune perte de qualité
#   visuelle ni de génération d'encodage.
#   Non lancée par défaut : `./run-public-media-pipeline.sh 04` ou la case 04
#   de l'interface web.
# ══════════════════════════════════════════════════════════════════════════════

# Attention : ce chemin est calculé À PARTIR du NAS_MOUNT ci-dessus, au moment
# où ce fichier est exécuté. Une surcouche qui redéfinit NAS_MOUNT (l'interface
# web, pour traiter un dossier Windows par exemple) ne le suit donc PAS — il
# faut éditer AUDIO_FOLDERS aussi, ce que le formulaire permet.
AUDIO_FOLDERS = [
    f"{NAS_MOUNT}/movies",
]

# Plafond de débit d'une piste, en Mb/s. Au-dessus, la piste est ré-encodée.
# Repères : AC3 5.1 = 0,45 ; EAC3 7.1 = 0,90 ; DTS core = 1,5 ;
#           DTS-HD MA = 4 à 8 ; TrueHD Atmos = 5 à 8.
# 1.0 attrape donc tout le lossless en laissant tranquilles les pistes
# compressées. 0 ou None = étape désactivée.
AUDIO_MAX_BITRATE = 1.0

# Codec de destination :
#   "eac3"    — décodé par tous les téléviseurs et amplis, MAIS l'encodeur de
#               ffmpeg ne dépasse pas 6 canaux : une piste 7.1 sera ramenée en
#               5.1 (cf. AUDIO_MAX_CHANNELS) ;
#   "libopus" — conserve le 7.1 et compresse mieux (0,45 Mb/s contre 0,64),
#               mais l'Opus multicanal n'est lu que par des lecteurs récents.
AUDIO_TARGET_CODEC = "eac3"
AUDIO_TARGET_BITRATE_KBPS = 640  # cible 5.1/7.1 ; réduit à 112 k/canal en deçà
AUDIO_MAX_CHANNELS = 6  # None pour ne jamais downmixer (à réserver à libopus)

# Deux pistes d'une même langue sont le plus souvent DEUX DOUBLAGES DIFFÉRENTS
# (VFF et VFQ, ou une version longue), pas un doublon. Par défaut on n'en
# supprime aucune : on se contente d'alléger celles qui dépassent le plafond.
AUDIO_DROP_DUPLICATE_LANGUAGES = False

AUDIO_EXTENSIONS = {".mkv", ".mp4", ".m4v"}

# Cache des sondages (gitignoré) : mesurer le débit réel d'une piste TrueHD
# impose de lire le fichier, on ne le refait pas à chaque run.
AUDIO_SCAN_CACHE = ".audio-cache.json"
AUDIO_SCAN_WORKERS = 8
