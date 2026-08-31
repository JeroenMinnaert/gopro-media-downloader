# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- A token expiring *mid-file* left that file's row claimed as `downloading`.
  The item went back on the queue, but the file was refused when it came round
  again and was silently skipped for the rest of the run — with the sync still
  exiting 0.
- An incomplete chaptered recording (the chapters not summing to the listed
  total) was reported on screen but exited 0, so a scheduled run looked clean.
- `verify --fix` re-queued a file without clearing its attempt count, so a file
  that had used up its budget was skipped in silence by the sync it told the
  user to run.
- A network failure while enumerating the library ended `sync` with a
  traceback, and an unreachable API was reported as "token rejected" — sending
  the user off to fetch a token that was never the problem.
- The cookie-auth fallback handed a 5xx body back as though it were the
  download response; the item then failed with a JSON parse error.
- A malformed `Retry-After` header raised `ValueError` out of the retry path.
- `©day` atoms written the standard QuickTime way (a length and language code
  ahead of the text) were never read, so `fix-dates` left that field alone in
  most real files.
- `fix-dates` slid clips as one batch whenever they shared a folder. That is
  for a stopped camera clock, whose signature is GoPro reporting one timestamp
  for the whole batch; clips with genuinely distinct capture times now get
  their own.
- `free_space` mis-parsed `df` output when the filesystem name contained
  spaces — the SMB shares the check exists for.
- Signed-URL refreshes now back off, so a CDN-side outage no longer burns every
  file's refresh budget within seconds.
- `report --csv` to an unwritable path is an error message, not a traceback.
- A date-repaired file that `verify --fix` re-queued kept describing the
  repair: the freshly downloaded original was then measured against the
  repaired copy's size, failed, was deleted and fetched again -- forever, for
  any file whose Exif addition exceeds the listing-drift tolerance. The
  origin's size, checksum and algorithm now move back into the live columns on
  re-queue (`origin_checksum_algo` is new, and added to existing manifests by
  the usual migration).
- `verify --deep` threw away the proof it computed: a resumed file, which
  cannot be hashed while streaming, stayed "size-only" in `status` however
  often it was re-hashed against the origin's ETag.
- The progress bar counted failed files as done.

### Added

- `gopro-dl --version`, which the bug-report template asks reporters for.
- `verify --deep --only-unverified` skips re-hashing files an earlier deep
  pass already proved, for finishing an interrupted pass over a library where
  re-reading every byte takes hours. Sizes are still checked on everything, and
  a re-download or a date repair clears the standing proof.

### Changed

- `status`, `report`, `verify`, `retry`, `backfill-etags` and `fix-dates` no
  longer create anything at a destination they were only meant to read:
  against a typo'd `--dest` they say where they looked instead of reporting an
  empty library, and leave no manifest or log directory behind.
- Structured log events (`file_done`, `size_learned`, …) are written to the
  JSONL run log and no longer printed to the console at default verbosity;
  `--verbose` shows them.
- `token` and `setup` no longer create a log directory under a destination the
  user has not settled on yet.
- `--since`/`--until` must be `YYYY-MM-DD` and `--limit` must be at least 1;
  both were silently accepted and quietly matched nothing (or everything).
- The saved token file is created with mode 600 rather than chmod'd afterwards.
- `setup` stores an absolute destination, so later runs no longer depend on the
  directory they start in.

### Security

- The maintainer's personal email address is no longer in `pyproject.toml`
  (and so not in the PyPI metadata) or `SECURITY.md`; vulnerability reports go
  through GitHub's private vulnerability reporting.

- The Docker publish workflow triggers on CI completion, whose `branches:`
  filter matches the *head* branch of the triggering run — a fork's own `main`
  satisfied it. Publishing now additionally requires a `push` event from this
  repository, so a pull request from a fork cannot push an image.

## [0.1.0] — unreleased

First public release. Everything below is the initial feature set rather than
a list of changes.

### Added

- **`sync`** — enumerates the whole GoPro Plus library into a SQLite manifest,
  then downloads the `source` variation (the original file off the camera) into
  flat `YYYY-MM-DD/` folders named by capture date. Resumable at the byte:
  correctly sized files are skipped, `.part` files continue via HTTP `Range`,
  and a server that ignores `Range` is detected rather than appended into.
- **Content verification against the origin.** Files are hashed while
  streaming and compared to the CDN's S3 multipart ETag, with every plausible
  part size tried at once. A mismatch is refetched; an unprovable result is
  reported `unverified` rather than as corruption.
- **Chapter fan-out.** One media item can be several physical files
  (`GX01…`/`GX02…`); work is queued per item but state is tracked per file, so
  a long recording resumes chapter by chapter.
- **`setup`** — first-run wizard: browser login against GoPro's own page,
  token validation, destination and timezone detection, written to one config
  file in the OS's per-user location.
- **Mid-run token expiry handling.** A 401/403 from the API parks every worker,
  tries the saved browser session, then prompts — or polls the token file under
  `--non-interactive`. No work is lost. A CDN 403 is correctly treated as an
  expired signed URL, not as auth failure.
- **`fix-dates`** — repairs capture dates *inside* the files: adds Exif
  `DateTimeOriginal` to the many GoPro JPEGs that carry none, and corrects
  `mvhd`/`tkhd`/`mdhd` times in place without rewriting the container. Handles
  the camera-clock-reset case by sliding a folder while preserving clip order.
- **`verify`**, **`verify --deep`**, **`backfill-etags`**, **`status`**,
  **`report`**, **`retry`**, **`token`**.
- **NAS support.** Media can be written straight to SMB/NFS; the manifest is
  automatically kept on local disk instead, since SMB silently degrades
  SQLite's WAL journal. macOS `smbfs` free-space truncation is worked around by
  reading `df`.
- **Circuit breaker and pre-flight checks** — a systemic outage pauses and
  probes instead of failing thousands of files; token, writability and free
  space are checked before the first byte.
- **Docker image** with an optional cron scheduler for unattended runs.

[Unreleased]: https://github.com/JeroenMinnaert/gopro-media-downloader/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/JeroenMinnaert/gopro-media-downloader/releases/tag/v0.1.0
