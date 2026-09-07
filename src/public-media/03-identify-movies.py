##################################################################
## identify movies against online databases & rename them
##   OpenSubtitles (empreinte du fichier) -> TMDB (titre / année)
##   -> renommage selon IDENTIFY_PATTERN
##################################################################

import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402

##################################################################
## Configuration : tout est dans 00-config.py (COMMUN + IDENTIFY_*)
##################################################################

config = _common.load_config(__file__)

ROOTS = config.IDENTIFY_FOLDERS
# DRY_RUN effectif : PIPELINE_DRY_RUN (exporté par le lanceur via --dry-run /
# --real) prime sur 00-config.py. En simulation, les bases sont INTERROGÉES
# (lecture seule) mais rien n'est renommé : c'est tout l'intérêt, on prévisualise
# les noms avant de toucher à la bibliothèque.
DRY_RUN = _common.resolve_dry_run(config)
PATTERN = config.IDENTIFY_PATTERN
SPACE = config.IDENTIFY_SPACE_REPLACEMENT or ""
LANGUAGE = config.IDENTIFY_LANGUAGE
OS_API_KEY = config.IDENTIFY_OPENSUBTITLES_API_KEY or None
TMDB_API_KEY = config.IDENTIFY_TMDB_API_KEY or None
FALLBACK_SEARCH = bool(config.IDENTIFY_FALLBACK_TITLE_SEARCH)
# Jetons ignorés dans la requête (jamais dans le nom du fichier) : ceux que 01
# retire déjà + la liste dédiée de 00-config.py.
QUERY_NOISE = {w.lower() for w in config.CLEAN_TECH_WORDS} | {
    w.lower() for w in config.IDENTIFY_QUERY_NOISE_WORDS
}
RENAME_SUBTITLES = bool(config.IDENTIFY_RENAME_SUBTITLES)
RENAME_FOLDER = bool(config.IDENTIFY_RENAME_FOLDER)
EXTENSIONS = {e.lower() for e in config.IDENTIFY_EXTENSIONS}
SUBTITLE_EXTENSIONS = {e.lower() for e in config.CLEAN_SUBTITLE_EXTENSIONS}
CACHE_PATH = (
    Path(__file__).with_name(config.IDENTIFY_CACHE)
    if getattr(config, "IDENTIFY_CACHE", None)
    else None
)
MISS_TTL = timedelta(days=int(config.IDENTIFY_MISS_TTL_DAYS))
REQUEST_DELAY = float(config.IDENTIFY_REQUEST_DELAY)
MAX_FILES = int(config.IDENTIFY_MAX_FILES) or None

# Schéma des entrées du cache d'identification : à incrémenter dès qu'on stocke
# un champ de plus, pour invalider les caches écrits par une version antérieure.
CACHE_VERSION = 1
CACHE_FLUSH_EVERY = 20  # un run interrompu ne doit pas perdre les appels d'API

OS_API = "https://api.opensubtitles.com/api/v1"
TMDB_API = "https://api.themoviedb.org/3"
USER_AGENT = "HorusScripts v1.0"
HTTP_TIMEOUT = 20
HTTP_RETRIES = 3

##################################################################

# Codes couleur ANSI, même politique que 02 : désactivés hors terminal (sinon
# l'interface web affiche les séquences d'échappement telles quelles).
_USE_COLOR = sys.stdout.isatty() or os.environ.get("FORCE_COLOR") == "1"
GREEN = "\033[92m" if _USE_COLOR else ""
RED = "\033[91m" if _USE_COLOR else ""
YELLOW = "\033[93m" if _USE_COLOR else ""
RESET = "\033[0m" if _USE_COLOR else ""


def log(msg: str = "") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for line in str(msg).split("\n"):
        colored = line
        if "[✗]" in line:
            colored = f"{RED}{line}{RESET}"
        elif "[✓]" in line:
            colored = f"{GREEN}{line}{RESET}"
        elif "[!]" in line:
            colored = f"{YELLOW}{line}{RESET}"
        print(f"{ts} | {colored}")


