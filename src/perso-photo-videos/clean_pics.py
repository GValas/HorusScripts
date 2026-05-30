import piexif
from datetime import datetime
import os

NULL_DATE = b"0000:00:00 00:00:00"
IMG_DT = piexif.ImageIFD.DateTime
EXF_DIG_DT = piexif.ExifIFD.DateTimeDigitized
EXF_ORI_DT = piexif.ExifIFD.DateTimeOriginal


def creation_date(file):
    t = os.path.getmtime(file)
    d = datetime.fromtimestamp(t)
    s = d.strftime("%Y:%m:%d %H:%M:%S")
    return s.encode("utf-8")


def get_files_to_process(dir):
    return [
        os.path.join(root, file)
        for root, _, files in os.walk(dir)
        for file in files
        if file.lower().endswith((".jpg", ".jpeg"))
    ]


def update_pics(files):
    print(f"parsing {pics_dir}")
    print(f"processing {len(files)} files")

    for file in files:

        # img = Image.open(file)
        pic_dict = piexif.load(file)
        oth_dict = pic_dict["0th"]
        exif_dict = pic_dict["Exif"]

        # key must be present and value not null
        has_idf = IMG_DT in oth_dict and oth_dict[IMG_DT] != NULL_DATE
        has_exif_digitized = (
            EXF_DIG_DT in exif_dict and exif_dict[EXF_DIG_DT] != NULL_DATE
        )
        has_exif_original = (
            EXF_ORI_DT in exif_dict and exif_dict[EXF_ORI_DT] != NULL_DATE
        )

        # at least one field must be missing
        if has_idf and has_exif_digitized and has_exif_original:
            continue

        # get best date proxy
        best_date = None
        if has_exif_digitized:
            best_date = exif_dict[EXF_DIG_DT]
        elif has_exif_original:
            best_date = exif_dict[EXF_ORI_DT]
        elif has_idf:
            best_date = oth_dict[IMG_DT]
        else:
            best_date = creation_date(file)

        # date needed for update
        if best_date is None:
            print(f"{file} has no best date ! ")
            continue

        # updating fields
        if not has_idf:
            oth_dict[IMG_DT] = best_date

        if not has_exif_original:
            exif_dict[EXF_ORI_DT] = best_date

        if not has_exif_digitized:
            exif_dict[EXF_DIG_DT] = best_date

        piexif.remove(file)
        exif_bytes = piexif.dump(pic_dict)
        piexif.insert(exif_bytes, file)
        print(f"{file} updated ")


if __name__ == "__main__":
    os.system("clear")
    pics_dir = os.path.join(os.getcwd(), "data")
    files = get_files_to_process(pics_dir)
    update_pics(files)
