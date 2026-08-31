# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A resumable CLI (`gopro-dl`) that downloads a user's entire GoPro Plus cloud
library in original quality into flat `YYYY-MM-DD/` folders, safe to run for
hours against terabytes of media and to interrupt/resume at any point. See
README.md and docs/ for the full user-facing story (token setup, NAS notes, integrity
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

Local dev: the checked-in `.envrc` sets `GOPRO_DL_HOME=$PWD/.dev-state`
(gitignored) so the token, config file, and browser-login profile land
inside the repo instead of your real `~/Library/Application Support`. With
direnv installed and hooked into your shell, `direnv allow` once and it's
automatic; without direnv, `locations.py: _read_envrc_home()` reads that
same line itself (via `dotenv_values`, not real shell evaluation) as a
fallback, walking up from cwd to find it. Comment the line out (or run from
outside the repo) to get the real OS locations instead -- there's no
install-type detection anywhere in the code; `GOPRO_DL_HOME` is the only
thing that ever decides this. A `.envrc` is only honored if it's owned by
the current user and not group/world-writable (`_envrc_is_trustworthy()`)
-- otherwise another local user on a shared machine could plant one above
your cwd and redirect your token/cookies into a directory they control.

Tests are fully mocked (respx for HTTP) — no network access or real GoPro
token needed to run the suite. Fixtures live in `tests/fixtures/`.

CI (`.github/workflows/ci.yml`) runs ruff, then pytest across Python
3.11/3.12/3.13 on **both** ubuntu-latest and macos-latest — macOS is not
incidental, see Architecture below. It also smoke-tests the packaged entry
point from outside the source tree and checks that a clean environment (no
`GOPRO_*` vars, no config file) yields no usable token.

A separate workflow (`.github/workflows/docker-publish.yml`) builds the
`Dockerfile` (linux/amd64 + linux/arm64) and pushes it to Docker Hub as
`<DOCKERHUB_USERNAME>/gopro-media-downloader:latest` and `:<sha>`. It
triggers via `workflow_run` on the `CI` workflow finishing on `main` (or via
manual dispatch), and only pushes when that CI run's conclusion was
`success` — a broken test matrix never reaches Docker Hub. Because of this,
it always publishes after a successful `main` CI run rather than only when
Docker-relevant paths changed (dropped in favor of the simpler,
harder-to-get-wrong gate). It authenticates with the repo secrets
`DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access token, not
the account password) — create the target repository on Docker Hub as
Private *before* the first push, since Docker Hub does not necessarily
default a new, auto-created repository to private.

A third workflow (`.github/workflows/release.yml`) builds and publishes to
PyPI via trusted publishing (OIDC, no token) on a `v*` tag push. Its `test`
job calls `ci.yml` as a reusable workflow (`workflow_call`) so a tag gets
the exact same lint + full OS/Python matrix as a normal push to `main` —
tags aren't otherwise covered by `ci.yml`'s own triggers. `build` `needs:
test` and `publish` `needs: build`, so nothing reaches PyPI unless that
whole suite passes first.

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
