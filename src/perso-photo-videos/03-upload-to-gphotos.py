##################################################################
## Upload d'une arborescence (produite par 02-compress-for-gphotos.py) vers
## Google Photos. Chaque dossier de PREMIER NIVEAU sous la racine devient un
## album du même nom ; seuls les fichiers situés DIRECTEMENT dans ce dossier
## sont envoyés (les sous-dossiers plus profonds sont ignorés).
##
##   racine/
##     Cambrils 2000/      -> album "Cambrils 2000"  (ses fichiers)
##       photo1.jpg                                   uploadés
##       sous-dossier/      <- IGNORÉ
##     Cannes 2000/        -> album "Cannes 2000"
##
## Idempotent : un état local (STATE_FILE) mémorise nom d'album -> albumId et
## les fichiers déjà envoyés, pour relancer sans recréer ni ré-uploader.
##
## Dépendances non déclarées (cf. CLAUDE.md) :
##   pip install google-auth-oauthlib requests
##
## Authentification : OAuth 2.0 « application installée ». Il faut un fichier
## client OAuth (type « Desktop ») téléchargé depuis Google Cloud Console, avec
## l'API « Photos Library » activée. Au premier lancement, le script affiche une
## URL de consentement ; le jeton obtenu est mis en cache (TOKEN_FILE) et
## rafraîchi automatiquement ensuite.
##################################################################

import os
import sys
import json
import time
import mimetypes
import logging
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

##################################################################
## Configuration (surchargeable par variables d'environnement)
##################################################################

# Racine à parcourir : 1er argument de la ligne de commande, sinon $GPHOTOS_ROOT,
# sinon le dossier de sortie par défaut de 02-compress-for-gphotos.py.
_DEFAULT_ROOT = str(Path(__file__).resolve().parent.parent.parent / "output" / "gphotos")
ROOT = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GPHOTOS_ROOT", _DEFAULT_ROOT)

# Secrets/État : par défaut dans env/ (gitignoré).
_ENV_DIR = Path(__file__).resolve().parent.parent.parent / "env"
CREDENTIALS_FILE = os.environ.get("GPHOTOS_CREDENTIALS", str(_ENV_DIR / "gphotos_client_secret.json"))
TOKEN_FILE = os.environ.get("GPHOTOS_TOKEN", str(_ENV_DIR / "gphotos_token.json"))
STATE_FILE = os.environ.get("GPHOTOS_STATE", str(_ENV_DIR / "gphotos_state.json"))

# DRY_RUN : True = simulation (aucun appel d'écriture vers Google). Défaut sûr.
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes", "on")

# appendonly suffit pour : uploader, créer un album, ajouter à un album créé par
# l'app. Pas de scope lecture nécessaire (on déduplique via l'état local).
SCOPES = ["https://www.googleapis.com/auth/photoslibrary.appendonly"]

API = "https://photoslibrary.googleapis.com/v1"
UPLOAD_URL = f"{API}/uploads"
BATCH_SIZE = 50  # mediaItems:batchCreate accepte 50 éléments max par appel

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".mpg", ".mpeg", ".wmv", ".flv", ".webm"}
MEDIA_EXTS = PHOTO_EXTS | VIDEO_EXTS

##################################################################


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger(__name__)


# ── État local (idempotence) ─────────────────────────────────────────────────


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("albums", {})     # nom d'album -> albumId
    data.setdefault("uploaded", {})   # chemin relatif -> mediaItemId
    return data


def save_state(state: dict) -> None:
    if DRY_RUN:
        return
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)  # écriture atomique


# ── Authentification ─────────────────────────────────────────────────────────


