"""yt-dlp wrapper: downloads videos one by one with retries, archive-based
skip logic, disk-space checks and error classification."""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .urls import Entry

log = logging.getLogger("ytbulk")

ARCHIVE_NAME = ".ytbulk-archive.json"
PARTIAL_DIR = ".partial"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SECRET_RE = re.compile(
    r"(?i)((?:set-)?cookie|authorization)(\s*[:=]\s*)[^\r\n]+"
    r"|\b(SAPISID|APISID|HSID|SSID|SID|LOGIN_INFO|__Secure-[\w-]+)=[^;\s]+"
)


def redact(text: str) -> str:
    """Strip colour codes and mask anything that looks like a cookie/auth token."""
    text = _ANSI_RE.sub("", str(text))
    return _SECRET_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}***" if m.group(1) else f"{m.group(3)}=***", text
    )


# (category, needles, hint) - first match wins, order matters.
_RULES = [
    ("disk_full", ("no space left", "errno 28", "disk full"),
     "Free up disk space or choose another --output directory, then re-run."),
    ("ffmpeg", ("ffmpeg is not installed", "ffmpeg not found", "ffprobe and ffmpeg not found",
                "ffmpeg could not be found"),
     "FFmpeg missing: re-run setup.bat / ./setup.sh (installs a bundled ffmpeg)."),
    ("private", ("private video", "video is private"),
     "Video is private. Make it unlisted, or pass --cookies from an account that can view it."),
    ("age_restricted", ("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"),
     "Age-restricted: pass --cookies / --cookies-from-browser for a signed-in account allowed to view it."),
    ("members_only", ("members-only", "members only", "join this channel"),
     "Members-only: requires cookies from an account with access."),
    ("bot_check", ("not a bot", "sign in to confirm"),
     "YouTube asked for sign-in (bot check). Pass --cookies, add --sleep, or retry later."),
    ("rate_limited", ("http error 429", "too many requests"),
     "Rate-limited by YouTube. Increase --sleep and retry later."),
    ("format", ("requested format is not available", "no video formats found"),
     "No format matched. Try --quality best, or update yt-dlp."),
    ("unavailable", ("video unavailable", "has been removed", "no longer available", "does not exist",
                     "not available", "is unavailable", "account associated with this video has been terminated",
                     "copyright claim", "incomplete youtube id"),
     "Video is deleted, blocked or unavailable."),
    ("network", ("timed out", "timeout", "connection", "network", "temporary failure", "name resolution",
                 "incompleteread", "incomplete read", "reset by peer", "unable to download",
                 "http error 5", "ssl", "remote end closed", "getaddrinfo", "content too short",
                 "did not get any data blocks"),
     "Network problem. Partial data is kept - re-run to resume."),
]
RETRYABLE = {"network", "rate_limited"}
FATAL = {"disk_full", "ffmpeg"}


def classify(message: str) -> tuple[str, str]:
    low = message.lower()
    for cat, needles, hint in _RULES:
        if any(n in low for n in needles):
            return cat, hint
    return "unknown", "See the log file for details; updating yt-dlp often fixes unknown errors."


@dataclass
class VideoResult:
    url: str
    video_id: str
    line: int
    status: str = "not_started"   # downloaded | skipped | failed | interrupted | not_started
    title: str | None = None
    file: str | None = None
    size_bytes: int | None = None
    resolution: str | None = None
    error_category: str | None = None
    error: str | None = None
    hint: str | None = None
    attempts: int = 0
    seconds: float | None = None


