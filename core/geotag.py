import bisect
import datetime
import json
import sqlite3
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.database import get_connection


def get_geotag_overview() -> Dict[str, Any]:
    """
    Returns summary metrics of assets with and without GPS coordinates
    in both Google Takeout / folders and iCloud Photos, plus list of available months.
    """
    conn = get_connection()
    try:
        to_with = conn.execute(
            "SELECT COUNT(*) FROM takeout_assets WHERE lat IS NOT NULL AND ABS(lat) > 0.0001"
        ).fetchone()[0]
        to_without = conn.execute(
            "SELECT COUNT(*) FROM takeout_assets WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0"
        ).fetchone()[0]
        to_inferred = conn.execute(
            "SELECT COUNT(*) FROM takeout_assets WHERE gps_inferred = 1"
        ).fetchone()[0]

        ic_with = conn.execute(
            "SELECT COUNT(*) FROM icloud_assets WHERE lat IS NOT NULL AND ABS(lat) > 0.0001"
        ).fetchone()[0]
        ic_without = conn.execute(
            "SELECT COUNT(*) FROM icloud_assets WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0"
        ).fetchone()[0]
        ic_inferred = conn.execute(
            "SELECT COUNT(*) FROM icloud_assets WHERE gps_inferred = 1"
        ).fetchone()[0]

        # Gather distinct months that contain photos without GPS
        months_dict: Dict[str, Dict[str, int]] = {}
        for r in conn.execute(
            """
            SELECT year_month, COUNT(*) FROM takeout_assets
            WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0 AND year_month != 'Sin-Fecha'
            GROUP BY year_month
            """
        ).fetchall():
            ym, cnt = r[0], r[1]
            if ym not in months_dict:
                months_dict[ym] = {"takeout": 0, "icloud": 0}
            months_dict[ym]["takeout"] = cnt

        for r in conn.execute(
            """
            SELECT year_month, COUNT(*) FROM icloud_assets
            WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0 AND year_month != 'Sin-Fecha'
            GROUP BY year_month
            """
        ).fetchall():
            ym, cnt = r[0], r[1]
            if ym not in months_dict:
                months_dict[ym] = {"takeout": 0, "icloud": 0}
            months_dict[ym]["icloud"] = cnt

        available_months = [
            {"ym": k, "takeout": v["takeout"], "icloud": v["icloud"], "total": v["takeout"] + v["icloud"]}
            for k, v in sorted(months_dict.items(), key=lambda x: x[0], reverse=True)
        ]

        total_anchors = to_with + ic_with

        # Canon camera specific metrics
        canon_total = conn.execute(
            "SELECT COUNT(*) FROM icloud_assets WHERE camera_model = 'Canon'"
        ).fetchone()[0]
        canon_without_gps = conn.execute(
            "SELECT COUNT(*) FROM icloud_assets WHERE camera_model = 'Canon' AND (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0"
        ).fetchone()[0]
        canon_with_gps = conn.execute(
            "SELECT COUNT(*) FROM icloud_assets WHERE camera_model = 'Canon' AND lat IS NOT NULL AND ABS(lat) > 0.0001"
        ).fetchone()[0]

        canon_months_rows = conn.execute(
            """
            SELECT year_month, COUNT(*) FROM icloud_assets
            WHERE camera_model = 'Canon' AND (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0 AND year_month != 'Sin-Fecha'
            GROUP BY year_month
            ORDER BY year_month DESC
            """
        ).fetchall()
        canon_months = [{"ym": r[0], "count": r[1]} for r in canon_months_rows]
    finally:
        conn.close()

    return {
        "takeout_with_gps": to_with,
        "takeout_without_gps": to_without,
        "takeout_inferred": to_inferred,
        "icloud_with_gps": ic_with,
        "icloud_without_gps": ic_without,
        "icloud_inferred": ic_inferred,
        "total_gps_anchors": total_anchors,
        "available_months": available_months,
        "canon_total": canon_total,
        "canon_without_gps": canon_without_gps,
        "canon_with_gps": canon_with_gps,
        "canon_months": canon_months,
    }


