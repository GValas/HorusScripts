# nas-lister

Script Python qui liste les fichiers d'un partage SMB (NAS).

## Prérequis sur Ubuntu WSL

### 1 — Installer le client SMB

```bash
sudo apt install cifs-utils
```

### 2 — Monter le partage (le plus simple)

Un script tout-en-un est fourni :

```bash
./scripts/mount-nas.sh
```

Il lit `env/.env` (variables `NAS_HOST`, `NAS_SHARE`, `NAS_MOUNT`),
crée le point de montage, et fait un `mount -t cifs` en mode `guest`.
Idempotent : ne refait rien si déjà monté.

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

### 5 — Montage automatique au démarrage (optionnel)

Ajouter dans `/etc/fstab` :

```
//192.168.1.X/NOM_DU_SHARE /mnt/nas cifs credentials=/root/.nas-credentials,uid=1000,gid=1000 0 0
```

> ⚠️ Remplacer `192.168.1.X` par l'IP de ton NAS et `NOM_DU_SHARE` par le nom du partage.

---

## Dev — lancer dans le dev container

1. Ouvrir le dossier dans VS Code
2. VS Code détecte `.devcontainer/` → cliquer **"Reopen in Container"**
3. Lancer le script depuis le terminal intégré :

```bash
python src/script.py
```

---

## Prod — lancer dans le conteneur de prod

> ⚠️ Le fichier `.env` est dans `env/`, donc il faut passer `--env-file` pour
> que docker-compose interpole `${NAS_MOUNT}` dans le mapping de volume :

```bash
# Build et lancement
docker compose --env-file env/.env up

# En arrière-plan
docker compose --env-file env/.env up -d

# Voir les logs
docker compose logs -f
```

Astuce : créer un alias shell pour éviter de retaper `--env-file env/.env`
à chaque fois, ou ajouter un `Makefile` plus tard si le projet grandit.

---

## Configuration

Le chemin du point de montage NAS est défini dans `env/.env` :

```bash
# Copier le template
cp env/.env.example env/.env

# Éditer si besoin
# NAS_MOUNT=/mnt/horus/photos
```

| Variable | Défaut | Description |
|---|---|---|
| `NAS_MOUNT` | `/mnt/horus/photos` | Chemin du point de montage NAS |

Ce fichier est utilisé par `docker compose` (volume + variable d'env) et par
`src/script.py` en exécution locale.

## Arborescence

```
nas-lister/
├── src/              # Code source
│   └── script.py
├── scripts/          # Scripts shell opérationnels
│   └── mount-nas.sh
├── env/              # Variables d'environnement (par environnement, plus tard)
│   ├── .env          # Local — gitignored
│   └── .env.example  # Template versionné
├── .devcontainer/    # Dev container VS Code
├── Dockerfile        # Image de prod
├── docker-compose.yml
├── requirements.txt
└── README.md
```