class Archive:
    """IDs of finished downloads -> file path. A video is skipped only if its
    recorded file still exists, so deleting a file triggers a re-download."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def lookup(self, vid: str) -> dict | None:
        rec = self.data.get(vid)
        if rec and Path(rec.get("file", "")).is_file():
            return rec
        return None

    def record(self, vid: str, rec: dict) -> None:
        self.data[vid] = rec
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


class _YdlLogger:
    def debug(self, msg):
        # yt-dlp sends normal status lines through debug(); real debug lines start with "[debug] "
        (log.debug if str(msg).startswith("[debug] ") else log.info)(redact(msg))

    def info(self, msg):
        log.info(redact(msg))

    def warning(self, msg):
        log.warning(redact(msg))

    def error(self, msg):
        log.error(redact(msg))


class _InsufficientSpace(Exception):
    pass


def _parse_rate(text: str) -> int:
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmg]?)i?b?\s*", text, re.I)
    if not m:
        raise ValueError(f"bad --rate-limit value: {text!r} (examples: 500K, 5M)")
    mult = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


class BulkDownloader:
    def __init__(self, settings, ffmpeg: str, js_runtimes: dict, ui):
        self.s = settings
        self.ffmpeg = ffmpeg
        self.js_runtimes = js_runtimes
        self.ui = ui
        self.out = Path(settings.output).resolve()
        self.archive = Archive(self.out / ARCHIVE_NAME)
        self.interrupted = False
        self.fatal: str | None = None
        self.stop_event = threading.Event()   # set() = stop asap (used by the web UI)

    def stop(self) -> None:
        self.stop_event.set()

    # ---- yt-dlp configuration -------------------------------------------
    def _format_sort(self) -> list[str]:
        res = "res" if str(self.s.quality) == "best" else f"res:{int(self.s.quality)}"
        if self.s.codec == "h264":
            # Max browser compatibility (H.264 + AAC). YouTube rarely offers H.264 above 1080p.
            return ["vcodec:h264", res, "acodec:aac", "ext:mp4:m4a"]
        return [res, "ext:mp4:m4a"]

    def ydl_params(self) -> dict:
        p = {
            "format": "bv*+ba/b",
            "format_sort": self._format_sort(),
            "merge_output_format": "mp4",
            "outtmpl": {"default": self.s.filename},
            "paths": {"home": str(self.out), "temp": str(self.out / PARTIAL_DIR)},
            "windowsfilenames": True,          # portable names on any filesystem
            "restrictfilenames": bool(self.s.safe_filenames),
            "trim_file_name": 120 if os.name == "nt" else 150,   # Windows 260-char path limit
            "noplaylist": True,
            "continuedl": True,                # resume .part files
            "nopart": False,
            "overwrites": False,
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 5,
            "extractor_retries": 3,
            "socket_timeout": 30,
            "ignoreerrors": False,
            "quiet": True,
            "noprogress": True,
            "logger": _YdlLogger(),
            "progress_hooks": [self._on_progress],
            "postprocessor_hooks": [self._on_postprocess],
            "ffmpeg_location": self.ffmpeg,
        }
        if self.js_runtimes:
            p["js_runtimes"] = self.js_runtimes
        if self.s.cookies:
            p["cookiefile"] = str(Path(self.s.cookies).expanduser())
        if self.s.cookies_from_browser:
            p["cookiesfrombrowser"] = tuple(self.s.cookies_from_browser.split(":", 1))
        if self.s.rate_limit:
            p["ratelimit"] = _parse_rate(self.s.rate_limit)
        return p

    # ---- hooks ------------------------------------------------------------
    def _on_progress(self, d: dict) -> None:
        if self.stop_event.is_set():
            raise KeyboardInterrupt        # unwinds yt-dlp; .part file is kept for resume
        if d.get("status") == "downloading":
            info = d.get("info_dict") or {}
            if info.get("vcodec") == "none":
                stage = "audio"
            elif info.get("acodec") == "none":
                stage = "video"
            else:
                stage = "download"
            self.ui.progress(
                d.get("filename"), stage, d.get("downloaded_bytes") or 0,
                d.get("total_bytes") or d.get("total_bytes_estimate"), d.get("speed"), d.get("eta"),
            )

    def _on_postprocess(self, d: dict) -> None:
        if d.get("status") == "started":
            pp = d.get("postprocessor", "")
            self.ui.stage("merging" if "Merger" in pp else "finishing")

    # ---- main loop ----------------------------------------------------------
    def run(self, entries: list[Entry]) -> list[VideoResult]:
        import yt_dlp

        self.results = results = [VideoResult(url=e.url, video_id=e.video_id, line=e.line) for e in entries]
        params = self.ydl_params()
        with yt_dlp.YoutubeDL(params) as ydl:
            for i, (entry, res) in enumerate(zip(entries, results), start=1):
                if self.fatal:
                    break
                if self.stop_event.is_set():
                    self.interrupted = True
                    break
                try:
                    rec = self.archive.lookup(entry.video_id)
                    if rec:
                        res.status, res.title, res.file = "skipped", rec.get("title"), rec.get("file")
                        res.error = "already downloaded"
                        self.ui.video_done(i, res)
                        continue
                    if i > 1 and self.s.sleep and self.stop_event.wait(self.s.sleep):
                        raise KeyboardInterrupt
                    self.ui.video_start(i, entry.url)
                    self._download_one(ydl, entry, res)
                except KeyboardInterrupt:
                    self.interrupted = True
                    if res.status == "not_started" and res.attempts:   # stopped mid-download
                        res.status = "interrupted"
                        res.error = "stopped by user (partial data kept - re-run to resume)"
                        self.ui.video_done(i, res)
                    break
                self.ui.video_done(i, res)
        return results

    def _download_one(self, ydl, entry: Entry, res: VideoResult) -> None:
        from yt_dlp.utils import YoutubeDLError

        t0 = time.monotonic()
        max_attempts = max(1, int(self.s.retries))
        for attempt in range(1, max_attempts + 1):
            res.attempts = attempt
            try:
                info = ydl.extract_info(entry.url, download=False)
                if not info:
                    raise YoutubeDLError("no information returned for this URL")
                res.title = info.get("title")
                self.ui.set_title(res.title or entry.video_id)
                self._check_space(info)
                info = ydl.process_ie_result(info, download=True)
                path = self._final_path(info)
                if not path or not path.is_file() or path.stat().st_size == 0:
                    raise YoutubeDLError(f"output file missing or empty: {path}")
                res.status, res.file = "downloaded", str(path)
                res.size_bytes = path.stat().st_size
                res.resolution = info.get("resolution") or (
                    f"{info.get('width')}x{info.get('height')}" if info.get("height") else None)
                res.error = res.error_category = res.hint = None
                res.seconds = round(time.monotonic() - t0, 1)
                self.archive.record(entry.video_id, {
                    "title": res.title, "file": res.file, "url": entry.url,
                    "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                log.info("OK %s -> %s", entry.url, res.file)
                return
            except _InsufficientSpace as e:
                self._fail(res, "disk_full", str(e), "Free up space; smaller videos will still be attempted.")
                return
            except (YoutubeDLError, OSError) as e:
                msg = redact(e.msg if hasattr(e, "msg") and e.msg else str(e))
                msg = re.sub(r"^ERROR:\s*", "", msg)
                if isinstance(e, OSError) and e.errno == errno.ENOSPC:
                    cat, hint = "disk_full", _RULES[0][2]
                else:
                    cat, hint = classify(msg)
                if cat in FATAL:
                    self.fatal = cat
                if cat in RETRYABLE and attempt < max_attempts:
                    wait = (30 if cat == "rate_limited" else 5) * attempt
                    log.warning("Attempt %d/%d failed for %s (%s): %s - retrying in %ds",
                                attempt, max_attempts, entry.url, cat, msg, wait)
                    self.ui.stage(f"retry {wait}s")
                    if self.stop_event.wait(wait):
                        raise KeyboardInterrupt
                    continue
                self._fail(res, cat, msg, hint)
                res.seconds = round(time.monotonic() - t0, 1)
                return

    def _fail(self, res: VideoResult, cat: str, msg: str, hint: str) -> None:
        res.status, res.error_category, res.error, res.hint = "failed", cat, msg, hint
        log.error("FAILED %s [%s]: %s", res.url, cat, msg)

    def _check_space(self, info: dict) -> None:
        fmts = info.get("requested_formats") or [info]
        est = sum((f.get("filesize") or f.get("filesize_approx") or 0) for f in fmts)
        if not est:
            return
        need = est * 2 + 200 * 1024**2      # parts + merged copy + safety margin
        free = shutil.disk_usage(self.out).free
        if free < need:
            from .checks import human_bytes
            raise _InsufficientSpace(
                f"needs ~{human_bytes(need)} (video ~{human_bytes(est)}), only {human_bytes(free)} free")

    @staticmethod
    def _final_path(info: dict) -> Path | None:
        for d in info.get("requested_downloads") or []:
            if d.get("filepath"):
                return Path(d["filepath"])
        fp = info.get("filepath") or info.get("_filename")
        return Path(fp) if fp else None