# ── Motif de nommage ─────────────────────────────────────────────────────────
# Alias acceptés (le motif est écrit par un humain dans 00-config.py ou dans
# l'interface web : « {année} » et « {annee} » doivent marcher tous les deux).
PATTERN_ALIASES = {
    "année": "annee",
    "yyyy": "annee",
    "aaaa": "annee",
    "extension": "ext",
    "titre_original": "titre_vo",
    "annee_sortie": "annee",
}
PATTERN_FIELDS = {"annee", "titre", "titre_vo", "ext"}
RE_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")

# Caractères interdits/piégeux dans un nom de fichier (SMB/CIFS + Windows).
RE_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def normalize_field(name: str) -> str:
    return PATTERN_ALIASES.get(name.strip().lower(), name.strip().lower())


def pattern_fields(pattern: str) -> list:
    """Champs référencés par le motif, normalisés et dans l'ordre."""
    return [normalize_field(m.group(1)) for m in RE_PLACEHOLDER.finditer(pattern)]


def validate_pattern(pattern: str) -> None:
    """Refuse un motif inutilisable AVANT de consommer le moindre appel d'API."""
    unknown = sorted(set(pattern_fields(pattern)) - PATTERN_FIELDS)
    if unknown:
        raise SystemExit(
            f"IDENTIFY_PATTERN invalide : champ(s) inconnu(s) "
            f"{', '.join('{' + u + '}' for u in unknown)} "
            f"(attendus : {', '.join('{' + f + '}' for f in sorted(PATTERN_FIELDS))})"
        )
    if "ext" not in pattern_fields(pattern):
        raise SystemExit(
            "IDENTIFY_PATTERN invalide : il doit contenir {ext} — sans extension, "
            "le fichier renommé n'est plus lisible par les médiathèques."
        )


