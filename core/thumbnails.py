import os
import shutil
import subprocess
import tempfile
import zipfile
from typing import Optional

from PIL import Image, ImageOps

from core.database import DATA_DIR, get_connection

THUMB_DIR = os.path.join(DATA_DIR, "thumbnails")


def get_or_create_thumbnail(asset_id: int) -> Optional[bytes]:
    """
    Returns JPEG thumbnail bytes for a takeout_asset (works for JPG, PNG, HEIC, and MP4/MOV videos,
    whether inside a ZIP archive, an uncompressed folder, or already exported).
    """
    os.makedirs(THUMB_DIR, exist_ok=True)
    cache_path = os.path.join(THUMB_DIR, f"thumb_{asset_id}.jpg")
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 100:
        with open(cache_path, "rb") as f:
            return f.read()

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, source_type, source_path, inner_path, ext, kind, exported_path FROM takeout_assets WHERE id = ?",
            (asset_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return None

    with tempfile.TemporaryDirectory() as tmp_dir:
        local_src = None
        if row["exported_path"] and os.path.exists(row["exported_path"]):
            local_src = row["exported_path"]
        elif row["source_type"] == "folder":
            cand = os.path.join(row["source_path"], row["inner_path"])
            if os.path.exists(cand):
                local_src = cand
        elif row["source_type"] == "zip" and os.path.exists(row["source_path"]):
            try:
                ext = row["ext"] or ".jpg"
                tmp_extract = os.path.join(tmp_dir, f"src{ext}")
                with zipfile.ZipFile(row["source_path"], "r") as zf:
                    with zf.open(row["inner_path"]) as zin, open(tmp_extract, "wb") as fout:
                        shutil.copyfileobj(zin, fout, length=1024 * 1024)
                local_src = tmp_extract
            except Exception:
                return None

        if not local_src or not os.path.exists(local_src):
            return None

        # 1. Try Pillow first for standard web images (.jpg, .png, .webp)
        ext_low = (row["ext"] or "").lower()
        if row["kind"] == "photo" and ext_low in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"):
            try:
                with Image.open(local_src) as img:
                    img = ImageOps.exif_transpose(img)
                    img.thumbnail((280, 280))
                    rgb = img.convert("RGB")
                    rgb.save(cache_path, "JPEG", quality=82)
                with open(cache_path, "rb") as f:
                    return f.read()
            except Exception:
                pass

        # 2. For .HEIC / .RAW / .PNG fallback on macOS: use native `sips`
        if row["kind"] == "photo":
            try:
                proc = subprocess.run(
                    ["sips", "-s", "format", "jpeg", "-Z", "280", local_src, "--out", cache_path],
                    capture_output=True,
                    timeout=10,
                )
                if proc.returncode == 0 and os.path.exists(cache_path) and os.path.getsize(cache_path) > 100:
                    with open(cache_path, "rb") as f:
                        return f.read()
            except Exception:
                pass

        # 3. For Videos (.mp4, .mov) or any file supported by macOS QuickLook: use `qlmanage -t`
        try:
            proc = subprocess.run(
                ["qlmanage", "-t", "-s", "280", "-o", tmp_dir, local_src],
                capture_output=True,
                timeout=12,
            )
            for fn in os.listdir(tmp_dir):
                if fn.endswith(".png") and fn != os.path.basename(local_src):
                    ql_png = os.path.join(tmp_dir, fn)
                    with Image.open(ql_png) as img:
                        img.thumbnail((280, 280))
                        img.convert("RGB").save(cache_path, "JPEG", quality=82)
                    with open(cache_path, "rb") as f:
                        return f.read()
        except Exception:
            pass

    return None
