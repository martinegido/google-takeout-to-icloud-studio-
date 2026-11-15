import datetime
import json
import os
import shutil
import subprocess
import time
import zipfile
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

from PIL import Image

from core.database import BASE_DIR, get_connection

DEFAULT_OUTPUT_ROOT = os.path.join(BASE_DIR, "Lotes_Listos_Para_iCloud")
EXIFTOOL_BIN = shutil.which("exiftool") or "/opt/homebrew/bin/exiftool"
FFMPEG_BIN = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

# Video codecs natively accepted by Apple Photos / iCloud Photos
APPLE_COMPATIBLE_VIDEO_CODECS = {"avc1", "hvc1", "hev1", "mp4v", "apcn", "apch", "apco", "apcs", "ap4h"}


def _has_exiftool() -> bool:
    return os.path.exists(EXIFTOOL_BIN) and os.access(EXIFTOOL_BIN, os.X_OK)


def _has_ffmpeg() -> bool:
    return os.path.exists(FFMPEG_BIN) and os.access(FFMPEG_BIN, os.X_OK)


def _ensure_apple_photos_compatibility(staged_items: List[Dict[str, Any]]) -> None:
    """
    Google Takeout often contains:
    1) MP4/WebM/MKV/AVI videos encoded with Google's VP9 (`vp09`) or AV1 (`av01`) codec,
       which Apple Photos rejects with "Formato desconocido / Error desconocido".
    2) JPEG images saved with a `.PNG` extension (or vice-versa).

    This function inspects the extracted copies with ExifTool and automatically transcodes
    any non-Apple video codec (like `vp09` or `av01`) to lossless-visual H.264 (`avc1`) MP4
    using ffmpeg, and corrects mismatched image extensions before importing into Apple Photos.
    """
    if not staged_items or not _has_exiftool():
        return

    paths = [x["dest_path"] for x in staged_items if os.path.exists(x["dest_path"])]
    if not paths:
        return

    meta_by_path: Dict[str, Dict[str, Any]] = {}
    try:
        proc = subprocess.run(
            [EXIFTOOL_BIN, "-j", "-FileType", "-FileTypeExtension", "-CompressorID"] + paths,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.stdout and proc.stdout.strip().startswith("["):
            for entry in json.loads(proc.stdout):
                sp = os.path.abspath(entry.get("SourceFile", ""))
                meta_by_path[sp] = entry
    except Exception:
        return

    for item in staged_items:
        p = os.path.abspath(item["dest_path"])
        info = meta_by_path.get(p, {})
        real_ext = str(info.get("FileTypeExtension", "")).lower()
        compressor = str(info.get("CompressorID", "")).lower()
        stem, curr_ext = os.path.splitext(p)
        curr_ext_low = curr_ext.lower()

        if item.get("kind") == "video":
            needs_transcode = False
            if curr_ext_low in (".webm", ".mkv", ".avi", ".3gp", ".wmv", ".mts"):
                needs_transcode = True
            elif compressor and compressor not in APPLE_COMPATIBLE_VIDEO_CODECS:
                # e.g., Google VP9 ('vp09') or AV1 ('av01') inside .mp4
                needs_transcode = True

            if needs_transcode and _has_ffmpeg():
                new_mp4 = f"{stem}.mp4"
                tmp_mp4 = f"{stem}.converting.mp4"
                try:
                    subprocess.run(
                        [
                            FFMPEG_BIN,
                            "-y",
                            "-i",
                            p,
                            "-c:v",
                            "libx264",
                            "-preset",
                            "fast",
                            "-crf",
                            "18",
                            "-pix_fmt",
                            "yuv420p",
                            "-c:a",
                            "aac",
                            "-b:a",
                            "160k",
                            "-movflags",
                            "+faststart",
                            tmp_mp4,
                        ],
                        check=True,
                        capture_output=True,
                        timeout=300,
                    )
                    if os.path.exists(tmp_mp4) and os.path.getsize(tmp_mp4) > 0:
                        if p != new_mp4 and os.path.exists(p):
                            os.remove(p)
                        os.replace(tmp_mp4, new_mp4)
                        item["dest_path"] = new_mp4
                except Exception:
                    if os.path.exists(tmp_mp4):
                        os.remove(tmp_mp4)
        else:
            # Photo: fix mismatched file extension (e.g. a JPEG saved as IMG_0426.PNG by Google)
            if real_ext in ("jpg", "jpeg", "png", "heic", "webp", "gif"):
                expected_dot_ext = ".jpg" if real_ext in ("jpg", "jpeg") else f".{real_ext}"
                if curr_ext_low != expected_dot_ext and not (
                    curr_ext_low in (".jpg", ".jpeg") and expected_dot_ext == ".jpg"
                ):
                    corrected_path = f"{stem}{expected_dot_ext}"
                    try:
                        os.replace(p, corrected_path)
                        item["dest_path"] = corrected_path
                    except Exception:
                        pass


def _apply_metadata_batch(staged_items: List[Dict[str, Any]]) -> None:
    """
    Restores original capture dates and GPS coordinates onto the COPIED files in the staging directory.
    """
    exif_args: List[str] = []

    for item in staged_items:
        dest_path = item["dest_path"]
        ts = item.get("timestamp") or 0
        lat = item.get("lat")
        lon = item.get("lon")

        if ts > 0 and os.path.exists(dest_path):
            try:
                os.utime(dest_path, (ts, ts))
            except Exception:
                pass

            if _has_exiftool():
                dt_str = datetime.datetime.fromtimestamp(ts).strftime("%Y:%m:%d %H:%M:%S")
                exif_args.extend(
                    [
                        "-overwrite_original",
                        "-ignoreMinorErrors",
                        f"-AllDates={dt_str}",
                        f"-TrackCreateDate={dt_str}",
                        f"-TrackModifyDate={dt_str}",
                        f"-MediaCreateDate={dt_str}",
                        f"-MediaModifyDate={dt_str}",
                    ]
                )
                if lat is not None and lon is not None:
                    lat_ref = "N" if lat >= 0 else "S"
                    lon_ref = "E" if lon >= 0 else "W"
                    exif_args.extend(
                        [
                            f"-GPSLatitude={abs(lat)}",
                            f"-GPSLatitudeRef={lat_ref}",
                            f"-GPSLongitude={abs(lon)}",
                            f"-GPSLongitudeRef={lon_ref}",
                        ]
                    )
                exif_args.extend([dest_path, "-execute"])

    if exif_args and _has_exiftool():
        arg_input = "\n".join(exif_args) + "\n"
        try:
            subprocess.run(
                [EXIFTOOL_BIN, "-stay_open", "False", "-@", "-"],
                input=arg_input,
                text=True,
                capture_output=True,
                timeout=180,
            )
        except Exception:
            pass

    for item in staged_items:
        dest_path = item["dest_path"]
        ts = item.get("timestamp") or 0
        if ts > 0 and os.path.exists(dest_path):
            try:
                os.utime(dest_path, (ts, ts))
            except Exception:
                pass


def _restart_photos_app() -> None:
    """
    Cleanly closes and restarts Apple Photos to flush macOS AppleEvents sandbox extension
    tokens (preventing error '[12: Cannot allocate memory]' during large batch imports).
    """
    try:
        subprocess.run(["osascript", "-e", 'tell application "Photos" to quit'], capture_output=True, timeout=5)
    except Exception:
        pass
    time.sleep(1.0)
    subprocess.run(["killall", "Photos"], capture_output=True)
    time.sleep(1.5)
    subprocess.run(["open", "-a", "Photos"], capture_output=True)
    time.sleep(2.5)


def _import_files_to_photos_app(file_paths: List[str], album_name: str, retry_count: int = 1) -> int:
    if not file_paths:
        return 0

    posix_list_items = []
    for p in file_paths:
        escaped = p.replace("\\", "\\\\").replace('"', '\\"')
        posix_list_items.append(f'(POSIX file "{escaped}")')
    applescript_list = "{" + ", ".join(posix_list_items) + "}"
    escaped_album = album_name.replace("\\", "\\\\").replace('"', '\\"')

    script = f"""
    tell application "Photos"
        activate
        if not (exists album "{escaped_album}") then
            make new album named "{escaped_album}"
        end if
        set targetAlbum to album "{escaped_album}"
        with timeout of 600 seconds
            set importedItems to import {applescript_list} into targetAlbum skip check duplicates true
            return count of importedItems
        end timeout
    end tell
    """
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    imported_count = 0
    if proc.returncode == 0 and proc.stdout.strip().isdigit():
        imported_count = int(proc.stdout.strip())

    if (proc.returncode != 0 or imported_count == 0) and retry_count > 0:
        # Recycle Photos.app to clear AppleEvents sandbox limits
        _restart_photos_app()
        return _import_files_to_photos_app(file_paths, album_name, retry_count=retry_count - 1)

    if proc.returncode != 0:
        raise RuntimeError(f"Error al importar en la app Fotos: {proc.stderr.strip()}")

    return imported_count


def run_batch(
    mode: str,
    year_month_filter: str = "ALL",
    kind_filter: str = "all",
    batch_limit: int = 200,
    folder_chunk_size: int = 500,
    custom_output_dir: Optional[str] = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    should_pause_or_stop: Optional[Callable[[], str]] = None,
) -> Dict[str, Any]:
    output_root = os.path.expanduser(custom_output_dir.strip()) if custom_output_dir else DEFAULT_OUTPUT_ROOT
    os.makedirs(output_root, exist_ok=True)

    where_clauses = ["status = 'missing'"]
    params: List[Any] = []

    if year_month_filter and year_month_filter != "ALL":
        if year_month_filter.startswith("YEAR:"):
            y_val = int(year_month_filter.split(":", 1)[1])
            where_clauses.append("year = ?")
            params.append(y_val)
        else:
            where_clauses.append("year_month = ?")
            params.append(year_month_filter)

    if kind_filter in ("photo", "video"):
        where_clauses.append("kind = ?")
        params.append(kind_filter)

    where_sql = " AND ".join(where_clauses)
    limit_sql = f"LIMIT {int(batch_limit)}" if batch_limit and int(batch_limit) > 0 else ""

    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT id, source_type, source_path, inner_path, filename,
                   ext, timestamp, year_month, kind, filesize, lat, lon
            FROM takeout_assets
            WHERE {where_sql}
            ORDER BY timestamp ASC, id ASC
            {limit_sql}
            """,
            params,
        ).fetchall()

        if not rows:
            return {"status": "empty", "message": "No hay archivos pendientes que coincidan con este filtro."}

        total = len(rows)
        label_ym = year_month_filter.replace("YEAR:", "Año ") if year_month_filter != "ALL" else "Todos"
        album_name = f"Google Takeout ({label_ym})"

        cur = conn.execute(
            """
            INSERT INTO batches (
                name, mode, filter_year_month, filter_kind, total_items,
                processed_items, success_items, failed_items, status,
                output_dir, album_name, created_at, last_message
            ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, 'running', ?, ?, ?, ?)
            """,
            (
                f"Tanda {label_ym} ({total} arch.)",
                mode,
                year_month_filter,
                kind_filter,
                total,
                output_root,
                album_name,
                time.time(),
                "Iniciando tanda...",
            ),
        )
        batch_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    SUB_CHUNK = 25
    processed = 0
    succeeded = 0
    failed = 0

    for chunk_start in range(0, total, SUB_CHUNK):
        if should_pause_or_stop:
            sig = should_pause_or_stop()
            while sig == "pause":
                _update_batch_row(batch_id, processed, succeeded, failed, "paused", "En pausa por el usuario...")
                if progress_cb:
                    progress_cb(processed, total, "⏸ Tanda en pausa. Pulsa Reanudar cuando quieras continuar.")
                time.sleep(1.0)
                sig = should_pause_or_stop()
            if sig == "stop":
                _update_batch_row(batch_id, processed, succeeded, failed, "cancelled", "Detenida por el usuario.")
                return {
                    "status": "cancelled",
                    "batch_id": batch_id,
                    "processed": processed,
                    "succeeded": succeeded,
                    "failed": failed,
                }

        sub_rows = rows[chunk_start : chunk_start + SUB_CHUNK]
        pct_msg = f"Extrayendo y preparando ({processed + 1}–{min(total, processed + len(sub_rows))} de {total})..."
        _update_batch_row(batch_id, processed, succeeded, failed, "running", pct_msg)
        if progress_cb:
            progress_cb(processed, total, pct_msg)

        by_source: Dict[str, List[Any]] = defaultdict(list)
        for r in sub_rows:
            by_source[r["source_path"]].append(r)

        staged_ok: List[Dict[str, Any]] = []

        for src_path, src_items in by_source.items():
            src_type = src_items[0]["source_type"]
            if src_type == "zip":
                try:
                    with zipfile.ZipFile(src_path, "r") as zf:
                        for item in src_items:
                            dest_path = _compute_dest_path(
                                output_root, mode, item, batch_id, processed + len(staged_ok), folder_chunk_size
                            )
                            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                            try:
                                with zf.open(item["inner_path"]) as zf_in, open(dest_path, "wb") as f_out:
                                    shutil.copyfileobj(zf_in, f_out, length=1024 * 1024)
                                dest_path = _maybe_convert_for_web(mode, dest_path, item["kind"])
                                staged_ok.append(
                                    {
                                        "id": item["id"],
                                        "dest_path": dest_path,
                                        "timestamp": item["timestamp"],
                                        "lat": item["lat"],
                                        "lon": item["lon"],
                                        "year_month": item["year_month"],
                                        "kind": item["kind"],
                                    }
                                )
                            except Exception as e:
                                _mark_asset_error(item["id"], batch_id, str(e))
                                failed += 1
                                processed += 1
                except Exception as e:
                    for item in src_items:
                        _mark_asset_error(item["id"], batch_id, f"Error abriendo ZIP: {e}")
                        failed += 1
                        processed += 1
            else:
                for item in src_items:
                    full_src = os.path.join(src_path, item["inner_path"])
                    dest_path = _compute_dest_path(
                        output_root, mode, item, batch_id, processed + len(staged_ok), folder_chunk_size
                    )
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    try:
                        shutil.copy2(full_src, dest_path)
                        dest_path = _maybe_convert_for_web(mode, dest_path, item["kind"])
                        staged_ok.append(
                            {
                                "id": item["id"],
                                "dest_path": dest_path,
                                "timestamp": item["timestamp"],
                                "lat": item["lat"],
                                "lon": item["lon"],
                                "year_month": item["year_month"],
                                "kind": item["kind"],
                            }
                        )
                    except Exception as e:
                        _mark_asset_error(item["id"], batch_id, str(e))
                        failed += 1
                        processed += 1

        # 1. Auto-convert any VP9/AV1 video to H.264 MP4 & fix mismatched extensions so Apple Photos never fails!
        if staged_ok:
            _ensure_apple_photos_compatibility(staged_ok)

        # 2. Restore EXIF / QuickTime creation dates on the extracted copies
        if staged_ok:
            _apply_metadata_batch(staged_ok)

        # Proactively restart Photos every 250 items to keep sandbox tokens fresh
        if mode == "import_photos_app" and processed > 0 and processed % 250 == 0:
            if progress_cb:
                progress_cb(processed, total, "Optimizando memoria y refrescando app Fotos...")
            _restart_photos_app()

        # 3. If mode is 'import_photos_app', send this sub-chunk to macOS Photos.app
        if mode == "import_photos_app" and staged_ok:
            import_msg = f"Importando {len(staged_ok)} archivos en la app Fotos de macOS (Álbum: {album_name})..."
            if progress_cb:
                progress_cb(processed, total, import_msg)
            try:
                imported_num = _import_files_to_photos_app([x["dest_path"] for x in staged_ok], album_name)
                if imported_num == 0 and len(staged_ok) > 0:
                    raise RuntimeError("Fotos.app no devolvió elementos importados.")
                new_status = "imported"
            except Exception as e:
                for x in staged_ok:
                    _mark_asset_error(x["id"], batch_id, str(e))
                    failed += 1
                    processed += 1
                staged_ok = []
                new_status = "error"
        else:
            new_status = "exported"

        if staged_ok:
            now = time.time()
            conn = get_connection()
            try:
                conn.executemany(
                    """
                    UPDATE takeout_assets
                    SET status = ?, exported_path = ?, batch_id = ?, error_msg = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    [(new_status, x["dest_path"], batch_id, now, x["id"]) for x in staged_ok],
                )
                conn.commit()
            finally:
                conn.close()
            succeeded += len(staged_ok)
            processed += len(staged_ok)

        _update_batch_row(
            batch_id,
            processed,
            succeeded,
            failed,
            "running",
            f"Procesados {processed}/{total} ({succeeded} correctos, {failed} errores)",
        )

    final_msg = f"¡Tanda completada! {succeeded:,} archivos procesados correctamente."
    _update_batch_row(batch_id, processed, succeeded, failed, "completed", final_msg, finished=True)
    if progress_cb:
        progress_cb(total, total, final_msg)

    return {
        "status": "completed",
        "batch_id": batch_id,
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "output_dir": output_root,
    }


