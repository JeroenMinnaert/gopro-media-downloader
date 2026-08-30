"""Rich progress: one overall bar plus one line per active worker."""

from __future__ import annotations

import threading

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)


class NullProgress:
    """Used with --quiet and in tests."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def start_file(self, name: str, size: int | None): return None
    def advance(self, handle, amount: int) -> None: ...
    def finish_file(self, handle, ok: bool = True) -> None: ...
    def advance_overall(self, files: int = 0, done_bytes: int = 0) -> None: ...
    def __enter__(self): return self
    def __exit__(self, *exc) -> None: ...


class DownloadProgress:
    def __init__(self, console: Console, total_files: int, total_bytes: int) -> None:
        self.console = console
        self._lock = threading.Lock()
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30),
            TaskProgressColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(compact=True),
            console=console,
            transient=False,
        )
        self.overall = self.progress.add_task(
            f"[bold]Overall[/bold] 0/{total_files} files", total=total_bytes or None
        )
        self.total_files = total_files
        self.files_done = 0

    def start(self) -> None:
        self.progress.start()

    def stop(self) -> None:
        self.progress.stop()

    def __enter__(self) -> "DownloadProgress":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start_file(self, name: str, size: int | None):
        short = name if len(name) <= 42 else "..." + name[-39:]
        with self._lock:
            return self.progress.add_task(short, total=size)

    def advance(self, handle, amount: int) -> None:
        if handle is None:
            return
        self.progress.advance(handle, amount)
        self.progress.advance(self.overall, amount)

    def finish_file(self, handle, ok: bool = True) -> None:
        if handle is None:
            return
        with self._lock:
            self.files_done += 1
            self.progress.remove_task(handle)
            self.progress.update(
                self.overall,
                description=f"[bold]Overall[/bold] {self.files_done}/{self.total_files} files",
            )

    def advance_overall(self, files: int = 0, done_bytes: int = 0) -> None:
        with self._lock:
            self.files_done += files
            if done_bytes:
                self.progress.advance(self.overall, done_bytes)
            self.progress.update(
                self.overall,
                description=f"[bold]Overall[/bold] {self.files_done}/{self.total_files} files",
            )
