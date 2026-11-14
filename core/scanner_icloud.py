import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from typing import Callable, Dict, List, Optional, Tuple

from core.database import DATA_DIR, get_connection, set_setting
from core.utils import classify_media, normalize_filename, timestamp_to_ym

APPLE_EPOCH_OFFSET = 978307200  # Seconds between 1970-01-01 and 2001-01-01
DEFAULT_PHOTOS_LIB = os.path.expanduser("~/Pictures/Photos Library.photoslibrary")
SNAPSHOT_DIR = os.path.join(DATA_DIR, "icloud_snapshot")


def check_icloud_access() -> Dict[str, any]:
    """
    Checks whether we can read Photos.sqlite directly or if a snapshot exists in data/icloud_snapshot.
    """
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    sys_db = os.path.join(DEFAULT_PHOTOS_LIB, "database", "Photos.sqlite")
    snap_db = os.path.join(SNAPSHOT_DIR, "Photos.sqlite")

    can_read_sys = False
    if os.path.exists(sys_db):
        try:
            with open(sys_db, "rb") as f:
                f.read(16)
            can_read_sys = True
        except PermissionError:
            can_read_sys = False
        except Exception:
            can_read_sys = False

    has_snapshot = os.path.exists(snap_db) and os.path.getsize(snap_db) > 10000

    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) FROM icloud_assets").fetchone()[0]
    finally:
        conn.close()

    return {
        "can_read_system_sqlite": can_read_sys,
        "system_sqlite_path": sys_db,
        "has_snapshot": has_snapshot,
        "snapshot_dir": SNAPSHOT_DIR,
        "snapshot_sqlite_path": snap_db,
        "indexed_count": count,
    }


