import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.database import get_connection, set_setting
from core.utils import (
    EDITED_SUFFIX_RE,
    classify_media,
    guess_timestamp_from_filename_or_path,
    normalize_filename,
    timestamp_to_ym,
)

EXIFTOOL_BIN = shutil.which("exiftool") or "/opt/homebrew/bin/exiftool"

JSON_SUFFIX_RE = re.compile(
    r"(\.supplemental-metadata|\.[a-z\-]*suppl[a-z\-]*|\.metadata)?\.json$",
    re.IGNORECASE,
)


def _parse_exif_date_str(val: str) -> int:
    if not val or not isinstance(val, str):
        return 0
    val = val.strip()
    if val.startswith("0000"):
        return 0
    clean = re.sub(r"([+-]\d{2}:?\d{2}|Z)$", "", val).strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            dt = datetime.datetime.strptime(clean[:19], fmt)
            if 1971 <= dt.year <= 2035:
                return int(dt.timestamp())
        except Exception:
            pass
    return 0


def _extract_exif_dates_from_files(
    file_paths: List[str],
) -> Dict[str, Tuple[int, Optional[float], Optional[float]]]:
    """
    Runs exiftool in batch mode over a list of disk files to get their true
    DateTimeOriginal / CreateDate / MediaCreateDate and GPS coordinates.
    """
    if not file_paths or not os.path.exists(EXIFTOOL_BIN):
        return {}

    result: Dict[str, Tuple[int, Optional[float], Optional[float]]] = {}
    arg_input = (
        "\n".join(
            [
                "-j",
                "-n",
                "-ignoreMinorErrors",
                "-DateTimeOriginal",
                "-CreateDate",
                "-MediaCreateDate",
                "-TrackCreateDate",
                "-GPSLatitude",
                "-GPSLongitude",
            ]
            + file_paths
        )
        + "\n"
    )
    try:
        proc = subprocess.run(
            [EXIFTOOL_BIN, "-@", "-"],
            input=arg_input,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.stdout and proc.stdout.strip().startswith("["):
            items = json.loads(proc.stdout)
            for item in items:
                src_f = os.path.abspath(item.get("SourceFile", ""))
                ts = 0
                for tag in (
                    "DateTimeOriginal",
                    "CreateDate",
                    "MediaCreateDate",
                    "TrackCreateDate",
                ):
                    raw_v = item.get(tag)
                    if raw_v:
                        ts = _parse_exif_date_str(str(raw_v))
                        if ts > 0:
                            break
                lat = item.get("GPSLatitude")
                lon = item.get("GPSLongitude")
                try:
                    lat_f = float(lat) if lat is not None else None
                    lon_f = float(lon) if lon is not None else None
                except Exception:
                    lat_f, lon_f = None, None
                if ts > 0:
                    result[src_f] = (ts, lat_f, lon_f)
    except Exception:
        pass

    return result


def _extract_exif_dates_from_zip_members(
    zf: zipfile.ZipFile, member_names: List[str]
) -> Dict[str, Tuple[int, Optional[float], Optional[float]]]:
    if not member_names or not os.path.exists(EXIFTOOL_BIN):
        return {}

    result: Dict[str, Tuple[int, Optional[float], Optional[float]]] = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        extracted_map: Dict[str, str] = {}
        tmp_files: List[str] = []
        for idx, m_name in enumerate(member_names):
            ext = os.path.splitext(m_name)[1].lower()
            safe_tmp = os.path.abspath(os.path.join(tmp_dir, f"f_{idx}{ext}"))
            try:
                with zf.open(m_name) as src, open(safe_tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                extracted_map[safe_tmp] = m_name
                tmp_files.append(safe_tmp)
            except Exception:
                pass

        if not tmp_files:
            return {}

        exif_by_tmp = _extract_exif_dates_from_files(tmp_files)
        for tmp_p, val in exif_by_tmp.items():
            orig_m = extracted_map.get(tmp_p)
            if orig_m:
                result[orig_m] = val

    return result


def _parse_google_json_bytes(raw_bytes: bytes) -> Dict[str, Any]:
    try:
        data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except Exception:
        return {}

    ts = 0
    ptt = data.get("photoTakenTime", {})
    if isinstance(ptt, dict) and ptt.get("timestamp"):
        try:
            ts = int(ptt["timestamp"])
        except Exception:
            ts = 0

    if not ts:
        ct = data.get("creationTime", {})
        if isinstance(ct, dict) and ct.get("timestamp"):
            try:
                ts = int(ct["timestamp"])
            except Exception:
                ts = 0

    lat, lon = None, None
    geo = data.get("geoData", {}) or data.get("geoDataExif", {})
    if isinstance(geo, dict):
        try:
            la = float(geo.get("latitude", 0.0))
            lo = float(geo.get("longitude", 0.0))
            if la != 0.0 or lo != 0.0:
                lat, lon = la, lo
        except Exception:
            pass

    return {
        "timestamp": ts,
        "title": data.get("title", ""),
        "description": data.get("description", ""),
        "lat": lat,
        "lon": lon,
    }


def _build_json_lookup(json_paths: List[str]) -> Tuple[Dict[str, str], Dict[Tuple[str, str], str]]:
    exact_map: Dict[str, str] = {}
    prefix_map: Dict[Tuple[str, str], str] = {}

    for jp in json_paths:
        jp_lower = jp.lower()
        exact_map[jp_lower] = jp

        stripped = JSON_SUFFIX_RE.sub("", jp_lower)
        exact_map[stripped] = jp

        dir_name = os.path.dirname(stripped)
        base_name = os.path.basename(stripped)
        m_dup = re.match(r"^(.+?)(\.[a-z0-9]+)(\(\d+\))$", base_name)
        if m_dup:
            reordered = f"{m_dup.group(1)}{m_dup.group(3)}{m_dup.group(2)}"
            full_reordered = f"{dir_name}/{reordered}" if dir_name else reordered
            exact_map[full_reordered] = jp

        if len(base_name) >= 40:
            prefix_map[(dir_name, base_name[:42])] = jp

    return exact_map, prefix_map


def _find_matching_json(
    media_rel_path: str,
    exact_map: Dict[str, str],
    prefix_map: Dict[Tuple[str, str], str],
) -> Optional[str]:
    p_lower = media_rel_path.lower()
    dir_name = os.path.dirname(p_lower)
    base_name = os.path.basename(p_lower)
    stem, ext = os.path.splitext(base_name)

    if p_lower in exact_map:
        return exact_map[p_lower]

    no_ext = f"{dir_name}/{stem}" if dir_name else stem
    if no_ext in exact_map:
        return exact_map[no_ext]

    clean_stem = EDITED_SUFFIX_RE.sub("", stem).strip()
    if clean_stem != stem:
        cand_clean = f"{dir_name}/{clean_stem}{ext}" if dir_name else f"{clean_stem}{ext}"
        if cand_clean in exact_map:
            return exact_map[cand_clean]
        cand_clean_no_ext = f"{dir_name}/{clean_stem}" if dir_name else clean_stem
        if cand_clean_no_ext in exact_map:
            return exact_map[cand_clean_no_ext]

    for alt_ext in (".heic", ".jpg", ".jpeg", ".png"):
        cand_alt = f"{dir_name}/{clean_stem}{alt_ext}" if dir_name else f"{clean_stem}{alt_ext}"
        if cand_alt in exact_map:
            return exact_map[cand_alt]

    if len(base_name) >= 42:
        pref_key = (dir_name, base_name[:42])
        if pref_key in prefix_map:
            return prefix_map[pref_key]

    return None


def _is_google_takeout_zip(zip_path: str) -> bool:
    bname = os.path.basename(zip_path).lower()
    if bname.startswith("takeout"):
        return True
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist()[:30]:
                nl = name.lower()
                if nl.startswith("takeout/") or "google fotos/" in nl or "google photos/" in nl:
                    return True
    except Exception:
        return False
    return False


def scan_takeout_zips(
    target_path: str,
    force_rescan: bool = False,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    APARTADO 2A: Escanea un archivo .ZIP concreto o todos los archivos .ZIP de Google Takeout
    dentro de una carpeta (en el Mac o en el NAS), sin descomprimirlos en disco.
    """
    target_path = os.path.expanduser(target_path.strip())
    if not os.path.exists(target_path):
        raise FileNotFoundError(f"La ruta indicada no existe: {target_path}")

    set_setting("takeout_root_path", target_path)

    zip_files: List[str] = []
    if os.path.isfile(target_path) and target_path.lower().endswith(".zip"):
        zip_files.append(target_path)
    else:
        for dirpath, dirnames, filenames in os.walk(target_path):
            dirnames[:] = [
                d
                for d in dirnames
                if not d.startswith(".")
                and d not in ("__MACOSX", "@eaDir", "#recycle", "Lotes_Listos_Para_iCloud")
            ]
            for fn in filenames:
                if fn.startswith(".") or fn.startswith("._"):
                    continue
                if fn.lower().endswith(".zip"):
                    full_p = os.path.join(dirpath, fn)
                    if _is_google_takeout_zip(full_p):
                        zip_files.append(full_p)

    zip_files.sort()
    if not zip_files:
        raise ValueError(
            "No se encontraron archivos .zip de Google Takeout en esa ruta. "
            "Si quieres añadir una carpeta con fotos ya descomprimidas, usa el Apartado 2B (Carpetas sin comprimir)."
        )

    conn = get_connection()
    try:
        existing_sources = {
            r["source_path"]: r
            for r in conn.execute("SELECT * FROM takeout_sources WHERE source_type='zip'").fetchall()
        }
    finally:
        conn.close()

    total_new = 0
    skipped = 0
    for idx, zp in enumerate(zip_files):
        if should_stop and should_stop():
            raise InterruptedError("Escaneo detenido por el usuario.")

        pct = int(5 + (idx / max(1, len(zip_files))) * 90)
        zip_name = os.path.basename(zp)
        st = os.stat(zp)

        if (
            not force_rescan
            and zp in existing_sources
            and existing_sources[zp]["filesize"] == st.st_size
            and abs(existing_sources[zp]["mtime"] - st.st_mtime) < 2.0
        ):
            skipped += 1
            if progress_cb:
                progress_cb(pct, 100, f"[{idx + 1}/{len(zip_files)}] {zip_name} ya estaba indexado.")
            continue

        if progress_cb:
            progress_cb(
                pct,
                100,
                f"[{idx + 1}/{len(zip_files)}] Analizando ZIP: {zip_name}...",
            )

        records = _scan_single_zip(zp, progress_cb, pct, should_stop)
        _save_source_records(zp, "zip", st.st_size, st.st_mtime, records)
        total_new += len(records)

    set_setting("last_takeout_scan", str(int(time.time())))
    if progress_cb:
        progress_cb(100, 100, f"¡Listo! {len(zip_files)} archivo(s) ZIP procesados.")

    return {
        "status": "ok",
        "zips_found": len(zip_files),
        "zips_skipped": skipped,
        "newly_indexed": total_new,
    }


def scan_uncompressed_folder(
    folder_path: str,
    include_subfolders: bool = True,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    APARTADO 2B: Escanea cualquier carpeta con fotos y vídeos descomprimidos.
    Ignora archivos .zip dentro de ella y obtiene la fecha real de captura vía:
    1) .json de Google Takeout (si existe en la carpeta)
    2) Metadatos EXIF / QuickTime reales incrustados en cada foto o vídeo (vía ExifTool)
    3) Fecha en el nombre del archivo (ej. IMG_20190512_143000.jpg)
    """
    folder_path = os.path.abspath(os.path.expanduser(folder_path.strip()))
    if not os.path.isdir(folder_path):
        raise NotADirectoryError(f"La carpeta indicada no existe: {folder_path}")

    set_setting("last_custom_folder_path", folder_path)

    if progress_cb:
        progress_cb(5, 100, f"Buscando fotos y vídeos descomprimidos en {os.path.basename(folder_path)}...")

    media_files: List[str] = []
    json_files: List[str] = []

    if include_subfolders:
        for dirpath, dirnames, filenames in os.walk(folder_path):
            dirnames[:] = [
                d
                for d in dirnames
                if not d.startswith(".")
                and d not in ("__MACOSX", "@eaDir", "#recycle", "Lotes_Listos_Para_iCloud", "icloud_snapshot")
            ]
            for fn in filenames:
                if fn.startswith(".") or fn.startswith("._"):
                    continue
                full_p = os.path.join(dirpath, fn)
                if fn.lower().endswith(".json"):
                    json_files.append(full_p)
                elif classify_media(fn) is not None:
                    media_files.append(full_p)
    else:
        for fn in os.listdir(folder_path):
            if fn.startswith(".") or fn.startswith("._"):
                continue
            full_p = os.path.join(folder_path, fn)
            if os.path.isfile(full_p):
                if fn.lower().endswith(".json"):
                    json_files.append(full_p)
                elif classify_media(fn) is not None:
                    media_files.append(full_p)

    media_files.sort()
    if not media_files:
        raise ValueError("No se encontraron fotos ni vídeos descomprimidos en esa carpeta.")

    if progress_cb:
        progress_cb(
            15,
            100,
            f"Encontrados {len(media_files):,} archivos en '{os.path.basename(folder_path)}'. Leyendo fechas reales EXIF/JSON...",
        )

    records = _scan_loose_files(folder_path, media_files, json_files, progress_cb, should_stop)
    _save_source_records(folder_path, "folder", 0, time.time(), records)
    set_setting("last_takeout_scan", str(int(time.time())))

    if progress_cb:
        progress_cb(
            100,
            100,
            f"¡Carpeta '{os.path.basename(folder_path)}' añadida con {len(records):,} archivos!",
        )

    return {
        "status": "ok",
        "folder": folder_path,
        "indexed": len(records),
    }


def remove_source(source_path: str) -> Dict[str, Any]:
    """
    Elimina un ZIP o Carpeta del índice local (sin tocar ni borrar ningún archivo de tu disco).
    """
    conn = get_connection()
    try:
        conn.execute("DELETE FROM takeout_assets WHERE source_path = ?", (source_path,))
        conn.execute("DELETE FROM takeout_sources WHERE source_path = ?", (source_path,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "removed": source_path}


def list_indexed_sources() -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                s.source_path,
                s.source_type,
                s.filesize,
                s.scanned_at,
                COUNT(a.id) as total_items,
                SUM(CASE WHEN a.status = 'matched' THEN 1 ELSE 0 END) as matched_items,
                SUM(CASE WHEN a.status = 'missing' THEN 1 ELSE 0 END) as missing_items,
                SUM(CASE WHEN a.status IN ('imported', 'exported') THEN 1 ELSE 0 END) as done_items
            FROM takeout_sources s
            LEFT JOIN takeout_assets a ON a.source_path = s.source_path
            GROUP BY s.source_path
            ORDER BY s.source_type DESC, s.scanned_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# Keep backwards-compatible alias
def scan_takeout_directory(
    root_path: str,
    force_rescan: bool = False,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    return scan_takeout_zips(root_path, force_rescan, progress_cb, should_stop)


def _scan_single_zip(
    zip_path: str,
    progress_cb: Optional[Callable[[int, int, str], None]],
    base_pct: int,
    should_stop: Optional[Callable[[], bool]],
) -> List[Tuple]:
    records = []
    zip_name = os.path.basename(zip_path)
    now = time.time()

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = zf.infolist()
            json_members: List[str] = []
            media_infos: List[zipfile.ZipInfo] = []

            for info in infos:
                if info.is_dir():
                    continue
                fn = os.path.basename(info.filename)
                if not fn or fn.startswith(".") or fn.startswith("._") or "__MACOSX" in info.filename:
                    continue
                if fn.lower().endswith(".json"):
                    json_members.append(info.filename)
                elif classify_media(fn) is not None:
                    media_infos.append(info)

            exact_map, prefix_map = _build_json_lookup(json_members)

            needs_exif_members: List[str] = []
            json_matched_map: Dict[str, Optional[str]] = {}
            for m_info in media_infos:
                jm = _find_matching_json(m_info.filename, exact_map, prefix_map)
                json_matched_map[m_info.filename] = jm
                if not jm:
                    needs_exif_members.append(m_info.filename)

            exif_dates_map: Dict[str, Tuple[int, Optional[float], Optional[float]]] = {}
            if needs_exif_members:
                CHUNK = 100
                for c_start in range(0, len(needs_exif_members), CHUNK):
                    if should_stop and should_stop():
                        raise InterruptedError("Escaneo pausado por el usuario.")
                    sub_members = needs_exif_members[c_start : c_start + CHUNK]
                    if progress_cb:
                        progress_cb(
                            base_pct,
                            100,
                            f"Leyendo fecha EXIF real en {zip_name} ({c_start + 1}/{len(needs_exif_members)})...",
                        )
                    exif_dates_map.update(_extract_exif_dates_from_zip_members(zf, sub_members))

            for m_idx, m_info in enumerate(media_infos):
                if m_idx % 250 == 0 and should_stop and should_stop():
                    raise InterruptedError("Escaneo pausado por el usuario.")

                inner_path = m_info.filename
                fn = os.path.basename(inner_path)
                kind = classify_media(fn) or "photo"
                norm_name, stem, ext = normalize_filename(fn)

                json_member = json_matched_map.get(inner_path)
                ts = 0
                date_source = "unknown"
                lat, lon = None, None
                desc = ""
                json_found = 0

                if json_member:
                    try:
                        meta = _parse_google_json_bytes(zf.read(json_member))
                        if meta.get("timestamp", 0) > 0:
                            ts = meta["timestamp"]
                            date_source = "json"
                            json_found = 1
                        lat = meta.get("lat")
                        lon = meta.get("lon")
                        desc = meta.get("description") or ""
                    except Exception:
                        pass

                if not ts and inner_path in exif_dates_map:
                    ex_ts, ex_lat, ex_lon = exif_dates_map[inner_path]
                    if ex_ts > 0:
                        ts = ex_ts
                        date_source = "exif"
                        if lat is None and ex_lat is not None:
                            lat, lon = ex_lat, ex_lon

                if not ts:
                    guessed = guess_timestamp_from_filename_or_path(fn, inner_path)
                    if guessed:
                        ts = guessed
                        date_source = "carpeta/nombre"

                if not ts:
                    try:
                        dt = datetime.datetime(*m_info.date_time)
                        if dt.year >= 1995:
                            ts = int(dt.timestamp())
                            date_source = "zip_date"
                    except Exception:
                        ts = 0

                ym, year, month = timestamp_to_ym(ts)
                unique_key = f"{zip_path}::{inner_path}"

                records.append(
                    (
                        unique_key,
                        "zip",
                        zip_path,
                        inner_path,
                        fn,
                        norm_name,
                        stem,
                        ext,
                        ts,
                        date_source,
                        ym,
                        year,
                        month,
                        kind,
                        int(m_info.file_size or 0),
                        json_found,
                        lat,
                        lon,
                        desc,
                        now,
                    )
                )
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"El archivo ZIP parece corrupto o incompleto ({zip_name}): {e}")

    return records


def _scan_loose_files(
    root_path: str,
    media_files: List[str],
    json_files: List[str],
    progress_cb: Optional[Callable[[int, int, str], None]],
    should_stop: Optional[Callable[[], bool]],
) -> List[Tuple]:
    rel_jsons = [os.path.relpath(jp, root_path) for jp in json_files]
    exact_map, prefix_map = _build_json_lookup(rel_jsons)
    now = time.time()
    records = []

    # First check which files have JSON and which need ExifTool batch read
    needs_exif_files: List[str] = []
    json_rel_by_full: Dict[str, Optional[str]] = {}
    for full_p in media_files:
        rel_p = os.path.relpath(full_p, root_path)
        jr = _find_matching_json(rel_p, exact_map, prefix_map)
        json_rel_by_full[full_p] = jr
        if not jr:
            needs_exif_files.append(os.path.abspath(full_p))

    exif_map: Dict[str, Tuple[int, Optional[float], Optional[float]]] = {}
    if needs_exif_files:
        CHUNK = 150
        for c_start in range(0, len(needs_exif_files), CHUNK):
            if should_stop and should_stop():
                raise InterruptedError("Escaneo pausado por el usuario.")
            sub_f = needs_exif_files[c_start : c_start + CHUNK]
            if progress_cb:
                pct = int(20 + (c_start / max(1, len(needs_exif_files))) * 60)
                progress_cb(
                    pct,
                    100,
                    f"Leyendo fecha real de cámara (EXIF) en carpeta ({c_start + 1}/{len(needs_exif_files)})...",
                )
            exif_map.update(_extract_exif_dates_from_files(sub_f))

    total_m = len(media_files)
    for idx, full_p in enumerate(media_files):
        if idx % 250 == 0 and should_stop and should_stop():
            raise InterruptedError("Escaneo pausado por el usuario.")

        abs_p = os.path.abspath(full_p)
        rel_p = os.path.relpath(full_p, root_path)
        fn = os.path.basename(full_p)
        kind = classify_media(fn) or "photo"
        norm_name, stem, ext = normalize_filename(fn)

        json_rel = json_rel_by_full.get(full_p)
        ts = 0
        date_source = "unknown"
        lat, lon = None, None
        desc = ""
        json_found = 0

        if json_rel:
            json_full = os.path.join(root_path, json_rel)
            try:
                with open(json_full, "rb") as jf:
                    meta = _parse_google_json_bytes(jf.read())
                if meta.get("timestamp", 0) > 0:
                    ts = meta["timestamp"]
                    date_source = "json"
                    json_found = 1
                lat = meta.get("lat")
                lon = meta.get("lon")
                desc = meta.get("description") or ""
            except Exception:
                pass

        # Fallback 1: Real camera EXIF / QuickTime date!
        if not ts and abs_p in exif_map:
            ex_ts, ex_lat, ex_lon = exif_map[abs_p]
            if ex_ts > 0:
                ts = ex_ts
                date_source = "exif"
                if lat is None and ex_lat is not None:
                    lat, lon = ex_lat, ex_lon

        # Fallback 2: Filename / Folder name pattern
        if not ts:
            guessed = guess_timestamp_from_filename_or_path(fn, rel_p)
            if guessed:
                ts = guessed
                date_source = "carpeta/nombre"

        try:
            st = os.stat(full_p)
            fsize = st.st_size
            # Use creation time (st_birthtime on macOS) or mtime only as last resort
            if not ts:
                btime = getattr(st, "st_birthtime", 0) or st.st_mtime
                if btime > 0:
                    ts = int(btime)
                    date_source = "archivo_mac"
        except Exception:
            fsize = 0

        ym, year, month = timestamp_to_ym(ts)
        unique_key = f"{root_path}::{rel_p}"

        records.append(
            (
                unique_key,
                "folder",
                root_path,
                rel_p,
                fn,
                norm_name,
                stem,
                ext,
                ts,
                date_source,
                ym,
                year,
                month,
                kind,
                fsize,
                json_found,
                lat,
                lon,
                desc,
                now,
            )
        )

    return records


def _save_source_records(
    source_path: str,
    source_type: str,
    filesize: int,
    mtime: float,
    records: List[Tuple],
) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM takeout_assets WHERE source_path = ? AND status NOT IN ('imported', 'exported')",
            (source_path,),
        )
        conn.executemany(
            """
            INSERT INTO takeout_assets (
                unique_key, source_type, source_path, inner_path, filename,
                norm_name, stem, ext, timestamp, date_source, year_month, year, month,
                kind, filesize, json_found, lat, lon, description, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(unique_key) DO UPDATE SET
                timestamp=excluded.timestamp,
                date_source=excluded.date_source,
                year_month=excluded.year_month,
                year=excluded.year,
                month=excluded.month,
                filesize=excluded.filesize,
                json_found=excluded.json_found,
                lat=excluded.lat,
                lon=excluded.lon,
                description=excluded.description,
                updated_at=excluded.updated_at
            """,
            records,
        )
        conn.execute(
            """
            INSERT INTO takeout_sources (source_path, source_type, filesize, mtime, scanned_at, item_count)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_path) DO UPDATE SET
                filesize=excluded.filesize,
                mtime=excluded.mtime,
                scanned_at=excluded.scanned_at,
                item_count=excluded.item_count
            """,
            (source_path, source_type, filesize, mtime, time.time(), len(records)),
        )
        conn.commit()
    finally:
        conn.close()
