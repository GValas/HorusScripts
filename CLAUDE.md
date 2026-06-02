# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal collection of standalone Python utility scripts for managing a home NAS named **horus** (SMB/CIFS share holding media: `movies`, `tvshows`, `cartoons`, `photos`, `perso`). Each script in `src/` is independent — there is no shared package, no imports between scripts, no framework. Python 3.12.

`requirements.txt` is intentionally empty, but that only covers the container scripts. The `src/zzz/` scripts have **undeclared dependencies**: `ffmpeg`/`ffprobe` binaries on PATH (the conversion/deletion scripts) and the `piexif` + `hachoir` PyPI packages (the photo/video date scripts). Install those manually before running a `zzz/` script; don't assume the empty `requirements.txt` means zero deps.

The codebase (comments, log messages, README) is written in **French**. Match that language when editing existing files.

## Two execution contexts with different path conventions — important

Scripts target the NAS in **two incompatible ways**, depending on where they are meant to run:

- **Linux / container scripts** read the mount point from the `NAS_MOUNT` env var (`/mnt/wsl/horus`). Example: [src/script.py](src/script.py). These run inside the dev container or the prod container, where the host's SMB mount is bind-mounted in. **Use `/mnt/wsl/horus`, not `/mnt/horus`** — see the NAS-mounting section below for why a `/mnt/horus` bind makes the container see empty folders.
- **Windows-host scripts** hardcode Windows UNC paths like `r"\\horus\movies"` in a `ROOT`/`ROOTS` constant at the top of the file. Example: [src/clean-movies-names.py](src/clean-movies-names.py) and everything in `src/zzz/`. These are meant to be run directly on a Windows machine against the NAS — they will **not** work in the Linux container as-is.

When adding or modifying a script, decide which context it belongs to and follow that context's path convention. Don't assume `NAS_MOUNT` applies to the UNC-path scripts.

## Script conventions

Operational scripts follow a consistent shape (see [src/clean-movies-names.py](src/clean-movies-names.py), the most complete example):

- A configuration block at the top of the file: `ROOT`/`ROOTS` constants and a `DRY_RUN` boolean. **`DRY_RUN = True` means simulate, log what would happen, change nothing.** Always default new destructive scripts to `DRY_RUN = True` and gate every mutating operation (`os.rename`, delete, etc.) behind it.
- A `setup_logging()` helper returning a configured `logging.Logger` (stdout handler, timestamped format).
- For rename/move operations, detect **collisions** (two source names mapping to the same target) before applying, and report a final tally (renamed / collisions / errors).

Note the `DRY_RUN` default is **not consistent** across scripts — some are committed with `DRY_RUN = True` (safe), others with `DRY_RUN = False` (live), and a couple have no dry-run guard at all (e.g. [src/zzz/convert-cine-movies-to-h265.py](src/zzz/convert-cine-movies-to-h265.py) always converts). Check the config block before running anything destructive.

### `src/zzz/` — media-processing scripts (Windows host)

These are **full, working implementations**, not stubs — they are kept separate because they require the external deps above and run against the NAS over UNC paths. What they do:

- [convert-cine-movies-to-h265.py](src/zzz/convert-cine-movies-to-h265.py) — re-encode movies to HEVC via **NVENC** (`hevc_nvenc`), writing `*_x265.mkv` alongside originals and verifying stream counts survive.
- [convert-perso-movies-to-mkv-h265.py](src/zzz/convert-perso-movies-to-mkv-h265.py) — re-encode *legacy-codec* videos (matched against a `LEGACY_CODECS` set) to `libx265` MKV, **deleting the original on success**, with per-extension/per-codec tally and a log file written into the source dir.
- [delete-cine-movies-x264.py](src/zzz/delete-cine-movies-x264.py) — delete source videos that already have a `*_x265.mkv` counterpart (reclaim space after conversion).
- [enrich-movies-photos-with-date.py](src/zzz/enrich-movies-photos-with-date.py) — repair missing date metadata. Photos via `piexif`; videos via **hand-rolled, dependency-free byte-level container parsing** — MP4/MOV `mvhd` atom (ISO BMFF, incl. 64-bit largesize/Mac-epoch handling) and AVI `ICRD` chunk (RIFF `LIST INFO`). When editing the video readers/writers, mind the offset/size arithmetic — these directly mutate binary containers in place. Fallback date strategy: another tag in the same file → a sibling photo's date in the same folder → give up.

`src/zzz/tests/clean_pics.py` is **not a test** — it's a standalone earlier/simpler variant of the photo-date script (it processes a local `./data` folder via `os.getcwd()`). There is no test runner in this project.

## Running

```bash
# Dev container (VS Code: "Reopen in Container") or local:
python src/script.py

# Prod container — note env/.env lives in env/, so --env-file is required
# so docker-compose can interpolate ${NAS_MOUNT} in the volume mapping:
docker compose --env-file env/.env up

# Lint / format (tools installed in the dev container, not in requirements.txt):
black src/ && isort src/ && pylint src/
```

`black` runs on save in the dev container (`editor.formatOnSave`). Keep code black-formatted.

## Configuration

Copy `env/.env.example` to `env/.env` (gitignored) and set `NAS_MOUNT` (use `/mnt/wsl/horus` — see NAS-mounting section). The same `.env` is consumed by both `docker compose` (volume + container env) and Linux scripts run locally. `NAS_HOST` / `NAS_SHARE` / `NAS_CREDENTIALS` are referenced only by the SMB-mount step.

## NAS mounting (WSL host — `/etc/`)

The NAS is mounted at WSL boot by `/etc/mount-nas-horus.sh`, called from `/etc/wsl.conf` (`[boot] command = /etc/mount-nas-horus.sh`). Both files live in `/etc/`, **not in this repo** (they are host/machine config, require `sudo` to edit). The script mounts the CIFS shares (`photos movies tvshows cartoons`) from `//192.168.1.182` using `~/.nas-credentials` (hardcoded — it does **not** read `env/.env`).

Key trick: Docker Desktop runs in a separate WSL distro that only sees Ubuntu's FS via `/mnt/wsl` (propagation `shared`). A CIFS mount under `/mnt/horus` is invisible in containers, so the script mounts the real CIFS under `/mnt/wsl/horus/*` (Docker-visible, `--make-shared`) and bind-mirrors it to `/mnt/horus/*` for convenience. It is idempotent (guards each mount with `mountpoint -q`) and can be re-run by hand.

**`NAS_MOUNT` must be `/mnt/wsl/horus`, never `/mnt/horus`.** `docker-compose.yml` bind-mounts `${NAS_MOUNT}:${NAS_MOUNT}` into the container. Docker Desktop only resolves host paths under `/mnt/wsl`; a bind on `/mnt/horus` captures the **empty ext4 mountpoint directory** (the CIFS submounts don't propagate to Docker's distro), so the container sees empty `movies/tvshows/...` folders. `/mnt/wsl/horus` is the shared CIFS mount Docker can actually see — and it also works for local (non-container) runs, so use it everywhere.

The CIFS lines in `/etc/fstab` are neutralized (commented) because they mounted with `private` propagation under `/mnt/horus`, invisible to containers. Backups exist as `/etc/wsl.conf.bak` and `/etc/fstab.bak`. After editing `/etc/wsl.conf`, validate the boot trigger with `wsl --shutdown` (PowerShell) then relaunch Ubuntu.
