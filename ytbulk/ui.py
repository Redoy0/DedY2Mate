"""Terminal progress display (rich): overall bar + current-video bar + result lines."""
from __future__ import annotations

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.progress import (BarColumn, DownloadColumn, MofNCompleteColumn, Progress,
                           TaskProgressColumn, TextColumn, TimeElapsedColumn, TransferSpeedColumn)

from .checks import human_bytes


def _fmt_eta(eta) -> str:
    if eta is None:
        return "--:--"
    eta = int(eta)
    h, rem = divmod(eta, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _short(text: str, width: int = 48) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


class ProgressUI:
    def __init__(self, console: Console, total: int):
        self.console = console
        self.total = total
        self.counts = {"downloaded": 0, "skipped": 0, "failed": 0, "uploaded": 0}
        self.overall = Progress(
            TextColumn("[bold cyan]Overall "), BarColumn(), MofNCompleteColumn(),
            TextColumn("{task.fields[stats]}"), TimeElapsedColumn(), console=console,
        )
        self.current = Progress(
            TextColumn("[magenta]{task.fields[stage]:>9}"),
            TextColumn("{task.description}", markup=False),
            BarColumn(), TaskProgressColumn(), DownloadColumn(),
            TextColumn("[green]{task.fields[speed]:>11}"),
            TextColumn("ETA {task.fields[eta]}"), console=console,
        )
        self.upload = Progress(
            TextColumn("[blue]{task.fields[stage]:>9}"),
            TextColumn("{task.description}", markup=False),
            BarColumn(), TaskProgressColumn(), DownloadColumn(), TransferSpeedColumn(), console=console,
        )
        self.o_task = self.overall.add_task("all", total=total, stats=self._stats())
        self.c_task = self.current.add_task("waiting", total=None, stage="", speed="", eta="--:--")
        self.u_task = self.upload.add_task("", total=None, stage="", visible=False)
        self.live = Live(Group(self.overall, self.current, self.upload), console=console, refresh_per_second=8)
        self._file = None

    def _stats(self) -> str:
        c = self.counts
        up = f" [blue]☁ {c['uploaded']}[/]" if c["uploaded"] else ""
        return f"[green]✓ {c['downloaded']}[/] [yellow]↷ {c['skipped']}[/] [red]✗ {c['failed']}[/]{up}"

    def __enter__(self):
        self.live.__enter__()
        return self

    def __exit__(self, *exc):
        self.current.update(self.c_task, visible=False)
        self.upload.update(self.u_task, visible=False)
        return self.live.__exit__(*exc)

    # -- called by the downloader --------------------------------------------
    def video_start(self, index: int, url: str) -> None:
        self._file = None
        self.current.reset(self.c_task, total=None, completed=0,
                           description=f"[{index}/{self.total}] {url}", stage="fetching", speed="", eta="--:--")

    def set_title(self, title: str) -> None:
        desc = self.current.tasks[0].description.split("] ", 1)[0] + "] "
        self.current.update(self.c_task, description=desc + _short(title))

    def stage(self, text: str) -> None:
        self.current.update(self.c_task, stage=text, speed="", eta="--:--")

    def progress(self, filename, stage, done, total, speed, eta) -> None:
        if filename != self._file:        # new stream (video, then audio) - restart bar
            self._file = filename
            self.current.reset(self.c_task, total=total, completed=0)
        self.current.update(self.c_task, total=total, completed=done, stage=stage,
                            speed=f"{human_bytes(speed)}/s" if speed else "", eta=_fmt_eta(eta))

    def video_done(self, index: int, res) -> None:
        name = escape(res.title or res.url)
        if res.status == "downloaded":
            self.counts["downloaded"] += 1
            self.console.print(f"[green]✓[/] [{index}/{self.total}] {name} "
                               f"[dim]({res.resolution or '?'}, {human_bytes(res.size_bytes)})[/]")
        elif res.status == "skipped":
            self.counts["skipped"] += 1
            self.console.print(f"[yellow]↷[/] [{index}/{self.total}] {name} [dim]({escape(res.error or 'skipped')})[/]")
        elif res.status == "failed":
            self.counts["failed"] += 1
            self.console.print(f"[red]✗[/] [{index}/{self.total}] {escape(res.url)} "
                               f"[red]{res.error_category}[/]: {escape(res.error or '')}")
            if res.hint:
                self.console.print(f"    [dim]hint: {escape(res.hint)}[/]")
        elif res.status == "interrupted":
            self.console.print(f"[yellow]■[/] [{index}/{self.total}] {name} [yellow]stopped[/] "
                               "[dim](partial data kept - re-run to resume)[/]")
        if res.status != "interrupted":
            self.overall.update(self.o_task, advance=1, stats=self._stats())
        self.current.reset(self.c_task, total=None, completed=0, description="", stage="", speed="", eta="")

    # -- called by the Google Drive upload worker (another thread) -------------
    def upload_start(self, index: int, res, size: int) -> None:
        self.upload.reset(self.u_task, total=size, completed=0, visible=True, stage="☁ upload",
                          description=f"[{index}/{self.total}] {_short(res.title or res.url)}")

    def upload_progress(self, index: int, sent: int, total: int) -> None:
        self.upload.update(self.u_task, completed=sent, total=total)

    def upload_done(self, index: int, res) -> None:
        name = escape(res.title or res.url)
        st = res.drive_status
        if st == "uploaded":
            self.counts["uploaded"] += 1
            self.console.print(f"[blue]☁[/] [{index}/{self.total}] {name} [dim]uploaded to Drive"
                               + (", local copy deleted" if res.local_deleted else "") + "[/]")
        elif st == "already":
            self.console.print(f"[blue]☁[/] [{index}/{self.total}] {name} [dim]already on Drive[/]")
        elif st == "failed":
            self.console.print(f"[red]☁✗[/] [{index}/{self.total}] {name} [red]Drive upload failed[/]: "
                               f"{escape(res.drive_error or '')}")
            if res.drive_hint:
                self.console.print(f"    [dim]hint: {escape(res.drive_hint)}[/]")
        self.overall.update(self.o_task, stats=self._stats())
        self.upload.update(self.u_task, visible=False)
