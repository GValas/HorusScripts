# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal toolkit for managing a home NAS named **horus** (SMB/CIFS share holding media: `movies`, `tvshows`, `cartoons`, `photos`, `perso`). Python 3.12. The repo is organized as **two independent pipelines** under `src/`, plus a scratch/archive folder:

- **`src/public-media/`** — clean movie/TV filenames, re-encode to HEVC, and (optionally) identify movies against online databases. Config-file-driven (`00-config.py`), run via **docker-compose**.
- **`src/perso-media/`** — a 4-step personal photo/video pipeline (normalize → date → compress → upload to Google Photos). Config-file-driven, run via the **`run-perso-media-pipeline.sh`** orchestrator.
- **`src/archives/`** — gitignored scratch: the old Python uploader (`04-…py.bak`), one-off audit scripts, and generated CSV reports. Not part of any pipeline; nothing imports it.

Each pipeline directory also holds a `_common.py` (config loading, disk-space
guard, scan cache, and — for perso — date/timezone helpers and the `_`/temp-file
exclusions). It is **per-pipeline on purpose**: a container mounts only one
pipeline directory on `/work`, so a module at the root of `src/` would not be
visible. The one genuinely shared script, [src/notify.py](src/notify.py), runs on
the *host* and takes the target pipeline's config path as its first argument.

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

