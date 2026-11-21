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
    in both Google Takeout / folders and iCloud Photos.
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

        total_anchors = to_with + ic_with
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
        # Skip duplicate points at the exact same second to keep binary search fast
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


def simulate_geotag(
    scope: str = "both",  # 'both', 'takeout', 'icloud'
    window_minutes: int = 45,
) -> Dict[str, Any]:
    """
    Simulates geotagging without saving any changes.
    Returns preview metrics and sample matches.
    """
    max_window_sec = window_minutes * 60
    times, points = _build_gps_timeline()

    conn = get_connection()
    try:
        takeout_candidates = []
        if scope in ("both", "takeout"):
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, filename, timestamp, year_month, kind
                FROM takeout_assets
                WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0
                ORDER BY timestamp ASC
                """
            )
            takeout_candidates = cur.fetchall()

        icloud_candidates = []
        if scope in ("both", "icloud"):
            cur = conn.cursor()
            cur.execute(
                """
                SELECT uuid, filename, timestamp, year_month, kind
                FROM icloud_assets
                WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0
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
            if len(sample_matches) < 20:
                dt_str = datetime.datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M:%S")
                sample_matches.append(
                    {
                        "target_id": str(t_id),
                        "filename": fn,
                        "date_str": dt_str,
                        "kind": kind,
                        "assigned_lat": res["lat"],
                        "assigned_lon": res["lon"],
                        "delta_min": res["delta_minutes"],
                        "method": res["method"],
                        "ref_file": res["ref_file"],
                        "desc": res["desc"],
                        "target_scope": "Takeout / Carpetas",
                    }
                )

    matched_icloud = 0
    for r in icloud_candidates:
        u_id, fn, ts, ym, kind = r
        res = _interpolate_coords(ts, times, points, max_window_sec)
        if res:
            matched_icloud += 1
            if len(sample_matches) < 30:
                dt_str = datetime.datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M:%S")
                sample_matches.append(
                    {
                        "target_id": str(u_id),
                        "filename": fn,
                        "date_str": dt_str,
                        "kind": kind,
                        "assigned_lat": res["lat"],
                        "assigned_lon": res["lon"],
                        "delta_min": res["delta_minutes"],
                        "method": res["method"],
                        "ref_file": res["ref_file"],
                        "desc": res["desc"],
                        "target_scope": "Apple Fotos (Mac)",
                    }
                )

    return {
        "window_minutes": window_minutes,
        "total_gps_anchors": len(points),
        "takeout_total": len(takeout_candidates),
        "takeout_matchable": matched_takeout,
        "icloud_total": len(icloud_candidates),
        "icloud_matchable": matched_icloud,
        "sample_matches": sample_matches,
    }


def apply_geotag_takeout(window_minutes: int = 45) -> Dict[str, Any]:
    """
    Applies interpolated GPS coordinates to takeout_assets in sync_state.db.
    Next time a batch is processed, ExifTool will write these coordinates to the files.
    """
    max_window_sec = window_minutes * 60
    times, points = _build_gps_timeline()

    conn = get_connection()
    try:
        cur = conn.cursor()
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
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    Assigns GPS coordinates directly to photos already inside macOS Photos.app
    using official JXA automation (item.location = [lat, lon]).
    """
    max_window_sec = window_minutes * 60
    times, points = _build_gps_timeline()

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT uuid, filename, timestamp
            FROM icloud_assets
            WHERE (lat IS NULL OR ABS(lat) <= 0.0001) AND timestamp > 0
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

    total = len(candidates)
    if total == 0:
        return {"status": "ok", "updated_count": 0, "message": "No se encontraron fotos candidatas."}

    if progress_cb:
        progress_cb(5, 100, f"Asignando ubicación a {total:,} fotos en la app Fotos...")

    chunk_size = 50
    updated_count = 0
    now = time.time()

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
