import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "sync_state.db")

_db_lock = threading.RLock()


def get_connection() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    with _db_lock:
        conn = get_connection()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS icloud_assets (
                    uuid TEXT PRIMARY KEY,
                    filename TEXT,
                    original_filename TEXT,
                    norm_name TEXT,
                    stem TEXT,
                    ext TEXT,
                    timestamp INTEGER,
                    year_month TEXT,
                    year INTEGER,
                    month INTEGER,
                    kind TEXT, -- 'photo' or 'video'
                    filesize INTEGER DEFAULT 0,
                    duration REAL DEFAULT 0,
                    width INTEGER DEFAULT 0,
                    height INTEGER DEFAULT 0,
                    lat REAL,
                    lon REAL,
                    gps_inferred INTEGER DEFAULT 0,
                    gps_source TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_icloud_norm_name ON icloud_assets(norm_name);
                CREATE INDEX IF NOT EXISTS idx_icloud_stem ON icloud_assets(stem);
                CREATE INDEX IF NOT EXISTS idx_icloud_timestamp ON icloud_assets(timestamp);
                CREATE INDEX IF NOT EXISTS idx_icloud_ym ON icloud_assets(year_month);

                CREATE TABLE IF NOT EXISTS takeout_sources (
                    source_path TEXT PRIMARY KEY,
                    source_type TEXT, -- 'zip' or 'folder'
                    filesize INTEGER DEFAULT 0,
                    mtime REAL DEFAULT 0,
                    scanned_at REAL DEFAULT 0,
                    item_count INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS takeout_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    unique_key TEXT UNIQUE, -- source_path + '::' + inner_path
                    source_type TEXT, -- 'zip' or 'folder'
                    source_path TEXT,
                    inner_path TEXT,
                    filename TEXT,
                    norm_name TEXT,
                    stem TEXT,
                    ext TEXT,
                    timestamp INTEGER,
                    date_source TEXT, -- 'json', 'exif_or_zip', 'filename'
                    year_month TEXT,
                    year INTEGER,
                    month INTEGER,
                    kind TEXT, -- 'photo' or 'video'
                    filesize INTEGER DEFAULT 0,
                    json_found INTEGER DEFAULT 0,
                    lat REAL,
                    lon REAL,
                    description TEXT,
                    status TEXT DEFAULT 'pending', -- 'pending', 'matched', 'missing', 'exported', 'imported', 'ignored', 'error'
                    match_reason TEXT,
                    matched_icloud_uuid TEXT,
                    exported_path TEXT,
                    batch_id INTEGER,
                    error_msg TEXT,
                    updated_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_takeout_status ON takeout_assets(status);
                CREATE INDEX IF NOT EXISTS idx_takeout_ym ON takeout_assets(year_month);
                CREATE INDEX IF NOT EXISTS idx_takeout_norm_name ON takeout_assets(norm_name);
                CREATE INDEX IF NOT EXISTS idx_takeout_stem ON takeout_assets(stem);
                CREATE INDEX IF NOT EXISTS idx_takeout_kind ON takeout_assets(kind);

                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    mode TEXT, -- 'import_photos_app' or 'export_folders'
                    filter_year_month TEXT,
                    filter_kind TEXT,
                    total_items INTEGER DEFAULT 0,
                    processed_items INTEGER DEFAULT 0,
                    success_items INTEGER DEFAULT 0,
                    failed_items INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'running', -- 'running', 'paused', 'completed', 'cancelled', 'error'
                    output_dir TEXT,
                    album_name TEXT,
                    created_at REAL,
                    Finished_at REAL,
                    last_message TEXT
                );
                """
            )
            conn.commit()

            # Migrations for existing tables
            try:
                ic_cols = [r[1] for r in conn.execute("PRAGMA table_info(icloud_assets)").fetchall()]
                if "lat" not in ic_cols:
                    conn.execute("ALTER TABLE icloud_assets ADD COLUMN lat REAL")
                if "lon" not in ic_cols:
                    conn.execute("ALTER TABLE icloud_assets ADD COLUMN lon REAL")
                if "gps_inferred" not in ic_cols:
                    conn.execute("ALTER TABLE icloud_assets ADD COLUMN gps_inferred INTEGER DEFAULT 0")
                if "gps_source" not in ic_cols:
                    conn.execute("ALTER TABLE icloud_assets ADD COLUMN gps_source TEXT")

                to_cols = [r[1] for r in conn.execute("PRAGMA table_info(takeout_assets)").fetchall()]
                if "gps_inferred" not in to_cols:
                    conn.execute("ALTER TABLE takeout_assets ADD COLUMN gps_inferred INTEGER DEFAULT 0")
                if "gps_source" not in to_cols:
                    conn.execute("ALTER TABLE takeout_assets ADD COLUMN gps_source TEXT")
                conn.commit()
            except Exception:
                pass
        finally:
            conn.close()


def get_setting(key: str, default: str = "") -> str:
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default
        finally:
            conn.close()


def set_setting(key: str, value: str) -> None:
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.commit()

            # Migrations for existing tables
            try:
                ic_cols = [r[1] for r in conn.execute("PRAGMA table_info(icloud_assets)").fetchall()]
                if "lat" not in ic_cols:
                    conn.execute("ALTER TABLE icloud_assets ADD COLUMN lat REAL")
                if "lon" not in ic_cols:
                    conn.execute("ALTER TABLE icloud_assets ADD COLUMN lon REAL")
                if "gps_inferred" not in ic_cols:
                    conn.execute("ALTER TABLE icloud_assets ADD COLUMN gps_inferred INTEGER DEFAULT 0")
                if "gps_source" not in ic_cols:
                    conn.execute("ALTER TABLE icloud_assets ADD COLUMN gps_source TEXT")

                to_cols = [r[1] for r in conn.execute("PRAGMA table_info(takeout_assets)").fetchall()]
                if "gps_inferred" not in to_cols:
                    conn.execute("ALTER TABLE takeout_assets ADD COLUMN gps_inferred INTEGER DEFAULT 0")
                if "gps_source" not in to_cols:
                    conn.execute("ALTER TABLE takeout_assets ADD COLUMN gps_source TEXT")
                conn.commit()
            except Exception:
                pass
        finally:
            conn.close()
