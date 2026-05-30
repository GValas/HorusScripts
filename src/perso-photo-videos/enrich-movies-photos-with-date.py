#!/usr/bin/env python3
"""
scan_media_dates.py
--------------------
Scanne récursivement un répertoire, détecte les tags de date manquants,
et les corrige automatiquement (photos JPEG via piexif, vidéos en natif ISO BMFF/RIFF).

Stratégie de date de remplacement (par ordre de priorité) :
  1. Un autre tag de date présent dans le même fichier
  2. La date d'une photo du même dossier qui possède déjà un tag valide
  3. Abandon (fichier signalé comme non corrigeable)

Dépendances:
    pip install piexif hachoir
"""

import os
import sys
import csv
import json
import struct
import shutil
from pathlib import Path
from datetime import datetime

import piexif

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — modifiez ces variables selon vos besoins
# ══════════════════════════════════════════════════════════════════════════════

REPERTOIRE   = r"\\horus\photos"   # Répertoire racine à scanner
EXPORT_CSV   = r"rapport.csv"      # Chemin du rapport CSV  (None pour désactiver)
EXPORT_JSON  = None                # Chemin du rapport JSON (None pour désactiver)

DRY_RUN      = True                # True = simulation, aucun fichier modifié
FIX_PHOTOS   = True                # Corriger les photos JPEG
FIX_VIDEOS   = True                # Corriger les vidéos

# ══════════════════════════════════════════════════════════════════════════════

NULL_DATE = b"0000:00:00 00:00:00"

PHOTO_EXTENSIONS = {".jpg", ".jpeg"}

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v",
    ".wmv", ".flv", ".webm", ".mts", ".m2ts",
}

# Cache : pour chaque dossier, la première date valide trouvée dans le dossier
_folder_date_cache: dict[Path, bytes | None] = {}


# ══════════════════════════════════════════════════════════════════════════════
# LECTURE DES TAGS
# ══════════════════════════════════════════════════════════════════════════════

def check_photo_tags(filepath: Path) -> dict:
    result = {
        "type": "photo",
        "tags_found": [],
        "tags_missing": [],
        "has_critical_tag": False,
        "primary_date": None,
        "pic_dict": None,   # conservé pour la correction
        "error": None,
    }

    TAG_DEFS = [
        ("0th",  piexif.ImageIFD.DateTime,          "Image DateTime",         False),
        ("Exif", piexif.ExifIFD.DateTimeOriginal,   "EXIF DateTimeOriginal",  True),
        ("Exif", piexif.ExifIFD.DateTimeDigitized,  "EXIF DateTimeDigitized", False),
    ]

    try:
        pic_dict = piexif.load(str(filepath))
        result["pic_dict"] = pic_dict

        for ifd, tag_id, tag_name, is_critical in TAG_DEFS:
            ifd_dict = pic_dict.get(ifd, {})
            val = ifd_dict.get(tag_id)
            if val and val != NULL_DATE:
                date_str = val.decode("utf-8", errors="replace") if isinstance(val, bytes) else str(val)
                result["tags_found"].append({"tag": tag_name, "value": date_str, "raw": val})
                if result["primary_date"] is None:
                    result["primary_date"] = date_str
                if is_critical:
                    result["has_critical_tag"] = True
            else:
                result["tags_missing"].append({"tag": tag_name})

    except Exception as e:
        result["error"] = str(e)
        for _, _, tag_name, _ in TAG_DEFS:
            result["tags_missing"].append({"tag": tag_name})

    return result