def sanitize_component(value, space: str = SPACE) -> str:
    """Nettoie un champ (titre, année…) pour un nom de fichier.

    Retire les caractères interdits par SMB/Windows, écrase les espaces
    multiples, puis applique le remplacement d'espace configuré. Un composant
    vide après nettoyage est traité comme MANQUANT par build_new_name.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = RE_ILLEGAL.sub(" ", text)
    # « Mission: Impossible - Fallout » -> le « - » isolé, une fois les espaces
    # remplacés par des points, donnerait un « .-. » illisible.
    text = re.sub(r"\s+[-–—]\s+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    if space:
        text = text.replace(" ", space)
    return text


def build_new_name(pattern: str, values: dict):
    """Applique le motif. Retourne (nom, champs_manquants).

    Un champ vide n'est PAS silencieusement omis : il est signalé, et l'appelant
    laisse le fichier tranquille. Un « (2019)..mkv » (titre introuvable) serait
    pire que le nom d'origine.
    """
    missing = []

    def repl(match):
        key = normalize_field(match.group(1))
        value = sanitize_component(values.get(key))
        if not value:
            missing.append(key)
        return value

    name = RE_PLACEHOLDER.sub(repl, pattern)
    name = re.sub(r"\.{2,}", ".", name).strip(" .")
    return name, missing


# ── Nom de fichier -> requête de repli ───────────────────────────────────────
RE_YEAR_PAREN = re.compile(r"\(((?:19|20)\d{2})\)")
RE_YEAR = re.compile(r"(?:\(|\.|\s|^)((?:19|20)\d{2})(?:\)|\.|\s|$)")


def parse_title_year(stem: str):
    """Déduit (titre, année) d'un nom de fichier déjà nettoyé par 01.

    « Joker.(2019) » -> ("Joker", 2019). Sert uniquement au repli par recherche
    TMDB quand l'empreinte du fichier est inconnue d'OpenSubtitles.

    L'année ENTRE PARENTHÈSES prime (c'est la forme produite par 01) : sinon
    « Blade.Runner.2049.(2017) » se ferait dater de 2049.
    """
    year = None
    match = RE_YEAR_PAREN.search(stem) or RE_YEAR.search(stem)
    if match:
        year = int(match.group(1))
        stem = stem[: match.start()] + " " + stem[match.end() :]
    return clean_query(stem), year


def clean_query(text: str, drop_numbering: bool = False) -> str:
    """Nettoie un fragment de nom pour en faire une requête de recherche.

    Retire les jetons parasites (QUERY_NOISE). Avec `drop_numbering`, retire
    aussi un chiffre isolé NON FINAL : « Harry.Potter.1.And.The.Sorcerers.Stone »
    ne trouve rien chez TMDB, « Harry Potter And The Sorcerers Stone » oui. Ce
    n'est jamais la requête principale : ce chiffre est parfois le seul élément
    qui distingue « Men In Black 1 » de « Men In Black 3 ». Un chiffre FINAL est
    toujours gardé — il fait partie du titre (« Spider Man 2 »).

    Chaîne vide si tout était parasite : mieux vaut ne pas chercher du tout que
    d'interroger TMDB avec « Vof » et de renommer d'après ce qu'il répondra.
    """
    words = [w for w in re.split(r"[^\w']+", text, flags=re.UNICODE) if w]
    kept = []
    for index, word in enumerate(words):
        if word.lower() in QUERY_NOISE:
            continue
        if drop_numbering and word.isdigit() and index < len(words) - 1:
            continue
        kept.append(word)
    return " ".join(kept)


def query_variants(stem: str):
    """Requêtes à essayer, de la plus FIDÈLE à la plus permissive.

    L'ordre compte : seule la première est jugée sur le titre (cf.
    match_is_plausible) ; les suivantes, plus spéculatives, exigent que l'année
    trouvée soit exactement celle du nom de fichier.

      1. le nom nettoyé de ses jetons parasites ;
      2. le même sans la numérotation interne d'une saga (Harry Potter) ;
      3. ce qui SUIT l'année entre parenthèses : la bibliothèque contient des
         « Coen.(2000).O.Brother » / « Tarantino.(2015).The.Hateful.Eight » où
         le préfixe est le réalisateur ou la collection. La plus risquée — sur un
         « Jurassic.Park.(1993).Mhd » elle chercherait le déchet de fin — d'où
         l'exigence d'année pour l'accepter.
    """
    variants = []
    full, year = parse_title_year(stem)
    if full:
        variants.append(full)
    unnumbered = clean_query(RE_YEAR_PAREN.sub(" ", stem), drop_numbering=True)
    match = RE_YEAR_PAREN.search(stem)
    after = clean_query(stem[match.end() :]) if match else ""
    for candidate in (unnumbered, after):
        if candidate and candidate.lower() not in {v.lower() for v in variants}:
            variants.append(candidate)
    return variants, year


# ── Garde-fou du repli par titre ─────────────────────────────────────────────
# Mots vides ignorés dans la comparaison titre <-> nom de fichier.
STOPWORDS = {"the", "a", "an", "le", "la", "les", "un", "une", "des", "of", "de",
             "du", "and", "et", "il", "l"}


def tokens(text: str) -> set:
    """Mots significatifs, sans accents ni casse, d'un titre ou d'un nom."""
    plain = unicodedata.normalize("NFKD", str(text or ""))
    plain = "".join(c for c in plain if not unicodedata.combining(c)).lower()
    return {w for w in re.split(r"[^a-z0-9]+", plain) if w and w not in STOPWORDS}


# Marqueurs de suite : chiffres et numéraux romains (« I » exclu, c'est un mot
# anglais courant). « Men in Black III » ne peut pas être « Men.In.Black.1 ».
ROMAN_MARKERS = {"ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def sequel_markers(text: str) -> set:
    """Numéros d'épisode d'un titre. Une année (4 chiffres) n'en est pas un."""
    return {
        w
        for w in tokens(text)
        if (w.isdigit() and len(w) < 4) or w in ROMAN_MARKERS
    }


def sequel_is_consistent(stem: str, fields: dict) -> bool:
    """Le numéro d'épisode du film trouvé doit figurer dans le nom du fichier.

    Sans ça, le recoupement de mots suffit à confondre un film et sa suite —
    « Men in Black III » contient tout « Men In Black 1 » — et l'année ne
    départage pas non plus « Matrix.2.Reloaded » de « Matrix.3.Revolutions »,
    tous deux sortis en 2003. L'inverse est toléré : la bibliothèque numérote
    des titres que TMDB ne numérote pas (« Matrix.2.Reloaded » / « Matrix
    Reloaded »).
    """
    stem_markers = sequel_markers(stem)
    candidates = [t for t in (fields.get("titre"), fields.get("titre_vo")) if t]
    return any(sequel_markers(t) <= stem_markers for t in candidates)


