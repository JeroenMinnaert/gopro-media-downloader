# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.1.0] — 2026-08-31

First public release. Everything below is the initial feature set rather than
a list of changes: the fixes made while getting here are in the commit
history, and describe code that never shipped.

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
- `verify --deep --only-unverified`, which skips re-hashing files an earlier
  deep pass already proved. Sizes are still checked on everything; a
  re-download or a date repair clears the standing proof.
- `gopro-dl --version`.
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