def check_video_tags(filepath: Path) -> dict:
    result = {
        "type": "video",
        "tags_found": [],
        "tags_missing": [],
        "has_critical_tag": False,
        "primary_date": None,
        "error": None,
    }

    try:
        from hachoir.parser import createParser
        from hachoir.metadata import extractMetadata
        parser = createParser(str(filepath))
        if parser:
            with parser:
                metadata = extractMetadata(parser)
            if metadata:
                date_val = None
                if metadata.has("creation_date"):
                    date_val = str(metadata.get("creation_date"))
                elif metadata.has("last_modification"):
                    date_val = str(metadata.get("last_modification"))
                if date_val:
                    result["tags_found"].append({"tag": "creation_date", "value": date_val})
                    result["primary_date"] = date_val
                    result["has_critical_tag"] = True
                else:
                    result["tags_missing"].append({"tag": "creation_date"})
                return result
    except ImportError:
        pass
    except Exception as e:
        result["error"] = f"hachoir: {e}"

    # Lecture ICRD sur AVI (RIFF natif, sans dépendance)
    if filepath.suffix.lower() == ".avi":
        try:
            date_val = _read_avi_icrd(filepath)
            if date_val:
                result["tags_found"].append({"tag": "ICRD (RIFF INFO)", "value": date_val})
                result["primary_date"] = date_val
                result["has_critical_tag"] = True
            else:
                result["tags_missing"].append({"tag": "ICRD (RIFF INFO)"})
        except Exception as e:
            if not result["error"]:
                result["error"] = str(e)
            result["tags_missing"].append({"tag": "ICRD (RIFF INFO)"})
        return result

    # Lecture atome mvhd (MP4/MOV)
    try:
        date_val = _read_mp4_creation_date(filepath)
        if date_val:
            result["tags_found"].append({"tag": "creation_time (mvhd)", "value": date_val})
            result["primary_date"] = date_val
            result["has_critical_tag"] = True
        else:
            result["tags_missing"].append({"tag": "creation_time"})
    except Exception as e:
        if not result["error"]:
            result["error"] = str(e)
        result["tags_missing"].append({"tag": "creation_time"})

    return result


# ── Utilitaires MP4/MOV (ISO BMFF) natifs ────────────────────────────────────

MAC_EPOCH_OFFSET = 2082844800  # secondes entre 1904-01-01 et 1970-01-01

