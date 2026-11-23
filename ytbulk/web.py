"""Local browser UI.  Start with:  ./ytbulk.sh --ui
Listens on 127.0.0.1 only. Uses the same downloader as the CLI."""
from __future__ import annotations

import argparse
import copy
import json
import logging
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .checks import run_checks
from .cli import QUALITIES, setup_logging
from .urls import parse_url_text

log = logging.getLogger("ytbulk")
HTML_FILE = Path(__file__).with_name("web_ui.html")
MAX_BODY = 5 * 1024 * 1024
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _checks(env) -> list[dict]:
    return [{"level": lv, "name": n, "message": m} for lv, n, m in env.lines]


class WebState:
    """Thread-safe progress state. Implements the same UI protocol as ui.ProgressUI."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = self._blank(0)

    @staticmethod
    def _blank(total: int) -> dict:
        return {"running": False, "stopping": False, "finished": False, "total": total,
                "started_at": None, "ended_at": None, "elapsed": 0,
                "counts": {"downloaded": 0, "skipped": 0, "failed": 0},
                "current": None, "results": [], "summary": None, "reports": {},
                "output_dir": None, "message": "", "error": None}

    def begin(self, total: int, output_dir: str) -> None:
        with self.lock:
            self.data = self._blank(total)
            self.data.update(running=True, started_at=time.time(), output_dir=output_dir)

    def snapshot(self) -> dict:
        with self.lock:
            d = copy.deepcopy(self.data)
        if d["started_at"]:
            d["elapsed"] = round((d["ended_at"] or time.time()) - d["started_at"])
        return d

    def set_stopping(self) -> None:
        with self.lock:
            self.data["stopping"] = True

    def finish(self, summary=None, reports=None, message="", error=None) -> None:
        with self.lock:
            self.data.update(running=False, stopping=False, finished=True, current=None, summary=summary,
                             reports=reports or {}, message=message, error=error, ended_at=time.time())

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
            self.data["results"].append({"index": index, **asdict(res)})
            self.data["current"] = None


class App:
    def __init__(self, base: argparse.Namespace):
        self.base = base
        self.state = WebState()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.dl = None
        self.report_paths: dict[str, str] = {}

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def defaults(self) -> dict:
        b = self.base
        return {"output": str(Path(b.output).expanduser().resolve()), "quality": b.quality, "codec": b.codec,
                "safe_filenames": bool(b.safe_filenames), "sleep": b.sleep, "cookies": b.cookies or "",
                "cookies_from_browser": b.cookies_from_browser or "", "qualities": QUALITIES}

    def _settings_from(self, body: dict) -> argparse.Namespace:
        s = argparse.Namespace(**vars(self.base))
        out = str(body.get("output") or "").strip()
        if not out:
            raise ValueError("Output folder is required.")
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
            env = run_checks(s.output, None, s.ffmpeg, s.cookies)
            if not env.ok:
                return 400, {"error": "Environment check failed - see the checks panel.", "checks": _checks(env)}

            rdir = Path(s.output) / "_reports"
            rdir.mkdir(parents=True, exist_ok=True)
            inp = rdir / f"input-{time.strftime('%Y%m%d-%H%M%S')}.txt"   # keep what was uploaded
            inp.write_text(text, encoding="utf-8")
            s.input = str(inp)

            self.state.begin(len(parsed.entries), s.output)
            self.dl = BulkDownloader(s, env.ffmpeg, env.js_runtimes, self.state)
            self.thread = threading.Thread(target=self._run, args=(s, parsed), daemon=True)
            self.thread.start()
            return 200, {"ok": True, "total": len(parsed.entries)}

    def _run(self, s, parsed) -> None:
        from .report import build_summary, write_reports

        started = time.time()
        try:
            setup_logging(Path(s.output), bool(s.verbose))
            log.info("=== web run start: %d videos -> %s (quality=%s codec=%s)",
                     len(parsed.entries), s.output, s.quality, s.codec)
            results = self.dl.run(parsed.entries)
            summary = build_summary(parsed, results, s, started, self.dl.interrupted)
            paths = write_reports(summary, s.output)
            self.report_paths = {k: str(v) for k, v in paths.items()}
            if self.dl.interrupted:
                msg = "Stopped. Press Start again to resume - finished videos are skipped."
            elif self.dl.fatal:
                msg = f"Stopped early: {self.dl.fatal.replace('_', ' ')}. Fix it and start again."
            else:
                msg = "Finished."
            self.state.finish(summary["counts"], {k: f"/api/report/{k}" for k in paths}, msg)
        except Exception as e:  # never leave the UI stuck in "running"
            log.exception("web run crashed")
            self.state.finish(error=f"{type(e).__name__}: {e}")

    def stop(self) -> dict:
        if self.running() and self.dl:
            self.dl.stop()
            self.state.set_stopping()
            return {"ok": True}
        return {"ok": False, "error": "Nothing is running."}


def _parse_payload(text: str) -> dict:
    p = parse_url_text(text)
    return {"total_found": p.total_found, "valid": len(p.entries),
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
        if path == "/api/parse":
            self._json(200, _parse_payload(str(body.get("text") or "")))
        elif path == "/api/start":
            code, obj = app.start(body)
            self._json(code, obj)
        elif path == "/api/stop":
            self._json(200, app.stop())
        else:
            self._json(404, {"error": "not found"})


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
