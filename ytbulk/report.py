"""Summary report (JSON + text), re-runnable failed-URL list, and console summary."""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from .checks import human_bytes
from .urls import ParsedInput

REPORTS_DIR = "_reports"


def build_summary(parsed: ParsedInput, results: list, settings, started: float, interrupted: bool) -> dict:
    by = lambda s: [r for r in results if r.status == s]  # noqa: E731
    return {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(time.time() - started, 1),
        "interrupted": interrupted,
        "input_file": str(Path(settings.input).resolve()),
        "output_dir": str(Path(settings.output).resolve()),
        "settings": {"quality": settings.quality, "codec": settings.codec, "filename": settings.filename,
                     "safe_filenames": settings.safe_filenames, "cookies_used": bool(
                         settings.cookies or settings.cookies_from_browser)},
        "counts": {
            "total_urls_found": parsed.total_found,
            "unique_valid": len(parsed.entries),
            "invalid": len(parsed.invalid),
            "duplicates": len(parsed.duplicates),
            "downloaded": len(by("downloaded")),
            "skipped_already_downloaded": len(by("skipped")),
            "failed": len(by("failed")),
            "interrupted": len(by("interrupted")),
            "not_started": len(by("not_started")),
        },
        "downloaded": [asdict(r) for r in by("downloaded")],
        "skipped": [asdict(r) for r in by("skipped")],
        "failed": [asdict(r) for r in by("failed")],
        "interrupted": [asdict(r) for r in by("interrupted")],
        "not_started": [asdict(r) for r in by("not_started")],
        "invalid_lines": [{"line": n, "text": t} for n, t in parsed.invalid],
        "duplicate_lines": [{"line": e.line, "url": e.raw, "video_id": e.video_id} for e in parsed.duplicates],
    }


def write_reports(summary: dict, output_dir: str | Path) -> dict:
    rdir = Path(output_dir) / REPORTS_DIR
    rdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    paths = {"json": rdir / f"report-{stamp}.json", "text": rdir / f"report-{stamp}.txt"}
    paths["json"].write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["text"].write_text(_text_report(summary), encoding="utf-8")

    retry = summary["failed"] + summary["interrupted"] + summary["not_started"]
    if retry:
        paths["retry"] = rdir / f"failed-{stamp}.txt"
        paths["retry"].write_text(
            "# URLs that did not finish - use this file as --input to retry\n"
            + "".join(f"{r['url']}\n" for r in retry), encoding="utf-8")
    return paths


def _text_report(s: dict) -> str:
    c = s["counts"]
    out = [
        "YouTube bulk download report",
        "=" * 60,
        f"Started:   {s['started_at']}",
        f"Finished:  {s['finished_at']}  ({s['elapsed_seconds']}s){'  [INTERRUPTED]' if s['interrupted'] else ''}",
        f"Input:     {s['input_file']}",
        f"Output:    {s['output_dir']}",
        "",
        f"Total URLs found:            {c['total_urls_found']}",
        f"Unique valid URLs:           {c['unique_valid']}",
        f"Invalid lines:               {c['invalid']}",
        f"Duplicate URLs:              {c['duplicates']}",
        f"Downloaded:                  {c['downloaded']}",
        f"Skipped (already have):      {c['skipped_already_downloaded']}",
        f"Failed:                      {c['failed']}",
        f"Interrupted / not started:   {c['interrupted'] + c['not_started']}",
        "",
    ]
    if s["downloaded"] or s["skipped"]:
        out.append("FILES")
        for r in s["downloaded"] + s["skipped"]:
            tag = "new " if r["status"] == "downloaded" else "have"
            out.append(f"  [{tag}] {r['file']}")
            out.append(f"         {r['url']}")
        out.append("")
    if s["failed"]:
        out.append("FAILED")
        for r in s["failed"]:
            out.append(f"  {r['url']}  (line {r['line']})")
            out.append(f"    {r['error_category']}: {r['error']}")
            if r.get("hint"):
                out.append(f"    hint: {r['hint']}")
        out.append("")
    if s["invalid_lines"]:
        out.append("INVALID LINES")
        out += [f"  line {x['line']}: {x['text']}" for x in s["invalid_lines"]]
        out.append("")
    if s["duplicate_lines"]:
        out.append("DUPLICATES (ignored)")
        out += [f"  line {x['line']}: {x['url']}" for x in s["duplicate_lines"]]
        out.append("")
    return "\n".join(out)


def print_summary(console, summary: dict, paths: dict) -> None:
    from rich.table import Table

    c = summary["counts"]
    t = Table(title="Summary", show_header=False, title_style="bold")
    t.add_column(style="bold")
    t.add_column(justify="right")
    t.add_row("Total URLs found", str(c["total_urls_found"]))
    t.add_row("Unique valid", str(c["unique_valid"]))
    t.add_row("Invalid lines", str(c["invalid"]))
    t.add_row("Duplicates", str(c["duplicates"]))
    t.add_row("[green]Downloaded", f"[green]{c['downloaded']}")
    t.add_row("[yellow]Skipped (already have)", f"[yellow]{c['skipped_already_downloaded']}")
    t.add_row("[red]Failed", f"[red]{c['failed']}")
    if c["interrupted"] or c["not_started"]:
        t.add_row("Interrupted / not started", str(c["interrupted"] + c["not_started"]))
    console.print(t)
    console.print(f"Videos:  {summary['output_dir']}")
    console.print(f"Report:  {paths['text']}")
    if "retry" in paths:
        console.print(f"[yellow]Retry list:[/] {paths['retry']}  "
                      f"[dim](run again with --input pointing at it)[/]")