def get_credentials(logger: logging.Logger) -> Credentials:
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        logger.info("Jeton expiré, rafraîchissement…")
        creds.refresh(Request())
    else:
        if not os.path.exists(CREDENTIALS_FILE):
            logger.error(
                "Fichier client OAuth introuvable : %s\n"
                "  -> Google Cloud Console > API Photos Library activée,\n"
                "     identifiants OAuth de type « Desktop », JSON téléchargé ici.",
                CREDENTIALS_FILE,
            )
            sys.exit(1)
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        # open_browser=False : en conteneur/SSH, on copie l'URL à la main.
        creds = flow.run_local_server(port=0, open_browser=False)

    # Persiste le jeton (refresh token inclus) pour les prochains lancements.
    Path(TOKEN_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    return creds


# ── Appels API Google Photos ─────────────────────────────────────────────────


def get_or_create_album(session, state, title, logger) -> str:
    """Retourne l'albumId pour `title`, le créant au besoin (mémorisé en état)."""
    existing = state["albums"].get(title)
    if existing:
        return existing

    if DRY_RUN:
        logger.info("[DRY RUN] créerait l'album : %s", title)
        return "DRYRUN-ALBUM"

    resp = session.post(f"{API}/albums", json={"album": {"title": title}})
    resp.raise_for_status()
    album_id = resp.json()["id"]
    state["albums"][title] = album_id
    save_state(state)
    logger.info("Album créé : %s", title)
    return album_id


def upload_bytes(session, filepath: Path, logger) -> str | None:
    """Envoie le binaire ; renvoie un upload token (à passer à batchCreate)."""
    mime = mimetypes.guess_type(filepath.name)[0] or "application/octet-stream"
    with open(filepath, "rb") as fh:
        data = fh.read()
    headers = {
        "Content-type": "application/octet-stream",
        "X-Goog-Upload-Content-Type": mime,
        "X-Goog-Upload-Protocol": "raw",
    }
    resp = session.post(UPLOAD_URL, data=data, headers=headers)
    if resp.status_code != 200 or not resp.text:
        logger.error("Échec upload binaire %s : %s %s", filepath.name, resp.status_code, resp.text[:200])
        return None
    return resp.text


def batch_create(session, album_id, items, state, logger) -> int:
    """Crée les mediaItems (par paquets de BATCH_SIZE) dans l'album. items =
    liste de (chemin_relatif, nom_fichier, upload_token). Renvoie le nb créés."""
    created = 0
    for i in range(0, len(items), BATCH_SIZE):
        chunk = items[i : i + BATCH_SIZE]
        body = {
            "albumId": album_id,
            "newMediaItems": [
                {"simpleMediaItem": {"fileName": name, "uploadToken": token}}
                for (_rel, name, token) in chunk
            ],
        }
        resp = session.post(f"{API}/mediaItems:batchCreate", json=body)
        resp.raise_for_status()
        results = resp.json().get("newMediaItemResults", [])
        for (rel, _name, _token), res in zip(chunk, results):
            status = res.get("status", {})
            # code 0 (ou absent) = OK ; sinon on logue et on ne mémorise pas.
            if status.get("code", 0) in (0, None):
                state["uploaded"][rel] = res.get("mediaItem", {}).get("id", "")
                created += 1
            else:
                logger.error("mediaItem refusé (%s) : %s", status.get("message"), rel)
        save_state(state)
    return created


# ── Parcours & orchestration ─────────────────────────────────────────────────


def media_files_in(folder: Path):
    """Fichiers média DIRECTEMENT dans `folder` (sous-dossiers ignorés)."""
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS
    )


def main() -> int:
    logger = setup_logging()
    root = Path(ROOT)

    mode = "DRY RUN (simulation)" if DRY_RUN else "UPLOAD RÉEL"
    logger.info("=" * 64)
    logger.info("Mode    : %s", mode)
    logger.info("Racine  : %s", root)
    logger.info("Règle   : 1 dossier de niveau 1 = 1 album (sous-dossiers ignorés)")
    logger.info("=" * 64)

    if not root.is_dir():
        logger.error("Racine introuvable : %s", root)
        return 1

    state = load_state()

    # Authentification + session HTTP authentifiée (sauf en dry-run, où l'on ne
    # contacte pas Google du tout).
    session = None
    if not DRY_RUN:
        creds = get_credentials(logger)
        session = requests.Session()
        session.headers.update({"Authorization": f"Bearer {creds.token}"})

    first_level = sorted(p for p in root.iterdir() if p.is_dir())

    # Fichiers à la racine (hors dossier) : pas d'album associé -> signalés.
    loose = media_files_in(root)
    if loose:
        logger.warning("%d fichier(s) à la racine sans album, ignoré(s).", len(loose))

    albums_done = uploaded = skipped = errors = 0

    for folder in first_level:
        album = folder.name
        files = media_files_in(folder)
        if not files:
            logger.info("Album vide, ignoré : %s", album)
            continue

        logger.info("\n── Album : %s  (%d fichier(s)) ──", album, len(files))

        # Fichiers pas encore envoyés (idempotence via état local).
        pending = []
        for f in files:
            rel = str(f.relative_to(root))
            if rel in state["uploaded"]:
                skipped += 1
                continue
            pending.append(f)

        if not pending:
            logger.info("  Tout déjà envoyé, rien à faire.")
            albums_done += 1
            continue

        album_id = get_or_create_album(session, state, album, logger)

        if DRY_RUN:
            for f in pending:
                logger.info("  [DRY RUN] uploaderait : %s", f.name)
            uploaded += len(pending)
            albums_done += 1
            continue

        # Upload binaire de chaque fichier -> upload token.
        tokens = []
        for f in pending:
            token = upload_bytes(session, f, logger)
            if token is None:
                errors += 1
                continue
            tokens.append((str(f.relative_to(root)), f.name, token))
            logger.info("  uploadé : %s", f.name)

        # Création des mediaItems dans l'album (par paquets de 50).
        if tokens:
            try:
                created = batch_create(session, album_id, tokens, state, logger)
                uploaded += created
            except requests.HTTPError as e:
                logger.error("  batchCreate échoué pour %s : %s", album, e)
                errors += len(tokens)

        albums_done += 1
        time.sleep(0.2)  # léger throttle pour ménager le quota API

    logger.info("\n" + "=" * 64)
    logger.info("BILAN")
    logger.info("  Albums traités       : %d", albums_done)
    logger.info("  Fichiers uploadés    : %d", uploaded)
    logger.info("  Déjà envoyés (sautés): %d", skipped)
    logger.info("  Erreurs              : %d", errors)
    if DRY_RUN:
        logger.info("  → DRY_RUN=false (ou --DRY_RUN=false via le .sh) pour envoyer réellement.")
    logger.info("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
