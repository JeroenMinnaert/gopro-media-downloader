"""The download state machine: resolve, resume, verify, finalise.

Two failure modes drive this design:

* Signed CDN URLs expire mid-download on large files. A 403 from the CDN is
  therefore routine -- we fetch a fresh URL and carry on from the byte we
  reached, rather than failing the file.
* The bearer token expires during a multi-day run. That surfaces as a 401 from
  api.gopro.com, which trips the AuthGate and pauses every worker instead.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .api import GoProClient
from .integrity import EtagVerifier, etag_header
from .logging_setup import log_event
from .manifest import Manifest
from .models import MediaItem, SourceFile, parse_download_response
from .paths import safe_filename, target_path

CHUNK_SIZE = 1024 * 1024
MAX_URL_REFRESHES = 10


class ShuttingDown(RuntimeError):
    pass


@dataclass
class FileOutcome:
    state: str  # done | skipped | failed | deferred
    bytes_written: int = 0
    size: int = 0
    reason: str = ""
    checksum: str | None = None
    checksum_state: str | None = None  # ok | mismatch | unverified


@dataclass
class StreamResult:
    written: int | None      # None => the URL must be refreshed
    error: str = ""
    total: int | None = None
    etag: str | None = None
    verifier: EtagVerifier | None = None


class Downloader:
    def __init__(
        self,
        client: GoProClient,
        manifest: Manifest,
        dest: Path,
        shutdown: threading.Event,
        on_progress: Callable[[object, int], None] | None = None,
        on_file_start: Callable[[str, int | None], object] | None = None,
        on_file_end: Callable[[object, FileOutcome], None] | None = None,
    ) -> None:
        self.client = client
        self.manifest = manifest
        self.dest = dest
        self.shutdown = shutdown
        self.on_progress = on_progress
        self.on_file_start = on_file_start
        self.on_file_end = on_file_end
        self._path_lock = threading.Lock()

    # -- resolution --------------------------------------------------------

    def resolve(self, item: MediaItem, date_folder: str) -> tuple[list[SourceFile], str | None]:
        """Call the download endpoint and create/refresh this item's file rows.

        Idempotent: called again later purely to get fresh signed URLs.
        """
        data = self.client.get_download(item.id)
        files, skip_reason = parse_download_response(data, item)
        if skip_reason:
            return [], skip_reason

        def upsert(source: SourceFile, filename: str, relpath: str) -> None:
            self.manifest.upsert_file(
                item.id,
                source.item_number,
                filename,
                relpath,
                source.size,
                source.checksum,
                source.checksum_algo,
            )

        for source in files:
            filename = safe_filename(source.filename, f"{item.id}_{source.item_number}")
            # An assigned path is never recomputed -- that stability is what
            # lets resume match files across runs.
            existing = self.manifest.get_file(item.id, source.item_number)
            if existing is not None:
                relpath = existing["target_path"]
                if (
                    existing["filename"] == filename
                    and existing["expected_size"] == source.size
                    and existing["checksum"] == source.checksum
                    and existing["checksum_algo"] == source.checksum_algo
                ):
                    continue  # nothing changed; skip the write
                upsert(source, filename, relpath)
                continue

            with self._path_lock:
                relpath = target_path(
                    date_folder,
                    filename,
                    item.id,
                    owner_of=lambda p, number=source.item_number: (
                        self.manifest.path_owner(
                            p, exclude_media_id=item.id, exclude_item_number=number
                        )
                    ),
                    item_number=source.item_number,
                    chapter_count=len(files),
                )
                upsert(source, filename, relpath)
        return files, None

    # -- one physical file -------------------------------------------------

    def fetch_file(
        self,
        item: MediaItem,
        source: SourceFile,
        file_row,
        date_folder: str,
    ) -> FileOutcome:
        relpath = file_row["target_path"]
        final = self.dest / relpath
        part = final.with_name(final.name + ".part")
        expected = file_row["expected_size"] or source.size
        final.parent.mkdir(parents=True, exist_ok=True)

        # Item-level idempotency: a correctly sized file already on disk is
        # done, even if this manifest has never seen it (rebuilt manifest,
        # files copied in from elsewhere).
        if final.exists():
            actual = final.stat().st_size
            if expected is None or actual == expected:
                return FileOutcome("done", 0, actual, "already_on_disk")
            log_event(
                logging.WARNING,
                "existing_file_size_mismatch",
                path=relpath,
                on_disk=actual,
                expected=expected,
            )
            final.unlink()

        url = source.url
        refreshes = 0
        last_stream_error = ""
        transferred = 0  # bytes actually pulled this run, excluding a resumed .part
        etag: str | None = file_row["checksum"]
        verifier: EtagVerifier | None = None
        handle = self.on_file_start(relpath, expected) if self.on_file_start else None
        outcome = FileOutcome("failed", 0, 0, "aborted")

        try:
            while True:
                if self.shutdown.is_set():
                    raise ShuttingDown()

                offset = part.stat().st_size if part.exists() else 0
                if expected is not None and offset == expected:
                    break  # complete .part from an interrupted run
                if expected is not None and offset > expected:
                    log_event(logging.WARNING, "part_too_large_restart", path=relpath, offset=offset)
                    part.unlink()
                    offset = 0

                try:
                    result = self._stream(url, part, offset, relpath, expected, handle)
                    written, last_stream_error = result.written, result.error
                    advertised = result.total
                    if result.etag:
                        etag = result.etag
                    if result.verifier is not None:
                        verifier = result.verifier
                    if advertised and expected is None:
                        # Chaptered files have no size from the API; the server
                        # just told us one, so verification becomes possible.
                        expected = advertised
                        log_event(
                            logging.DEBUG, "size_learned", path=relpath, expected=expected
                        )
                    if written is not None:
                        transferred += written
                        break
                    # written is None => the CDN URL died; get a fresh one
                    raise _UrlExpired()
                except _UrlExpired:
                    refreshes += 1
                    if refreshes > MAX_URL_REFRESHES:
                        outcome = FileOutcome(
                            "failed",
                            0,
                            0,
                            f"stream failed {refreshes} times (last: {last_stream_error or 'url expired'})",
                        )
                        return outcome
                    log_event(
                        logging.INFO, "refreshing_signed_url", path=relpath, refresh=refreshes
                    )
                    fresh, skip = self.resolve(item, date_folder)
                    if skip:
                        outcome = FileOutcome("skipped", 0, 0, skip)
                        return outcome
                    match = next(
                        (f for f in fresh if f.item_number == source.item_number), None
                    )
                    if match is None:
                        outcome = FileOutcome("failed", 0, 0, "chapter vanished on refresh")
                        return outcome
                    url = match.url
                    if expected is None:
                        expected = match.size

            # -- verify then finalise, never the other way round
            actual = part.stat().st_size
            if expected is not None and actual != expected:
                if actual > expected:
                    part.unlink()  # corrupt beyond salvage; start clean next time
                outcome = FileOutcome(
                    "failed",
                    transferred,
                    actual,
                    f"size mismatch: got {actual}, expected {expected}",
                )
                return outcome

            # Content check: proves the bytes on disk are the bytes S3 holds.
            checksum_state = "unverified"
            if verifier is not None:
                verdict = verifier.result()
                if verdict == "mismatch":
                    log_event(
                        logging.ERROR, "checksum_mismatch", path=relpath, etag=etag, size=actual
                    )
                    part.unlink(missing_ok=True)  # the bytes are wrong; start clean
                    outcome = FileOutcome(
                        "failed", transferred, actual,
                        f"content checksum mismatch against origin ETag {etag}",
                        checksum=etag, checksum_state="mismatch",
                    )
                    return outcome
                if verdict == "ok":
                    checksum_state = "ok"

            os.replace(part, final)
            outcome = FileOutcome(
                "done", transferred, actual,
                "" if expected is not None else "size_unverified",
                checksum=etag, checksum_state=checksum_state,
            )
            return outcome
        except ShuttingDown:
            outcome = FileOutcome("deferred", 0, 0, "shutdown")
            raise
        finally:
            if self.on_file_end is not None and handle is not None:
                self.on_file_end(handle, outcome)

    def _stream(
        self,
        url: str,
        part: Path,
        offset: int,
        relpath: str,
        expected: int | None,
        handle,
    ) -> StreamResult:
        """Stream one attempt.

        Returns (bytes_written, error, total_size). bytes_written is None when
        the URL must be refreshed -- either the signature expired or the
        connection dropped; `error` distinguishes the two for the log.

        total_size is what the server says the whole file is (Content-Length on
        a 200, the total in Content-Range on a 206/416). Chaptered recordings
        get no size from the API at all, so this is the only way to verify them
        -- without it a truncated download would be accepted as complete.
        """
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            # No per-request timeout override: the client's read timeout applies
            # per read(), not per file, so large downloads are unaffected while a
            # stalled connection still fails fast and resumes from the .part.
            with self.client.client.stream("GET", url, headers=headers) as response:
                status = response.status_code

                # An expired signature looks like 403 (sometimes 401/410) from
                # the CDN. That is not our bearer token -- refresh the URL.
                total = _advertised_total(response, offset)
                etag = etag_header(response.headers)

                if status in (401, 403, 410):
                    response.close()
                    return StreamResult(None, f"signed URL rejected ({status})")

                if status == 416:
                    # Server says the range is unsatisfiable: the .part is very
                    # likely already complete. Let the verify step judge.
                    response.close()
                    return StreamResult(0, "", total, etag)

                if status == 200 and offset:
                    # Range was ignored -- restart cleanly rather than
                    # appending and silently corrupting the file.
                    log_event(logging.WARNING, "range_ignored_restarting", path=relpath)
                    mode = "wb"
                    offset = 0
                elif status == 206:
                    log_event(
                        logging.INFO, "resumed_from_offset", path=relpath, offset=offset,
                        total=total,
                    )
                    mode = "r+b" if part.exists() else "wb"
                elif status == 200:
                    mode = "wb"
                else:
                    response.raise_for_status()
                    mode = "wb"

                # Hashing is free here: the bytes are already streaming past.
                # Only a clean pass from byte 0 can be verified this way; a
                # resumed file is checked later by `verify --deep`.
                verifier = EtagVerifier(etag, total) if offset == 0 else None

                written = 0
                with open(part, mode) as fh:
                    if mode == "r+b":
                        fh.seek(offset)
                        fh.truncate()
                    for chunk in response.iter_bytes(CHUNK_SIZE):
                        if self.shutdown.is_set():
                            fh.flush()
                            os.fsync(fh.fileno())
                            raise ShuttingDown()
                        fh.write(chunk)
                        if verifier is not None:
                            verifier.update(chunk)
                        written += len(chunk)
                        if self.on_progress:
                            self.on_progress(handle, len(chunk))
                    fh.flush()
                    os.fsync(fh.fileno())
                return StreamResult(written, "", total, etag, verifier)
        except httpx.HTTPError as exc:
            # A dropped connection mid-stream leaves a valid .part; resuming
            # from its offset is the whole point of writing it incrementally.
            log_event(logging.WARNING, "stream_interrupted", path=relpath, error=str(exc))
            return StreamResult(None, str(exc)), None


def _advertised_total(response: httpx.Response, offset: int) -> int | None:
    """Total file size according to the server, across 200/206/416 replies."""
    content_range = response.headers.get("Content-Range")
    if content_range and "/" in content_range:
        total = content_range.rsplit("/", 1)[1].strip()
        if total.isdigit():
            return int(total)
    length = response.headers.get("Content-Length")
    if length and length.isdigit():
        if response.status_code == 206:
            return offset + int(length)
        if response.status_code == 200:
            return int(length)
    return None


class _UrlExpired(RuntimeError):
    pass
