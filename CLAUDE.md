# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal toolkit for managing a home NAS named **horus** (SMB/CIFS share holding media: `movies`, `tvshows`, `cartoons`, `photos`, `perso`). Python 3.12. The repo is organized as **two independent pipelines** under `src/`, plus a scratch/archive folder:

- **`src/public-media/`** — clean movie/TV filenames and re-encode to HEVC. Config-file-driven (`00-config.py`), run via **docker-compose**.
- **`src/perso-media/`** — a 4-step personal photo/video pipeline (normalize → date → compress → upload to Google Photos). Config-file-driven, run via the **`run-perso-media-pipeline.sh`** orchestrator.
- **`src/archives/`** — gitignored scratch: the old Python uploader (`04-…py.bak`), one-off audit scripts, and generated CSV reports. Not part of any pipeline; nothing imports it.

Both pipelines share a **single Docker image** (`horus-convert-h265`, built from [Dockerfile](Dockerfile)) that bakes in every dependency. The two launchers at the repo root are the entry points:

- [run-public-media-pipeline.sh](run-public-media-pipeline.sh) — public pipeline (via `docker compose`).
- [run-perso-media-pipeline.sh](run-perso-media-pipeline.sh) — perso pipeline (via `docker run`, orchestrating steps 01→04).

The codebase (comments, log messages) is written in **French**. Match that language when editing existing files. (This guidance file is the exception — keep it in English.)

## Dependencies

`requirements.txt` is **intentionally empty** — dependencies live in the Docker image, not in pip. The [Dockerfile](Dockerfile) installs everything:

- `ffmpeg`/`ffprobe` — used by both pipelines (codec detection, remux, re-encode).
- **NVENC / GPU** — all H.265 encoding uses `hevc_nvenc` (NVIDIA GPU only, **no CPU fallback**). The image is based on `nvidia/cuda:*-runtime`; the proprietary `libnvidia-encode` is injected at runtime by nvidia-container-toolkit. Containers must be launched with GPU access **and** `NVIDIA_DRIVER_CAPABILITIES=all` (the launchers do this) — `--gpus all` alone only exposes compute/utility, not the encoder.
- `Pillow` + `piexif` + `pillow-heif` — perso pipeline only (photo resize in 03, EXIF dates in 02, HEIC/HEIF→JPG decode in 01).
- `rclone` — perso upload (step 04).
- `tzdata` + `ENV TZ=Europe/Paris` — so log timestamps are Paris time, not UTC.

To run a script outside Docker you'd have to install these yourself, but the intended path is always the Docker image.

## Path convention — `/mnt/wsl/horus`, never `/mnt/horus`

Everything runs in Linux/containers and targets the NAS at **`/mnt/wsl/horus`**. Both pipelines hardcode the mount in their `00-config.py`: `NAS_MOUNT = "/mnt/wsl/horus"` for public ([src/public-media/00-config.py](src/public-media/00-config.py)), `PHOTOS_SRC = "/mnt/wsl/horus/photos"` for perso ([src/perso-media/00-config.py](src/perso-media/00-config.py)).

**Use `/mnt/wsl/horus`, not `/mnt/horus`.** A bind on `/mnt/horus` makes the container see empty folders — see the NAS-mounting section below for why. This applies everywhere (containers and local runs).

## The public pipeline (`src/public-media/`)