def match_is_plausible(stem: str, fields: dict, strict: bool = False) -> bool:
    """Le résultat d'une recherche par TITRE colle-t-il au nom du fichier ?

    Le repli TMDB renvoie TOUJOURS quelque chose : sans ce contrôle, un film mal
    nommé (ou déjà renommé par un run précédent) pourrait être renommé d'après
    un film sans rapport.
    Une identification par EMPREINTE, elle, est autoritaire et ne passe pas ici.

    Deux signaux suffisent, séparément :
      - l'ANNÉE trouvée figure dans le nom du fichier. La recherche a justement
        été filtrée sur cette année : « Russian.Dolls.(2005) » -> « Les Poupées
        russes » (2005) est bon, alors qu'aucun mot ne se recoupe (titre
        français d'un film français distribué sous titre anglais) ;
      - la moitié des mots du titre (localisé OU original) figure dans le nom.
    Sans année exploitable, seul le recoupement de mots protège :
    « The.Way.mkv » -> « Avatar : La Voie de l'eau » est bien rejeté.
    """
    stem_tokens = tokens(stem)
    if not stem_tokens:
        return False
    if not sequel_is_consistent(stem, fields):
        return False
    year_ok = str(fields.get("annee") or "") in stem_tokens
    if strict:
        # Requête spéculative (numérotation retirée, ou fragment suivant
        # l'année) : le recoupement de mots ne prouve rien puisque la requête
        # elle-même n'est plus le nom du fichier. Seule l'année tranche.
        return year_ok
    if year_ok:
        return True
    best = 0.0
    for candidate in (fields.get("titre"), fields.get("titre_vo")):
        title_tokens = tokens(candidate)
        if title_tokens:
            best = max(best, len(stem_tokens & title_tokens) / len(title_tokens))
    return best >= 0.5


# ── Empreinte OpenSubtitles ──────────────────────────────────────────────────
HASH_CHUNK = 65536


def opensubtitles_hash(path):
    """Empreinte « moviehash » d'OpenSubtitles : taille + 64 Kio de tête et de
    queue, sommés en entiers 64 bits little-endian.

    C'est la clé d'indexation de la base de sous-titres : elle identifie une
    copie exacte du fichier sans en lire le contenu. None si le fichier est trop
    petit (< 128 Kio) ou illisible.
    """
    try:
        size = os.path.getsize(path)
        if size < HASH_CHUNK * 2:
            return None
        mask = 0xFFFFFFFFFFFFFFFF
        digest = size & mask
        with open(path, "rb") as handle:
            for offset in (0, size - HASH_CHUNK):
                handle.seek(offset)
                buf = handle.read(HASH_CHUNK)
                if len(buf) < HASH_CHUNK:
                    return None
                for i in range(0, HASH_CHUNK, 8):
                    chunk = int.from_bytes(buf[i : i + 8], "little")
                    digest = (digest + chunk) & mask
        return f"{digest:016x}"
    except OSError:
        return None


# ── Appels HTTP (stdlib : les scripts public n'ont aucune dépendance pip) ────
_last_call = 0.0


