import json
import mimetypes
import os
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from core.batch_processor import DEFAULT_OUTPUT_ROOT, _has_exiftool, run_batch
from core.database import BASE_DIR, get_connection, get_setting, init_db
from core.matcher import get_dashboard_summary, run_matching
from core.scanner_icloud import (
    DEFAULT_PHOTOS_LIB,
    SNAPSHOT_DIR,
    check_icloud_access,
    scan_icloud_sqlite,
    scan_icloud_via_photos_app,
)
from core.scanner_takeout import (
    list_indexed_sources,
    remove_source,
    scan_takeout_zips,
    scan_uncompressed_folder,
)
from core.thumbnails import get_or_create_thumbnail

STATIC_DIR = os.path.join(BASE_DIR, "static")

_job_lock = threading.RLock()
_current_job: Dict[str, Any] = {
    "active": False,
    "type": None,
    "status": "idle",
    "current": 0,
    "total": 100,
    "percent": 0,
    "message": "Listo para empezar.",
    "control": "run",
    "started_at": 0,
    "error": None,
    "result": None,
    "logs": [],
}


def _add_log(msg: str) -> None:
    ts_str = time.strftime("%H:%M:%S")
    with _job_lock:
        _current_job["logs"].append(f"[{ts_str}] {msg}")
        if len(_current_job["logs"]) > 150:
            _current_job["logs"] = _current_job["logs"][-150:]


def _progress_cb(current: int, total: int, message: str) -> None:
    with _job_lock:
        _current_job["current"] = current
        _current_job["total"] = max(1, total)
        _current_job["percent"] = min(100, int((current / max(1, total)) * 100))
        _current_job["message"] = message
    _add_log(message)


def _should_stop() -> bool:
    with _job_lock:
        return _current_job["control"] == "stop"


def _should_pause_or_stop() -> str:
    with _job_lock:
        return _current_job["control"]


