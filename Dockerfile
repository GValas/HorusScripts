# Image de prod pour convert-h265 — ré-encodage HEVC via NVENC (GPU NVIDIA requis).
#
# Base CUDA runtime : fournit les bibliothèques attendues par hevc_nvenc.
# Les ffmpeg/ffprobe d'Ubuntu 24.04 sont compilés avec le support NVENC ; les
# libs propriétaires (libnvidia-encode) sont injectées au runtime par
# nvidia-container-toolkit lorsqu'on lance le conteneur avec accès GPU.
# Ubuntu 24.04 fournit Python 3.12. Les scripts cine n'ont aucune dépendance
# PyPI (stdlib + binaires ffmpeg/ffprobe) ; le pipeline perso ajoute Pillow (03)
# et piexif (02), et rclone pour l'upload (04) — installés plus bas.
FROM nvidia/cuda:13.1.2-runtime-ubuntu24.04

# tzdata : sans lui, le conteneur tourne en UTC et les logs sont décalés de 2h
# par rapport à Paris (CEST). TZ ci-dessous fixe le fuseau utilisé par Python.
# python3-pip : requis pour installer Pillow (compress-for-gphotos).
# curl/unzip/ca-certificates : pour installer rclone depuis le binaire officiel
# (cf. ci-dessous) — PAS le paquet apt.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg python3 python3-pip tzdata ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# rclone : upload vers Google Photos (04-upload-to-gphotos.sh).
# On installe le binaire officiel (dernière version stable) et NON le paquet apt
# d'Ubuntu 24.04, figé en v1.60 — trop ancien : il lui manque le mode batch
# Google Photos (--gphotos-batch-mode), qui regroupe jusqu'à 50 créations de
# médias par appel API au lieu d'un appel batchCreate lent par fichier. Sans lui
# l'upload plafonne à ~1 fichier / 10 s. install.sh récupère la dernière stable.
RUN curl -fsSL https://rclone.org/install.sh | bash

# Dépendances Python du pipeline perso :
#   - Pillow : redimensionnement / ré-encodage des photos (03)
#   - piexif : lecture/écriture des dates EXIF des photos (02)
# --break-system-packages : Ubuntu 24.04 applique PEP668 (env « externally
# managed ») et refuse sinon l'install système ; OK ici, c'est un conteneur.
RUN pip install --no-cache-dir --break-system-packages Pillow piexif

# Fuseau horaire : aligne les timestamps des logs sur l'heure de Paris.
ENV TZ=Europe/Paris

WORKDIR /app
COPY src/cine-videos/01-clean-names.py ./01-clean-names.py
COPY src/cine-videos/02-convert-to-h265.py ./02-convert-to-h265.py
# 00-config.py doit être à côté des scripts perso (chargé via Path(__file__)).
COPY src/perso-photo-videos/00-config.py ./00-config.py
COPY src/perso-photo-videos/03-compress-for-gphotos.py ./03-compress-for-gphotos.py
# Upload Google Photos via rclone (lit 00-config.py à côté pour SRC/DRY_RUN).
COPY src/perso-photo-videos/04-upload-to-gphotos.sh ./04-upload-to-gphotos.sh

# Logs en direct (docker compose logs -f), sans bufferisation
ENV PYTHONUNBUFFERED=1

# Pas de CMD « métier » par défaut : l'image embarque plusieurs scripts (cine :
# clean-names + convert-h265 ; perso : 01->04). Chaque lanceur fournit sa propre
# commande (run-cine-pipeline.sh via compose ; run-perso-pipeline.sh via docker
# run). Ce CMD neutre évite de lancer silencieusement un workflow.
CMD ["sh", "-c", "echo 'Image utilitaire — lance run-cine-pipeline.sh ou run-perso-pipeline.sh' >&2; exit 1"]