def _throttle():
    """Respecte IDENTIFY_REQUEST_DELAY entre deux appels (quotas des API)."""
    global _last_call
    wait = REQUEST_DELAY - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def api_get(url: str, headers=None):
    """GET JSON avec relances. None si la ressource est absente (404) ou si
    toutes les tentatives échouent (l'appelant traite ça comme « non trouvé »)."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            **(headers or {}),
        },
    )
    for attempt in range(HTTP_RETRIES):
        _throttle()
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                endpoint = url.split("?")[0]
                log(f"    [✗] Accès refusé ({exc.code}) — clé d'API ? {endpoint}")
                return None
            if exc.code == 404:
                return None
            if exc.code == 429 or exc.code >= 500:
                time.sleep(2 * (attempt + 1))  # quota/erreur serveur : on patiente
                continue
            log(f"    [!] HTTP {exc.code} sur {url.split('?')[0]}")
            return None
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            if attempt == HTTP_RETRIES - 1:
                log(f"    [!] Échec réseau : {exc}")
                return None
            time.sleep(2 * (attempt + 1))
    return None


# ── OpenSubtitles : empreinte -> film ────────────────────────────────────────
def opensubtitles_feature(movie_hash: str):
    """Interroge la base de sous-titres par empreinte -> (imdb_id, tmdb_id,
    titre, année) du film correspondant, ou None."""
    if not OS_API_KEY:
        return None
    url = f"{OS_API}/subtitles?" + urllib.parse.urlencode(
        {"moviehash": movie_hash, "type": "movie"}
    )
    data = api_get(url, {"Api-Key": OS_API_KEY})
    if not data or not isinstance(data.get("data"), list):
        return None
    for item in data["data"]:
        attrs = item.get("attributes") or {}
        # moviehash_match : le résultat correspond bien à CE fichier et pas à une
        # simple recherche élargie renvoyée par l'API.
        if not attrs.get("moviehash_match"):
            continue
        feature = attrs.get("feature_details") or {}
        if str(feature.get("feature_type", "Movie")).lower() != "movie":
            continue
        imdb = feature.get("imdb_id")
        return {
            "imdb_id": f"tt{int(imdb):07d}" if imdb else None,
            "tmdb_id": feature.get("tmdb_id"),
            "titre": feature.get("title") or feature.get("movie_name"),
            "annee": feature.get("year"),
        }
    return None


# ── TMDB : identifiant -> titre / année ──────────────────────────────────────
def tmdb_id_from_imdb(imdb_id: str):
    url = f"{TMDB_API}/find/{imdb_id}?" + urllib.parse.urlencode(
        {"api_key": TMDB_API_KEY, "external_source": "imdb_id"}
    )
    data = api_get(url)
    results = (data or {}).get("movie_results") or []
    return results[0].get("id") if results else None


def tmdb_search(title: str, year, stem: str = ""):
    """Cherche un film et retourne le MEILLEUR résultat, pas le premier.

    Le classement de TMDB est générique : sur « Batman The Dark Knight » (2008)
    il place d'abord le documentaire « Batman Unmasked: The Psychology of The
    Dark Knight ». On reclasse donc les résultats de la première page selon,
    dans l'ordre : la part des mots du titre présents dans le nom du fichier,
    le nombre de mots communs (« The Dark Knight » bat « Dark »), l'année
    exacte, puis la popularité TMDB pour départager.
    """
    params = {"api_key": TMDB_API_KEY, "query": title, "language": LANGUAGE}
    if year:
        params["year"] = year
    data = api_get(f"{TMDB_API}/search/movie?" + urllib.parse.urlencode(params))
    best = best_result((data or {}).get("results") or [], stem or title, year)
    return best.get("id") if best else None


def best_result(results: list, stem: str, year):
    """Meilleur résultat d'une page de recherche TMDB, ou None si elle est vide."""
    reference = tokens(stem)

    def rank(result):
        best_share = 0.0
        best_common = 0
        for name in (result.get("title"), result.get("original_title")):
            candidate = tokens(name)
            if not candidate:
                continue
            common = len(reference & candidate)
            share = common / len(candidate)
            if (share, common) > (best_share, best_common):
                best_share, best_common = share, common
        same_year = bool(year) and (result.get("release_date") or "")[:4] == str(year)
        return (best_share, best_common, same_year, result.get("popularity") or 0)

    return max(results, key=rank) if results else None


def tmdb_details(tmdb_id):
    """Fiche film -> champs du motif."""
    url = f"{TMDB_API}/movie/{tmdb_id}?" + urllib.parse.urlencode(
        {"api_key": TMDB_API_KEY, "language": LANGUAGE}
    )
    data = api_get(url)
    if not data or not data.get("id"):
        return None
    return {
        "tmdb_id": data["id"],
        "imdb_id": data.get("imdb_id"),
        "titre": data.get("title") or data.get("original_title"),
        "titre_vo": data.get("original_title"),
        "annee": (data.get("release_date") or "")[:4] or None,
    }


