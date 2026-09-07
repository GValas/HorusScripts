# HorusScripts

Outils pour gérer le NAS **horus** (partage SMB/CIFS : `movies`, `tvshows`,
`cartoons`, `photos`, `perso`). Le dépôt contient **deux pipelines** indépendants,
chacun avec son lanceur à la racine. Tout tourne dans une **image Docker unique**
(`horus-convert-h265`) qui embarque toutes les dépendances (ffmpeg/NVENC, Pillow,
piexif, rclone).

| Pipeline | Dossier | Lanceur | Rôle |
|---|---|---|---|
| **Public** | [`src/public-media/`](src/public-media/) | [`run-public-media-pipeline.sh`](run-public-media-pipeline.sh) | Nettoie les noms + ré-encode films/séries en HEVC + identifie les films en ligne |
| **Perso** | [`src/perso-media/`](src/perso-media/) | [`run-perso-media-pipeline.sh`](run-perso-media-pipeline.sh) | Photos/vidéos perso : normalise → date → compresse → upload Google Photos |

> ⚠️ Le NAS doit toujours être adressé via **`/mnt/wsl/horus`**, jamais
> `/mnt/horus` (sinon les conteneurs voient des dossiers vides — cf. la section
> montage). Cette règle vaut aussi pour les exécutions locales.

---

## Pipeline public — `src/public-media/`

Quatre étapes sur les mêmes dossiers (`INPUT_FOLDERS`), **01 et 02 par défaut**,
**03 et 04 à la demande** :

1. [`01-clean-names`](src/public-media/01-clean-names.py) — nettoie les noms de
   dossiers/fichiers (retire `1080p`, `x264`…, normalise casse et séparateurs, met
   l'année entre parenthèses), avec détection de collisions. Ne renomme que les
   **vidéos et leurs sous-titres** (`CLEAN_VIDEO_EXTENSIONS` /
   `CLEAN_SUBTITLE_EXTENSIONS`) — jaquettes et `.nfo` restent intacts — et
   **préserve le suffixe de langue** des sous-titres (`…fr.srt` / `…en.srt` ne
   peuvent plus se retrouver sur le même nom).
2. [`02-convert-to-h265`](src/public-media/02-convert-to-h265.py) — ré-encode
   récursivement tout ce qui n'est pas déjà en HEVC vers x265 via **NVENC** (GPU
   NVIDIA), conserve toutes les pistes audio/sous-titres, puis **remplace
   l'original** après vérification du nombre de pistes.
3. [`03-identify-movies`](src/public-media/03-identify-movies.py) — **mode
   identification en ligne** (optionnel). Calcule l'empreinte *moviehash* de
   chaque film — la clé d'indexation de la **base de sous-titres OpenSubtitles**,
   celle qui sert à retrouver les sous-titres d'une copie donnée — interroge
   cette base pour savoir **de quel film il s'agit**, complète titre et année
   via **TMDB**, puis renomme le film, ses sous-titres et son dossier selon
   `IDENTIFY_PATTERN` (défaut : `{titre}.({yyyy}).{ext}` → `Joker.(2019).mkv`).
   Garde-fous : rien n'est renommé si un champ du motif manque, si la cible
   existe déjà, si **deux fichiers visent le même nom** (collision signalée dès
   le dry-run, comme dans 01) ou si le film trouvé ne ressemble pas au nom du
   fichier ; les résultats sont mis en cache (`.identify-cache.json`) pour
   ménager les quotas.

4. [`04-slim-audio`](src/public-media/04-slim-audio.py) — **allègement audio**
   (optionnel). Sur une logithèque déjà en HEVC, les pistes **lossless**
   (TrueHD Atmos, DTS-HD MA, PCM) pèsent souvent **plus que la vidéo** —
   jusqu'aux deux tiers du fichier. Cette étape les ré-encode en EAC3 (ou Opus)
   et **copie la vidéo bit à bit** : aucune perte d'image, aucune génération
   d'encodage, quelques dizaines de secondes par film. Les langues sont
   conservées ; les doublons ne sont supprimés que sur demande explicite
   (`AUDIO_DROP_DUPLICATE_LANGUAGES`), deux pistes d'une même langue étant
   souvent deux doublages différents. Le remplacement n'a lieu qu'après
   vérification du nombre de pistes, de la durée et du gain réel.