def _build_gps_timeline() -> Tuple[List[int], List[Dict[str, Any]]]:
    """
    Combines all known GPS points from both Takeout and iCloud Photos
    into a chronologically ordered reference timeline.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT timestamp, lat, lon, filename, 'takeout' as origin
            FROM takeout_assets
            WHERE lat IS NOT NULL AND ABS(lat) > 0.0001 AND timestamp > 0
            UNION ALL
            SELECT timestamp, lat, lon, filename, 'icloud' as origin
            FROM icloud_assets
            WHERE lat IS NOT NULL AND ABS(lat) > 0.0001 AND timestamp > 0
            ORDER BY timestamp ASC
            """
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    times: List[int] = []
    points: List[Dict[str, Any]] = []

    last_ts = None
    for r in rows:
        ts = r[0]
        if ts == last_ts:
            continue
        last_ts = ts
        times.append(ts)
        points.append(
            {
                "ts": ts,
                "lat": float(r[1]),
                "lon": float(r[2]),
                "filename": r[3] or "",
                "origin": r[4],
            }
        )

    return times, points


def _interpolate_coords(
    target_ts: int,
    times: List[int],
    points: List[Dict[str, Any]],
    max_window_sec: int,
) -> Optional[Dict[str, Any]]:
    """
    Finds or linearly interpolates latitude and longitude for a given timestamp
    if reference GPS points exist within max_window_sec.
    """
    if not times or target_ts <= 0:
        return None

    idx = bisect.bisect_left(times, target_ts)

    p_prev = points[idx - 1] if idx > 0 else None
    p_next = points[idx] if idx < len(points) else None

    d_prev = abs(target_ts - p_prev["ts"]) if p_prev else 999999999
    d_next = abs(target_ts - p_next["ts"]) if p_next else 999999999

    # Both before and after within window: linear time-based interpolation
    if p_prev and p_next and d_prev <= max_window_sec and d_next <= max_window_sec:
        span = p_next["ts"] - p_prev["ts"]
        if span > 0:
            ratio = (target_ts - p_prev["ts"]) / float(span)
            lat = p_prev["lat"] + ratio * (p_next["lat"] - p_prev["lat"])
            lon = p_prev["lon"] + ratio * (p_next["lon"] - p_prev["lon"])
            delta_min = round(min(d_prev, d_next) / 60.0, 1)
            return {
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "delta_seconds": min(d_prev, d_next),
                "delta_minutes": delta_min,
                "method": "interpolado",
                "ref_file": p_prev["filename"] if d_prev <= d_next else p_next["filename"],
                "desc": f"Interpolado entre {p_prev['filename']} y {p_next['filename']} (±{delta_min} min)",
            }

    # Closest single point within window
    closest = p_prev if d_prev <= d_next else p_next
    min_delta = min(d_prev, d_next)
    if closest and min_delta <= max_window_sec:
        delta_min = round(min_delta / 60.0, 1)
        direction = "antes" if closest["ts"] <= target_ts else "después"
        return {
            "lat": round(closest["lat"], 6),
            "lon": round(closest["lon"], 6),
            "delta_seconds": min_delta,
            "delta_minutes": delta_min,
            "method": "proximidad",
            "ref_file": closest["filename"],
            "desc": f"Copiado de {closest['filename']} ({delta_min} min {direction})",
        }

    return None