def identify(stem: str, movie_hash):
    """Identifie un film : empreinte OpenSubtitles, puis repli recherche TMDB.

    Retourne un dict de champs prêts pour le motif, ou None. `movie_hash` est
    calculé par l'appelant (il sert aussi de clé de cache) : le fichier n'est
    lu qu'une fois.
    """
    fields = None
    feature = None

    if movie_hash and OS_API_KEY:
        feature = opensubtitles_feature(movie_hash)
        if feature:
            log(
                f"    empreinte reconnue : {feature.get('titre')} "
                f"({feature.get('annee')})"
            )

    tmdb_id = None
    via_hash = False  # identification autoritaire (empreinte) vs. recherche
    speculative = False  # match issu d'une requête reconstruite (rang > 0)
    if feature:
        tmdb_id = feature.get("tmdb_id")
        if not tmdb_id and feature.get("imdb_id") and TMDB_API_KEY:
            tmdb_id = tmdb_id_from_imdb(feature["imdb_id"])
        via_hash = bool(tmdb_id)

    if not tmdb_id and TMDB_API_KEY and FALLBACK_SEARCH:
        # Le moviehash change à chaque ré-encodage (02) : après conversion, la
        # plupart des fichiers ne sont plus connus d'OpenSubtitles. On se rabat
        # sur le titre/l'année déduits du nom, déjà nettoyé par 01.
        variants, year = query_variants(stem)
        for rank, title in enumerate(variants):
            log(f"    repli recherche TMDB : « {title} » ({year or 'année inconnue'})")
            tmdb_id = tmdb_search(title, year, stem)
            if not tmdb_id and year:
                # L'année du nom de fichier est souvent celle d'une sortie
                # nationale, décalée de celle de TMDB : on retente sans le
                # filtre. Le contrôle de vraisemblance reste appliqué derrière.
                log("    (rien avec cette année — nouvelle tentative sans)")
                tmdb_id = tmdb_search(title, None, stem)
            if tmdb_id:
                # rank > 0 : requête reconstruite, on exigera l'année exacte.
                speculative = rank > 0
                break

    if tmdb_id and TMDB_API_KEY:
        details = tmdb_details(tmdb_id)
        if details and not via_hash and not match_is_plausible(
            stem, details, strict=speculative
        ):
            log(
                f"    [!] Résultat écarté : « {details.get('titre')} » ne "
                "correspond pas au nom du fichier (recherche par titre)."
            )
            details = None
        if details:
            fields = dict(details)

    if fields is None and feature:
        # Pas de TMDB (pas de clé, ou fiche introuvable) : on garde ce que la
        # base de sous-titres a donné.
        fields = {
            "titre": feature.get("titre"),
            "titre_vo": feature.get("titre"),
            "annee": feature.get("annee"),
            "imdb_id": feature.get("imdb_id"),
            "tmdb_id": feature.get("tmdb_id"),
        }
    return fields


# ── Cache d'identification ───────────────────────────────────────────────────
def cache_get(cache: dict, key: str):
    """Entrée de cache exploitable : succès toujours valable, échec périmé après
    IDENTIFY_MISS_TTL_DAYS (les bases s'enrichissent avec le temps)."""
    entry = cache.get(key)
    if not entry:
        return None
    if entry.get("found"):
        return entry
    try:
        when = datetime.fromisoformat(entry.get("when", ""))
    except ValueError:
        return None
    return entry if datetime.now() - when < MISS_TTL else None


# ── Renommage ────────────────────────────────────────────────────────────────
def companion_files(video: Path):
    """Sous-titres voisins du film : même préfixe de nom, extension de sous-titre.

    « Joker.(2019).fr.srt » suit « Joker.(2019).mkv » et doit être renommé avec
    lui, suffixe de langue conservé — sinon la piste est orpheline.
    """
    out = []
    prefix = video.stem
    for sibling in sorted(video.parent.iterdir()):
        if not sibling.is_file() or sibling == video:
            continue
        if sibling.suffix.lower() not in SUBTITLE_EXTENSIONS:
            continue
        if sibling.stem == prefix or sibling.stem.startswith(prefix + "."):
            out.append((sibling, sibling.stem[len(prefix) :]))
    return out


