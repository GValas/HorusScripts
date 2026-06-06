# HorusScripts

Outils pour gérer le NAS **horus** (partage SMB/CIFS : `movies`, `tvshows`,
`cartoons`, `photos`, `perso`). Le dépôt contient **deux pipelines** indépendants,
chacun avec son lanceur à la racine. Tout tourne dans une **image Docker unique**
(`horus-convert-h265`) qui embarque toutes les dépendances (ffmpeg/NVENC, Pillow,
piexif, rclone).

| Pipeline | Dossier | Lanceur | Rôle |
|---|---|---|---|
| **Ciné** | [`src/cine-videos/`](src/cine-videos/) | [`run-cine-pipeline.sh`](run-cine-pipeline.sh) | Nettoie les noms + ré-encode films/séries en HEVC |
| **Perso** | [`src/perso-photo-videos/`](src/perso-photo-videos/) | [`run-perso-pipeline.sh`](run-perso-pipeline.sh) | Photos/vidéos perso : normalise → date → compresse → upload Google Photos |

> ⚠️ Le NAS doit toujours être adressé via **`/mnt/wsl/horus`**, jamais
> `/mnt/horus` (sinon les conteneurs voient des dossiers vides — cf. la section
> montage). Cette règle vaut aussi pour les exécutions locales.

---

## Pipeline ciné — `src/cine-videos/`

Deux scripts enchaînés sur les mêmes dossiers (`INPUT_FOLDERS`) :

1. [`01-clean-names`](src/cine-videos/01-clean-names.py) — nettoie les noms de
   dossiers/fichiers (retire `1080p`, `x264`…, normalise casse et séparateurs, met
   l'année entre parenthèses), avec détection de collisions.
2. [`02-convert-to-h265`](src/cine-videos/02-convert-to-h265.py) — ré-encode
   récursivement tout ce qui n'est pas déjà en HEVC vers x265 via **NVENC** (GPU
   NVIDIA), conserve toutes les pistes audio/sous-titres, puis **remplace
   l'original** après vérification du nombre de pistes.

Réglages dans `env/.env` (`NAS_MOUNT`, `INPUT_FOLDERS`, `CQ`, `PRESET`, `DRY_RUN`),
partagés par les deux scripts. Lancement :

```bash
./run-cine-pipeline.sh --DRY_RUN=true   # simulation (recommandé d'abord)
./run-cine-pipeline.sh                  # réel
# Tout argument supplémentaire est transmis à `docker compose up`.
```

---

## Pipeline perso — `src/perso-photo-videos/`

Quatre étapes numérotées, **toutes réglées depuis un seul fichier**,
[`00-config.py`](src/perso-photo-videos/00-config.py) (aucune variable d'env). Il
contient un bloc COMMUN (un `DRY_RUN` unique, `PHOTO_EXT=.jpg`, `VIDEO_EXT=.mkv`,
réglages NVENC, racine NAS, bornes d'années) puis un bloc par étape.

Chaque étape parcourt le partage `photos` et **ignore les dossiers commençant par
`_`** :

1. [`01-convert-to-mkv+h265`](src/perso-photo-videos/01-convert-to-mkv+h265.py) —
   **normalisation de format uniquement** : toute vidéo finit en **H.265 + MKV**
   quel que soit le codec/conteneur d'origine (NVENC obligatoire) ; `.jpeg`→`.jpg`.
   Conserve les tags de date existants mais **n'en infère pas** (c'est le rôle de 02).
2. [`02-enrich-movies-photos-with-date`](src/perso-photo-videos/02-enrich-movies-photos-with-date.py) —
   **inférence des dates** manquantes, par priorité : tag existant → nom de fichier
   (`YYYYMMDD_HHMMSS`, `2022-06-19 at 21.59.44`…) → photo voisine du même dossier →
   dossier parent `YY.MM` → date de modification du fichier.
3. [`03-compress-for-gphotos`](src/perso-photo-videos/03-compress-for-gphotos.py) —
   écrit une **copie compressée** dans `output/gphotos` (photos redimensionnées,
   vidéos ré-encodées 720p). Ne produit que `.jpg` + `.mkv`.
4. [`04-upload-to-gphotos.sh`](src/perso-photo-videos/04-upload-to-gphotos.sh) —
   **upload via rclone** de `output/gphotos` vers Google Photos ; chaque dossier de
   premier niveau devient un album du même nom. Auth dans `env/rclone.conf`
   (gitignoré, cf. Configuration).

Lancement via l'orchestrateur (monte les scripts en *live* → éditer `00-config.py`
prend effet sans rebuild ; lance le conteneur en `--user` pour que les fichiers
produits t'appartiennent) :

```bash
./run-perso-pipeline.sh            # toutes les étapes ; confirme si DRY_RUN=False
./run-perso-pipeline.sh -y         # sans confirmation
./run-perso-pipeline.sh 03 04      # un sous-ensemble d'étapes seulement
```

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

### Ciné — `env/.env`

```bash
cp env/.env.example env/.env   # gitignored ; éditer ensuite
```

| Variable | Défaut | Description |
|---|---|---|
| `NAS_MOUNT` | `/mnt/wsl/horus` | Point de montage NAS (monté tel quel dans le conteneur) |
| `INPUT_FOLDERS` | `…/tvshows,…/movies,…/cartoons` | Dossiers à scanner, séparés par des virgules (doivent être **sous** `NAS_MOUNT`) |
| `CQ` | `26` | Qualité NVENC (plus bas = meilleur, 24–28 conseillé) |
| `PRESET` | `p4` | Préréglage NVENC, `p1` (rapide) → `p7` (qualité) |
| `DRY_RUN` | `false` | `true` = simulation, aucun fichier renommé/écrit/supprimé |

`NAS_MOUNT` est aussi consommé par `docker compose` pour le mapping de volume,
d'où l'option `--env-file env/.env` (cf. le lanceur).

### Perso — `src/perso-photo-videos/00-config.py`

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
python src/cine-videos/02-convert-to-h265.py
```

---

## Arborescence

```
HorusScripts/
├── run-cine-pipeline.sh           # Lanceur ciné (docker compose)
├── run-perso-pipeline.sh          # Lanceur perso (orchestre 01→04 en docker run)
├── src/
│   ├── cine-videos/
│   │   ├── 01-clean-names.py       # Renommage (avant conversion)
│   │   └── 02-convert-to-h265.py   # Ré-encodage NVENC HEVC
│   ├── perso-photo-videos/
│   │   ├── 00-config.py            # Réglages centralisés des 4 étapes
│   │   ├── 01-convert-to-mkv+h265.py
│   │   ├── 02-enrich-movies-photos-with-date.py
│   │   ├── 03-compress-for-gphotos.py
│   │   └── 04-upload-to-gphotos.sh # Upload rclone → Google Photos
│   └── archives/                   # Scratch gitignoré (ancien uploader, audits, CSV)
├── env/
│   ├── .env / .env.example         # Config ciné (.env gitignored)
│   └── rclone.conf / .example      # Auth Google Photos (rclone.conf gitignored)
├── .devcontainer/                  # Dev container VS Code
├── Dockerfile                      # Image unique horus-convert-h265 (CUDA + ffmpeg + Pillow/piexif/rclone)
├── docker-compose.yml              # Service convert-h265 (ciné)
├── requirements.txt                # Vide (dépendances dans l'image Docker)
└── README.md
```
