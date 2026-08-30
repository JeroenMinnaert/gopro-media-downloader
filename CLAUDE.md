# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A resumable CLI (`gopro-dl`) that downloads a user's entire GoPro Plus cloud
library in original quality into flat `YYYY-MM-DD/` folders, safe to run for
hours against terabytes of media and to interrupt/resume at any point. See
README.md for the full user-facing story (token setup, NAS notes, integrity
model) — it's detailed and not repeated here.

## Commands

```bash
# Setup
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

# Test
.venv/bin/python -m pytest -q                    # full suite
.venv/bin/python -m pytest -q tests/test_paths.py    # single file
.venv/bin/python -m pytest -q -k test_name           # single test by name
.venv/bin/python -m pytest -q --timeout=120          # as CI runs it

# Lint (must be clean; CI fails otherwise)
.venv/bin/ruff check src tests

# Run the CLI
.venv/bin/gopro-dl sync --dry-run --limit 5
```

Tests are fully mocked (respx for HTTP) — no network access or real GoPro
token needed to run the suite. Fixtures live in `tests/fixtures/`.

CI (`.github/workflows/ci.yml`) runs ruff, then pytest across Python
3.11/3.12/3.13 on **both** ubuntu-latest and macos-latest — macOS is not
incidental, see Architecture below. It also smoke-tests the packaged entry
point from outside the source tree and checks that a clean environment (no
`GOPRO_*` vars, no `.env`) yields no usable token.

## Architecture

### Pipeline

1. **`api.py`** (`GoProClient`) — talks to `api.gopro.com`: paginated search,
   per-item `/download` calls, retry/backoff with jitter, `Retry-After`
   honoring, and a shared `AuthGate`/`CircuitBreaker`.
2. **`models.py`** — pure parsing of the API's JSON into `MediaItem` /
   `SourceFile`. This is where the sharpest domain traps live (see "API
   landmines" below) — `parse_download_response` is the single place that
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
   computed while streaming (see README's "How the ETag works" for the math).
7. **`auth.py`** (`AuthGate`, `TokenProvider`) — a 401/403 from the API
   (distinct from a CDN 403) parks every worker; the main thread runs the
   interactive/non-interactive refresh prompt and releases the gate. No work
   is lost — the item that hit the 401 is put back on the queue first.
8. **`circuit.py`** (`CircuitBreaker`) — opens when most recent operations
   fail for systemic reasons, so an outage pauses+probes instead of marking
   thousands of files failed.
9. **`cli.py`** — argparse subcommands (`sync`, `status`, `report`, `verify`,
   `retry`, `backfill-etags`, `token`) wiring the above together.

### Concurrency model

Workers claim items via `manifest.claim_item()`/`claim_file()` (SQLite as the
lock) so two workers never race on the same file. On auth expiry, a worker
puts its item back on the queue *before* tripping the gate and parking —
this ordering is load-bearing for not losing work.

### Cross-cutting invariants worth knowing before editing

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
- Config precedence is flag → env var → `.env` → default (`config.py`).