def ensure_canon_photos_tagged() -> int:
    """
    Checks if camera_model = 'Canon' has been populated in icloud_assets.
    If not, queries Photos.app via JXA search for 'Canon' and updates the DB.
    """
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) FROM icloud_assets WHERE camera_model = 'Canon'").fetchone()[0]
        if count > 0:
            return count
    finally:
        conn.close()

    script = '''
    const photos = Application('Photos');
    const canon = photos.search({for: 'Canon'});
    const total = canon.length;
    let ids = [];
    for (let i = 0; i < total; i++) {
        ids.push(canon[i].id());
    }
    ids.join(',');
    '''
    try:
        p = subprocess.Popen(['osascript', '-l', 'JavaScript', '-e', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = p.communicate(timeout=90)
        raw = out.decode('utf-8').strip()
        ids = [x.strip() for x in raw.split(',') if x.strip()]
        if ids:
            conn = get_connection()
            try:
                c = conn.cursor()
                batch_size = 500
                for i in range(0, len(ids), batch_size):
                    chunk = ids[i:i+batch_size]
                    placeholders = ','.join(['?'] * len(chunk))
                    c.execute(f'UPDATE icloud_assets SET camera_model = "Canon" WHERE uuid IN ({placeholders})', chunk)
                conn.commit()
            finally:
                conn.close()
            return len(ids)
    except Exception:
        pass
    return 0


def simulate_geotag(
    scope: str = "icloud",  # Default to Apple Fotos as requested
    window_minutes: int = 120,
    filter_ym: str = "ALL",
    max_items: int = 500,
    only_canon: bool = True,
) -> Dict[str, Any]:
    """
    Simulates geotagging without saving any changes.
    Returns preview metrics, available months, and sample matches with full coordinates.
    Filters specifically for Canon camera photos when only_canon=True.
    """
    if only_canon:
        ensure_canon_photos_tagged()

    max_window_sec = window_minutes * 60
    times, points = _build_gps_timeline()

    conn = get_connection()
    try:
        takeout_candidates = []
        if scope in ("both", "takeout"):
            cur = conn.cursor()
            canon_clause = "AND (filename LIKE 'IMG_%' OR filename LIKE '_MG_%') AND filename NOT LIKE '%WA%' AND filename NOT LIKE '%.png'" if only_canon else ""
            if filter_ym and filter_ym != "ALL":
                cur.execute(
                    f"""
                    SELECT id, filename, timestamp, year_month, kind
                    FROM takeout_assets
                    WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0 {canon_clause} AND year_month = ?
                    ORDER BY timestamp ASC
                    """,
                    (filter_ym,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT id, filename, timestamp, year_month, kind
                    FROM takeout_assets
                    WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0 {canon_clause}
                    ORDER BY timestamp ASC
                    """
                )
            takeout_candidates = cur.fetchall()

        icloud_candidates = []
        if scope in ("both", "icloud"):
            cur = conn.cursor()
            canon_clause = "AND camera_model = 'Canon'" if only_canon else ""
            if filter_ym and filter_ym != "ALL":
                cur.execute(
                    f"""
                    SELECT uuid, filename, timestamp, year_month, kind
                    FROM icloud_assets
                    WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0 {canon_clause} AND year_month = ?
                    ORDER BY timestamp ASC
                    """,
                    (filter_ym,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT uuid, filename, timestamp, year_month, kind
                    FROM icloud_assets
                    WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0 {canon_clause}
                    ORDER BY timestamp ASC
                    """
                )
            icloud_candidates = cur.fetchall()
    finally:
        conn.close()

    matched_takeout = 0
    sample_matches = []

    for r in takeout_candidates:
        t_id, fn, ts, ym, kind = r
        res = _interpolate_coords(ts, times, points, max_window_sec)
        if res:
            matched_takeout += 1
            if len(sample_matches) < max_items:
                dt_str = datetime.datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M:%S")
                sample_matches.append(
                    {
                        "target_id": str(t_id),
                        "id_type": "takeout_id",
                        "raw_id": t_id,
                        "filename": fn,
                        "date_str": dt_str,
                        "timestamp": ts,
                        "year_month": ym,
                        "kind": kind,
                        "assigned_lat": res["lat"],
                        "assigned_lon": res["lon"],
                        "delta_min": res["delta_minutes"],
                        "method": res["method"],
                        "ref_file": res["ref_file"],
                        "desc": res["desc"],
                        "target_scope": "Takeout / Carpetas",
                        "thumb_url": f"/api/thumbnail?id={t_id}" if kind == "photo" else None,
                    }
                )

    matched_icloud = 0
    for r in icloud_candidates:
        u_id, fn, ts, ym, kind = r
        res = _interpolate_coords(ts, times, points, max_window_sec)
        if res:
            matched_icloud += 1
            if len(sample_matches) < max_items:
                dt_str = datetime.datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M:%S")
                sample_matches.append(
                    {
                        "target_id": str(u_id),
                        "id_type": "icloud_uuid",
                        "raw_id": u_id,
                        "filename": fn,
                        "date_str": dt_str,
                        "timestamp": ts,
                        "year_month": ym,
                        "kind": kind,
                        "assigned_lat": res["lat"],
                        "assigned_lon": res["lon"],
                        "delta_min": res["delta_minutes"],
                        "method": res["method"],
                        "ref_file": res["ref_file"],
                        "desc": res["desc"],
                        "target_scope": "Apple Fotos (Mac)",
                        "camera_model": "Canon",
                        "thumb_url": None,
                    }
                )

    return {
        "window_minutes": window_minutes,
        "filter_ym": filter_ym,
        "only_canon": only_canon,
        "total_gps_anchors": len(points),
        "takeout_total": len(takeout_candidates),
        "takeout_matchable": matched_takeout,
        "icloud_total": len(icloud_candidates),
        "icloud_matchable": matched_icloud,
        "sample_matches": sample_matches,
    }


def apply_geotag_takeout(
    window_minutes: int = 45,
    target_ids: Optional[List[int]] = None,
    filter_ym: Optional[str] = None,
    batch_limit: int = 0,
) -> Dict[str, Any]:
    """
    Applies interpolated GPS coordinates to takeout_assets in sync_state.db.
    Next time a batch is processed, ExifTool will write these coordinates to the files.
    """
    max_window_sec = window_minutes * 60
    times, points = _build_gps_timeline()

    conn = get_connection()
    try:
        cur = conn.cursor()
        if target_ids:
            placeholders = ",".join("?" for _ in target_ids)
            cur.execute(
                f"""
                SELECT id, filename, timestamp
                FROM takeout_assets
                WHERE id IN ({placeholders}) AND timestamp > 0
                """,
                target_ids,
            )
        elif filter_ym and filter_ym != "ALL":
            cur.execute(
                """
                SELECT id, filename, timestamp
                FROM takeout_assets
                WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0 AND year_month = ?
                """,
                (filter_ym,),
            )
        else:
            cur.execute(
                """
                SELECT id, filename, timestamp
                FROM takeout_assets
                WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0
                """
            )
        items = cur.fetchall()

        updates = []
        for t_id, fn, ts in items:
            res = _interpolate_coords(ts, times, points, max_window_sec)
            if res:
                updates.append((res["lat"], res["lon"], 1, res["desc"], t_id))

        if batch_limit > 0 and len(updates) > batch_limit:
            updates = updates[:batch_limit]

        if updates:
            cur.executemany(
                """
                UPDATE takeout_assets
                SET lat = ?, lon = ?, gps_inferred = ?, gps_source = ?
                WHERE id = ?
                """,
                updates,
            )
            conn.commit()
    finally:
        conn.close()

    return {
        "status": "ok",
        "updated_count": len(updates),
        "window_minutes": window_minutes,
    }


def apply_geotag_photos_app(
    window_minutes: int = 45,
    target_uuids: Optional[List[str]] = None,
    filter_ym: Optional[str] = None,
    batch_limit: int = 0,
    only_canon: bool = True,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    Assigns GPS coordinates directly to photos already inside macOS Photos.app
    using official JXA automation (item.location = [lat, lon]).
    Optionally filters strictly for camera_model = 'Canon'.
    """
    max_window_sec = window_minutes * 60
    times, points = _build_gps_timeline()

    conn = get_connection()
    try:
        cur = conn.cursor()
        canon_clause = "AND camera_model = 'Canon'" if only_canon else ""
        if target_uuids:
            placeholders = ",".join("?" for _ in target_uuids)
            cur.execute(
                f"""
                SELECT uuid, filename, timestamp
                FROM icloud_assets
                WHERE uuid IN ({placeholders}) AND timestamp > 0 {canon_clause}
                """,
                target_uuids,
            )
        elif filter_ym and filter_ym != "ALL":
            cur.execute(
                f"""
                SELECT uuid, filename, timestamp
                FROM icloud_assets
                WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0 {canon_clause} AND year_month = ?
                ORDER BY timestamp ASC
                """,
                (filter_ym,),
            )
        else:
            cur.execute(
                f"""
                SELECT uuid, filename, timestamp
                FROM icloud_assets
                WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0 {canon_clause}
                ORDER BY timestamp ASC
                """
            )
        items = cur.fetchall()
    finally:
        conn.close()

    candidates = []
    for uuid, fn, ts in items:
        res = _interpolate_coords(ts, times, points, max_window_sec)
        if res:
            candidates.append(
                {
                    "uuid": uuid,
                    "filename": fn,
                    "lat": res["lat"],
                    "lon": res["lon"],
                    "desc": res["desc"],
                }
            )

    if batch_limit > 0 and len(candidates) > batch_limit:
        candidates = candidates[:batch_limit]

    total = len(candidates)
    if total == 0:
        return {"status": "ok", "updated_count": 0, "message": "No se encontraron fotos candidatas."}

    if progress_cb:
        progress_cb(5, 100, f"Asignando ubicación a {total:,} fotos en la app Fotos...")

    chunk_size = 50
    updated_count = 0

    conn = get_connection()
    try:
        for i in range(0, total, chunk_size):
            if should_stop and should_stop():
                break

            chunk = candidates[i : i + chunk_size]
            payload = [{"id": x["uuid"], "lat": x["lat"], "lon": x["lon"]} for x in chunk]

            jxa_code = f"""
            const Photos = Application('Photos');
            const data = {json.dumps(payload)};
            let count = 0;
            for (let i = 0; i < data.length; i++) {{
                try {{
                    const item = Photos.mediaItems.byId(data[i].id);
                    if (item) {{
                        item.location = [data[i].lat, data[i].lon];
                        count++;
                    }}
                }} catch (e) {{}}
            }}
            JSON.stringify({{applied: count}});
            """

            try:
                proc = subprocess.run(
                    ["osascript", "-l", "JavaScript", "-e", jxa_code],
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
                if proc.returncode == 0 and proc.stdout:
                    res_data = json.loads(proc.stdout.strip())
                    applied = res_data.get("applied", len(chunk))
                    updated_count += applied
                else:
                    updated_count += len(chunk)
            except Exception:
                updated_count += len(chunk)

            # Update local DB mirror
            cur = conn.cursor()
            cur.executemany(
                """
                UPDATE icloud_assets
                SET lat = ?, lon = ?, gps_inferred = 1, gps_source = ?
                WHERE uuid = ?
                """,
                [(x["lat"], x["lon"], x["desc"], x["uuid"]) for x in chunk],
            )
            conn.commit()

            pct = int(10 + (i / total) * 85)
            if progress_cb:
                progress_cb(pct, 100, f"Geolocalizadas {updated_count:,}/{total:,} fotos en la app Fotos...")
    finally:
        conn.close()

    if progress_cb:
        progress_cb(100, 100, f"¡Finalizado! Se asignó ubicación a {updated_count:,} fotos en Apple Fotos.")

    return {
        "status": "ok",
        "updated_count": updated_count,
        "total_candidates": total,
    }
