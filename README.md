# HorusScripts

Collection de scripts Python autonomes pour gérer le NAS **horus** (partage
SMB/CIFS : `movies`, `tvshows`, `cartoons`, `photos`, `perso`).

Le script de prod packagé ici est [`convert-h265`](src/cine-videos/convert-h265.py) :
il scanne récursivement des dossiers vidéo et ré-encode tout ce qui n'est pas
déjà en HEVC vers x265 via **NVENC** (GPU NVIDIA), en conservant toutes les
pistes audio/sous-titres, puis **remplace l'original** par le fichier converti
après vérification du nombre de pistes.

## Prérequis sur Ubuntu WSL

### 1 — Installer le client SMB

```bash
sudo apt install cifs-utils
```

### 2 — Monter le partage (le plus simple)

Deux scripts sont fournis dans `scripts/` :

```bash
# Monter les partages tout de suite (relançable à la main)
sudo ./scripts/mount-nas.sh

# Installer le montage automatique au démarrage de WSL (une seule fois)
sudo ./scripts/install-boot-mount.sh
```

[`mount-nas.sh`](scripts/mount-nas.sh) monte les partages CIFS du NAS horus
(`photos movies tvshows cartoons`) avec les credentials de
`~/.nas-credentials`. Subtilité importante : Docker Desktop tourne dans une
distro WSL séparée qui ne voit le FS d'Ubuntu que via `/mnt/wsl` (propagation
`shared`). Un montage CIFS placé directement sous `/mnt/horus` est `private` →
invisible dans les conteneurs. Le script monte donc le CIFS réel sous
`/mnt/wsl/horus/*` (visible par Docker) puis fait un bind-miroir vers
`/mnt/horus/*` pour les exécutions locales (`NAS_MOUNT=/mnt/horus`).
Idempotent : ne refait rien si déjà monté.

[`install-boot-mount.sh`](scripts/install-boot-mount.sh) configure
`/etc/wsl.conf` pour appeler `mount-nas.sh` au boot, neutralise les lignes
CIFS de `/etc/fstab` (qui montaient en `private`) avec sauvegarde préalable,
puis lance le montage. À exécuter `sudo` une seule fois.

### 3 — (Alternative) Monter manuellement avec credentials

```bash
# Créer le fichier
cat > ~/.nas-credentials << EOF
username=TONUSER
password=TONPASSWORD
EOF

# Sécuriser les permissions
chmod 600 ~/.nas-credentials
```

### 4 — Monter le partage

```bash
# Montage manuel (test)
sudo mount -t cifs //192.168.1.X/NOM_DU_SHARE /mnt/nas \
  -o credentials=/root/.nas-credentials,uid=1000,gid=1000

# Vérifier que ça fonctionne
ls /mnt/nas
```

### 5 — Montage automatique au démarrage

Sous WSL, utiliser [`scripts/install-boot-mount.sh`](scripts/install-boot-mount.sh)
(voir section 2) plutôt que `/etc/fstab` : un montage CIFS via `fstab` se fait
en propagation `private` et reste invisible dans les conteneurs Docker.

> ⚠️ Adapter l'IP (`HOST`) et la liste des partages (`SHARES`) en tête de
> `scripts/mount-nas.sh` si nécessaire.

---

## Dev — lancer dans le dev container

1. Ouvrir le dossier dans VS Code
2. VS Code détecte `.devcontainer/` → cliquer **"Reopen in Container"**
3. Lancer le script depuis le terminal intégré :

```bash
python src/cine-videos/convert-h265.py
```

---

## Prod — lancer convert-h265 dans le conteneur

Le ré-encodage utilise **NVENC** (`hevc_nvenc`) : le conteneur a donc besoin
d'accéder au **GPU NVIDIA** de l'hôte, et le NAS doit être monté en
**lecture-écriture** (le script supprime l'original et écrit le fichier converti).

### 0 — Prérequis hôte (une seule fois)

- Un **GPU NVIDIA** avec pilote installé sur l'hôte (`nvidia-smi` fonctionne).
- **nvidia-container-toolkit** installé et configuré pour Docker :

  ```bash
  # Installation (Ubuntu/WSL) — voir la doc NVIDIA pour la version à jour
  sudo apt install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
  ```

- Le NAS monté sur l'hôte (voir « Prérequis sur Ubuntu WSL » plus haut).

Vérifier que Docker voit bien le GPU :

```bash
docker run --rm --gpus all nvidia/cuda:13.1.2-runtime-ubuntu24.04 nvidia-smi
```

### 1 — Configurer

```bash
# Copier le template puis éditer
cp env/.env.example env/.env
```

Régler dans `env/.env` : `NAS_MOUNT`, les `INPUT_FOLDERS` à traiter et, **pour
un premier essai, `DRY_RUN=true`** (simulation, ne touche à aucun fichier).

### 2 — Lancer

> ⚠️ Le fichier `.env` est dans `env/`, donc il faut passer `--env-file` pour
> que docker-compose interpole `${NAS_MOUNT}` dans le mapping de volume.

```bash
# Build + exécution, logs en direct (recommandé : on suit la progression ffmpeg)
docker compose --env-file env/.env up --build

# En arrière-plan
docker compose --env-file env/.env up --build -d
docker compose logs -f convert-h265

# Forcer une reconstruction de l'image seule
docker compose --env-file env/.env build

# Nettoyer le conteneur après une exécution ponctuelle
docker compose --env-file env/.env down
```

Le conteneur traite les dossiers puis s'arrête (`restart: "no"`). Lancer
d'abord avec `DRY_RUN=true`, vérifier les logs, puis repasser `DRY_RUN=false`
et relancer pour la conversion réelle.

Astuce : créer un alias shell pour éviter de retaper `--env-file env/.env`.

---

## Configuration

Tous les réglages de prod vivent dans `env/.env` (template : `env/.env.example`).
Ils surchargent le bloc de configuration en tête de
[`convert-h265.py`](src/cine-videos/convert-h265.py) ; en l'absence de variable,
les valeurs par défaut du script s'appliquent.

| Variable | Défaut | Description |
|---|---|---|
| `NAS_MOUNT` | `/mnt/horus` | Point de montage NAS, monté tel quel dans le conteneur |
| `INPUT_FOLDERS` | `/mnt/horus/tvshows` | Dossiers à scanner, séparés par des virgules |
| `CQ` | `26` | Qualité (plus bas = meilleur, 24–28 conseillé) |
| `PRESET` | `p4` | Préréglage NVENC, `p1` (rapide) → `p7` (qualité) |
| `DRY_RUN` | `false` | `true` = simulation, aucun fichier écrit/supprimé |

`NAS_MOUNT` est aussi consommé par `docker compose` pour le mapping de volume.
Les chemins de `INPUT_FOLDERS` doivent se trouver **sous** `NAS_MOUNT`.

## Arborescence

```
HorusScripts/
├── src/
│   ├── cine-videos/
│   │   └── convert-h265.py        # Script packagé en prod (NVENC HEVC)
│   └── perso-photo-videos/        # Scripts photos/vidéos perso (hôte Windows)
├── scripts/                       # Montage NAS côté WSL
│   ├── mount-nas.sh
│   └── install-boot-mount.sh
├── env/
│   ├── .env                       # Local — gitignored
│   └── .env.example               # Template versionné
├── .devcontainer/                 # Dev container VS Code
├── Dockerfile                     # Image de prod convert-h265 (CUDA + ffmpeg)
├── docker-compose.yml             # Service convert-h265 (accès GPU + NAS rw)
├── requirements.txt
└── README.md
```