def differs_only_by_case(old: str, new: str) -> bool:
    """Deux noms identiques à la casse près."""
    return old != new and old.lower() == new.lower()


def safe_rename(old: Path, new: Path) -> str:
    """Renomme sans jamais écraser. Retourne 'ok' | 'skip' | 'error'."""
    if old == new:
        return "same"
    if differs_only_by_case(old.name, new.name):
        # Le partage SMB du NAS est INSENSIBLE À LA CASSE : « …Love.And.Thunder »
        # -> « …Love.and.Thunder » n'est PAS appliqué par le serveur, qui renvoie
        # pourtant un succès. Le tenter ferait reproposer le même renommage à
        # chaque run, et le contournement par un nom intermédiaire a laissé un
        # fichier « .casetmp » en rade. Une différence de casse seule ne vaut pas
        # ce risque : le nom est considéré conforme.
        log(f"    [✓] Conforme à la casse près (SMB insensible) : {old.name}")
        return "same"
    try:
        if new.exists() and not os.path.samefile(old, new):
            log(f"    [!] CIBLE EXISTE : {new.name} — {old.name} non renommé")
            return "skip"
    except OSError as exc:
        log(f"    [✗] {old.name} | {exc}")
        return "error"
    if DRY_RUN:
        log(f"    [DRY RUN] {old.name}\n              → {new.name}")
        return "ok"
    try:
        os.rename(old, new)
        log(f"    [✓] {old.name}\n        → {new.name}")
        return "ok"
    except OSError as exc:
        log(f"    [✗] {old.name} | {exc}")
        return "error"


def collect_movies(roots):
    files = []
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if path.suffix.lower() in EXTENSIONS:
                    files.append(path)
    return sorted(files)