def scan_icloud_sqlite(
    custom_db_path: Optional[str] = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, any]:
    """
    Reads Apple Photos SQLite database in 100% READ-ONLY mode using a temporary copy.
    Never modifies the original database.
    """
    access = check_icloud_access()
    target_db = custom_db_path
    if not target_db:
        if access["can_read_system_sqlite"]:
            target_db = access["system_sqlite_path"]
        elif access["has_snapshot"]:
            target_db = access["snapshot_sqlite_path"]
        else:
            raise PermissionError(
                "macOS bloquea la lectura directa de Photos.sqlite sin 'Acceso total al disco'. "
                "Puedes usar el botón 'Escanear vía App Fotos (Oficial)' o copiar Photos.sqlite a la carpeta snapshot."
            )

    if progress_cb:
        progress_cb(0, 100, "Creando copia temporal de solo lectura de Photos.sqlite...")

    with tempfile.TemporaryDirectory() as tmp_dir:
        base_dir = os.path.dirname(target_db)
        base_name = os.path.basename(target_db)
        tmp_db = os.path.join(tmp_dir, base_name)
        shutil.copy2(target_db, tmp_db)

        for suffix in ["-wal", "-shm"]:
            aux = os.path.join(base_dir, base_name + suffix)
            if os.path.exists(aux):
                try:
                    shutil.copy2(aux, os.path.join(tmp_dir, base_name + suffix))
                except Exception:
                    pass

        if progress_cb:
            progress_cb(15, 100, "Leyendo catálogo de iCloud Photos desde SQLite...")

        ro_uri = f"file:{tmp_db}?mode=ro"
        pconn = sqlite3.connect(ro_uri, uri=True)
        pconn.row_factory = sqlite3.Row
        try:
            # Inspect available table name (ZASSET in modern macOS Photos, ZGENERICASSET in older)
            tables = {
                r[0]
                for r in pconn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            asset_table = "ZASSET" if "ZASSET" in tables else "ZGENERICASSET"

            query = f"""
                SELECT
                    a.ZUUID as uuid,
                    a.ZFILENAME as filename,
                    attr.ZORIGINALFILENAME as original_filename,
                    a.ZDATECREATED as date_created,
                    a.ZKIND as kind_code,
                    attr.ZORIGINALFILESIZE as filesize,
                    a.ZDURATION as duration,
                    a.ZWIDTH as width,
                    a.ZHEIGHT as height
                FROM {asset_table} a
                LEFT JOIN ZADDITIONALASSETATTRIBUTES attr ON attr.ZASSET = a.Z_PK
                WHERE a.ZTRASHEDSTATE = 0
            """
            rows = pconn.execute(query).fetchall()
        finally:
            pconn.close()

    total = len(rows)
    if progress_cb:
        progress_cb(40, 100, f"Procesando {total:,} fotos y vídeos encontrados en iCloud...")

    records = []
    for idx, r in enumerate(rows):
        uuid = r["uuid"] or f"row_{idx}"
        orig_fn = r["original_filename"] or r["filename"] or ""
        fn = r["filename"] or orig_fn
        if not orig_fn and not fn:
            continue

        norm_name, stem, ext = normalize_filename(orig_fn or fn)
        # ZKIND: 0 = photo, 1 = video
        kind_code = r["kind_code"]
        if kind_code == 1:
            kind = "video"
        elif kind_code == 0:
            kind = "photo"
        else:
            kind = classify_media(orig_fn or fn) or "photo"

        raw_date = r["date_created"]
        ts = int(raw_date + APPLE_EPOCH_OFFSET) if raw_date is not None else 0
        ym, year, month = timestamp_to_ym(ts)

        records.append(
            (
                uuid,
                fn,
                orig_fn,
                norm_name,
                stem,
                ext,
                ts,
                ym,
                year,
                month,
                kind,
                int(r["filesize"] or 0),
                float(r["duration"] or 0.0),
                int(r["width"] or 0),
                int(r["height"] or 0),
            )
        )

    if progress_cb:
        progress_cb(75, 100, f"Guardando {len(records):,} registros en índice local...")

    conn = get_connection()
    try:
        conn.execute("DELETE FROM icloud_assets")
        conn.executemany(
            """
            INSERT OR REPLACE INTO icloud_assets (
                uuid, filename, original_filename, norm_name, stem, ext,
                timestamp, year_month, year, month, kind, filesize, duration, width, height
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )
        conn.commit()
    finally:
        conn.close()

    set_setting("last_icloud_scan", str(int(time.time())))
    set_setting("last_icloud_method", "SQLite (Instantáneo)")

    if progress_cb:
        progress_cb(100, 100, f"¡Completado! {len(records):,} elementos indexados de iCloud.")

    return {"status": "ok", "count": len(records), "method": "sqlite"}


def scan_icloud_via_photos_app(
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, any]:
    """
    Uses Apple's official Automation interface (osascript JXA) to query Photos.app directly.
    Works without Full Disk Access by requesting standard macOS Photos permission.
    Reads in bulk arrays for high speed.
    """
    if progress_cb:
        progress_cb(2, 100, "Conectando con la app Fotos de macOS (acepta el permiso si aparece en pantalla)...")

    # First get count and bulk properties using JXA bulk property retrieval (much faster than looping item by item!)
    jxa_script = """
    const Photos = Application('Photos');
    const items = Photos.mediaItems;
    const total = items.length;
    const ids = items.id();
    const filenames = items.filename();
    const dates = items.date();
    const sizes = items.size();
    const widths = items.width();
    const heights = items.height();
    const result = [];
    for (let i = 0; i < total; i++) {
        const d = dates[i];
        result.push([
            ids[i] || ('id_' + i),
            filenames[i] || '',
            d ? Math.floor(d.getTime() / 1000) : 0,
            sizes[i] || 0,
            widths[i] || 0,
            heights[i] || 0
        ]);
    }
    JSON.stringify({total: total, items: result});
    """

    if progress_cb:
        progress_cb(10, 100, "Solicitando lista de fotos y vídeos a la app Fotos de macOS...")

    proc = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", jxa_script],
        capture_output=True,
        text=True,
    )

    if proc.returncode != 0:
        err = proc.stderr.strip()
        raise RuntimeError(
            f"No se pudo leer desde la app Fotos: {err}. "
            "Asegúrate de que la app Fotos esté abierta y hayas aceptado el permiso de automatización."
        )

    data = json.loads(proc.stdout.strip())
    raw_items = data.get("items", [])
    total = len(raw_items)

    if progress_cb:
        progress_cb(60, 100, f"Recibidos {total:,} elementos de Fotos.app. Indexando...")

    records = []
    for row in raw_items:
        if should_stop and should_stop():
            raise InterruptedError("Escaneo cancelado por el usuario.")
        uuid, fn, ts, size, w, h = row
        if not fn:
            continue
        norm_name, stem, ext = normalize_filename(fn)
        kind = classify_media(fn) or "photo"
        ym, year, month = timestamp_to_ym(ts)
        records.append(
            (
                uuid,
                fn,
                fn,
                norm_name,
                stem,
                ext,
                int(ts or 0),
                ym,
                year,
                month,
                kind,
                int(size or 0),
                0.0,
                int(w or 0),
                int(h or 0),
            )
        )

    conn = get_connection()
    try:
        conn.execute("DELETE FROM icloud_assets")
        conn.executemany(
            """
            INSERT OR REPLACE INTO icloud_assets (
                uuid, filename, original_filename, norm_name, stem, ext,
                timestamp, year_month, year, month, kind, filesize, duration, width, height
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )
        conn.commit()
    finally:
        conn.close()

    set_setting("last_icloud_scan", str(int(time.time())))
    set_setting("last_icloud_method", "App Fotos (Automatización Oficial)")

    if progress_cb:
        progress_cb(100, 100, f"¡Completado! {len(records):,} elementos indexados desde la app Fotos.")

    return {"status": "ok", "count": len(records), "method": "photos_app"}