def _start_background_job(job_type: str, target_fn, *args, **kwargs) -> bool:
    with _job_lock:
        if _current_job["active"]:
            return False
        _current_job["active"] = True
        _current_job["type"] = job_type
        _current_job["status"] = "running"
        _current_job["current"] = 0
        _current_job["total"] = 100
        _current_job["percent"] = 0
        _current_job["message"] = "Iniciando tarea..."
        _current_job["control"] = "run"
        _current_job["started_at"] = time.time()
        _current_job["error"] = None
        _current_job["result"] = None

    def runner():
        try:
            _add_log(f"▶ Iniciando tarea: {job_type}")
            res = target_fn(*args, **kwargs)
            with _job_lock:
                if _current_job["control"] == "stop":
                    _current_job["status"] = "cancelled"
                    _current_job["message"] = "Tarea detenida por el usuario."
                else:
                    _current_job["status"] = "completed"
                    _current_job["percent"] = 100
                _current_job["result"] = res
        except InterruptedError as ie:
            with _job_lock:
                _current_job["status"] = "cancelled"
                _current_job["message"] = str(ie)
            _add_log(f"⏹ {ie}")
        except Exception as e:
            err_str = str(e)
            traceback.print_exc()
            with _job_lock:
                _current_job["status"] = "error"
                _current_job["error"] = err_str
                _current_job["message"] = f"Error: {err_str}"
            _add_log(f"❌ Error: {err_str}")
        finally:
            with _job_lock:
                _current_job["active"] = False

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    return True


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/status":
            with _job_lock:
                job_copy = dict(_current_job)
                job_copy["logs"] = list(_current_job["logs"][-40:])

            volumes = []
            if os.path.exists("/Volumes"):
                try:
                    for v in sorted(os.listdir("/Volumes")):
                        if not v.startswith(".") and "TimeMachine" not in v:
                            volumes.append(os.path.join("/Volumes", v))
                except Exception:
                    pass

            self._send_json(
                {
                    "job": job_copy,
                    "icloud_access": check_icloud_access(),
                    "has_exiftool": _has_exiftool(),
                    "takeout_root_path": get_setting("takeout_root_path", ""),
                    "last_custom_folder_path": get_setting("last_custom_folder_path", ""),
                    "last_icloud_scan": get_setting("last_icloud_scan", ""),
                    "last_icloud_method": get_setting("last_icloud_method", ""),
                    "last_takeout_scan": get_setting("last_takeout_scan", ""),
                    "output_root": DEFAULT_OUTPUT_ROOT,
                    "volumes": volumes,
                }
            )
            return

        if path == "/api/sources":
            self._send_json({"sources": list_indexed_sources()})
            return

        if path == "/api/summary":
            self._send_json(get_dashboard_summary())
            return

        if path == "/api/thumbnail":
            asset_id = int(qs.get("id", ["0"])[0])
            thumb_bytes = get_or_create_thumbnail(asset_id) if asset_id > 0 else None
            if thumb_bytes:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(thumb_bytes)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(thumb_bytes)
            else:
                self.send_error(404, "No thumbnail")
            return

        if path == "/api/missing-items":
            ym = qs.get("year_month", ["ALL"])[0]
            kind = qs.get("kind", ["all"])[0]
            status_filter = qs.get("status", ["missing"])[0]
            batch_id_filter = qs.get("batch_id", [""])[0]
            source_filter = qs.get("source_path", [""])[0]
            page = max(1, int(qs.get("page", ["1"])[0]))
            page_size = min(200, max(10, int(qs.get("page_size", ["48"])[0])))
            offset = (page - 1) * page_size

            where = ["1=1"]
            params = []
            if batch_id_filter:
                where.append("batch_id = ?")
                params.append(int(batch_id_filter))
            elif status_filter == "imported_or_exported":
                where.append("status IN ('imported', 'exported')")
            elif status_filter != "all":
                where.append("status = ?")
                params.append(status_filter)

            if source_filter:
                where.append("source_path = ?")
                params.append(source_filter)

            if ym and ym != "ALL":
                if ym.startswith("YEAR:"):
                    where.append("year = ?")
                    params.append(int(ym.split(":", 1)[1]))
                else:
                    where.append("year_month = ?")
                    params.append(ym)
            if kind in ("photo", "video"):
                where.append("kind = ?")
                params.append(kind)

            w_sql = " AND ".join(where)
            conn = get_connection()
            try:
                total_c = conn.execute(
                    f"SELECT COUNT(*) FROM takeout_assets WHERE {w_sql}", params
                ).fetchone()[0]
                rows = conn.execute(
                    f"""
                    SELECT id, filename, inner_path, source_path, source_type,
                           timestamp, date_source, year_month, kind, filesize,
                           json_found, status, match_reason, exported_path, batch_id, error_msg
                    FROM takeout_assets
                    WHERE {w_sql}
                    ORDER BY updated_at DESC, timestamp DESC, id ASC
                    LIMIT ? OFFSET ?
                    """,
                    params + [page_size, offset],
                ).fetchall()
                items = [dict(r) for r in rows]
            finally:
                conn.close()

            self._send_json(
                {
                    "total": total_c,
                    "page": page,
                    "page_size": page_size,
                    "items": items,
                }
            )
            return

        if path == "/api/batches":
            conn = get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM batches ORDER BY id DESC LIMIT 30"
                ).fetchall()
                batches = [dict(r) for r in rows]
            finally:
                conn.close()
            self._send_json({"batches": batches})
            return

        if path == "/" or path == "":
            path = "/index.html"
        safe_rel = os.path.normpath(path.lstrip("/"))
        full_static = os.path.join(STATIC_DIR, safe_rel)
        if os.path.isfile(full_static) and full_static.startswith(STATIC_DIR):
            ctype, _ = mimetypes.guess_type(full_static)
            with open(full_static, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype or "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        self.send_error(404, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()

        if path == "/api/pick-folder":
            prompt = body.get("prompt", "Selecciona la carpeta")
            script = f'POSIX path of (choose folder with prompt "{prompt}")'
            proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
            if proc.returncode == 0 and proc.stdout.strip():
                self._send_json({"status": "ok", "path": proc.stdout.strip()})
            else:
                self._send_json({"status": "cancelled"})
            return

        if path == "/api/pick-zip-file":
            prompt = body.get("prompt", "Selecciona un archivo .ZIP de Google Takeout")
            script = f'POSIX path of (choose file of type {{"zip", "public.zip-archive"}} with prompt "{prompt}")'
            proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
            if proc.returncode == 0 and proc.stdout.strip():
                self._send_json({"status": "ok", "path": proc.stdout.strip()})
            else:
                self._send_json({"status": "cancelled"})
            return

        if path == "/api/open-finder":
            target = body.get("path", DEFAULT_OUTPUT_ROOT)
            target = os.path.expanduser(target)
            os.makedirs(target, exist_ok=True)
            subprocess.run(["open", target])
            self._send_json({"status": "ok", "path": target})
            return

        if path == "/api/reveal-file":
            target = body.get("path", "")
            if target and os.path.exists(target):
                subprocess.run(["open", "-R", target])
                self._send_json({"status": "ok"})
            else:
                self._send_json({"error": "El archivo aún no está extraído en disco."}, status=404)
            return

        if path == "/api/open-photos-app":
            subprocess.run(["open", "-a", "Photos"])
            self._send_json({"status": "ok"})
            return

        if path == "/api/open-photos-sqlite-help":
            os.makedirs(SNAPSHOT_DIR, exist_ok=True)
            db_folder = os.path.join(DEFAULT_PHOTOS_LIB, "database")
            if os.path.exists(db_folder):
                subprocess.run(["open", db_folder])
            else:
                subprocess.run(["open", "-R", DEFAULT_PHOTOS_LIB])
            subprocess.run(["open", SNAPSHOT_DIR])
            self._send_json({"status": "ok", "snapshot_dir": SNAPSHOT_DIR})
            return

        if path == "/api/scan-icloud":
            method = body.get("method", "auto")
            custom_db = body.get("custom_db_path", "").strip() or None

            def task_icloud():
                acc = check_icloud_access()
                if method == "sqlite" or (method == "auto" and (acc["can_read_system_sqlite"] or acc["has_snapshot"] or custom_db)):
                    res = scan_icloud_sqlite(custom_db_path=custom_db, progress_cb=_progress_cb)
                else:
                    res = scan_icloud_via_photos_app(progress_cb=_progress_cb, should_stop=_should_stop)
                conn = get_connection()
                tk_count = conn.execute("SELECT COUNT(*) FROM takeout_assets").fetchone()[0]
                conn.close()
                if tk_count > 0:
                    run_matching(progress_cb=_progress_cb)
                return res

            if not _start_background_job("icloud_scan", task_icloud):
                self._send_json({"error": "Ya hay una tarea en ejecución."}, status=409)
                return
            self._send_json({"status": "started"})
            return

        if path in ("/api/scan-zips", "/api/scan-takeout"):
            root_path = body.get("root_path", "").strip()
            force_rescan = bool(body.get("force_rescan", False))
            if not root_path:
                self._send_json({"error": "Selecciona un archivo .ZIP o una carpeta que contenga ZIPs de Takeout."}, status=400)
                return

            def task_zips():
                res = scan_takeout_zips(
                    target_path=root_path,
                    force_rescan=force_rescan,
                    progress_cb=_progress_cb,
                    should_stop=_should_stop,
                )
                run_matching(progress_cb=_progress_cb)
                return res

            if not _start_background_job("takeout_scan", task_zips):
                self._send_json({"error": "Ya hay una tarea en ejecución."}, status=409)
                return
            self._send_json({"status": "started"})
            return

        if path == "/api/scan-custom-folder":
            folder_path = body.get("folder_path", "").strip()
            include_subfolders = bool(body.get("include_subfolders", True))
            if not folder_path:
                self._send_json({"error": "Selecciona la carpeta con fotos/vídeos sin comprimir."}, status=400)
                return

            def task_folder():
                res = scan_uncompressed_folder(
                    folder_path=folder_path,
                    include_subfolders=include_subfolders,
                    progress_cb=_progress_cb,
                    should_stop=_should_stop,
                )
                run_matching(progress_cb=_progress_cb)
                return res

            if not _start_background_job("folder_scan", task_folder):
                self._send_json({"error": "Ya hay una tarea en ejecución."}, status=409)
                return
            self._send_json({"status": "started"})
            return

        if path == "/api/remove-source":
            source_path = body.get("source_path", "").strip()
            if not source_path:
                self._send_json({"error": "Falta source_path"}, status=400)
                return
            remove_source(source_path)
            run_matching()
            self._send_json({"status": "ok"})
            return

        if path == "/api/run-matching":
            if not _start_background_job("matching", run_matching, progress_cb=_progress_cb):
                self._send_json({"error": "Ya hay una tarea en ejecución."}, status=409)
                return
            self._send_json({"status": "started"})
            return

        if path == "/api/start-batch":
            mode = body.get("mode", "import_photos_app")
            ym_filter = body.get("year_month", "ALL")
            kind_filter = body.get("kind", "all")
            batch_limit = int(body.get("batch_limit", 200))
            folder_chunk_size = int(body.get("folder_chunk_size", 500))
            custom_out = body.get("output_dir", "").strip() or None

            if not _start_background_job(
                "batch",
                run_batch,
                mode=mode,
                year_month_filter=ym_filter,
                kind_filter=kind_filter,
                batch_limit=batch_limit,
                folder_chunk_size=folder_chunk_size,
                custom_output_dir=custom_out,
                progress_cb=_progress_cb,
                should_pause_or_stop=_should_pause_or_stop,
            ):
                self._send_json({"error": "Ya hay una tarea en ejecución."}, status=409)
                return
            self._send_json({"status": "started"})
            return

        if path == "/api/job-control":
            action = body.get("action", "run")
            with _job_lock:
                _current_job["control"] = action
                if action == "pause" and _current_job["active"]:
                    _current_job["status"] = "paused"
                elif action == "run" and _current_job["active"]:
                    _current_job["status"] = "running"
            _add_log(f"Control de usuario: {action.upper()}")
            self._send_json({"status": "ok", "control": action})
            return

        self.send_error(404, "Endpoint not found")


def main():
    init_db()
    port = int(os.environ.get("PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"\n=======================================================")
    print(f" 🚀 Sincronizador Inteligente Google Fotos -> iCloud")
    print(f" 🌐 Panel de Control Web: http://localhost:{port}")
    print(f" 🔒 Modo 100% Seguro (Cero borrados en origen)")
    print(f"=======================================================\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
