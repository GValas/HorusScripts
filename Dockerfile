# Image de prod pour convert-h265 — ré-encodage HEVC via NVENC (GPU NVIDIA requis).
#
# Base CUDA runtime : fournit les bibliothèques attendues par hevc_nvenc.
# Les ffmpeg/ffprobe d'Ubuntu 24.04 sont compilés avec le support NVENC ; les
# libs propriétaires (libnvidia-encode) sont injectées au runtime par
# nvidia-container-toolkit lorsqu'on lance le conteneur avec accès GPU.
# Ubuntu 24.04 fournit Python 3.12 ; le script n'a aucune dépendance PyPI
# (uniquement la stdlib + les binaires ffmpeg/ffprobe).
FROM nvidia/cuda:13.1.2-runtime-ubuntu24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY src/cine-videos/clean-names.py ./clean-names.py
COPY src/cine-videos/convert-h265.py ./convert-h265.py

# Logs en direct (docker compose logs -f), sans bufferisation
ENV PYTHONUNBUFFERED=1

# On nettoie d'abord les noms (renommage), puis on convertit en HEVC.
# Les deux scripts partagent INPUT_FOLDERS / DRY_RUN (via env/.env).
CMD ["sh", "-c", "python3 clean-names.py && python3 convert-h265.py"]
