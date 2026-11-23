"""Local browser UI.  Start with:  ./ytbulk.sh --ui   (Windows: ytbulk-ui.bat)
Listens on 127.0.0.1 only. Uses the same downloader / Drive uploader as the CLI."""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import gdrive
from .checks import run_checks
from .cli import QUALITIES, setup_logging
from .urls import parse_url_text

log = logging.getLogger("ytbulk")
HTML_FILE = Path(__file__).with_name("web_ui.html")
MAX_BODY = 5 * 1024 * 1024
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
DRIVE_FIELDS = ("drive_status", "drive_link", "drive_error", "drive_hint", "local_deleted")


def _checks(env) -> list[dict]:
    return [{"level": lv, "name": n, "message": m} for lv, n, m in env.lines]


class WebState:
    """Thread-safe progress state. Implements the same UI protocol as ui.ProgressUI."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = self._blank(0, "local")

    @staticmethod
    def _blank(total: int, dest: str) -> dict:
        return {"running": False, "stopping": False, "finished": False, "total": total, "dest": dest,
                "started_at": None, "ended_at": None, "elapsed": 0, "phase": "",
                "counts": {"downloaded": 0, "skipped": 0, "failed": 0},
                "drive_counts": {"uploaded": 0, "already": 0, "failed": 0, "not_uploaded": 0},
                "current": None, "upload": None, "results": {}, "summary": None, "reports": {},
                "output_dir": None, "drive_folder": None, "message": "", "error": None, "hint": None}

    def begin(self, total: int, output_dir: str, dest: str, drive_folder: dict | None) -> None:
        with self.lock:
            self.data = self._blank(total, dest)
            self.data.update(running=True, started_at=time.time(), output_dir=output_dir,
                             drive_folder=drive_folder, phase="downloading")

    def snapshot(self) -> dict:
        with self.lock:
            d = copy.deepcopy(self.data)
        now = d["ended_at"] or time.time()
        if d["started_at"]:
            d["elapsed"] = round(now - d["started_at"])
        if (u := d["upload"]) and u.get("t0"):
            dt = max(0.001, time.time() - u["t0"])
            u["speed"] = u["sent"] / dt if u["sent"] else None
            u["eta"] = (u["total"] - u["sent"]) / u["speed"] if u.get("speed") else None
        d["results"] = [d["results"][k] for k in sorted(d["results"])]
        return d

    def update(self, **kw) -> None:
        with self.lock:
            self.data.update(kw)

    def finish(self, summary=None, reports=None, message="", error=None, hint=None) -> None:
        with self.lock:
            self.data.update(running=False, stopping=False, finished=True, current=None, upload=None,
                             summary=summary, reports=reports or {}, message=message, error=error,
                             hint=hint, ended_at=time.time(), phase="")

    def _cur(self, **kw) -> None:
        with self.lock:
            if self.data["current"] is not None:
                self.data["current"].update(kw)

    # --- protocol used by BulkDownloader ---
    def video_start(self, index, url):
        with self.lock:
            self.data["current"] = {"index": index, "url": url, "title": None, "stage": "fetching",
                                    "done": 0, "total": None, "speed": None, "eta": None}

    def set_title(self, title):
        self._cur(title=title)

    def stage(self, text):
        self._cur(stage=text, speed=None, eta=None)

    def progress(self, filename, stage, done, total, speed, eta):
        self._cur(stage=stage, done=done, total=total, speed=speed, eta=eta)

    def video_done(self, index, res):
        with self.lock:
            if res.status in self.data["counts"]:
                self.data["counts"][res.status] += 1
            row = {"index": index, **asdict(res)}
            if self.data["dest"] == "drive" and row["drive_status"] is None and res.status in ("downloaded", "skipped"):
                row["drive_status"] = "queued"
            if row["drive_status"] == "already":
                self.data["drive_counts"]["already"] += 1
            self.data["results"][index] = row
            self.data["current"] = None

    # --- protocol used by gdrive.UploadWorker ---
    def upload_start(self, index, res, size):
        with self.lock:
            self.data["upload"] = {"index": index, "title": res.title or Path(res.file or "").name,
                                   "sent": 0, "total": size, "t0": time.time()}
            if index in self.data["results"]:
                self.data["results"][index]["drive_status"] = "uploading"

    def upload_progress(self, index, sent, total):
        with self.lock:
            if (u := self.data["upload"]) and u["index"] == index:
                u["sent"], u["total"] = sent, total

    def upload_done(self, index, res):
        with self.lock:
            st = res.drive_status
            if st in self.data["drive_counts"]:
                self.data["drive_counts"][st] += 1
            row = self.data["results"].get(index)
            if row is not None:
                row.update({k: getattr(res, k) for k in DRIVE_FIELDS})
            if (u := self.data["upload"]) and u["index"] == index:
                self.data["upload"] = None


class App:
    def __init__(self, base: argparse.Namespace):
        self.base = base
        self.state = WebState()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.dl = None
        self.worker = None
        self.report_paths: dict[str, str] = {}
        # Google Drive connection state
        self.drive_connecting = False
        self.drive_error: str | None = None
        self._account: dict | None = None

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def defaults(self) -> dict:
        b = self.base
        return {"output": str(Path(b.output).expanduser().resolve()), "quality": b.quality, "codec": b.codec,
                "safe_filenames": bool(b.safe_filenames), "sleep": b.sleep, "cookies": b.cookies or "",
                "cookies_from_browser": b.cookies_from_browser or "", "qualities": QUALITIES,
                "destination": "drive" if b.drive_folder else "local", "drive_folder": b.drive_folder or "",
                "delete_local": bool(b.delete_local)}

    # ---------------------------------------------------------------- Google Drive
    def drive_status(self) -> dict:
        st = {"available": gdrive.libs_available(), "client": gdrive.client_file().is_file(),
              "connected": False, "email": None, "free": None, "connecting": self.drive_connecting,
              "error": self.drive_error, "config_dir": str(gdrive.config_dir())}
        if not st["available"] or not st["client"] or self.drive_connecting:
            return st
        creds = gdrive.load_credentials()
        if not creds:
            if gdrive.token_file().is_file():
                st["error"] = st["error"] or "Google sign-in expired - click Connect again."
            return st
        st["connected"] = True
        if self._account is None:
            try:
                self._account = gdrive.DriveClient.from_credentials(creds).about()
            except gdrive.DriveError as e:
                st.update(connected=False, error=str(e))
                return st
        st.update(email=self._account.get("email"), free=self._account.get("free"))
        return st

    def drive_set_client(self, text: str) -> tuple[int, dict]:
        try:
            gdrive.save_client_secret(text)
        except ValueError as e:
            return 400, {"error": str(e)}
        self.drive_error, self._account = None, None
        gdrive.token_file().unlink(missing_ok=True)   # a token belongs to the old client
        return 200, self.drive_status()

    def drive_connect(self) -> tuple[int, dict]:
        if self.drive_connecting:
            return 200, {"ok": True}
        if not gdrive.client_file().is_file():
            return 400, {"error": "Add your Google client file first (step 1)."}
        self.drive_connecting, self.drive_error, self._account = True, None, None

        def work():
            try:
                gdrive.connect(timeout=300)
            except Exception as e:  # noqa: BLE001 - show any sign-in failure in the UI
                self.drive_error = f"Sign-in did not complete ({type(e).__name__}). Try Connect again."
                log.warning("Drive sign-in failed: %s", e)
            finally:
                self.drive_connecting = False

        threading.Thread(target=work, daemon=True, name="drive-connect").start()
        return 200, {"ok": True}

    def drive_disconnect(self) -> dict:
        gdrive.disconnect()
        self._account, self.drive_error = None, None
        return self.drive_status()

    def drive_browse(self, where: str) -> tuple[int, dict]:
        try:
            return 200, {"items": self._client().list_folders(where or "root")}
        except gdrive.DriveError as e:
            return 400, {"error": str(e), "hint": e.hint}

    def drive_mkdir(self, parent: str, name: str) -> tuple[int, dict]:
        if parent in ("shared", "drives", ""):
            return 400, {"error": "Open a folder first, then create the new folder inside it."}
        try:
            return 200, self._client().create_folder(parent, _check_new_name(name))
        except ValueError as e:
            return 400, {"error": str(e)}
        except gdrive.DriveError as e:
            return 400, {"error": str(e), "hint": e.hint}

    def _client(self):
        creds = gdrive.load_credentials()
        if not creds:
            raise gdrive.DriveError("Not connected to Google Drive.", "auth", "Click 'Connect Google account'.")
        return gdrive.DriveClient.from_credentials(creds)

    def drive_folder(self, url: str) -> tuple[int, dict]:
        fid = gdrive.parse_folder_id(url)
        if not fid:
            return 400, {"error": "That doesn't look like a Google Drive folder link."}
        try:
            info = self._client().folder_info(fid)
        except gdrive.DriveError as e:
            return 400, {"error": str(e), "hint": e.hint}
        if info["trashed"]:
            return 400, {"error": f"The folder '{info['name']}' is in the trash."}
        if not info["can_upload"]:
            return 400, {"error": f"The connected account can't add files to '{info['name']}'.",
                         "hint": "Ask the owner for Editor access, or connect a different account."}
        return 200, {"ok": True, **info}

    # ---------------------------------------------------------------- jobs
    def _settings_from(self, body: dict) -> argparse.Namespace:
        s = argparse.Namespace(**vars(self.base))
        out = str(body.get("output") or "").strip()
        if not out:
            raise ValueError("Choose a folder on this computer.")
        s.output = str(Path(out).expanduser().resolve())
        s.quality = str(body.get("quality", s.quality))
        if s.quality not in QUALITIES:
            raise ValueError(f"Quality must be one of {', '.join(QUALITIES)}.")
        s.codec = str(body.get("codec", s.codec))
        if s.codec not in ("any", "h264"):
            raise ValueError("Format must be 'any' or 'h264'.")
        s.safe_filenames = bool(body.get("safe_filenames", s.safe_filenames))
        try:
            s.sleep = max(0.0, float(body.get("sleep", s.sleep) or 0))
        except (TypeError, ValueError):
            raise ValueError("Pause between videos must be a number.") from None
        s.cookies = str(body.get("cookies") or "").strip() or None
        s.cookies_from_browser = str(body.get("cookies_from_browser") or "").strip() or None
        s.destination = "drive" if body.get("destination") == "drive" else "local"
        s.drive_folder = (str(body.get("drive_folder") or "").strip() or None) if s.destination == "drive" else None
        s.delete_local = bool(body.get("delete_local")) and s.destination == "drive"
        return s

    def start(self, body: dict) -> tuple[int, dict]:
        from .downloader import BulkDownloader

        with self.lock:
            if self.running():
                return 409, {"error": "A download is already running."}
            text = str(body.get("text") or "")
            parsed = parse_url_text(text)
            if not parsed.entries:
                return 400, {"error": "The list has no valid YouTube video URLs."}
            try:
                s = self._settings_from(body)
            except ValueError as e:
                return 400, {"error": str(e)}

            client, folder = None, None
            if s.destination == "drive":
                if not gdrive.libs_available():
                    return 400, {"error": "Google Drive support is not installed - run setup again."}
                code, folder = self.drive_folder(s.drive_folder or "")
                if code != 200:
                    return code, folder
                client = self._client()

            env = run_checks(s.output, None, s.ffmpeg, s.cookies)
            if not env.ok:
                return 400, {"error": "Environment check failed - see the checks panel.", "checks": _checks(env)}

            rdir = Path(s.output) / "_reports"
            rdir.mkdir(parents=True, exist_ok=True)
            inp = rdir / f"input-{time.strftime('%Y%m%d-%H%M%S')}.txt"   # keep what was uploaded
            inp.write_text(text, encoding="utf-8")
            s.input = str(inp)

            self.state.begin(len(parsed.entries), s.output, s.destination, folder)
            self.dl = BulkDownloader(s, env.ffmpeg, env.js_runtimes, self.state)
            self.worker = gdrive.attach(self.dl, client, folder["id"], self.state, s.delete_local) if client else None
            self.thread = threading.Thread(target=self._run, args=(s, parsed), daemon=True)
            self.thread.start()
            return 200, {"ok": True, "total": len(parsed.entries)}

    def _run(self, s, parsed) -> None:
        from .report import build_summary, write_reports

        started = time.time()
        try:
            setup_logging(Path(s.output), bool(s.verbose))
            log.info("=== web run start: %d videos -> %s (quality=%s codec=%s dest=%s)",
                     len(parsed.entries), s.output, s.quality, s.codec, s.destination)
            results = self.dl.run(parsed.entries)
            if self.worker:
                self.state.update(phase="uploading")
                gdrive.wait(self.worker)
            summary = build_summary(parsed, results, s, started, self.dl.interrupted)
            paths = write_reports(summary, s.output)
            self.report_paths = {k: str(v) for k, v in paths.items()}
            hint = None
            if self.worker and self.worker.fatal:
                msg, hint = f"Stopped: {self.worker.fatal}", self.worker.fatal.hint
            elif self.dl.interrupted:
                msg = "Stopped. Press Start again to resume - finished videos are skipped."
            elif self.dl.fatal:
                msg = f"Stopped early: {self.dl.fatal.replace('_', ' ')}. Fix it and start again."
            else:
                msg = "Finished." if s.destination == "local" else "Finished - videos are in your Drive folder."
            self.state.finish(summary["counts"], {k: f"/api/report/{k}" for k in paths}, msg, hint=hint)
        except Exception as e:  # never leave the UI stuck in "running"
            log.exception("web run crashed")
            self.state.finish(error=f"{type(e).__name__}: {e}")

    def stop(self) -> dict:
        if self.running() and self.dl:
            self.dl.stop()                     # the upload worker shares this stop event
            self.state.update(stopping=True)
            return {"ok": True}
        return {"ok": False, "error": "Nothing is running."}


def _places() -> list[dict]:
    home = Path.home()
    out = [{"name": "Home", "path": str(home)}]
    for label in ("Downloads", "Videos", "Desktop", "Documents"):
        if (home / label).is_dir():
            out.append({"name": label, "path": str(home / label)})
    if os.name == "nt":
        out += [{"name": f"{c}:", "path": f"{c}:\\"} for c in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.isdir(f"{c}:\\")]
    else:
        out.append({"name": "Computer", "path": "/"})
        for media in (Path("/media") / home.name, Path("/mnt")):
            if media.is_dir():
                out.append({"name": "External drives" if media.parent.name == "media" else str(media),
                            "path": str(media)})
    return out


def fs_list(path: str | None, show_hidden: bool = False) -> tuple[int, dict]:
    """Folder picker for this computer (the page can't read real paths itself)."""
    p = Path(path).expanduser() if path else Path.home()
    try:
        p = p.resolve()
    except OSError:
        p = Path.home()
    while not p.is_dir() and p != p.parent:     # typed path doesn't exist yet: show nearest parent
        p = p.parent
    dirs, err = [], None
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir() and (show_hidden or not e.name.startswith((".", "$"))):
                        dirs.append(e.name)
                except OSError:
                    continue
    except OSError as e:
        err = f"Can't open this folder: {e.strerror or e}"
    crumbs, acc = [], None
    for part in p.parts:
        acc = Path(part) if acc is None else acc / part
        crumbs.append({"name": part.rstrip("\\/") or part, "path": str(acc)})
    return 200, {"path": str(p), "parent": str(p.parent) if p.parent != p else None,
                 "dirs": sorted(dirs, key=str.lower)[:2000], "crumbs": crumbs, "places": _places(),
                 "writable": os.access(p, os.W_OK), "error": err}


def _check_new_name(name: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or any(c in name for c in '/\\:*?"<>|') or len(name) > 120:
        raise ValueError("Use a simple folder name without / \\ : * ? \" < > |")
    return name


def fs_mkdir(path: str, name: str) -> tuple[int, dict]:
    try:
        target = Path(path).expanduser() / _check_new_name(name)
        target.mkdir(parents=False, exist_ok=True)
    except (ValueError, OSError) as e:
        return 400, {"error": str(e)}
    return fs_list(str(target))


def open_folder(path: str) -> tuple[int, dict]:
    p = Path(path or "").expanduser()
    if not p.is_dir():
        return 400, {"error": "That folder doesn't exist yet - it is created when the download starts."}
    try:
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return 500, {"error": f"Could not open the folder: {e}"}
    return 200, {"ok": True}


def _parse_payload(text: str) -> dict:
    p = parse_url_text(text)
    return {"total_found": p.total_found, "valid": len(p.entries), "sections": p.sections,
            "invalid": [{"line": n, "text": t} for n, t in p.invalid],
            "duplicates": [{"line": e.line, "url": e.raw, "video_id": e.video_id} for e in p.duplicates]}


class Handler(BaseHTTPRequestHandler):
    server_version = "ytbulk-ui"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _guard(self) -> bool:
        # Reject foreign Host headers (DNS-rebinding protection).
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in LOCAL_HOSTS:
            self._json(403, {"error": "forbidden"})
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        app: App = self.server.app
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(200, app.state.snapshot())
        elif path == "/api/defaults":
            self._json(200, app.defaults())
        elif path == "/api/check":
            env = run_checks(app.base.output, None, app.base.ffmpeg, None)
            self._json(200, {"ok": env.ok, "checks": _checks(env)})
        elif path == "/api/drive/status":
            self._json(200, app.drive_status())
        elif path.startswith("/api/report/"):
            p = app.report_paths.get(path.rsplit("/", 1)[-1])
            if not p or not Path(p).is_file():
                return self._json(404, {"error": "report not found"})
            ctype = "application/json" if p.endswith(".json") else "text/plain; charset=utf-8"
            self._send(200, Path(p).read_bytes(), ctype,
                       {"Content-Disposition": f'inline; filename="{Path(p).name}"'})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard():
            return
        # Requiring JSON forces a CORS preflight for cross-site requests, which we never approve.
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"error": "JSON body required"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "File too large (max 5 MB)."})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"error": "Bad JSON."})
        app: App = self.server.app
        path = urlparse(self.path).path
        routes = {
            "/api/parse": lambda: (200, _parse_payload(str(body.get("text") or ""))),
            "/api/start": lambda: app.start(body),
            "/api/stop": lambda: (200, app.stop()),
            "/api/open-folder": lambda: open_folder(str(body.get("path") or "")),
            "/api/fs/list": lambda: fs_list(body.get("path"), bool(body.get("hidden"))),
            "/api/fs/mkdir": lambda: fs_mkdir(str(body.get("path") or ""), str(body.get("name") or "")),
            "/api/drive/browse": lambda: app.drive_browse(str(body.get("id") or "root")),
            "/api/drive/mkdir": lambda: app.drive_mkdir(str(body.get("parent") or ""), str(body.get("name") or "")),
            "/api/drive/client": lambda: app.drive_set_client(str(body.get("text") or "")),
            "/api/drive/connect": app.drive_connect,
            "/api/drive/disconnect": lambda: (200, app.drive_disconnect()),
            "/api/drive/folder": lambda: app.drive_folder(str(body.get("url") or "")),
        }
        if path not in routes:
            return self._json(404, {"error": "not found"})
        try:
            code, obj = routes[path]()
        except Exception as e:  # noqa: BLE001 - never crash the request thread silently
            log.exception("API %s failed", path)
            code, obj = 500, {"error": f"{type(e).__name__}: {e}"}
        self._json(code, obj)


def serve(base: argparse.Namespace, port: int = 8765, open_browser: bool = True) -> int:
    app = App(base)
    httpd = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        print(f"error: no free port between {port} and {port + 19}")
        return 2
    httpd.daemon_threads = True
    httpd.app = app
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"ytbulk UI running at {url}\nKeep this terminal open. Press Ctrl+C here to quit.", flush=True)
    if open_browser:
        threading.Timer(0.7, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        if app.running():
            app.stop()
            app.thread.join(timeout=20)
    finally:
        httpd.server_close()
    return 0
