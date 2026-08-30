"""Orchestration: enumerate, plan, then download with a small worker pool.

Workers pull whole media items off a queue (an item may fan out into several
chapter files). When the bearer token dies, the worker that noticed puts its
item back, trips the gate, and parks; the main thread runs the refresh prompt
and releases everyone. Nothing is lost and nothing is re-downloaded.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field

from rich.console import Console

from .api import ApiError, AuthExpired, GoProClient
from .auth import AuthGate, TokenProvider, refresh_token_interactively
from .config import Config
from .downloader import Downloader, FileOutcome, ShuttingDown
from .logging_setup import log_event
from .manifest import Manifest
from .models import MediaItem
from .paths import CaptureDateError, date_folder


@dataclass
class SyncStats:
    items_seen: int = 0
    items_new: int = 0
    items_skipped: int = 0
    files_done: int = 0
    files_failed: int = 0
    files_skipped: int = 0
    bytes_downloaded: int = 0
    tz_warnings: dict[str, int] = field(default_factory=dict)
    size_mismatches: list = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


def refresh_manifest(
    client: GoProClient,
    manifest: Manifest,
    config: Config,
    console: Console,
    stats: SyncStats,
) -> None:
    """Page through the library and bring the manifest up to date."""
    console.print("[bold]Enumerating media library...[/bold]")
    seen_before = {row["id"] for row in manifest.pending_items(max_attempts=10**9)}

    def on_page(page: int, total: int) -> None:
        console.print(f"  page {page}/{total}", highlight=False)

    for item in client.iter_media(types=config.types, on_page=on_page):
        stats.items_seen += 1
        skip = item.skip_reason()
        folder = "0000-00-00"
        if not skip:
            try:
                folder, warning = date_folder(
                    item.captured_at,
                    item.captured_at_timezone,
                    config.fallback_timezone,
                )
                if warning:
                    stats.tz_warnings[warning] = stats.tz_warnings.get(warning, 0) + 1
            except CaptureDateError as exc:
                skip = f"bad_captured_at: {exc}"
        if skip:
            stats.items_skipped += 1
        if item.id and item.id not in seen_before:
            stats.items_new += 1
        if item.id:
            manifest.upsert_item(item, folder, skip)

    manifest.set_meta("last_full_sync", str(int(time.time())))
    log_event(
        logging.INFO,
        "manifest_refreshed",
        items_seen=stats.items_seen,
        items_skipped=stats.items_skipped,
    )


class DownloadRunner:
    def __init__(
        self,
        client: GoProClient,
        manifest: Manifest,
        config: Config,
        console: Console,
        tokens: TokenProvider,
        gate: AuthGate,
        shutdown: threading.Event,
        progress,
        stats: SyncStats,
        max_attempts: int = 4,
    ) -> None:
        self.client = client
        self.manifest = manifest
        self.config = config
        self.console = console
        self.tokens = tokens
        self.gate = gate
        self.shutdown = shutdown
        self.progress = progress
        self.stats = stats
        self.max_attempts = max_attempts

        self.queue: queue.Queue = queue.Queue()
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._stats_lock = threading.Lock()

        self.downloader = Downloader(
            client=client,
            manifest=manifest,
            dest=config.dest,
            shutdown=shutdown,
            on_progress=self._on_progress,
            on_file_start=self._on_file_start,
            on_file_end=self._on_file_end,
        )

    # -- progress bridge ---------------------------------------------------

    def _on_file_start(self, relpath: str, size: int | None):
        return self.progress.start_file(relpath, size)

    def _on_progress(self, handle, amount: int) -> None:
        self.progress.advance(handle, amount)

    def _on_file_end(self, handle, outcome: FileOutcome) -> None:
        self.progress.finish_file(handle, ok=outcome.state == "done")

    # -- worker ------------------------------------------------------------

    def _process_item(self, row) -> None:
        item = MediaItem.from_json(json.loads(row["raw_json"]))
        folder = row["date_folder"]

        if not self.manifest.claim_item(item.id):
            return  # another worker has it, or it finished since we queued it

        try:
            files, skip = self.downloader.resolve(item, folder)
            if skip:
                self.manifest.set_item_state(item.id, "skipped", skip)
                with self._stats_lock:
                    self.stats.items_skipped += 1
                log_event(logging.INFO, "item_skipped", media_id=item.id, reason=skip)
                return

            for source in files:
                if self.shutdown.is_set():
                    raise ShuttingDown()
                file_row = self.manifest.get_file(item.id, source.item_number)
                if file_row is None or file_row["state"] in ("done", "skipped"):
                    continue
                if file_row["attempts"] >= self.max_attempts:
                    continue
                if not self.manifest.claim_file(file_row["id"]):
                    continue

                outcome = self.downloader.fetch_file(item, source, file_row, folder)
                self._record(file_row, outcome)

            if self.manifest.refresh_item_state(item.id) == "done":
                self._check_item_total(item)
        except (AuthExpired, ShuttingDown):
            self.manifest.release_item(item.id, charge_attempt=False)
            raise
        except (ApiError, OSError, ValueError) as exc:
            self.manifest.mark_item_failed(item.id, str(exc))
            log_event(logging.ERROR, "item_failed", media_id=item.id, error=str(exc))

    def _check_item_total(self, item: MediaItem) -> None:
        """The chapters of a recording must sum to the size the listing gave.

        This is the safety net that catches a whole chapter going missing --
        each individual file can verify fine while the recording is incomplete.
        """
        on_disk, listed = self.manifest.item_byte_total(item.id)
        if not listed or not on_disk or on_disk == listed:
            return
        message = f"item total {on_disk} != listed {listed}"
        with self._stats_lock:
            self.stats.size_mismatches.append((item.id, item.filename, on_disk, listed))
        log_event(
            logging.ERROR,
            "item_total_mismatch",
            media_id=item.id,
            filename=item.filename,
            on_disk=on_disk,
            listed=listed,
        )
        self.manifest.set_item_state(item.id, "failed")
        self.manifest.mark_item_failed(item.id, message)

    def _record(self, file_row, outcome: FileOutcome) -> None:
        file_id = file_row["id"]
        path = file_row["target_path"]
        if outcome.state == "done":
            self.manifest.mark_done(
                file_id,
                outcome.size,
                size_unverified=outcome.reason == "size_unverified",
                checksum=outcome.checksum,
                checksum_algo="s3-etag" if outcome.checksum else None,
                checksum_state=outcome.checksum_state,
            )
            with self._stats_lock:
                self.stats.files_done += 1
                self.stats.bytes_downloaded += outcome.bytes_written
            log_event(
                logging.INFO,
                "file_done",
                path=path,
                size=outcome.size,
                checksum_state=outcome.checksum_state,
                note=outcome.reason or None,
            )
            if outcome.reason == "already_on_disk":
                self.progress.advance_overall(done_bytes=outcome.size)
        elif outcome.state == "skipped":
            self.manifest.mark_skipped(file_id, outcome.reason)
            with self._stats_lock:
                self.stats.files_skipped += 1
        elif outcome.state == "deferred":
            self.manifest.mark_pending(file_id, charge_attempt=False)
        else:
            self.manifest.mark_failed(file_id, outcome.reason)
            with self._stats_lock:
                self.stats.files_failed += 1
            log_event(logging.ERROR, "file_failed", path=path, error=outcome.reason)

    def _worker(self) -> None:
        while not self.shutdown.is_set():
            try:
                row = self.queue.get(timeout=0.5)
            except queue.Empty:
                if self._done_event.is_set():
                    return
                continue
            with self._inflight_lock:
                self._inflight += 1
            try:
                if not self.gate.wait(self.shutdown):
                    self.queue.put(row)
                    return
                self._process_item(row)
            except AuthExpired as exc:
                # Put the work back before parking, so the refresh loses nothing.
                self.queue.put(row)
                self.gate.trip(str(exc))
                self.gate.wait(self.shutdown)
            except ShuttingDown:
                self.queue.put(row)
                return
            except Exception as exc:
                log_event(logging.ERROR, "worker_error", error=str(exc))
            finally:
                with self._inflight_lock:
                    self._inflight -= 1
                self.queue.task_done()

    # -- main loop ---------------------------------------------------------

    def run(self, rows) -> None:
        self._done_event = threading.Event()
        for row in rows:
            self.queue.put(row)

        workers = [
            threading.Thread(target=self._worker, name=f"dl-{i}", daemon=True)
            for i in range(self.config.concurrency)
        ]
        for worker in workers:
            worker.start()

        try:
            while any(w.is_alive() for w in workers):
                if self.gate.is_paused and not self.shutdown.is_set():
                    self._handle_auth_pause()
                with self._inflight_lock:
                    idle = self._inflight == 0
                if idle and self.queue.empty():
                    self._done_event.set()
                time.sleep(0.2)
        finally:
            self._done_event.set()
            for worker in workers:
                worker.join(timeout=5.0)

    def _handle_auth_pause(self) -> None:
        self.progress.stop()

        def validate(token: str) -> bool:
            return self.client.validate_token(token) is not None

        ok = refresh_token_interactively(
            self.tokens,
            validate,
            self.console,
            non_interactive=self.config.non_interactive,
            shutdown=self.shutdown,
        )
        if not ok:
            self.console.print("[red]No valid token; stopping. Progress is saved.[/red]")
            self.shutdown.set()
        self.gate.resume()
        self.progress.start()
