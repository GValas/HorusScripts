# HorusScripts

Outils pour gérer le NAS **horus** (partage SMB/CIFS : `movies`, `tvshows`,
`cartoons`, `photos`, `perso`). Le dépôt contient **deux pipelines** indépendants,
chacun avec son lanceur à la racine. Tout tourne dans une **image Docker unique**
(`horus-convert-h265`) qui embarque toutes les dépendances (ffmpeg/NVENC, Pillow,
piexif, rclone).

| Pipeline | Dossier | Lanceur | Rôle |
|---|---|---|---|
| **Public** | [`src/public-media/`](src/public-media/) | [`run-public-media-pipeline.sh`](run-public-media-pipeline.sh) | Nettoie les noms + ré-encode films/séries en HEVC |
| **Perso** | [`src/perso-media/`](src/perso-media/) | [`run-perso-media-pipeline.sh`](run-perso-media-pipeline.sh) | Photos/vidéos perso : normalise → date → compresse → upload Google Photos |

> ⚠️ Le NAS doit toujours être adressé via **`/mnt/wsl/horus`**, jamais
> `/mnt/horus` (sinon les conteneurs voient des dossiers vides — cf. la section
> montage). Cette règle vaut aussi pour les exécutions locales.

---

## Pipeline public — `src/public-media/`

Deux scripts enchaînés sur les mêmes dossiers (`INPUT_FOLDERS`) :

1. [`01-clean-names`](src/public-media/01-clean-names.py) — nettoie les noms de
   dossiers/fichiers (retire `1080p`, `x264`…, normalise casse et séparateurs, met
   l'année entre parenthèses), avec détection de collisions.
2. [`02-convert-to-h265`](src/public-media/02-convert-to-h265.py) — ré-encode
   récursivement tout ce qui n'est pas déjà en HEVC vers x265 via **NVENC** (GPU
   NVIDIA), conserve toutes les pistes audio/sous-titres, puis **remplace
   l'original** après vérification du nombre de pistes.

Réglages centralisés dans [`src/public-media/00-config.py`](src/public-media/00-config.py)
(`NAS_MOUNT`, `INPUT_FOLDERS`, `DRY_RUN`, `CLEAN_*`, `CONVERT_*`), partagés par
les deux scripts — même principe que le pipeline perso, plus d'`env/.env`. Lancement :

```bash
./run-public-media-pipeline.sh        # demande confirmation si DRY_RUN=False
./run-public-media-pipeline.sh -y     # sans confirmation
# Tout autre argument est transmis à `docker compose up` (ex: -d).
```

---

## Pipeline perso — `src/perso-media/`

Quatre étapes numérotées, **toutes réglées depuis un seul fichier**,
[`00-config.py`](src/perso-media/00-config.py) (aucune variable d'env). Il
contient un bloc COMMUN (un `DRY_RUN` unique, `PHOTO_EXT=.jpg`, `VIDEO_EXT=.mkv`,
réglages NVENC, racine NAS, bornes d'années) puis un bloc par étape.

Chaque étape parcourt le partage `photos` et **ignore les dossiers commençant par
`_`** :

1. [`01-convert-to-mkv+h265`](src/perso-media/01-convert-to-mkv+h265.py) —
   **normalisation de format uniquement** : toute vidéo finit en **H.265 + MKV**
   quel que soit le codec/conteneur d'origine (NVENC obligatoire) ; `.jpeg`→`.jpg`.
   Conserve les tags de date existants mais **n'en infère pas** (c'est le rôle de 02).
2. [`02-enrich-movies-photos-with-date`](src/perso-media/02-enrich-movies-photos-with-date.py) —
   **inférence des dates** manquantes, par priorité : tag existant → nom de fichier
   (`YYYYMMDD_HHMMSS`, `2022-06-19 at 21.59.44`…) → photo voisine du même dossier →
   dossier parent `YY.MM` → date de modification du fichier.
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
- choisir le **sous-ensemble d'étapes** `01/02/03/04` pour le perso (le public
  enchaîne `01 → 02`) ;
- suivre les **logs en direct** (Server-Sent Events) avec l'état et le code de sortie ;
- **arrêter** un run en cours (`SIGINT` au groupe de processus, escalade `SIGKILL`) ;
- **éditer les `00-config.py`** (NVENC, workers, `PHOTOS_SRC`/`NAS_MOUNT`,
  `INPUT_FOLDERS`, webhook…) via un formulaire ; la réécriture est ciblée et
  préserve commentaires et mise en forme.

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
| `DRY_RUN` | `False` | `True` = simulation, aucun fichier renommé/écrit/supprimé |
| `CONVERT_CQ` | `26` | Qualité NVENC (plus bas = meilleur, 24–28 conseillé) |
| `CONVERT_PRESET` | `p4` | Préréglage NVENC, `p1` (rapide) → `p7` (qualité) |
| `CONVERT_EXTENSIONS` | `.mkv .mp4 .avi .m4v .mov` | Conteneurs vidéo scannés |
| `CONVERT_SKIP_SUFFIX` | `_x265` | Suffixe des fichiers déjà convertis (ignorés) |
| `CONVERT_MAX_RESOLUTION` | `None` | Résolution max de sortie (`"720p"`, `"1080p"`, `"1440p"`, `"2160p"`/`"4k"`). Les fichiers plus grands sont downscalés (HDR 10-bit préservé, Dolby Vision perdu). `None` = pas de downscale |
| `CLEAN_TECH_WORDS` | (liste) | Mots techniques retirés des noms par 01 |

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
│   ├── public-media/
│   │   ├── 00-config.py            # Réglages centralisés des 2 scripts
│   │   ├── 01-clean-names.py       # Renommage (avant conversion)
│   │   └── 02-convert-to-h265.py   # Ré-encodage NVENC HEVC
│   ├── perso-media/
│   │   ├── 00-config.py            # Réglages centralisés des 4 étapes
│   │   ├── 01-convert-to-mkv+h265.py
│   │   ├── 02-enrich-movies-photos-with-date.py
│   │   ├── 03-compress-for-gphotos.py
│   │   └── 04-upload-to-gphotos.sh # Upload rclone → Google Photos
│   ├── gui/                        # Interface web locale (stdlib)
│   │   ├── server.py               # Serveur HTTP + moteur de run + édition config
│   │   └── index.html              # Page unique (onglets, logs live, formulaires)
│   └── archives/                   # Scratch gitignoré (ancien uploader, audits, CSV)
├── env/
│   └── rclone.conf / .example      # Auth Google Photos (rclone.conf gitignored)
├── .devcontainer/                  # Dev container VS Code
├── Dockerfile                      # Image unique horus-convert-h265 (CUDA + ffmpeg + Pillow/piexif/rclone)
├── docker-compose.yml              # Service convert-h265 (public)
├── requirements.txt                # Vide (dépendances dans l'image Docker)
└── README.md
```