def _compute_dest_path(
    output_root: str,
    mode: str,
    item: Any,
    batch_id: int,
    seq_idx: int,
    folder_chunk_size: int,
) -> str:
    ym = os.path.basename(item["year_month"] or "Sin-Fecha")
    fn = os.path.basename(item["filename"] or "archivo")
    stem, ext = os.path.splitext(fn)
    safe_fn = f"{stem}_{item['id']}{ext}"

    if mode == "export_web_jpg":
        if item["kind"] == "video":
            sub = os.path.join(output_root, f"Tanda_{batch_id:03d}_Web", "Videos_Subir_Con_App_Fotos", ym)
        else:
            lote_num = (seq_idx // max(1, min(folder_chunk_size, 950))) + 1
            sub = os.path.join(output_root, f"Tanda_{batch_id:03d}_Web", f"Web_JPG_Lote_{lote_num:02d}_(Max500)")
    elif mode == "import_photos_app":
        sub = os.path.join(output_root, f"Importados_App_Fotos_Tanda_{batch_id:03d}", ym)
    else:
        lote_num = (seq_idx // max(1, folder_chunk_size)) + 1
        sub = os.path.join(output_root, f"{ym}_Tanda_{batch_id:03d}_Lote_{lote_num:02d}")

    dest = os.path.abspath(os.path.join(sub, safe_fn))
    abs_root = os.path.abspath(output_root)
    if os.path.commonpath([abs_root, dest]) != abs_root:
        raise ValueError("Ruta de destino fuera del directorio permitido (Zip Slip bloqueado).")
    return dest


def _maybe_convert_for_web(mode: str, dest_path: str, kind: str) -> str:
    if mode != "export_web_jpg" or kind != "photo":
        return dest_path
    stem, ext = os.path.splitext(dest_path)
    if ext.lower() in (".jpg", ".jpeg"):
        return dest_path
    try:
        with Image.open(dest_path) as img:
            rgb = img.convert("RGB")
            jpg_path = f"{stem}.jpg"
            rgb.save(jpg_path, "JPEG", quality=95)
        os.remove(dest_path)
        return jpg_path
    except Exception:
        return dest_path


def _mark_asset_error(asset_id: int, batch_id: int, err: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE takeout_assets SET status='error', batch_id=?, error_msg=?, updated_at=? WHERE id=?",
            (batch_id, err[:500], time.time(), asset_id),
        )
        conn.commit()
    finally:
        conn.close()


def _update_batch_row(
    batch_id: int,
    processed: int,
    succeeded: int,
    failed: int,
    status: str,
    msg: str,
    finished: bool = False,
) -> None:
    conn = get_connection()
    try:
        if finished:
            conn.execute(
                """
                UPDATE batches
                SET processed_items=?, success_items=?, failed_items=?, status=?, last_message=?, Finished_at=?
                WHERE id=?
                """,
                (processed, succeeded, failed, status, msg, time.time(), batch_id),
            )
        else:
            conn.execute(
                """
                UPDATE batches
                SET processed_items=?, success_items=?, failed_items=?, status=?, last_message=?
                WHERE id=?
                """,
                (processed, succeeded, failed, status, msg, batch_id),
            )
        conn.commit()
    finally:
        conn.close()
