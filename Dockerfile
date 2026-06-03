# Image de prod pour convert-h265 — ré-encodage HEVC via NVENC (GPU NVIDIA requis).
#
# Base CUDA runtime : fournit les bibliothèques attendues par hevc_nvenc.
# Les ffmpeg/ffprobe d'Ubuntu 24.04 sont compilés avec le support NVENC ; les
# libs propriétaires (libnvidia-encode) sont injectées au runtime par
# nvidia-container-toolkit lorsqu'on lance le conteneur avec accès GPU.
# Ubuntu 24.04 fournit Python 3.12. Les scripts cine n'ont aucune dépendance
# PyPI (stdlib + binaires ffmpeg/ffprobe) ; 02-compress-for-gphotos.py ajoute
# Pillow + pillow-heif (installés plus bas).
FROM nvidia/cuda:13.1.2-runtime-ubuntu24.04

# tzdata : sans lui, le conteneur tourne en UTC et les logs sont décalés de 2h
# par rapport à Paris (CEST). TZ ci-dessous fixe le fuseau utilisé par Python.
# python3-pip : requis pour installer Pillow/pillow-heif (compress-for-gphotos).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg python3 python3-pip tzdata \
    && rm -rf /var/lib/apt/lists/*

# Dépendances Python de 02-compress-for-gphotos.py :
#   - Pillow      : redimensionnement / ré-encodage des photos
#   - pillow-heif : support .heic/.heif (iPhone), libheif embarquée dans la wheel
# --break-system-packages : Ubuntu 24.04 applique PEP668 (env « externally
# managed ») et refuse sinon l'install système ; OK ici, c'est un conteneur.
RUN pip install --no-cache-dir --break-system-packages Pillow pillow-heif

# Fuseau horaire : aligne les timestamps des logs sur l'heure de Paris.
ENV TZ=Europe/Paris

WORKDIR /app
COPY src/cine-videos/01-clean-names.py ./01-clean-names.py
COPY src/cine-videos/02-convert-to-h265.py ./02-convert-to-h265.py
COPY src/perso-photo-videos/02-compress-for-gphotos.py ./02-compress-for-gphotos.py

# Logs en direct (docker compose logs -f), sans bufferisation
ENV PYTHONUNBUFFERED=1

# Pas de CMD « métier » par défaut : l'image embarque plusieurs scripts
# (clean-names + convert-h265 pour le cine, compress-for-gphotos pour les
# photos). Chaque service de docker-compose.yml fournit son propre `command`.
# Ce CMD neutre évite de lancer silencieusement un workflow si l'image est
# exécutée sans commande explicite.
CMD ["sh", "-c", "echo 'Préciser un service : docker compose run --rm <convert-h265|compress-gphotos>' >&2; exit 1"]
