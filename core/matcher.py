import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.database import get_connection


def run_matching(
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """
    Compares all takeout_assets against icloud_assets and internal Takeout duplicates.
    Never modifies any files on disk; updates status in sync_state.db.
    """
    if progress_cb:
        progress_cb(5, 100, "Cargando índice de iCloud Photos en memoria...")

    conn = get_connection()
    try:
        icloud_rows = conn.execute(
            "SELECT uuid, norm_name, stem, ext, timestamp, year_month, kind, filesize FROM icloud_assets"
        ).fetchall()

        takeout_rows = conn.execute(
            """
            SELECT id, unique_key, inner_path, filename, norm_name, stem, ext,
                   timestamp, year_month, year, month, kind, filesize, status
            FROM takeout_assets
            ORDER BY
                -- Prefer "Photos from YYYY" / "Fotos de YYYY" canonical folders over album folders when deduplicating
                CASE WHEN inner_path LIKE '%Photos from %' OR inner_path LIKE '%Fotos de %' THEN 0 ELSE 1 END,
                id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    by_norm_name: Dict[str, List[ sqlite3.Row ]] = defaultdict(list)
    by_stem: Dict[str, List[ sqlite3.Row ]] = defaultdict(list)
    by_ym_kind: Dict[Tuple[str, str], List[ sqlite3.Row ]] = defaultdict(list)

    for r in icloud_rows:
        by_norm_name[r["norm_name"]].append(r)
        by_stem[r["stem"]].append(r)
        if r["year_month"] and r["year_month"] != "Sin-Fecha":
            by_ym_kind[(r["year_month"], r["kind"])].append(r)

    total = len(takeout_rows)
    if progress_cb:
        progress_cb(20, 100, f"Comparando {total:,} archivos de Google Takeout contra {len(icloud_rows):,} de iCloud...")

    updates: List[Tuple[str, Optional[str], Optional[str], float, int]] = []
    seen_takeout_keys: Dict[Tuple[str, int, str], int] = {}

    matched_count = 0
    missing_count = 0
    internal_dup_count = 0
    already_done_count = 0
    now = time.time()

    for idx, tr in enumerate(takeout_rows):
        if idx % 1000 == 0 and progress_cb and total > 0:
            pct = int(20 + (idx / total) * 65)
            progress_cb(pct, 100, f"Analizando coincidencias: {idx:,}/{total:,}...")

        curr_status = tr["status"]
        if curr_status in ("imported", "exported", "ignored"):
            already_done_count += 1
            continue

        t_id = tr["id"]
        norm_name = tr["norm_name"]
        stem = tr["stem"]
        ts = tr["timestamp"] or 0
        ym = tr["year_month"]
        kind = tr["kind"]
        fsize = tr["filesize"] or 0

        # Step 0: Internal Google Takeout deduplication (e.g. same photo in "Fotos de 2020" and an Album folder)
        # Round timestamp to 10-second bucket for internal duplicate check
        ts_bucket = (ts // 10) if ts > 0 else 0
        dedup_sig = (norm_name, fsize, ts_bucket)
        if fsize > 0 and dedup_sig in seen_takeout_keys:
            orig_id = seen_takeout_keys[dedup_sig]
            updates.append(
                (
                    "matched",
                    "Duplicado interno en otro álbum de Google Takeout",
                    f"takeout_id:{orig_id}",
                    now,
                    t_id,
                )
            )
            internal_dup_count += 1
            matched_count += 1
            continue
        seen_takeout_keys[dedup_sig] = t_id

        matched_uuid = None
        match_reason = None

        # Rule 1: Exact normalized filename match (within ±48h if both have timestamps, or same filesize)
        if norm_name in by_norm_name:
            for ic in by_norm_name[norm_name]:
                ic_ts = ic["timestamp"] or 0
                if ts > 0 and ic_ts > 0:
                    if abs(ts - ic_ts) <= 172800:  # 48 hours tolerance for timezone offsets
                        matched_uuid = ic["uuid"]
                        match_reason = "Mismo nombre y fecha en iCloud"
                        break
                elif fsize > 0 and ic["filesize"] == fsize:
                    matched_uuid = ic["uuid"]
                    match_reason = "Mismo nombre y tamaño exacto en iCloud"
                    break
                elif ts == 0 or ic_ts == 0:
                    matched_uuid = ic["uuid"]
                    match_reason = "Mismo nombre de archivo en iCloud"
                    break

        # Rule 2: Same filename stem (e.g. IMG_1234.HEIC in iCloud vs IMG_1234.JPG or Live Photo clip in Takeout)
        if not matched_uuid and stem in by_stem and len(stem) >= 4:
            for ic in by_stem[stem]:
                ic_ts = ic["timestamp"] or 0
                if ts > 0 and ic_ts > 0 and abs(ts - ic_ts) <= 129600:  # 36 hours
                    if ic["kind"] == kind:
                        matched_uuid = ic["uuid"]
                        match_reason = f"Mismo archivo ({ic['ext']} vs {tr['ext']}) y fecha en iCloud"
                        break
                    elif kind == "video" and ic["kind"] == "photo" and fsize < 25 * 1024 * 1024:
                        # Live Photo companion video clip (<25MB) whose main photo is already in iCloud
                        matched_uuid = ic["uuid"]
                        match_reason = "Clip de Live Photo cuya foto ya está en iCloud"
                        break

        # Rule 3: Exact capture timestamp (±2 seconds) + same media kind in the same Year-Month
        if not matched_uuid and ts > 0 and ym != "Sin-Fecha":
            candidates = by_ym_kind.get((ym, kind), [])
            for ic in candidates:
                ic_ts = ic["timestamp"] or 0
                if ic_ts > 0 and abs(ts - ic_ts) <= 2:
                    ic_size = ic["filesize"] or 0
                    if fsize == 0 or ic_size == 0 or abs(fsize - ic_size) / max(fsize, ic_size) <= 0.15:
                        matched_uuid = ic["uuid"]
                        match_reason = "Misma fecha/hora exacta de captura y tamaño en iCloud"
                        break

        if matched_uuid:
            updates.append(("matched", match_reason, matched_uuid, now, t_id))
            matched_count += 1
        else:
            updates.append(("missing", "No encontrado en iCloud", None, now, t_id))
            missing_count += 1

    if progress_cb:
        progress_cb(88, 100, f"Guardando resultados ({missing_count:,} faltantes detectados)...")

    conn = get_connection()
    try:
        conn.executemany(
            """
            UPDATE takeout_assets
            SET status = ?, match_reason = ?, matched_icloud_uuid = ?, updated_at = ?
            WHERE id = ?
            """,
            updates,
        )
        conn.commit()
    finally:
        conn.close()

    if progress_cb:
        progress_cb(100, 100, "¡Comparación finalizada!")

    return {
        "total_takeout": total,
        "total_icloud": len(icloud_rows),
        "matched": matched_count,
        "internal_duplicates": internal_dup_count,
        "missing": missing_count,
        "already_processed": already_done_count,
    }


def get_dashboard_summary() -> Dict[str, Any]:
    """
    Returns overall counters and the complete Year-Month comparison matrix for the UI.
    """
    conn = get_connection()
    try:
        ic_totals = conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN kind='photo' THEN 1 ELSE 0 END) as photos,
                SUM(CASE WHEN kind='video' THEN 1 ELSE 0 END) as videos
            FROM icloud_assets
            """
        ).fetchone()

        tk_totals = conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='matched' THEN 1 ELSE 0 END) as matched,
                SUM(CASE WHEN status='missing' THEN 1 ELSE 0 END) as missing,
                SUM(CASE WHEN status='missing' AND kind='photo' THEN 1 ELSE 0 END) as missing_photos,
                SUM(CASE WHEN status='missing' AND kind='video' THEN 1 ELSE 0 END) as missing_videos,
                SUM(CASE WHEN status='missing' THEN filesize ELSE 0 END) as missing_bytes,
                SUM(CASE WHEN status IN ('imported', 'exported') THEN 1 ELSE 0 END) as completed
            FROM takeout_assets
            """
        ).fetchone()

        # Group by year_month for iCloud
        ic_ym_rows = conn.execute(
            """
            SELECT year_month, year, month,
                   COUNT(*) as ic_total,
                   SUM(CASE WHEN kind='photo' THEN 1 ELSE 0 END) as ic_photos,
                   SUM(CASE WHEN kind='video' THEN 1 ELSE 0 END) as ic_videos
            FROM icloud_assets
            GROUP BY year_month
            """
        ).fetchall()

        # Group by year_month for Takeout
        tk_ym_rows = conn.execute(
            """
            SELECT year_month, year, month,
                   COUNT(*) as tk_total,
                   SUM(CASE WHEN status='matched' THEN 1 ELSE 0 END) as matched,
                   SUM(CASE WHEN status='missing' THEN 1 ELSE 0 END) as missing,
                   SUM(CASE WHEN status='missing' AND kind='photo' THEN 1 ELSE 0 END) as missing_photos,
                   SUM(CASE WHEN status='missing' AND kind='video' THEN 1 ELSE 0 END) as missing_videos,
                   SUM(CASE WHEN status='missing' THEN filesize ELSE 0 END) as missing_bytes,
                   SUM(CASE WHEN status IN ('imported', 'exported') THEN 1 ELSE 0 END) as done
            FROM takeout_assets
            GROUP BY year_month
            """
        ).fetchall()
    finally:
        conn.close()

    ym_map: Dict[str, Dict[str, Any]] = {}
    for r in ic_ym_rows:
        ym = r["year_month"] or "Sin-Fecha"
        ym_map[ym] = {
            "year_month": ym,
            "year": r["year"] or 0,
            "month": r["month"] or 0,
            "icloud_total": r["ic_total"] or 0,
            "icloud_photos": r["ic_photos"] or 0,
            "icloud_videos": r["ic_videos"] or 0,
            "takeout_total": 0,
            "matched": 0,
            "missing": 0,
            "missing_photos": 0,
            "missing_videos": 0,
            "missing_bytes": 0,
            "done": 0,
        }

    for r in tk_ym_rows:
        ym = r["year_month"] or "Sin-Fecha"
        if ym not in ym_map:
            ym_map[ym] = {
                "year_month": ym,
                "year": r["year"] or 0,
                "month": r["month"] or 0,
                "icloud_total": 0,
                "icloud_photos": 0,
                "icloud_videos": 0,
                "takeout_total": 0,
                "matched": 0,
                "missing": 0,
                "missing_photos": 0,
                "missing_videos": 0,
                "missing_bytes": 0,
                "done": 0,
            }
        entry = ym_map[ym]
        entry["takeout_total"] = r["tk_total"] or 0
        entry["matched"] = r["matched"] or 0
        entry["missing"] = r["missing"] or 0
        entry["missing_photos"] = r["missing_photos"] or 0
        entry["missing_videos"] = r["missing_videos"] or 0
        entry["missing_bytes"] = r["missing_bytes"] or 0
        entry["done"] = r["done"] or 0

    ym_list = sorted(
        ym_map.values(),
        key=lambda x: (x["year"] if x["year"] > 0 else 9999, x["month"] if x["month"] > 0 else 99),
        reverse=True,
    )

    # Also aggregate by Year for quick overview
    years_map: Dict[int, Dict[str, Any]] = {}
    for item in ym_list:
        y = item["year"]
        if y not in years_map:
            years_map[y] = {
                "year": y if y > 0 else "Sin Fecha",
                "icloud_total": 0,
                "takeout_total": 0,
                "matched": 0,
                "missing": 0,
                "missing_photos": 0,
                "missing_videos": 0,
                "missing_bytes": 0,
                "done": 0,
                "months": [],
            }
        y_entry = years_map[y]
        y_entry["icloud_total"] += item["icloud_total"]
        y_entry["takeout_total"] += item["takeout_total"]
        y_entry["matched"] += item["matched"]
        y_entry["missing"] += item["missing"]
        y_entry["missing_photos"] += item["missing_photos"]
        y_entry["missing_videos"] += item["missing_videos"]
        y_entry["missing_bytes"] += item["missing_bytes"]
        y_entry["done"] += item["done"]
        y_entry["months"].append(item)

    return {
        "icloud": {
            "total": ic_totals["total"] or 0,
            "photos": ic_totals["photos"] or 0,
            "videos": ic_totals["videos"] or 0,
        },
        "takeout": {
            "total": tk_totals["total"] or 0,
            "matched": tk_totals["matched"] or 0,
            "missing": tk_totals["missing"] or 0,
            "missing_photos": tk_totals["missing_photos"] or 0,
            "missing_videos": tk_totals["missing_videos"] or 0,
            "missing_bytes": tk_totals["missing_bytes"] or 0,
            "completed": tk_totals["completed"] or 0,
        },
        "by_year": list(years_map.values()),
        "by_year_month": ym_list,
    }
