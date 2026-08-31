<!-- Architecture notes for anyone changing this codebase, human or otherwise.
     Read this before editing: several rules below exist because a real
     multi-terabyte run found the bug the hard way. -->

# Architecture

How `gopro-dl` is put together, and the invariants that are easy to break by
accident. For getting the tool running, see the
[README](README.md) and [docs/](docs/README.md); for contributing workflow, see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Pipeline

1. **`api.py`** (`GoProClient`) — talks to `api.gopro.com`: paginated search,
   per-item `/download` calls, retry/backoff with jitter, `Retry-After`
   honoring, and a shared `AuthGate`/`CircuitBreaker`.
2. **`models.py`** — pure parsing of the API's JSON into `MediaItem` /
   `SourceFile`. This is where the sharpest domain traps live (see [docs/api-notes.md](docs/api-notes.md)) — `parse_download_response` is the single place that
   decides which URL is actually the original file.
3. **`manifest.py`** (`Manifest`, SQLite/WAL) — the source of truth for what
   exists and what's done. Two tables because **one media item can fan out
   into several physical files** (a chaptered recording → `GX01.../GX02...`),
   discoverable only after calling `/media/{id}/download`. Work is *queued*
   per item but *state* is tracked per file.
4. **`runner.py`** (`DownloadRunner`) — thread pool of workers pulling items
   off a queue. `refresh_manifest()` pages the whole library into the
   manifest before any download starts, so progress/ETA are meaningful from
   the first byte.
5. **`downloader.py`** (`Downloader`) — per-file state machine: resolve →
   resume (HTTP `Range`, falls back cleanly if the server ignores it and
   replies `200`) → stream to `file.ext.part` → verify → atomic rename. A
   403 from the CDN mid-download is routine (signed URLs expire) and is
   handled by refetching a fresh URL, *not* by failing the file.
6. **`integrity.py`** / **`verify.py`** — S3 multipart ETag verification
   computed while streaming (see docs/file-integrity.md for the math).
7. **`auth.py`** (`AuthGate`, `TokenProvider`) — a 401/403 from the API
   (distinct from a CDN 403) parks every worker; the main thread runs the
   interactive/non-interactive refresh prompt and releases the gate. No work
   is lost — the item that hit the 401 is put back on the queue first.
8. **`circuit.py`** (`CircuitBreaker`) — opens when most recent operations
   fail for systemic reasons, so an outage pauses+probes instead of marking
   thousands of files failed.
9. **`mediadates.py`** / **`fixdates.py`** — the `fix-dates` command: byte-level
   reading and repair of the capture dates *inside* JPEG and MP4 files, driven
   by the manifest's `captured_at`. `mediadates.py` is the container work
   (pure functions over bytes plus thin file I/O); `fixdates.py` is the
   orchestration, modelled on `backfill.py`. No API calls.
10. **`cli.py`** — argparse subcommands (`sync`, `status`, `report`, `verify`,
   `retry`, `backfill-etags`, `fix-dates`, `token`) wiring the above together.

## Concurrency model

Workers claim items via `manifest.claim_item()`/`claim_file()` (SQLite as the
lock) so two workers never race on the same file. On auth expiry, a worker
puts its item back on the queue *before* tripping the gate and parking --
this ordering is load-bearing for not losing work. Both claims are released,
item *and* file: a file row left `downloading` refuses the claim when the
requeued item comes round again after the gate lifts, and is skipped in
silence for the rest of the run.

## Cross-cutting invariants worth knowing before editing

- Only `_embedded.variations` entries labelled `"source"` are ever
  downloaded — `_embedded.files` points at a transcode for videos
  (`models.py: parse_download_response`).
- A file is only renamed from `.part` to its final name once its byte count
  matches the expected size; a final-named file is therefore never partial.
- Chapter fan-out, chapter naming, and chapter-number-from-URL are all pure
  functions in `models.py` and are the most heavily unit-tested surface in
  the repo (multi-chapter fan-out, proxy trap, collision stability).
- `paths.py` and `models.py` are deliberately pure (no I/O), which is why
  they're fully unit-testable without mocks.
- macOS `smbfs` truncates `statvfs` block counts to 32 bits, so
  `preflight.py` shells out to `df` for free-space checks instead of using
  `shutil.disk_usage` — this is the reason CI runs on macOS at all.
- Config precedence is flag → env var → config file → default (`config.py`).
  The config file's own location is a separate, one-variable question
  (`GOPRO_DL_HOME` set or unset — see `locations.py`), resolved once in
  `cli.py: main()` and carried on `Config.app_dirs` from there; nothing else
  in the codebase decides where gopro-dl's state lives.
- `preflight.py: is_network_filesystem()` detects a network `--dest` (`mount`
  on macOS, `df --output=fstype` on Linux) so `config.py:
  apply_network_manifest_redirect()` can steer the manifest/logs onto local
  disk automatically — SMB/NFS silently corrupt SQLite's WAL journal. Only
  applies when `--manifest-dir` wasn't given explicitly, and only runs for
  the commands that actually open the manifest (`cli.py: build_parser()`'s
  `needs_manifest` flag, set per-subcommand via `common()`); fails open
  (assumes local) on any detection error.
- `fix-dates` is the one place that edits media after download, and the
  distinction between the two containers is load-bearing. **JPEGs are rebuilt**
  (splice a new APP1 in, copy every other byte through, temp file + atomic
  rename) because the common defect is a *missing* `DateTimeOriginal`, not a
  wrong one — most GoPro Plus photos carry an Exif block holding only
  orientation and pixel dimensions, so apps fall back to the mtime. **MP4s are
  only ever patched in place**: their `mvhd`/`tkhd`/`mdhd` times exist and are
  fixed-width, and rebuilding a multi-gigabyte ISO-BMFF container over SMB to
  correct 8 bytes is all risk and no benefit. A JPEG whose Exif holds a
  MakerNote or thumbnail IFD is skipped, because a rebuild relocates every
  value and those hold TIFF-absolute offsets of their own.
- Because `fix-dates` deliberately changes bytes, the origin's size and ETag
  stop describing the local file. `manifest.record_date_fix()` moves them into
  `origin_size`/`origin_checksum` and installs a local md5 (`checksum_algo`
  `md5`), which `verify.py`'s existing non-etag branch already re-hashes —
  otherwise `verify --fix` would delete each repaired file and re-download the
  broken one. `upsert_file`'s ON CONFLICT freezes `expected_size`/`checksum`
  once `dates_fixed_at` is set, for the same reason `date_folder` is frozen for
  done items: the next `sync` must not undo the repair.
- `mediadates.scan_mp4` walks the box tree with **seeks on an open file**, not
  a slurped header. Reading even a 4 MiB head from each of a thousand videos is
  gigabytes over SMB to inspect a few hundred bytes; `fix-dates` traverses the
  whole library.
- Two unrelated storage roots, do not conflate them: `<dest>/.gopro-dl/`
  (`config.py`) is the per-destination manifest/log dir and travels with a
  given `--dest`; `locations.py: AppDirs` (`platformdirs`-based) is the
  tool's own global, per-user state — the saved token, its config file, and
  the persisted Playwright browser-login profile (`browser_login.py`).