Four stdlib-only scripts (ffmpeg/ffprobe binaries aside), **all configured from one file**, [00-config.py](src/public-media/00-config.py) — same principle as the perso pipeline, no env vars. It holds a COMMON section (`NAS_MOUNT`, `INPUT_FOLDERS`, single `DRY_RUN`) plus a per-script section (`CLEAN_*` for 01, `CONVERT_*` for 02, `IDENTIFY_*` for 03, `AUDIO_*` for 04). All scripts load it through `_common.load_config()` — `importlib` under the hood (the `00-config.py` filename has digits/`-`, so it isn't importable directly), plus the `00-config.local.py` overlay.

- [01-clean-names.py](src/public-media/01-clean-names.py) — rename movies/shows: strip technical tokens (codecs, resolutions, release groups) and apply naming conventions. Detects **collisions** (two names mapping to the same target) before applying. Only **video and subtitle** extensions are renamed (`CLEAN_VIDEO_EXTENSIONS` / `CLEAN_SUBTITLE_EXTENSIONS`) — artwork and `.nfo` are left alone — and a subtitle's **language suffix is re-attached** after the technical cut (`CLEAN_SUBTITLE_LANG_TOKENS`), otherwise `…fr.srt` and `…en.srt` collapse onto the same target and one track is lost. A name made entirely of technical tokens is left untouched rather than becoming an empty (hidden) filename.
- [02-convert-to-h265.py](src/public-media/02-convert-to-h265.py) — re-encode to HEVC via NVENC, with colorized timestamped logging and stream-count verification. Three independent triggers: a non-HEVC codec, `CONVERT_MAX_RESOLUTION` (downscale, re-encodes even already-HEVC files), and `CONVERT_MAX_BITRATE` in Mb/s — the only knob that targets *size* rather than definition, since a 1080p film can weigh 24 GB purely from its bitrate. When bitrate is the **only** reason (already HEVC, within the resolution limit), the original is kept unless the output is actually smaller — otherwise the picture is degraded for nothing. An unmeasurable bitrate (no duration) never triggers a re-encode: that decision deletes an original. The scan cache stores the duration alongside codec/dimensions (`SCAN_CACHE_VERSION` 3), probed in the same `ffprobe` call via `-show_format`.
- [03-identify-movies.py](src/public-media/03-identify-movies.py) — **optional, opt-in step**: identify each movie online and rename it after `IDENTIFY_PATTERN` (default `{titre}.({yyyy}).{ext}`). It computes the **OpenSubtitles moviehash** (size + first/last 64 KiB — the subtitle database's own indexing key), asks OpenSubtitles which movie that file *is*, then fills title and year from **TMDB**; when the hash is unknown (02 re-encodes, which changes the hash) it falls back to a TMDB title search built from the already-cleaned filename. Subtitles next to the movie follow it (language suffix kept) and the movie's own folder is renamed when it holds a single film. API keys live in `IDENTIFY_*_API_KEY` and **must never be committed** — enter them in the web GUI, which writes them to the gitignored `00-config.local.py`.

  Safety rails, all deliberate: a missing pattern field skips the file (a truncated `2019..mkv` is worse than a dirty name); a fallback (non-hash) match is rejected unless the found title's words overlap the filename (`match_is_plausible`), so a re-run never renames a film after an unrelated one; TMDB's *best* result is chosen rather than its first (`best_result` — searching "Batman The Dark Knight" ranks a documentary first); a sequel number in the found title must appear in the filename too (`sequel_is_consistent` — "Men in Black III" contains all of "Men.In.Black.1", and the year separates neither); two files mapping to the same target collide and are reported in the dry run (`planned`, same idea as 01's `detect_collisions`); existing targets are never overwritten; identifications are cached in `.identify-cache.json` (misses re-tried after `IDENTIFY_MISS_TTL_DAYS`) so quotas survive repeated runs; the file is read once for the hash; and `DRY_RUN` still queries the (read-only) APIs so you can preview every new name.

The compose service `convert-h265` mounts `src/public-media` live at `/work`. Since steps are selectable, the launcher does **not** use `docker compose up` (whose `command:` is fixed at 01 → 02); it runs `docker compose build` then `docker compose run --rm -T convert-h265 sh -c "<steps>"`. The launcher reads `NAS_MOUNT`/`DRY_RUN` via `python3 src/public-media/_common.py KEY` (overlay included), exports `NAS_MOUNT` so compose can interpolate the volume bind and `HOST_UID`/`HOST_GID` so the container doesn't write as root, and prompts to confirm if `DRY_RUN=False` (02 deletes originals).

Run it:

```bash
./run-public-media-pipeline.sh          # 01 + 02 (default); prompts if DRY_RUN=False
./run-public-media-pipeline.sh -y       # skip the confirmation
./run-public-media-pipeline.sh 03       # only the online identification
./run-public-media-pipeline.sh 01 02 03 # everything
```

- [04-slim-audio.py](src/public-media/04-slim-audio.py) — **optional, opt-in step**: on a library already in HEVC, the *lossless* audio tracks (TrueHD Atmos, DTS-HD MA, PCM) routinely outweigh the video — measured up to two thirds of the file. This step re-encodes any track above `AUDIO_MAX_BITRATE` to EAC3 (or Opus) and **copies the video bit-for-bit**: no image loss, no encode generation, seconds per film instead of hours. Bitrate is *measured* by summing packets over a window, because TrueHD/DTS-HD do not declare one — the very tracks that matter. ffmpeg's `eac3` encoder caps at 6 channels, so a 7.1 source is downmixed (`AUDIO_MAX_CHANNELS`); `libopus` keeps 7.1 at a lower bitrate but only recent players decode multichannel Opus. Same rails as 02: track counts, duration and an actual size reduction are all verified before the original is replaced, an unmeasurable bitrate never triggers anything, and a plan that would leave *no* audio track is cancelled. Duplicate-language tracks are kept unless `AUDIO_DROP_DUPLICATE_LANGUAGES` is set — two `fre` tracks are usually two different dubs, not a duplicate.

03 and 04 are **not** in the default step list on purpose: 03 calls external APIs and renames the whole library, 04 rewrites files.

## The perso pipeline (`src/perso-media/`)

Four numbered steps, **all configured from one file**, [00-config.py](src/perso-media/00-config.py). There are no env vars and no per-script config blocks — `00-config.py` holds a COMMON section (single `DRY_RUN`, `PHOTO_EXT=.jpg`, `VIDEO_EXT=.mkv`, NVENC `VIDEO_CQ`/`VIDEO_PRESET`, `PHOTOS_SRC`, plausible-year bounds) plus a per-script section (`CONVERT_*`, `ENRICH_*`, `COMPRESS_*`, `UPLOAD_*`). Python steps load it through `_common.load_config()` (filenames contain digits/`+`/`-`, so they aren't importable directly); the launcher and the shell step read individual keys with `python3 src/perso-media/_common.py KEY` — same loading, overlay included.

The steps (each operates on the `photos` share, skipping folders whose name starts with `_` — enforced by `_common.in_excluded_folder`; 01 and 02 used to ignore this rule and write to `_` folders anyway). Steps 01 and 02 also start by purging orphan `*.h265tmp.mkv` / `*.datetmp.mkv` left by an interrupted run: they carry the library extension and would otherwise be treated as real videos and end up on Google Photos.

1. [01-convert-to-mkv+h265.py](src/perso-media/01-convert-to-mkv+h265.py) — **format normalization only**. Re-encodes with an explicit `-map 0:v:0 -map 0:a?` (ffmpeg's default selection keeps only *one* audio track), preserves **10-bit/HDR** (main10 + bt2020/PQ signalling) instead of flattening everything to 8-bit `yuv420p`, and **copies audio** already in an MKV-muxable compressed codec instead of re-encoding it to AAC. Every video ends up **H.265 + MKV** regardless of source codec/container (re-encodes non-HEVC, including existing `.mkv`, in place via temp + `os.replace`; remuxes already-HEVC). Normalizes photos too: decodes **HEIC/HEIF→JPG** (iPhone, EXIF preserved) and renames `.jpeg`→`.jpg`. **Preserves existing date tags but never infers them** (that is step 02's job). NVENC-only; aborts if no GPU encoder. Verifies output **duration** matches the source before deleting the original.
2. [02-enrich-movies-photos-with-date.py](src/perso-media/02-enrich-movies-photos-with-date.py) — **all date inference**. Fills missing capture dates from, in priority order: an existing tag → a **Google Takeout JSON sidecar** (`*.json` with `photoTakenTime`) → the filename (patterns like `YYYYMMDD_HHMMSS` and `2022-06-19 at 21.59.44`) → a neighboring photo's date in the same folder → a parent `YY.MM` folder name → file mtime. Photos via `piexif`, videos via ffprobe/ffmpeg.
3. [03-compress-for-gphotos.py](src/perso-media/03-compress-for-gphotos.py) — write a **compressed copy** to `output/gphotos` (photos resized so the long side ≤ `COMPRESS_MAX_PHOTO_SIZE`, videos re-encoded to `COMPRESS_VIDEO_HEIGHT`p via NVENC). Only ever produces `.jpg` + `.mkv`. `ImageFile.LOAD_TRUNCATED_IMAGES = True` to survive slightly-truncated JPEGs.
4. [04-upload-to-gphotos.sh](src/perso-media/04-upload-to-gphotos.sh) — **rclone** upload of `output/gphotos` to Google Photos; each first-level folder becomes an album of the same name. Replaces the old Python uploader (archived in `src/archives/`). Reads `UPLOAD_*` + `DRY_RUN` via the neighbouring `_common.py`; OAuth credentials come from `env/rclone.conf` (see Configuration).

Run it with the orchestrator:

```bash
./run-perso-media-pipeline.sh            # all steps; prompts to confirm if DRY_RUN=False
./run-perso-media-pipeline.sh -y         # skip the confirmation
./run-perso-media-pipeline.sh 03 04      # run only a subset of steps
```

The orchestrator mounts the perso scripts live (`-v src/perso-media:/work`), so editing `00-config.py` takes effect **without rebuilding** the image. It runs the container with `--user "$(id -u):$(id -g)"` so generated files are owned by you, not root.

## The web GUI (`src/gui/`)

A small **local web app** to launch, monitor and configure both pipelines from a browser instead of the CLI. **Stdlib-only Python** (`http.server`) — no pip deps on the host, consistent with the rest of the repo. It runs on the **host** (it must invoke `docker`/the launchers), reuses no business logic of its own, and **shells out to the two `run-*-pipeline.sh` launchers** in a subprocess.

- [src/gui/server.py](src/gui/server.py) — threaded HTTP server. Step entries are `[id, desc]` or `[id, desc, checked_by_default]` (public `03` uses `False`); a field may carry `"secret": True` to render masked (API keys). A single global `RunManager` enforces **one run at a time** (GPU/Docker are serial), buffers log lines, and streams them over **SSE** (`/api/stream`). Runs are launched with `start_new_session=True`; **Stop** sends `SIGINT` to the process group (so `docker compose`/`docker run` shut down cleanly), escalating to `SIGKILL` after ~12s. The `PIPELINES` dict is the single source of truth for each pipeline's launcher, config path, steps, and **editable config fields** (whitelisted; typed `bool`/`int`/`str`/`str_or_none`/`list`/`choice`).
- [src/gui/index.html](src/gui/index.html) — one self-contained page (vanilla JS, no build step), light and deliberately dense. A **sticky top bar** holds everything that acts: pipeline switch (segmented control), dry-run/real selector (mapped to the launchers' `--dry-run`/`--real` flags, real prompts to confirm), *Enregistrer* / *Lancer* / *Arrêter*. Below it, **two columns**: on the left a *Général* card (the fields with no `step` — folders/mount first) then an *Étapes & options* card where each step is a toggle followed by **the config fields that tune that very step** (a field's `"step"` key in `PIPELINES` places it there; a disabled step dims its own options); on the right the live log console, sticky and full-height. Booleans render as switches, each field shows its config key underneath the label, and input widths are capped by type (a number is not as wide as a path).
- [run-gui.sh](run-gui.sh) — launcher: `./run-gui.sh [PORT] [HOST]` (default `8765` on `0.0.0.0`).

**Config editing writes a generated overlay, never the tracked file**: `write_overlay()` reads the current effective values (`00-config.py` + existing overlay, same as the launchers) and re-serializes every whitelisted field into `00-config.local.py` (temp + `os.replace`). The previous regex rewrite of `00-config.py` produced a `git status` diff on every launch and mangled any value containing a `#` (taken for a comment). When adding an editable field, add it to the pipeline's `fields` list in `PIPELINES` — the UI, the overlay writer and its `_literal()` serializer are all driven from there.

The log buffer is a bounded `deque` (`MAX_LOG_LINES`) with an absolute `total` counter: a multi-hour run emits hundreds of thousands of lines, and an unbounded list grew the server's memory without limit while every new SSE client replayed the whole thing. SSE cursors are absolute, so a client that falls behind gets a "lignes tronquées" marker instead of a silent gap.

The mode selector always passes `-y` plus an explicit `--dry-run`/`--real` override, so the GUI's choice wins over `00-config.py`'s `DRY_RUN` for that run. Default binding is `0.0.0.0` (LAN-reachable); since the GUI can trigger destructive runs, only expose it on a trusted network or bind `127.0.0.1`. On WSL2 the auto-detected IP is the internal NAT IP — reaching it from a phone needs a Windows `netsh portproxy`.

## Script conventions

- **`DRY_RUN` gates every mutating operation** (rename, delete, write, upload). `True` = simulate and log, change nothing. **Each pipeline has a single `DRY_RUN` in its own `00-config.py`** driving all its steps. The launchers also accept **`--dry-run` / `--real`** to override it for a single run without editing the file — they export `PIPELINE_DRY_RUN` (1/0), which every step honours above `00-config.py`. Always check it before running anything destructive — step 01 deletes originals after conversion, and 04 publishes to the internet.
- A `setup_logging()` / `log()` helper with **timestamped, Paris-timezone** output (the image sets `TZ=Europe/Paris`; some scripts also pin the logging converter to `Europe/Paris`).
- All H.265 encoding is **NVENC-only** — no CPU/libx265 fallback. A script aborts rather than silently encoding on CPU.
- Rename/move operations detect **collisions** before applying and report a final tally.
- **Data-safety guards before deleting an original**: stream-count *and* **duration** of the output must match the source; a **disk-space** check precedes every re-encode/compress. An **unreadable output duration counts as a failure** — that is exactly what ffprobe returns for a badly truncated file, and treating it as "check skipped" would delete the original.
- **Every script exits non-zero when anything failed.** The launchers rely on `set -e` + `trap ERR` to stop at the failing step and notify a failure; a script that always returns 0 makes a fully failed run indistinguishable from a clean one.
- **Never destroy before you can rebuild**: e.g. `piexif.dump()` runs *before* touching the photo — the previous order (`piexif.remove()` first) left photos stripped of GPS/orientation whenever the dump raised.
- **Dates are handled in Paris local time and written to video containers in UTC** (`_common.parse_any_date` / `to_utc_iso`). ffmpeg reads a bare `creation_time` as UTC, so writing local time shifts the whole library by 1–2 h in Google Photos. Never truncate a timezone suffix — convert it.
- **Scan cache + parallelism**: the convert steps memoise per-file codec/dimensions in a gitignored `.scan-cache.json` (keyed by mtime+size, versioned, written atomically and **flushed periodically** so an interrupted multi-hour run doesn't lose the scan; stale entries are pruned at the end) and probe with a thread pool, so repeated runs don't re-`ffprobe` the whole library. Photo compression (03) is parallelised; video/GPU work stays serial. All tunable in `00-config.py` (`CONVERT_SCAN_CACHE`, `CONVERT_SCAN_WORKERS`, `COMPRESS_PHOTO_WORKERS`).
- **End-of-run notification**: set `NOTIFY_WEBHOOK` (ntfy/webhook URL) in `00-config.py` and the launcher POSTs a success/failure summary via `python3 src/notify.py <config> "<message>"` (no-op when unset). One shared script for both pipelines — it used to be duplicated byte-for-byte.

## Configuration

Every entry point — the scripts, both launchers, `04-upload-to-gphotos.sh`, the
GUI — loads config the same way, via `_common.load_config()`: **`00-config.py`
first, then `00-config.local.py` if present**, executed in the same namespace so
the latter overrides. That overlay is gitignored and **entirely generated by the
web GUI**; the tracked `00-config.py` is never rewritten, so the repo stays clean
across runs. Deleting the overlay restores the tracked defaults. Scripts log
`surcouche active — …` when one is in effect — check for it before concluding
that an edit to `00-config.py` had no effect.

Launchers read individual keys with `python3 src/<pipeline>/_common.py KEY`,
which applies the same overlay — never re-implement the loading inline.

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

The image contains **no project script or config** — both launchers always mount the pipeline directory live on `/work` and run from there. Copying files into `/app` only froze a config that then silently diverged from the repo. The compose service runs as `${HOST_UID}:${HOST_GID}` (exported by the launcher), matching the perso pipeline's `--user`, so generated files (`conversion.log`, `.scan-cache.json`) belong to you and not to root.

`docker-compose.yml` defines only the `convert-h265` service; the perso pipeline deliberately does **not** go through compose (it needs per-step volume/GPU/user variations that the orchestrator applies in `docker run`). The image's default `CMD` is a neutral "use a launcher" message, so running the bare image does nothing destructive.

`black` runs on save in the dev container (`editor.formatOnSave`). Keep code black-formatted.

## Tests

`python3 -m unittest discover -s tests -v` — stdlib only, no NAS, no GPU, no pip
dependency (piexif is stubbed to import the enrich step). They cover the pure
functions that produced silent bugs: name cleaning, date/timezone parsing,
filename- and folder-derived dates, the scan cache, the config overlay, and
step 03's pure parts (rename pattern, moviehash, filename-derived title/year,
companion subtitles, match plausibility). Add a
case here rather than reasoning about these by hand.

## NAS mounting (WSL host — `/etc/`)

The NAS is mounted at WSL boot by `/etc/mount-nas-horus.sh`, called from
`/etc/wsl.conf`. Both live in `/etc/`, **not in this repo** (machine config,
`sudo` to edit). **[README.md](README.md#prérequis-hôte-une-seule-fois) holds the
full explanation and the script to copy** — it is not duplicated here.

The one rule that matters when editing this repo: **`NAS_MOUNT` must be
`/mnt/wsl/horus`, never `/mnt/horus`.** Docker Desktop runs in a separate WSL
distro that only sees Ubuntu's FS through `/mnt/wsl`; a CIFS mount under
`/mnt/horus` has `private` propagation, so `docker-compose.yml`'s
`${NAS_MOUNT}:${NAS_MOUNT}` bind would capture the **empty ext4 mountpoint** and
the container would see empty `movies/tvshows/…` folders. This applies to local
runs too, so use `/mnt/wsl/horus` everywhere.