def main() -> int:
    validate_pattern(PATTERN)

    mode = "DRY RUN (simulation)" if DRY_RUN else "RENOMMAGE RÉEL"
    log("=" * 64)
    log("03 — identification en ligne des films (OpenSubtitles + TMDB)")
    log(f"Mode    : {mode}")
    log(f"Dossiers: {ROOTS}")
    log(f"Motif   : {PATTERN}")
    if getattr(config, "_OVERLAY_PATH", None):
        log(f"Config  : surcouche active — {config._OVERLAY_PATH}")
    if not OS_API_KEY:
        log("[!] IDENTIFY_OPENSUBTITLES_API_KEY absente : pas d'identification par")
        log("    empreinte, uniquement le repli par recherche de titre.")
    if not TMDB_API_KEY:
        log("[!] IDENTIFY_TMDB_API_KEY absente : pas de titre localisé.")
    if not OS_API_KEY and not TMDB_API_KEY:
        log("[✗] Aucune clé d'API : rien à interroger. Renseigne au moins TMDB")
        log("    (interface web -> Configuration, ou 00-config.local.py).")
        return 1
    log("=" * 64)

    cache = _common.load_scan_cache(CACHE_PATH, CACHE_VERSION)
    movies = collect_movies(ROOTS)
    log(f"{len(movies)} fichier(s) vidéo à examiner.")
    if MAX_FILES:
        movies = movies[:MAX_FILES]
        log(
            f"[!] IDENTIFY_MAX_FILES={MAX_FILES} : traitement limité "
            "au début de la liste."
        )

    identified = renamed = already = unknown = skipped = errors = 0
    collisions = 0
    api_calls = 0
    # Cibles déjà réservées par un film traité plus tôt : deux copies du même
    # film dans un dossier (« Desperado.(1995) » et « Tarantino.(1995).Desperado »)
    # visent le même nom. Sans ce registre, la collision n'apparaîtrait qu'en
    # mode RÉEL, au moment où le second renommage trouve la cible occupée — le
    # dry-run, lui, annoncerait tranquillement deux renommages. Même principe
    # que detect_collisions() dans 01.
    planned = {}

    for index, video in enumerate(movies, 1):
        log(f"\n[{index}/{len(movies)}] {video}")

        movie_hash = opensubtitles_hash(video)
        key = movie_hash or f"path:{video}"
        entry = cache_get(cache, key)
        if entry is None:
            fields = identify(video.stem, movie_hash)
            api_calls += 1
            if fields:
                cache[key] = {"found": True, **fields}
            elif TMDB_API_KEY:
                # Un échec n'est mémorisé que si la chaîne d'identification
                # était COMPLÈTE. Sans clé TMDB, aucune recherche n'a lieu :
                # mémoriser l'échec bloquerait le film pendant
                # IDENTIFY_MISS_TTL_DAYS une fois la clé enfin renseignée.
                cache[key] = {
                    "found": False,
                    "when": datetime.now().isoformat(timespec="seconds"),
                }
            if api_calls % CACHE_FLUSH_EVERY == 0:
                _common.save_scan_cache(CACHE_PATH, cache, CACHE_VERSION)
        elif entry.get("found"):
            fields = {k: v for k, v in entry.items() if k != "found"}
            log("    (déjà identifié — cache)")
        else:
            log("    (échec d'identification mémorisé — cache)")
            fields = None

        if not fields:
            log("    [!] Film non identifié — laissé tel quel.")
            unknown += 1
            continue
        identified += 1

        values = dict(fields)
        values["ext"] = video.suffix.lstrip(".").lower()
        new_name, missing = build_new_name(PATTERN, values)
        if missing:
            log(
                "    [!] Champ(s) indisponible(s) : "
                + ", ".join(sorted(set(missing)))
                + " — laissé tel quel."
            )
            skipped += 1
            continue
        # Racine du nom (sans extension) : sert aux sous-titres et au dossier.
        new_stem = Path(new_name).stem.strip(" .")

        reservation = (str(video.parent), new_name.lower())
        if reservation in planned:
            log(
                f"    [!] COLLISION : même cible que « {planned[reservation]} » "
                f"→ {new_name} — laissé tel quel (doublon dans la logithèque ?)"
            )
            collisions += 1
            continue
        planned[reservation] = video.name

        # Sous-titres d'abord : tant que le film n'a pas bougé, leur préfixe est
        # encore celui d'origine.
        targets = []
        if RENAME_SUBTITLES:
            for subtitle, suffix in companion_files(video):
                target = subtitle.with_name(new_stem + suffix + subtitle.suffix)
                targets.append((subtitle, target))
        targets.append((video, video.with_name(new_name)))

        results = [safe_rename(old, new) for old, new in targets]
        if "error" in results:
            errors += 1
        elif "skip" in results:
            skipped += 1
        elif all(r == "same" for r in results):
            log("    [✓] Déjà conforme au motif.")
            already += 1
        else:
            renamed += 1

        # Dossier dédié au film : on l'aligne sur le nouveau nom (sans extension).
        # Jamais une racine de IDENTIFY_FOLDERS, jamais un dossier partagé par
        # plusieurs films.
        if RENAME_FOLDER and "error" not in results:
            folder = video.parent
            roots = {str(Path(r)) for r in ROOTS}
            if str(folder) not in roots and folder.name != new_stem:
                videos_here = [
                    f for f in folder.iterdir() if f.suffix.lower() in EXTENSIONS
                ]
                if len(videos_here) == 1:
                    if safe_rename(folder, folder.with_name(new_stem)) == "error":
                        errors += 1

    _common.save_scan_cache(CACHE_PATH, cache, CACHE_VERSION)

    log("\n" + "=" * 64)
    log("BILAN FINAL")
    log(f"  Films examinés    : {len(movies)}")
    log(f"  Identifiés        : {identified}")
    log(f"  Renommés          : {renamed}")
    log(f"  Déjà conformes    : {already}")
    log(f"  Non identifiés    : {unknown}")
    log(f"  Ignorés           : {skipped}  (champ manquant ou cible existante)")
    log(f"  Collisions        : {collisions}  (deux fichiers → même nom)")
    log(f"  Erreurs           : {errors}")
    if DRY_RUN:
        log("  → Relance en mode réel (--real / interface web) pour appliquer.")
    log("=" * 64)

    # Comme 01 et 02 : sortie non nulle dès qu'une opération a échoué, pour que
    # le lanceur (set -e + trap ERR) notifie l'échec au lieu d'un « ✅ ».
    # Les collisions, elles, ne sont PAS des erreurs (rien n'est écrasé, le
    # fichier reste en place) — même convention que 01.
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
