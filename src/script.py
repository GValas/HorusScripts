#!/usr/bin/env python3
"""
Liste les fichiers d'un partage NAS monté en SMB.
Le partage doit être monté sur /mnt/nas (voir README.md).
"""

import os
import sys
from pathlib import Path
from datetime import datetime

NAS_MOUNT = Path(os.environ.get("NAS_MOUNT", "/mnt/horus/photos"))


def format_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def list_files(path: Path, indent: int = 0) -> None:
    prefix = "  " * indent
    try:
        entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    except PermissionError:
        print(f"{prefix}[Permission refusée]")
        return

    for entry in entries:
        try:
            stat = entry.stat()
            modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")

            if entry.is_dir():
                print(f"{prefix}📁 {entry.name}/  ({modified})")
                if indent < 2:  # limite la profondeur à 2 niveaux
                    list_files(entry, indent + 1)
            else:
                size = format_size(stat.st_size)
                print(f"{prefix}📄 {entry.name}  [{size}]  ({modified})")
        except (PermissionError, OSError) as e:
            print(f"{prefix}⚠️  {entry.name}  [Erreur: {e}]")


def main() -> None:
    print(f"{'='*60}")
    print(f"  Listage du NAS : {NAS_MOUNT}")
    print(f"  Date           : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    if not NAS_MOUNT.exists():
        print(f"❌ Le point de montage {NAS_MOUNT} n'existe pas.")
        print("   Vérifiez que le partage SMB est bien monté.")
        print("   Voir README.md pour les instructions.")
        sys.exit(1)

    if not os.access(NAS_MOUNT, os.R_OK):
        print(f"❌ Pas de droits de lecture sur {NAS_MOUNT}.")
        sys.exit(1)

    list_files(NAS_MOUNT)
    print(f"\n{'='*60}")
    print("  Terminé.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
