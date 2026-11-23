"""Command-line entry point."""
from __future__ import annotations

import argparse
import logging
import sys
import time

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None
from pathlib import Path

from . import __version__
from .checks import run_checks
from .urls import parse_url_file

DEFAULTS = {
    "input": "videos.txt",
    "output": "downloads",
    "quality": "best",
    "codec": "any",
    "filename": "%(title)s [%(id)s].%(ext)s",
    "safe_filenames": False,
    "cookies": None,
    "cookies_from_browser": None,
    "retries": 3,
    "sleep": 2.0,
    "rate_limit": None,
    "ffmpeg": None,
    "verbose": False,
}
QUALITIES = ["best", "2160", "1440", "1080", "720", "480", "360"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ytbulk",
        description="Bulk-download YouTube videos you own (public or unlisted) from a text file of URLs.",
        epilog="Press Ctrl+C to stop; partial downloads are kept and resume on the next run.",
    )
    p.add_argument("-i", "--input", help="text file with one URL per line (default: videos.txt)")
    p.add_argument("-o", "--output", help="output directory (default: downloads)")
    p.add_argument("-q", "--quality", choices=QUALITIES, help="max resolution (default: best)")
    p.add_argument("--codec", choices=["any", "h264"],
                   help="any = highest quality (may be AV1/VP9 in MP4); "
                        "h264 = most browser-compatible MP4 (usually max 1080p). default: any")
    p.add_argument("--filename", help="yt-dlp output template (default: '%%(title)s [%%(id)s].%%(ext)s')")
    p.add_argument("--safe-filenames", action="store_true", default=None,
                   help="ASCII-only names, no spaces (handy for web URLs)")
    p.add_argument("--cookies", help="Netscape cookies.txt for age-restricted/private videos you can access")
    p.add_argument("--cookies-from-browser", metavar="BROWSER[:PROFILE]",
                   help="read cookies from a local browser, e.g. firefox or chrome")
    p.add_argument("--retries", type=int, help="attempts per video on network errors (default: 3)")
    p.add_argument("--sleep", type=float, help="seconds to wait between videos (default: 2)")
    p.add_argument("--rate-limit", help="max download speed, e.g. 5M or 800K")
    p.add_argument("--ffmpeg", help="path to ffmpeg binary (default: auto-detect)")
    p.add_argument("-c", "--config", help="TOML config file (default: ./config.toml if present)")
    p.add_argument("--ui", action="store_true", help="open the browser UI instead of downloading from the terminal")
    p.add_argument("--port", type=int, default=8765, help="port for --ui (default: 8765, localhost only)")
    p.add_argument("--no-browser", action="store_true", help="with --ui: don't open a browser automatically")
    p.add_argument("--check", action="store_true", help="only run environment checks")
    p.add_argument("--dry-run", action="store_true", help="parse the input file and list what would be downloaded")
    p.add_argument("-v", "--verbose", action="store_true", default=None, help="debug-level log file")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def load_settings(argv=None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    cfg_path = Path(args.config) if args.config else Path("config.toml")
    cfg = {}
    if cfg_path.is_file():
        if tomllib is None:
            build_parser().error("config.toml needs Python 3.11+; use command-line flags instead")
        with cfg_path.open("rb") as f:
            raw = tomllib.load(f)
        for k, v in raw.items():
            key = k.replace("-", "_")
            if key in DEFAULTS:
                cfg[key] = v
            else:
                print(f"warning: unknown config key '{k}' in {cfg_path} (ignored)", file=sys.stderr)
    elif args.config:
        build_parser().error(f"config file not found: {cfg_path}")
    for key, default in DEFAULTS.items():       # precedence: CLI > config file > defaults
        if getattr(args, key, None) is None:
            setattr(args, key, cfg.get(key, default))
    args.quality = str(args.quality)
    if args.quality not in QUALITIES:
        build_parser().error(f"quality must be one of {QUALITIES}")
    if args.codec not in ("any", "h264"):
        build_parser().error("codec must be 'any' or 'h264'")
    args.config_used = str(cfg_path) if cfg else None
    return args


def setup_logging(output: Path, verbose: bool) -> Path:
    from .downloader import redact

    class _Redact(logging.Filter):
        def filter(self, record):
            record.msg, record.args = redact(record.getMessage()), None
            return True

    log_dir = output / "_reports"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "ytbulk.log"
    h = logging.FileHandler(log_file, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    h.addFilter(_Redact())
    logger = logging.getLogger("ytbulk")
    for old in list(logger.handlers):
        logger.removeHandler(old)
        old.close()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(h)
    logger.propagate = False
    return log_file


def main(argv=None) -> int:
    s = load_settings(argv)
    if s.config_used:
        print(f"Using config: {s.config_used}")

    if s.ui:
        from .web import serve
        return serve(s, s.port, open_browser=not s.no_browser)

    if s.check:
        env = run_checks(s.output, s.input if Path(s.input).exists() else None, s.ffmpeg, s.cookies)
        env.print()
        return 0 if env.ok else 2

    if not Path(s.input).is_file():
        print(f"error: input file not found: {s.input}  (copy videos.example.txt to videos.txt)", file=sys.stderr)
        return 2
    parsed = parse_url_file(s.input)
    print(f"Input: {s.input} - {parsed.total_found} URL(s) found, {len(parsed.entries)} unique valid, "
          f"{len(parsed.invalid)} invalid, {len(parsed.duplicates)} duplicate(s)")
    for n, text in parsed.invalid:
        print(f"  invalid   line {n}: {text}")
    for e in parsed.duplicates:
        print(f"  duplicate line {e.line}: {e.raw}  (video {e.video_id})")

    if s.dry_run:
        for e in parsed.entries:
            print(f"  would get line {e.line}: {e.url}")
        return 0
    if not parsed.entries:
        print("Nothing to download.")
        return 1

    env = run_checks(s.output, s.input, s.ffmpeg, s.cookies)
    env.print()
    if not env.ok:
        print("Fix the FAIL items above and try again.", file=sys.stderr)
        return 2

    from rich.console import Console

    from .downloader import BulkDownloader
    from .report import build_summary, print_summary, write_reports
    from .ui import ProgressUI

    out = Path(s.output).resolve()
    log_file = setup_logging(out, bool(s.verbose))
    logging.getLogger("ytbulk").info("=== run start: %d videos -> %s (quality=%s codec=%s)",
                                     len(parsed.entries), out, s.quality, s.codec)
    console = Console()
    started = time.time()
    with ProgressUI(console, len(parsed.entries)) as ui:
        dl = BulkDownloader(s, env.ffmpeg, env.js_runtimes, ui)
        try:
            results = dl.run(parsed.entries)
        except KeyboardInterrupt:      # Ctrl+C between videos
            dl.interrupted = True
            results = getattr(dl, "results", None) or []
    console.line()
    if not results:   # interrupted before any result objects existed
        from .downloader import VideoResult
        results = [VideoResult(url=e.url, video_id=e.video_id, line=e.line) for e in parsed.entries]

    if dl.fatal:
        console.print(f"[bold red]Stopped early: fatal error ({dl.fatal}). Remaining videos were not started.")
    if dl.interrupted:
        console.print("[yellow]Stopped by user. Re-run the same command to resume.")

    summary = build_summary(parsed, results, s, started, dl.interrupted)
    paths = write_reports(summary, out)
    console.print()
    print_summary(console, summary, paths)
    console.print(f"Log:     {log_file}")

    if dl.interrupted:
        return 130
    return 1 if summary["counts"]["failed"] or dl.fatal else 0