def _find_mp4_box(data: bytes, target: bytes, start: int = 0, end: int = None):
    """
    Recherche récursive d'un atome ISO BMFF / QuickTime. Retourne (offset, size) ou None.
    Gère : size=0 (jusqu'à EOF), size=1 (64-bit largesize), wide boxes (padding QuickTime).
    """
    if end is None:
        end = len(data)
    offset = start
    while offset + 8 <= end:
        raw_size = struct.unpack(">I", data[offset:offset+4])[0]
        box_type = data[offset+4:offset+8]

        # size=0 : l'atome s'étend jusqu'à la fin du container
        if raw_size == 0:
            box_size = end - offset
        # size=1 : taille réelle encodée sur 8 octets après le type (largesize)
        elif raw_size == 1:
            if offset + 16 > end:
                break
            box_size    = struct.unpack(">Q", data[offset+8:offset+16])[0]
            inner_start = offset + 16   # payload commence après largesize
        else:
            box_size    = raw_size
            inner_start = offset + 8

        if box_size < 8:
            break

        # wide box (8 octets, padding QuickTime avant mdat/moov) : ignorer
        if box_type == b"wide":
            offset += box_size
            continue

        if box_type == target:
            return offset, box_size

        # Descendre dans les containers
        if box_type in (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta"):
            result = _find_mp4_box(data, target, inner_start, offset + box_size)
            if result:
                return result

        offset += box_size
    return None


def _parse_date_str(date_str: str) -> datetime:
    """Parse une date dans les formats courants EXIF/ISO."""
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(date_str[:19], fmt)
        except ValueError:
            continue
    raise ValueError(f"Format de date non reconnu: {date_str!r}")


def _read_mp4_creation_date(filepath: Path) -> str | None:
    """Lit la date de création depuis l'atome mvhd d'un MP4/MOV."""
    try:
        with open(filepath, "rb") as f:
            data = f.read(min(8 * 1024 * 1024, os.path.getsize(filepath)))
        result = _find_mp4_box(data, b"mvhd")
        if not result:
            return None
        off, size = result
        payload = data[off+8:off+size]
        version = payload[0]
        creation_time = struct.unpack(">I", payload[4:8])[0] if version == 0                         else struct.unpack(">Q", payload[4:12])[0]
        if creation_time > 0:
            unix_ts = creation_time - MAC_EPOCH_OFFSET
            if 0 < unix_ts < 4_102_444_800:
                return datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return None


def _write_mp4_creation_date(filepath: Path, date_str: str) -> None:
    """
    Écrit la date directement dans l'atome mvhd d'un MP4/MOV, in-place.
    Modifie creation_time ET modification_time.
    Gère les atomes 64-bit (largesize) : le payload mvhd commence à off+16 au lieu de off+8.
    """
    with open(filepath, "r+b") as f:
        data = bytearray(f.read())
    result = _find_mp4_box(bytes(data), b"mvhd")
    if not result:
        raise ValueError("Atome mvhd introuvable dans le fichier")
    off, _ = result
    # Déterminer où commence le payload (après size+type, ou après size+type+largesize)
    raw_size    = struct.unpack(">I", data[off:off+4])[0]
    payload_off = off + 16 if raw_size == 1 else off + 8
    version     = data[payload_off]
    dt          = _parse_date_str(date_str)
    mac_ts      = int(dt.timestamp()) + MAC_EPOCH_OFFSET
    if version == 0:
        struct.pack_into(">I", data, payload_off + 4, mac_ts)   # creation_time
        struct.pack_into(">I", data, payload_off + 8, mac_ts)   # modification_time
    else:
        struct.pack_into(">Q", data, payload_off + 4,  mac_ts)
        struct.pack_into(">Q", data, payload_off + 12, mac_ts)
    with open(filepath, "wb") as f:
        f.write(data)


# ══════════════════════════════════════════════════════════════════════════════
# RECHERCHE DE DATE DE REMPLACEMENT
# ══════════════════════════════════════════════════════════════════════════════

def _best_date_from_tags(info: dict) -> bytes | None:
    """Retourne la meilleure date trouvée dans les tags existants du fichier."""
    for tag in info.get("tags_found", []):
        raw = tag.get("raw")
        if raw and isinstance(raw, bytes) and raw != NULL_DATE:
            return raw
    return None


def _date_from_folder(filepath: Path) -> bytes | None:
    """
    Cherche la date d'une autre photo JPEG dans le même dossier
    qui possède déjà un tag DateTimeOriginal valide.
    Résultat mis en cache par dossier.
    """
    folder = filepath.parent
    if folder in _folder_date_cache:
        return _folder_date_cache[folder]

    date_found = None
    for sibling in sorted(folder.iterdir()):
        if sibling == filepath:
            continue
        if sibling.suffix.lower() not in PHOTO_EXTENSIONS:
            continue
        try:
            pic = piexif.load(str(sibling))
            val = pic.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
            if val and val != NULL_DATE:
                date_found = val
                break
        except Exception:
            continue

    _folder_date_cache[folder] = date_found
    return date_found


def resolve_date(filepath: Path, info: dict) -> tuple[bytes | None, str]:
    """
    Retourne (date_bytes, source) où source décrit l'origine de la date.
    Retourne (None, "introuvable") si aucune date n'est disponible.
    """
    # Priorité 1 : autre tag dans le même fichier
    date = _best_date_from_tags(info)
    if date:
        return date, "tag existant dans le fichier"

    # Priorité 2 : date d'une photo voisine dans le même dossier
    date = _date_from_folder(filepath)
    if date:
        return date, f"photo voisine dans {filepath.parent.name}/"

    return None, "introuvable"


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTION DES FICHIERS
# ══════════════════════════════════════════════════════════════════════════════

def fix_photo(filepath: Path, info: dict, date: bytes, dry_run: bool) -> str:
    """Injecte les tags de date manquants dans une photo JPEG."""
    pic_dict = info.get("pic_dict")
    if pic_dict is None:
        return "erreur: EXIF non chargé"

    oth_dict  = pic_dict.setdefault("0th", {})
    exif_dict = pic_dict.setdefault("Exif", {})

    changed = False
    if not oth_dict.get(piexif.ImageIFD.DateTime) or oth_dict.get(piexif.ImageIFD.DateTime) == NULL_DATE:
        oth_dict[piexif.ImageIFD.DateTime] = date
        changed = True
    if not exif_dict.get(piexif.ExifIFD.DateTimeOriginal) or exif_dict.get(piexif.ExifIFD.DateTimeOriginal) == NULL_DATE:
        exif_dict[piexif.ExifIFD.DateTimeOriginal] = date
        changed = True
    if not exif_dict.get(piexif.ExifIFD.DateTimeDigitized) or exif_dict.get(piexif.ExifIFD.DateTimeDigitized) == NULL_DATE:
        exif_dict[piexif.ExifIFD.DateTimeDigitized] = date
        changed = True

    if not changed:
        return "déjà complet"

    if dry_run:
        return "DRY RUN — serait mis à jour"

    try:
        piexif.remove(str(filepath))
        exif_bytes = piexif.dump(pic_dict)
        piexif.insert(exif_bytes, str(filepath))
        return "corrigé ✅"
    except Exception as e:
        return f"erreur écriture: {e}"



def _read_avi_icrd(filepath: Path) -> str | None:
    """Lit le chunk ICRD (date de création) dans un fichier AVI (format RIFF)."""
    try:
        with open(filepath, "rb") as f:
            data = f.read(min(4 * 1024 * 1024, os.path.getsize(filepath)))
        if data[:4] != b"RIFF" or data[8:12] != b"AVI ":
            return None
        offset = 12
        while offset < len(data) - 8:
            chunk_id   = data[offset:offset+4]
            chunk_size = struct.unpack("<I", data[offset+4:offset+8])[0]
            if chunk_id == b"LIST" and data[offset+8:offset+12] == b"INFO":
                inner = offset + 12
                end   = offset + 8 + chunk_size
                while inner < end - 8:
                    sub_id   = data[inner:inner+4]
                    sub_size = struct.unpack("<I", data[inner+4:inner+8])[0]
                    if sub_id == b"ICRD":
                        val = data[inner+8:inner+8+sub_size].rstrip(b"\x00")
                        return val.decode("latin-1", errors="replace").strip()
                    inner += 8 + sub_size + (sub_size % 2)
            offset += 8 + chunk_size + (chunk_size % 2)
    except Exception:
        pass
    return None


def _write_avi_icrd(filepath: Path, date_str: str) -> None:
    """
    Écrit (ou remplace) le chunk ICRD dans le bloc LIST INFO d'un AVI.
    Si le bloc LIST INFO n'existe pas, il est créé juste après le header AVI.
    La date est formatée en chaîne ISO : YYYY-MM-DD HH:MM:SS
    """
    with open(filepath, "r+b") as f:
        data = bytearray(f.read())

    if data[:4] != b"RIFF" or data[8:12] != b"AVI ":
        raise ValueError("Pas un fichier AVI valide")

    icrd_value = date_str.encode("latin-1") + b"\x00"
    if len(icrd_value) % 2 != 0:
        icrd_value += b"\x00"  # padding RIFF word-align

    # Cherche un LIST INFO existant
    offset = 12
    list_info_offset = None
    while offset < len(data) - 8:
        chunk_id   = data[offset:offset+4]
        chunk_size = struct.unpack("<I", data[offset+4:offset+8])[0]
        if chunk_id == b"LIST" and data[offset+8:offset+12] == b"INFO":
            list_info_offset = offset
            break
        offset += 8 + chunk_size + (chunk_size % 2)

    if list_info_offset is not None:
        # Cherche ICRD dans ce LIST INFO
        list_size = struct.unpack("<I", data[list_info_offset+4:list_info_offset+8])[0]
        inner     = list_info_offset + 12
        end       = list_info_offset + 8 + list_size
        icrd_offset = None
        while inner < end - 8:
            sub_id   = data[inner:inner+4]
            sub_size = struct.unpack("<I", data[inner+4:inner+8])[0]
            if sub_id == b"ICRD":
                icrd_offset = inner
                break
            inner += 8 + sub_size + (sub_size % 2)

        new_icrd_chunk = b"ICRD" + struct.pack("<I", len(icrd_value)) + icrd_value

        if icrd_offset is not None:
            # Remplace l'ancien chunk ICRD
            old_size     = struct.unpack("<I", data[icrd_offset+4:icrd_offset+8])[0]
            old_size_pad = old_size + (old_size % 2)
            data[icrd_offset:icrd_offset+8+old_size_pad] = new_icrd_chunk
        else:
            # Insère ICRD à la fin du LIST INFO
            insert_pos = end
            data[insert_pos:insert_pos] = new_icrd_chunk

        # Recalcule la taille du LIST INFO
        new_list_size = len(data) - list_info_offset - 8
        # Retrouve la vraie taille (distance jusqu'au prochain chunk de même niveau)
        # Plus simple : on recalcule depuis l'intérieur
        inner = list_info_offset + 12
        content_size = 4  # "INFO" type
        while inner < len(data) - 8:
            sub_id   = data[inner:inner+4]
            if sub_id in (b"RIFF", b"LIST", b"idx1", b"movi") or sub_id == bytes(4):
                break
            sub_size = struct.unpack("<I", data[inner+4:inner+8])[0]
            content_size += 8 + sub_size + (sub_size % 2)
            inner += 8 + sub_size + (sub_size % 2)
        struct.pack_into("<I", data, list_info_offset+4, content_size)

    else:
        # Crée un nouveau LIST INFO après le header AVI (offset 12)
        new_icrd_chunk  = b"ICRD" + struct.pack("<I", len(icrd_value)) + icrd_value
        list_content    = b"INFO" + new_icrd_chunk
        new_list_chunk  = b"LIST" + struct.pack("<I", len(list_content)) + list_content
        data[12:12]     = new_list_chunk

    # Met à jour la taille globale RIFF
    struct.pack_into("<I", data, 4, len(data) - 8)

    with open(filepath, "wb") as f:
        f.write(data)

def fix_video(filepath: Path, date: bytes, dry_run: bool) -> str:
    """
    Injecte la date de création dans une vidéo :
    - MP4/MOV/M4V/3GP : atome mvhd (natif ISO BMFF, sans dépendance)
    - AVI             : chunk ICRD dans LIST INFO (RIFF natif, sans dépendance)
    """
    ext      = filepath.suffix.lower()
    date_str = date.decode("utf-8", errors="replace") if isinstance(date, bytes) else str(date)

    # Normalise vers "YYYY-MM-DD HH:MM:SS"
    try:
        dt      = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
        date_iso = dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        date_iso = date_str

    # ── AVI (RIFF natif) ──────────────────────────────────────────────────────
    if ext == ".avi":
        if dry_run:
            return "DRY RUN — serait mis à jour (RIFF ICRD)"
        try:
            _write_avi_icrd(filepath, date_iso)
            return "corrigé ✅ (RIFF ICRD)"
        except Exception as e:
            return f"erreur écriture AVI: {e}"

    # ── MP4 / MOV / M4V / 3GP (natif — atome mvhd) ──────────────────────────
    if ext in {".mp4", ".mov", ".m4v", ".3gp"}:
        if dry_run:
            return "DRY RUN — serait mis à jour (mvhd)"
        try:
            _write_mp4_creation_date(filepath, date_iso)
            return "corrigé ✅ (mvhd)"
        except Exception as e:
            return f"erreur écriture MP4: {e}"

    return "correction non supportée pour ce format vidéo"


# ══════════════════════════════════════════════════════════════════════════════
# SCAN PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

def scan_and_fix(root: Path, dry_run: bool, fix_photos: bool, fix_videos: bool) -> list[dict]:
    results = []
    all_files = sorted(root.rglob("*"))
    media_files = [
        f for f in all_files
        if f.is_file() and f.suffix.lower() in (PHOTO_EXTENSIONS | VIDEO_EXTENSIONS)
    ]

    total = len(media_files)
    mode  = "🧪 DRY RUN" if dry_run else "✏️  ÉCRITURE"
    print(f"\n{mode} — {total} fichier(s) média dans « {root} »\n")

    for i, filepath in enumerate(media_files, 1):
        is_video = filepath.suffix.lower() in VIDEO_EXTENSIONS
        info     = check_video_tags(filepath) if is_video else check_photo_tags(filepath)

        fix_result = None
        date_source = None

        if not info["has_critical_tag"]:
            date, date_source = resolve_date(filepath, info)

            if date:
                if is_video and fix_videos:
                    fix_result = fix_video(filepath, date, dry_run)
                elif not is_video and fix_photos:
                    fix_result = fix_photo(filepath, info, date, dry_run)
                else:
                    fix_result = "correction désactivée"
            else:
                fix_result = "⛔ aucune date disponible"

            print(f"  ⚠️  [{i}/{total}] {filepath}")
            print(f"       source date : {date_source or 'introuvable'}")
            print(f"       action      : {fix_result}")

        record = {
            "fichier":              str(filepath),
            "type":                 info["type"],
            "tag_critique_present": info["has_critical_tag"],
            "date_principale":      info.get("primary_date") or "",
            "tags_presents":        "; ".join(t["tag"] for t in info.get("tags_found", [])),
            "tags_manquants":       "; ".join(t["tag"] for t in info.get("tags_missing", [])),
            "date_source":          date_source or "",
            "action":               fix_result or "",
            "erreur":               info.get("error") or "",
        }
        results.append(record)

    return results


# ══════════════════════════════════════════════════════════════════════════════
# RAPPORT
# ══════════════════════════════════════════════════════════════════════════════

def print_summary(results: list[dict], dry_run: bool) -> None:
    ok       = sum(1 for r in results if r["tag_critique_present"])
    ko       = sum(1 for r in results if not r["tag_critique_present"])
    fixed    = sum(1 for r in results if "corrigé" in r["action"] or "DRY RUN" in r["action"])
    unfixable= sum(1 for r in results if "introuvable" in r["action"] or "⛔" in r["action"])
    errors   = sum(1 for r in results if r["erreur"])
    total    = len(results)

    print("\n" + "═" * 60)
    print(f"  RÉSUMÉ {'(DRY RUN)' if dry_run else ''}")
    print("═" * 60)
    print(f"  Total analysé        : {total}")
    print(f"  ✅ Tag déjà OK       : {ok}")
    print(f"  ⚠️  Tag manquant      : {ko}")
    print(f"  🔧 Corrigé{'(simulé)' if dry_run else ''}  : {fixed}")
    print(f"  ⛔ Non corrigeable   : {unfixable}")
    if errors:
        print(f"  ❌ Erreurs lecture  : {errors}")
    print("═" * 60)

    if dry_run and fixed > 0:
        print("\n💡  C'est un DRY RUN. Passez DRY_RUN = False pour appliquer les corrections.")


def save_csv(results: list[dict], output_path: str) -> None:
    fieldnames = [
        "fichier", "type", "tag_critique_present", "date_principale",
        "tags_presents", "tags_manquants", "date_source", "action", "erreur",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    print(f"\n📄  Rapport CSV enregistré : {output_path}")


# ══════════════════════════════════════════════════════════════════════════════
# POINT D'ENTRÉE
# ══════════════════════════════════════════════════════════════════════════════

def main():
    root = Path(REPERTOIRE)
    if not root.exists():
        print(f"❌  Répertoire introuvable : {root}", file=sys.stderr)
        sys.exit(1)

    results = scan_and_fix(root, dry_run=DRY_RUN, fix_photos=FIX_PHOTOS, fix_videos=FIX_VIDEOS)
    print_summary(results, dry_run=DRY_RUN)

    if EXPORT_CSV:
        save_csv(results, EXPORT_CSV)

    if EXPORT_JSON:
        with open(EXPORT_JSON, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"📄  Rapport JSON enregistré : {EXPORT_JSON}")


if __name__ == "__main__":
    main()