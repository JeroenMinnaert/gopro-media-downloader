# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Docker images are cut from releases instead of from every commit on `main`.
  A `v*` tag now publishes `:<version>`, `:<major.minor>` and `:latest` once
  the release itself has succeeded, so `:latest` means the newest release
  rather than the newest commit, and a version can be pinned. A pre-release
  publishes only its exact version.

## [0.1.1] — 2026-08-31

### Fixed

- `sync` created the destination's log directory before discovering there was
  no token, so someone trying the tool out before configuring it was left with
  a tree under a destination they never got to use. The token is now checked
  alongside the manifest, before any state is created. Same for
  `backfill-etags` and `token`.
- A cancelled browser login left an empty `browser-profile/` directory behind.
  It is removed when a login comes back empty-handed -- and only while it is
  empty, so a real saved session is never touched.

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

[Unreleased]: https://github.com/JeroenMinnaert/gopro-media-downloader/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/JeroenMinnaert/gopro-media-downloader/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/JeroenMinnaert/gopro-media-downloader/releases/tag/v0.1.0
