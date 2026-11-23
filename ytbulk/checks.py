"""Pre-flight environment checks. Uses only the standard library so it can run
(and explain what is missing) even before dependencies are installed."""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

IS_WIN = os.name == "nt"
SETUP_CMD = "setup.bat" if IS_WIN else "./setup.sh"
PIP_CMD = r".venv\Scripts\python -m pip" if IS_WIN else ".venv/bin/pip"
HARD_MIN_FREE = 500 * 1024**2     # refuse to start below this
WARN_MIN_FREE = 5 * 1024**3       # warn below this
YTDLP_MAX_AGE_DAYS = 60           # YouTube changes often; old yt-dlp breaks


@dataclass
class EnvReport:
    ok: bool = True
    ffmpeg: str | None = None
    js_runtimes: dict = field(default_factory=dict)
    lines: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, level: str, name: str, msg: str) -> None:
        self.lines.append((level, name, msg))
        if level == "FAIL":
            self.ok = False

    def print(self) -> None:
        marks = {"OK": "\033[32m OK \033[0m", "WARN": "\033[33mWARN\033[0m", "FAIL": "\033[31mFAIL\033[0m"}
        if not sys.stdout.isatty() or (IS_WIN and "WT_SESSION" not in os.environ):
            marks = {k: f"{k:^4}" for k in marks}   # plain text (old Windows consoles show raw ANSI)
        print("Environment checks:")
        for level, name, msg in self.lines:
            print(f"  [{marks[level]}] {name:<14} {msg}")
        print()


def human_bytes(n: float | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def find_ffmpeg(explicit: str | None = None) -> tuple[str | None, str | None]:
    """Explicit path > system ffmpeg > ffmpeg bundled with imageio-ffmpeg."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if sys_ff := shutil.which("ffmpeg"):
        candidates.append(sys_ff)
    try:
        import imageio_ffmpeg  # type: ignore
        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    for c in candidates:
        try:
            out = subprocess.run([c, "-version"], capture_output=True, text=True, timeout=15)
            if out.returncode == 0:
                return c, (out.stdout.splitlines() or ["ffmpeg"])[0]
        except (OSError, subprocess.SubprocessError):
            continue
    return None, None


def find_js_runtimes() -> dict:
    """yt-dlp needs a JS runtime (deno/node/bun) to solve YouTube's player challenges.
    The venv's own bin/Scripts dir is searched first (pip-installed deno lives there)."""
    search = os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")])
    found = {}
    for name in ("deno", "node", "bun"):
        if path := shutil.which(name, path=search):
            found[name] = {"path": path}
    return found


def run_checks(output_dir: str | Path, input_file: str | Path | None = None,
               ffmpeg: str | None = None, cookies: str | None = None) -> EnvReport:
    r = EnvReport()

    # Python
    if sys.version_info < (3, 10):
        r.add("FAIL", "python", f"{sys.version.split()[0]} - need 3.10+")
    else:
        r.add("OK", "python", sys.version.split()[0])

    # yt-dlp
    try:
        import yt_dlp.version as ytv  # type: ignore
        ver = ytv.__version__
        try:
            age = (dt.date.today() - dt.date(*map(int, ver.split(".")[:3]))).days
        except ValueError:
            age = 0
        if age > YTDLP_MAX_AGE_DAYS:
            r.add("WARN", "yt-dlp", f"{ver} is {age} days old - update: {PIP_CMD} install -U -r requirements.txt")
        else:
            r.add("OK", "yt-dlp", ver)
    except ImportError:
        r.add("FAIL", "yt-dlp", f"not installed - run {SETUP_CMD}")

    try:
        import rich  # type: ignore # noqa: F401
        r.add("OK", "rich", "installed")
    except ImportError:
        r.add("FAIL", "rich", f"not installed - run {SETUP_CMD}")

    # FFmpeg (needed to merge separate video+audio streams into MP4)
    path, ver = find_ffmpeg(ffmpeg)
    if path:
        r.ffmpeg = path
        r.add("OK", "ffmpeg", f"{ver[:60]} ({path})")
    else:
        r.add("FAIL", "ffmpeg", f"not found - re-run {SETUP_CMD} (installs a bundled ffmpeg)"
                                + ("" if IS_WIN else " or: sudo apt install ffmpeg"))

    # JS runtime
    r.js_runtimes = find_js_runtimes()
    if r.js_runtimes:
        r.add("OK", "js runtime", ", ".join(f"{k} ({v['path']})" for k, v in r.js_runtimes.items()))
    else:
        r.add("WARN", "js runtime", "none of deno/node/bun found - YouTube may offer only low-quality formats "
                                    f"or fail. Re-run {SETUP_CMD} (installs deno) or install Node.js 20+")

    # Input file
    if input_file is not None:
        p = Path(input_file)
        if p.is_file() and os.access(p, os.R_OK):
            r.add("OK", "input file", str(p))
        else:
            r.add("FAIL", "input file", f"{p} not found or not readable")

    # Cookies (never print contents)
    if cookies:
        cp = Path(cookies)
        if not cp.is_file():
            r.add("FAIL", "cookies", f"{cp} not found")
        elif not IS_WIN and cp.stat().st_mode & 0o077:
            r.add("WARN", "cookies", f"file is readable by other users - run: chmod 600 {cp}")
        else:
            r.add("OK", "cookies", "file provided (contents never logged)")

    # Output dir: exists/writable + free space
    out = Path(output_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".ytbulk-write-test"
        probe.write_bytes(b"ok")
        probe.unlink()
        free = shutil.disk_usage(out).free
        if free < HARD_MIN_FREE:
            r.add("FAIL", "disk space", f"only {human_bytes(free)} free in {out}")
        elif free < WARN_MIN_FREE:
            r.add("WARN", "disk space", f"only {human_bytes(free)} free in {out}")
        else:
            r.add("OK", "output dir", f"{out.resolve()} ({human_bytes(free)} free)")
    except OSError as e:
        r.add("FAIL", "output dir", f"{out}: {e}")

    return r
