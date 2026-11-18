import datetime
import os
import re
from typing import Optional, Tuple

PHOTO_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".webp",
    ".gif",
    ".tif",
    ".tiff",
    ".bmp",
    ".dng",
    ".cr2",
    ".nef",
    ".arw",
    ".raw",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".3gp",
    ".avi",
    ".mkv",
    ".mts",
    ".webm",
    ".mpg",
    ".mpeg",
    ".wmv",
}

ALL_MEDIA_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS

# Common Google Takeout suffix patterns added to edited or duplicate files,
# as well as batch processor import ID suffixes like _14162
EDITED_SUFFIX_RE = re.compile(
    r"(-ha editado|-editado|-edited|-modifié|-bearbeitet|-수정됨|\(\d+\)|_\d{1,6})$",
    re.IGNORECASE,
)

# Extract dates from common camera/WhatsApp/screenshot filenames when JSON is missing
FILENAME_DATE_PATTERNS = [
    # IMG_20190514_153022 or VID_20190514_153022 or PXL_20210812_112233444
    re.compile(r"(?:IMG|VID|PXL|Screenshot|wp|signal)[-_]?((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])[-_]?([01]\d|2[0-3])([0-5]\d)([0-5]\d)"),
    # 2019-05-14 15.30.22 or 2019-05-14_15-30-22
    re.compile(r"((?:19|20)\d{2})[-_](0[1-9]|1[0-2])[-_](0[1-9]|[12]\d|3[01])[\sT_-]([01]\d|2[0-3])[\.:-]([0-5]\d)[\.:-]([0-5]\d)"),
    # IMG-20190514-WA0001 or VID-20190514-WA0001 or 20190514_153022
    re.compile(r"(?:^|\D)((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?:[-_]WA\d+|[-_]\d{6}|$)"),
]


def classify_media(filename: str) -> Optional[str]:
    ext = os.path.splitext(filename)[1].lower()
    if ext in PHOTO_EXTENSIONS:
        return "photo"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return None


def normalize_filename(filename: str) -> Tuple[str, str, str]:
    """
    Returns (norm_name, stem, ext) in lowercase.
    Strips Google Takeout duplicate/edited markers from stem so 'IMG_1234(1).JPG'
    or 'IMG_1234-ha editado.jpg' can still be matched cleanly when needed.
    """
    base = os.path.basename(filename).strip()
    stem, ext = os.path.splitext(base)
    ext_lower = ext.lower()
    clean_stem = EDITED_SUFFIX_RE.sub("", stem).strip().lower()
    norm_name = f"{clean_stem}{ext_lower}"
    return norm_name, clean_stem, ext_lower


def timestamp_to_ym(ts: Optional[int]) -> Tuple[str, int, int]:
    if not ts or ts <= 0:
        return "Sin-Fecha", 0, 0
    try:
        dt = datetime.datetime.fromtimestamp(ts)
        if dt.year < 1971 or dt.year > 2035:
            return "Sin-Fecha", 0, 0
        return f"{dt.year:04d}-{dt.month:02d}", dt.year, dt.month
    except Exception:
        return "Sin-Fecha", 0, 0


def guess_timestamp_from_filename_or_path(filename: str, inner_path: str = "") -> Optional[int]:
    base = os.path.basename(filename)
    for pat in FILENAME_DATE_PATTERNS:
        m = pat.search(base)
        if m:
            groups = m.groups()
            try:
                year = int(groups[0])
                month = int(groups[1])
                day = int(groups[2])
                hour = int(groups[3]) if len(groups) > 3 else 12
                minute = int(groups[4]) if len(groups) > 4 else 0
                second = int(groups[5]) if len(groups) > 5 else 0
                dt = datetime.datetime(year, month, day, hour, minute, second)
                return int(dt.timestamp())
            except ValueError:
                pass

    # Fallback: check if Google Takeout folder is "Photos from YYYY" or "Fotos de YYYY"
    if inner_path:
        m_folder = re.search(r"(?:Photos from|Fotos de)\s+((?:19|20)\d{2})", inner_path, re.IGNORECASE)
        if m_folder:
            year = int(m_folder.group(1))
            return int(datetime.datetime(year, 6, 15, 12, 0, 0).timestamp())

    return None


def format_bytes(size: int) -> str:
    if not size or size <= 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} PB"