Réglages centralisés dans [`src/public-media/00-config.py`](src/public-media/00-config.py)
(`NAS_MOUNT`, `INPUT_FOLDERS`, `DRY_RUN`, `CLEAN_*`, `CONVERT_*`, `IDENTIFY_*`,
`AUDIO_*`),
partagés par les scripts — même principe que le pipeline perso, plus d'`env/.env`.
Lancement :

```bash
./run-public-media-pipeline.sh          # 01 + 02 ; confirmation si DRY_RUN=False
./run-public-media-pipeline.sh -y       # sans confirmation
./run-public-media-pipeline.sh 03       # uniquement l'identification en ligne
./run-public-media-pipeline.sh 04       # uniquement l'allègement audio
./run-public-media-pipeline.sh 01 02 03 04  # tout
```

> L'étape 03 exige au moins une **clé d'API TMDB** (gratuite,
> [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)) pour le
> titre et l'année, et idéalement une clé **OpenSubtitles**
> ([opensubtitles.com/consumers](https://www.opensubtitles.com/consumers)) pour
> l'identification par empreinte. Sans clé OpenSubtitles — ou après un
> ré-encodage par 02, qui change l'empreinte — 03 se rabat sur une recherche
> TMDB par titre/année déduits du nom de fichier. **Ne jamais écrire ces clés
> dans `00-config.py`** (fichier versionné) : les saisir dans l'interface web,
> qui les enregistre dans `00-config.local.py` (gitignoré).

---

## Pipeline perso — `src/perso-media/`

Quatre étapes numérotées, **toutes réglées depuis un seul fichier**,
[`00-config.py`](src/perso-media/00-config.py) (aucune variable d'env). Il
contient un bloc COMMUN (un `DRY_RUN` unique, `PHOTO_EXT=.jpg`, `VIDEO_EXT=.mkv`,
réglages NVENC, racine NAS, bornes d'années) puis un bloc par étape.

Chaque étape parcourt le partage `photos` et **ignore les dossiers commençant par
`_`**. Les étapes 01 et 02 commencent par **nettoyer les fichiers temporaires**
(`*.h265tmp.mkv`, `*.datetmp.mkv`) qu'un run interrompu aurait laissés : ils
portent l'extension `.mkv` et seraient sinon pris pour de vraies vidéos.

1. [`01-convert-to-mkv+h265`](src/perso-media/01-convert-to-mkv+h265.py) —
   **normalisation de format uniquement** : toute vidéo finit en **H.265 + MKV**
   quel que soit le codec/conteneur d'origine (NVENC obligatoire) ; `.jpeg`→`.jpg`.
   Conserve les tags de date existants mais **n'en infère pas** (c'est le rôle de 02).
2. [`02-enrich-movies-photos-with-date`](src/perso-media/02-enrich-movies-photos-with-date.py) —
   **inférence des dates** manquantes, par priorité : tag existant → **sidecar JSON
   Google Takeout** (`photoTakenTime`) → nom de fichier (`YYYYMMDD_HHMMSS`,
   `2022-06-19 at 21.59.44`…) → photo voisine du même dossier → dossier parent
   `YY.MM` → date de modification du fichier. Les dates sont manipulées en heure
   de Paris et écrites en UTC dans les vidéos (ffmpeg lit `creation_time` comme de
   l'UTC : écrire une heure locale décalerait la chronologie de 1 à 2 h).
3. [`03-compress-for-gphotos`](src/perso-media/03-compress-for-gphotos.py) —
   écrit une **copie compressée** dans `output/gphotos` (photos redimensionnées,
   vidéos ré-encodées 720p). Ne produit que `.jpg` + `.mkv`.
4. [`04-upload-to-gphotos.sh`](src/perso-media/04-upload-to-gphotos.sh) —
   **upload via rclone** de `output/gphotos` vers Google Photos ; chaque dossier de
   premier niveau devient un album du même nom. Auth dans `env/rclone.conf`
   (gitignoré, cf. Configuration).

Lancement via l'orchestrateur (monte les scripts en *live* → éditer `00-config.py`
prend effet sans rebuild ; lance le conteneur en `--user` pour que les fichiers
produits t'appartiennent) :

```bash
./run-perso-media-pipeline.sh            # toutes les étapes ; confirme si DRY_RUN=False
./run-perso-media-pipeline.sh -y         # sans confirmation
./run-perso-media-pipeline.sh 03 04      # un sous-ensemble d'étapes seulement
```

---

## Interface web — pilotage des deux pipelines

Une petite **appli web locale** ([`src/gui/`](src/gui/)) permet de lancer et
suivre les pipelines depuis un navigateur, plutôt qu'en ligne de commande. Elle
est en **Python stdlib uniquement** (aucune dépendance pip sur l'hôte) et ne
réimplémente aucune logique : elle **appelle les lanceurs ci-dessus** en
sous-processus et streame leurs logs.

```bash
./run-gui.sh                  # http://127.0.0.1:8765 (écoute aussi le LAN)
./run-gui.sh 9000             # autre port
./run-gui.sh 8765 127.0.0.1   # local uniquement (pas d'accès réseau)
```

Depuis la page tu peux :

- **lancer** un pipeline en mode **simulation (dry-run)** ou **réel** (mappé sur
  les flags `--dry-run`/`--real` des lanceurs ; le mode réel demande confirmation) ;
- choisir le **sous-ensemble d'étapes** : `01/02/03/04` pour le perso,
  `01/02/03` pour le public (l'étape `03`, identification en ligne, est
  **décochée par défaut**) ;
- suivre les **logs en direct** (Server-Sent Events) avec l'état et le code de sortie ;
- **arrêter** un run en cours (`SIGINT` au groupe de processus, escalade `SIGKILL`) ;
- **éditer les réglages** (NVENC, workers, `PHOTOS_SRC`/`NAS_MOUNT`,
  `INPUT_FOLDERS`, webhook…) via un formulaire. Les valeurs sont écrites dans une
  **surcouche `00-config.local.py`** (gitignorée, générée), chargée *après*
  `00-config.py` par les scripts comme par les lanceurs. Le fichier versionné
  n'est jamais réécrit : plus de bruit dans `git status` à chaque lancement, et
  supprimer la surcouche suffit à revenir aux valeurs par défaut. Un bandeau
  signale la surcouche quand elle est active.

Un seul run à la fois (le GPU/Docker travaillent en série). Recharger la page
pendant un run reprend le suivi des logs.

> ⚠️ Par défaut le serveur écoute sur `0.0.0.0` (accessible depuis le LAN).
> L'interface peut déclencher des opérations **destructrices** → à n'exposer que
> sur un réseau de confiance ; sinon binder sur `127.0.0.1` (3ᵉ argument).
> Pour y accéder depuis un téléphone sous **WSL2**, l'IP affichée est l'IP interne
> WSL (NAT) : il faut un `netsh interface portproxy` depuis Windows vers cette IP,
> puis viser l'IP LAN du PC Windows.

---

## Prérequis hôte (une seule fois)

### GPU NVIDIA (les deux pipelines encodent en NVENC)

- Un **GPU NVIDIA** avec pilote installé sur l'hôte (`nvidia-smi` fonctionne).
- **nvidia-container-toolkit** installé et configuré pour Docker :

  ```bash
  sudo apt install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
  ```

  Vérifier que Docker voit le GPU :

  ```bash
  docker run --rm --gpus all nvidia/cuda:13.1.2-runtime-ubuntu24.04 nvidia-smi
  ```

  > Les lanceurs ajoutent `NVIDIA_DRIVER_CAPABILITIES=all` : `--gpus all` seul
  > n'expose pas l'encodeur `libnvidia-encode` requis par NVENC.

### Client SMB + montage du NAS (Ubuntu WSL)

```bash
sudo apt install cifs-utils
```

Le montage est assuré par un script `/etc/mount-nas-horus.sh` (hors repo, config
machine) appelé au démarrage par `/etc/wsl.conf` (`[boot] command = …`). Il monte
les partages CIFS du NAS horus (`photos movies tvshows cartoons`) avec les
credentials de `~/.nas-credentials`.

Subtilité importante : Docker Desktop tourne dans une distro WSL séparée qui ne
voit le FS d'Ubuntu que via `/mnt/wsl` (propagation `shared`). Un montage CIFS
placé directement sous `/mnt/horus` est `private` → **invisible dans les
conteneurs**. Le script monte donc le CIFS réel sous `/mnt/wsl/horus/*` (visible
par Docker, `--make-shared`) puis fait un bind-miroir vers `/mnt/horus/*` pour le
confort. **Utiliser `/mnt/wsl/horus` partout** (y compris `NAS_MOUNT`). Idempotent :
relançable à la main avec `sudo /etc/mount-nas-horus.sh`.

`/etc/mount-nas-horus.sh` (à créer une fois en `sudo`) :

```sh
#!/bin/sh
set -eu
HOST=//192.168.1.182
CRED=/home/gege/.nas-credentials
OPTS="credentials=$CRED,uid=1000,gid=1000,iocharset=utf8,nofail"
SHARES="photos movies tvshows cartoons"
for share in $SHARES; do
    wsl_dir=/mnt/wsl/horus/$share
    host_dir=/mnt/horus/$share
    mkdir -p "$wsl_dir" "$host_dir"
    mountpoint -q "$wsl_dir" || mount -t cifs "$HOST/$share" "$wsl_dir" -o "$OPTS"
    mount --make-shared "$wsl_dir"
    mountpoint -q "$host_dir" || mount --bind "$wsl_dir" "$host_dir"
done
```

`/etc/wsl.conf` :

```ini
[boot]
systemd=true
command = /etc/mount-nas-horus.sh
```

Les lignes CIFS de `/etc/fstab` doivent être neutralisées (commentées) — un
montage CIFS via `fstab` se fait en propagation `private` et reste invisible dans
les conteneurs. Faire des sauvegardes (`/etc/wsl.conf.bak`, `/etc/fstab.bak`)
avant. Tester le déclenchement au boot : `wsl --shutdown` (PowerShell) puis
relancer Ubuntu.

> ⚠️ Adapter l'IP (`HOST`) et la liste des partages (`SHARES`) en tête de
> `/etc/mount-nas-horus.sh` si nécessaire.

---

## Configuration

### Public — `src/public-media/00-config.py`

Tous les réglages sont des variables Python dans ce fichier (pas d'env), chargé
par les deux scripts via `importlib` — même principe que le pipeline perso.

| Variable | Défaut | Description |
|---|---|---|
| `NAS_MOUNT` | `/mnt/wsl/horus` | Point de montage NAS (monté tel quel dans le conteneur) |
| `INPUT_FOLDERS` | `…/tvshows`, `…/movies`, `…/cartoons` | Dossiers à scanner (doivent être **sous** `NAS_MOUNT`) |
| `DRY_RUN` | `True` | `True` = simulation, aucun fichier renommé/écrit/supprimé |
| `CONVERT_CQ` | `26` | Qualité NVENC (plus bas = meilleur, 24–28 conseillé) |
| `CONVERT_PRESET` | `p4` | Préréglage NVENC, `p1` (rapide) → `p7` (qualité) |
| `CONVERT_EXTENSIONS` | `.mkv .mp4 .avi .m4v .mov` | Conteneurs vidéo scannés |
| `CONVERT_SKIP_SUFFIX` | `_x265` | Suffixe des fichiers déjà convertis (ignorés) |
| `CONVERT_MAX_RESOLUTION` | `None` | Résolution max de sortie (`"720p"`, `"1080p"`, `"1440p"`, `"2160p"`/`"4k"`). Les fichiers plus grands sont downscalés (HDR 10-bit préservé, Dolby Vision perdu). `None` = pas de downscale |
| `CONVERT_MAX_BITRATE` | `None` | Débit max en **Mb/s** (taille × 8 / durée). Au-dessus, le fichier est ré-encodé **même déjà en HEVC et à la bonne résolution** — seul réglage qui vise la *taille* et non la définition. Si le débit est la seule raison et que la sortie n'est pas plus petite, l'original est conservé. `None`/`0` = désactivé |
| `CLEAN_TECH_WORDS` | (liste) | Mots techniques retirés des noms par 01 |
| `CLEAN_VIDEO_EXTENSIONS` | `.mkv .mp4 .avi …` | Vidéos renommées par 01 |
| `CLEAN_SUBTITLE_EXTENSIONS` | `.srt .ass .sub …` | Sous-titres renommés par 01 (suffixe de langue préservé) |
| `CLEAN_SUBTITLE_LANG_TOKENS` | (liste) | Jetons de langue/variante ré-attachés aux sous-titres (`fr`, `en`, `forced`…) |
| `CONVERT_SCAN_CACHE` | `.scan-cache.json` | Cache ffprobe (codec/dimensions), sauvegardé périodiquement ; `None` pour désactiver |
| `CONVERT_SCAN_WORKERS` | `8` | Sondes ffprobe parallèles au scan (l'encodage reste séquentiel) |
| `NOTIFY_WEBHOOK` | `None` | URL ntfy/webhook notifiée en fin de run (succès **et** échec) |
| `IDENTIFY_FOLDERS` | `…/movies` | Dossiers scannés par 03 — **films uniquement** (le motif n'a pas de sens pour une série) |
| `IDENTIFY_PATTERN` | `{titre}.({yyyy}).{ext}` | Motif de renommage. Champs : `{annee}` (`{année}`, `{yyyy}`), `{titre}`, `{titre_vo}`, `{ext}` (`{extension}`, obligatoire) |
| `IDENTIFY_OPENSUBTITLES_API_KEY` | `None` | Clé OpenSubtitles (identification par empreinte). **À saisir via l'interface web**, jamais ici |
| `IDENTIFY_TMDB_API_KEY` | `None` | Clé TMDB v3 (titre localisé, année). Idem |
| `IDENTIFY_LANGUAGE` | `fr-FR` | Langue des titres demandés à TMDB |
| `IDENTIFY_SPACE_REPLACEMENT` | `"."` | Remplacement des espaces dans les champs ; vide = espaces conservés |
| `IDENTIFY_FALLBACK_TITLE_SEARCH` | `True` | Repli recherche TMDB par titre/année du nom de fichier quand l'empreinte est inconnue |
| `IDENTIFY_RENAME_SUBTITLES` | `True` | Renomme aussi les sous-titres voisins (suffixe de langue conservé) |
| `IDENTIFY_RENAME_FOLDER` | `True` | Renomme le dossier du film s'il n'en contient qu'un |
| `IDENTIFY_EXTENSIONS` | `.mkv .mp4 .avi .m4v .mov` | Conteneurs considérés comme des films par 03 |
| `IDENTIFY_CACHE` / `IDENTIFY_MISS_TTL_DAYS` | `.identify-cache.json` / `30` | Cache des identifications (les échecs sont re-tentés après ce délai) ; `None` pour désactiver |
| `IDENTIFY_REQUEST_DELAY` / `IDENTIFY_MAX_FILES` | `0.5` / `0` | Délai entre appels d'API (s) et plafond de films par run (`0` = illimité) |
| `AUDIO_MAX_BITRATE` | `1.0` | Débit max d'une piste audio, en **Mb/s**. Au-dessus, la piste est ré-encodée. Repères : AC3 5.1 = 0,45 ; EAC3 7.1 = 0,90 ; DTS core = 1,5 ; DTS-HD MA et TrueHD = 4 à 8. `0` = étape désactivée |
| `AUDIO_TARGET_CODEC` | `eac3` | `eac3` (lu partout, **6 canaux max** côté encodeur ffmpeg : un 7.1 devient 5.1) ou `libopus` (garde le 7.1, lecteurs récents) |
| `AUDIO_TARGET_BITRATE_KBPS` | `640` | Débit cible en 5.1/7.1 ; réduit à 112 k par canal en deçà |
| `AUDIO_MAX_CHANNELS` | `6` | Downmix au-delà (limite de l'encodeur eac3). `None` = jamais |
| `AUDIO_DROP_DUPLICATE_LANGUAGES` | `False` | Supprimer les pistes surnuméraires d'une même langue — à activer en connaissance de cause |
| `AUDIO_SCAN_CACHE` / `AUDIO_SCAN_WORKERS` | `.audio-cache.json` / `8` | Cache des débits mesurés (TrueHD/DTS-HD ne les déclarent pas) et sondes parallèles |

`NAS_MOUNT` est aussi consommé par `docker compose` pour le mapping de volume :
le lanceur le lit dans `00-config.py` et l'exporte avant `docker compose up`.

### Perso — `src/perso-media/00-config.py`

Tous les réglages sont des variables Python dans ce fichier (pas d'env). Le plus
important : `DRY_RUN` (commun aux 4 étapes, **défaut `True` = simulation**). Le
passer à `False` déclenche les écritures réelles **et l'upload Google Photos**.

### Auth upload — `env/rclone.conf`

```bash
cp env/rclone.conf.example env/rclone.conf   # gitignored
```

Remplir `client_id` / `client_secret` / `token`. Le token s'obtient avec
`rclone authorize "google photos"` sur une machine avec navigateur + rclone (ex.
`rclone.exe` sous Windows) — aucun rclone requis sur cet hôte. Le lanceur monte ce
fichier dans le conteneur au runtime ; **les secrets ne sont jamais dans l'image
ni commités** (seul le `.example` à placeholders est versionné).

---

## Tests

Les fonctions pures (nettoyage de noms, parsing de dates et fuseaux, inférence
depuis le nom de fichier ou le dossier, cache de scan, surcouche de config) sont
couvertes par une suite **stdlib**, sans NAS, sans GPU et sans dépendance pip :

```bash
python3 -m unittest discover -s tests -v
```

## Codes de sortie

Chaque script renvoie **1 si au moins une opération a échoué**, 0 sinon. Les
lanceurs (`set -e` + `trap ERR`) s'arrêtent alors à l'étape fautive et la
notification `NOTIFY_WEBHOOK` signale l'échec — un run entièrement raté ne peut
plus être annoncé comme un succès.

---

## Dev — lancer dans le dev container

1. Ouvrir le dossier dans VS Code.
2. VS Code détecte `.devcontainer/` → **"Reopen in Container"**.
3. Lancer un script depuis le terminal intégré, ex. :

```bash
python src/public-media/02-convert-to-h265.py
```

---

## Arborescence

```
HorusScripts/
├── run-public-media-pipeline.sh           # Lanceur public (docker compose)
├── run-perso-media-pipeline.sh          # Lanceur perso (orchestre 01→04 en docker run)
├── run-gui.sh                            # Lanceur de l'interface web (sert src/gui)
├── src/
│   ├── notify.py                   # Notification de fin de run (commune aux 2 pipelines)
│   ├── public-media/
│   │   ├── 00-config.py            # Réglages centralisés des 4 scripts
│   │   ├── 00-config.local.py      # Surcouche générée par l'interface web (gitignorée)
│   │   ├── _common.py              # Config, espace disque, cache de scan (+ lecture CLI)
│   │   ├── 01-clean-names.py       # Renommage (avant conversion)
│   │   ├── 02-convert-to-h265.py   # Ré-encodage NVENC HEVC
│   │   ├── 03-identify-movies.py   # Identification en ligne + renommage (optionnel)
│   │   └── 04-slim-audio.py        # Allègement des pistes audio (optionnel)
│   ├── perso-media/
│   │   ├── 00-config.py            # Réglages centralisés des 4 étapes
│   │   ├── 00-config.local.py      # Surcouche générée par l'interface web (gitignorée)
│   │   ├── _common.py              # Config, dates/fuseaux, exclusions, cache de scan
│   │   ├── 01-convert-to-mkv+h265.py
│   │   ├── 02-enrich-movies-photos-with-date.py
│   │   ├── 03-compress-for-gphotos.py
│   │   └── 04-upload-to-gphotos.sh # Upload rclone → Google Photos
│   ├── gui/                        # Interface web locale (stdlib)
│   │   ├── server.py               # Serveur HTTP + moteur de run + édition config
│   │   └── index.html              # Page unique (onglets, logs live, formulaires)
│   └── archives/                   # Scratch gitignoré (ancien uploader, audits, CSV)
├── tests/                          # Tests unittest (stdlib, ni NAS ni GPU requis)
├── env/
│   └── rclone.conf / .example      # Auth Google Photos (rclone.conf gitignored)
├── .devcontainer/                  # Dev container VS Code
├── Dockerfile                      # Image unique horus-convert-h265 (CUDA + ffmpeg + Pillow/piexif/rclone)
├── docker-compose.yml              # Service convert-h265 (public)
├── requirements.txt                # Vide (dépendances dans l'image Docker)
└── README.md
```