Two stdlib-only scripts (ffmpeg/ffprobe binaries aside), **all configured from one file**, [00-config.py](src/public-media/00-config.py) — same principle as the perso pipeline, no env vars. It holds a COMMON section (`NAS_MOUNT`, `INPUT_FOLDERS`, single `DRY_RUN`) plus a per-script section (`CLEAN_*` for 01, `CONVERT_*` for 02). Both scripts load it via `importlib` (the `00-config.py` filename has digits/`-`, so it isn't importable directly).

- [01-clean-names.py](src/public-media/01-clean-names.py) — rename movies/shows: strip technical tokens (codecs, resolutions, release groups) and apply naming conventions. Detects **collisions** (two names mapping to the same target) before applying.
- [02-convert-to-h265.py](src/public-media/02-convert-to-h265.py) — re-encode to HEVC via NVENC, with colorized timestamped logging and stream-count verification.

The compose service `convert-h265` mounts `src/public-media` live at `/work` and runs them in sequence: `python3 01-clean-names.py && python3 02-convert-to-h265.py`. The launcher reads `NAS_MOUNT`/`DRY_RUN` from `00-config.py` (small inline `python3` call), exports `NAS_MOUNT` so compose can interpolate the volume bind, and prompts to confirm if `DRY_RUN=False` (02 deletes originals).

Run it:

```bash
./run-public-media-pipeline.sh        # prompts to confirm if DRY_RUN=False
./run-public-media-pipeline.sh -y     # skip the confirmation
```

## The perso pipeline (`src/perso-media/`)

Four numbered steps, **all configured from one file**, [00-config.py](src/perso-media/00-config.py). There are no env vars and no per-script config blocks — `00-config.py` holds a COMMON section (single `DRY_RUN`, `PHOTO_EXT=.jpg`, `VIDEO_EXT=.mkv`, NVENC `VIDEO_CQ`/`VIDEO_PRESET`, `PHOTOS_SRC`, plausible-year bounds) plus a per-script section (`CONVERT_*`, `ENRICH_*`, `COMPRESS_*`, `UPLOAD_*`). Python steps load it via `importlib` (filenames contain digits/`+`/`-`, so they aren't importable directly); the shell step reads values via a small inline `python3` call.

The steps (each operates on the `photos` share, skipping folders whose name starts with `_`):

1. [01-convert-to-mkv+h265.py](src/perso-media/01-convert-to-mkv+h265.py) — **format normalization only**. Every video ends up **H.265 + MKV** regardless of source codec/container (re-encodes non-HEVC, including existing `.mkv`, in place via temp + `os.replace`; remuxes already-HEVC). Normalizes photos too: decodes **HEIC/HEIF→JPG** (iPhone, EXIF preserved) and renames `.jpeg`→`.jpg`. **Preserves existing date tags but never infers them** (that is step 02's job). NVENC-only; aborts if no GPU encoder. Verifies output **duration** matches the source before deleting the original.
2. [02-enrich-movies-photos-with-date.py](src/perso-media/02-enrich-movies-photos-with-date.py) — **all date inference**. Fills missing capture dates from, in priority order: an existing tag → a **Google Takeout JSON sidecar** (`*.json` with `photoTakenTime`) → the filename (patterns like `YYYYMMDD_HHMMSS` and `2022-06-19 at 21.59.44`) → a neighboring photo's date in the same folder → a parent `YY.MM` folder name → file mtime. Photos via `piexif`, videos via ffprobe/ffmpeg.
3. [03-compress-for-gphotos.py](src/perso-media/03-compress-for-gphotos.py) — write a **compressed copy** to `output/gphotos` (photos resized so the long side ≤ `COMPRESS_MAX_PHOTO_SIZE`, videos re-encoded to `COMPRESS_VIDEO_HEIGHT`p via NVENC). Only ever produces `.jpg` + `.mkv`. `ImageFile.LOAD_TRUNCATED_IMAGES = True` to survive slightly-truncated JPEGs.
4. [04-upload-to-gphotos.sh](src/perso-media/04-upload-to-gphotos.sh) — **rclone** upload of `output/gphotos` to Google Photos; each first-level folder becomes an album of the same name. Replaces the old Python uploader (archived in `src/archives/`). Reads `UPLOAD_*` + `DRY_RUN` from `00-config.py`; OAuth credentials come from `env/rclone.conf` (see Configuration).

Run it with the orchestrator:

```bash
./run-perso-media-pipeline.sh            # all steps; prompts to confirm if DRY_RUN=False
./run-perso-media-pipeline.sh -y         # skip the confirmation
./run-perso-media-pipeline.sh 03 04      # run only a subset of steps
```

The orchestrator mounts the perso scripts live (`-v src/perso-media:/work`), so editing `00-config.py` takes effect **without rebuilding** the image. It runs the container with `--user "$(id -u):$(id -g)"` so generated files are owned by you, not root.

## The web GUI (`src/gui/`)

A small **local web app** to launch, monitor and configure both pipelines from a browser instead of the CLI. **Stdlib-only Python** (`http.server`) — no pip deps on the host, consistent with the rest of the repo. It runs on the **host** (it must invoke `docker`/the launchers), reuses no business logic of its own, and **shells out to the two `run-*-pipeline.sh` launchers** in a subprocess.

- [src/gui/server.py](src/gui/server.py) — threaded HTTP server. A single global `RunManager` enforces **one run at a time** (GPU/Docker are serial), buffers log lines, and streams them over **SSE** (`/api/stream`). Runs are launched with `start_new_session=True`; **Stop** sends `SIGINT` to the process group (so `docker compose`/`docker run` shut down cleanly), escalating to `SIGKILL` after ~12s. The `PIPELINES` dict is the single source of truth for each pipeline's launcher, config path, steps, and **editable config fields** (whitelisted; typed `bool`/`int`/`str`/`str_or_none`/`list`/`choice`).
- [src/gui/index.html](src/gui/index.html) — one self-contained page (vanilla JS, no build step). Tabs per pipeline; dry-run/real selector (mapped to the launchers' `--dry-run`/`--real` flags, real prompts to confirm); step checkboxes (`01/02/03/04`) for perso; live log console; a form to edit the `00-config.py`.
- [run-gui.sh](run-gui.sh) — launcher: `./run-gui.sh [PORT] [HOST]` (default `8765` on `0.0.0.0`).

**Config editing is a targeted regex rewrite**, not a full re-serialization: it reads current values via `importlib` (same as the launchers) and rewrites only the whitelisted assignment lines, **preserving comments and formatting** (writes via temp + `os.replace`). `INPUT_FOLDERS` entries under `NAS_MOUNT` are re-emitted as `f"{NAS_MOUNT}/…"`. When adding an editable field, add it to the pipeline's `fields` list in `PIPELINES` — the UI and the rewriter are both driven from there.

The mode selector always passes `-y` plus an explicit `--dry-run`/`--real` override, so the GUI's choice wins over `00-config.py`'s `DRY_RUN` for that run. Default binding is `0.0.0.0` (LAN-reachable); since the GUI can trigger destructive runs, only expose it on a trusted network or bind `127.0.0.1`. On WSL2 the auto-detected IP is the internal NAT IP — reaching it from a phone needs a Windows `netsh portproxy`.

## Script conventions

- **`DRY_RUN` gates every mutating operation** (rename, delete, write, upload). `True` = simulate and log, change nothing. **Each pipeline has a single `DRY_RUN` in its own `00-config.py`** driving all its steps. The launchers also accept **`--dry-run` / `--real`** to override it for a single run without editing the file — they export `PIPELINE_DRY_RUN` (1/0), which every step honours above `00-config.py`. Always check it before running anything destructive — step 01 deletes originals after conversion, and 04 publishes to the internet.
- A `setup_logging()` / `log()` helper with **timestamped, Paris-timezone** output (the image sets `TZ=Europe/Paris`; some scripts also pin the logging converter to `Europe/Paris`).
- All H.265 encoding is **NVENC-only** — no CPU/libx265 fallback. A script aborts rather than silently encoding on CPU.
- Rename/move operations detect **collisions** before applying and report a final tally.
- **Data-safety guards before deleting an original**: stream-count *and* **duration** of the output must match the source; a **disk-space** check precedes every re-encode/compress.
- **Scan cache + parallelism**: the convert steps memoise per-file codec/dimensions in a gitignored `.scan-cache.json` (keyed by mtime+size) and probe with a thread pool, so repeated runs don't re-`ffprobe` the whole library. Photo compression (03) is parallelised; video/GPU work stays serial. All tunable in `00-config.py` (`CONVERT_SCAN_CACHE`, `CONVERT_SCAN_WORKERS`, `COMPRESS_PHOTO_WORKERS`).
- **End-of-run notification**: set `NOTIFY_WEBHOOK` (ntfy/webhook URL) in `00-config.py` and the launcher POSTs a success/failure summary via `notify.py` (no-op when unset).

## Configuration

- **Public:** edit [src/public-media/00-config.py](src/public-media/00-config.py) directly — no env vars. Set `NAS_MOUNT` (= `/mnt/wsl/horus`), `INPUT_FOLDERS`, `DRY_RUN`, and NVENC `CONVERT_CQ`/`CONVERT_PRESET`. The launcher exports `NAS_MOUNT` (read from this file) so `docker compose` can interpolate the volume bind.
- **Perso:** edit [src/perso-media/00-config.py](src/perso-media/00-config.py) directly — no env vars.
- **Upload auth:** copy `env/rclone.conf.example` → `env/rclone.conf` (gitignored) and fill `client_id` / `client_secret` / `token`. Get the token with `rclone authorize "google photos"` on a machine with a browser + rclone (no rclone needed on this host). The launcher bind-mounts this file into the container at runtime — **secrets are never baked into the image and must never be committed** (only the `.example` placeholder is tracked).

## Docker

Both pipelines build the same image, `horus-convert-h265`:

```bash
# Public — compose (NAS_MOUNT must be exported so ${NAS_MOUNT} interpolates):
export NAS_MOUNT=/mnt/wsl/horus && docker compose up convert-h265
# …or just: ./run-public-media-pipeline.sh   (reads NAS_MOUNT from 00-config.py)

# Perso — orchestrator (docker run under the hood):
./run-perso-media-pipeline.sh
```

`docker-compose.yml` defines only the `convert-h265` service; the perso pipeline deliberately does **not** go through compose (it needs per-step volume/GPU/user variations that the orchestrator applies in `docker run`). The image's default `CMD` is a neutral "use a launcher" message, so running the bare image does nothing destructive.

`black` runs on save in the dev container (`editor.formatOnSave`). Keep code black-formatted.

## NAS mounting (WSL host — `/etc/`)

The NAS is mounted at WSL boot by `/etc/mount-nas-horus.sh`, called from `/etc/wsl.conf` (`[boot] command = /etc/mount-nas-horus.sh`). Both files live in `/etc/`, **not in this repo** (host/machine config, require `sudo` to edit). The script mounts the CIFS shares (`photos movies tvshows cartoons`) from `//192.168.1.182` using `~/.nas-credentials` (hardcoded — it does **not** read any pipeline config).

Key trick: Docker Desktop runs in a separate WSL distro that only sees Ubuntu's FS via `/mnt/wsl` (propagation `shared`). A CIFS mount under `/mnt/horus` is invisible in containers, so the script mounts the real CIFS under `/mnt/wsl/horus/*` (Docker-visible, `--make-shared`) and bind-mirrors it to `/mnt/horus/*` for convenience. It is idempotent (guards each mount with `mountpoint -q`) and can be re-run by hand.

**`NAS_MOUNT` must be `/mnt/wsl/horus`, never `/mnt/horus`.** `docker-compose.yml` bind-mounts `${NAS_MOUNT}:${NAS_MOUNT}` into the container. Docker Desktop only resolves host paths under `/mnt/wsl`; a bind on `/mnt/horus` captures the **empty ext4 mountpoint directory** (the CIFS submounts don't propagate to Docker's distro), so the container sees empty `movies/tvshows/...` folders. `/mnt/wsl/horus` is the shared CIFS mount Docker can actually see — and it also works for local (non-container) runs, so use it everywhere.

The CIFS lines in `/etc/fstab` are neutralized (commented) because they mounted with `private` propagation under `/mnt/horus`, invisible to containers. Backups exist as `/etc/wsl.conf.bak` and `/etc/fstab.bak`. After editing `/etc/wsl.conf`, validate the boot trigger with `wsl --shutdown` (PowerShell) then relaunch Ubuntu.
