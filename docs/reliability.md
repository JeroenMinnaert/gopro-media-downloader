# How it stays safe on a multi-terabyte run

- **Manifest first.** The whole library is enumerated into
  `<dest>/.gopro-dl/manifest.db` before anything downloads, so progress and ETA
  are meaningful from the first byte.
- **Resume at two levels.** A correctly sized file on disk is skipped; a `.part`
  file resumes via HTTP `Range`. A server that ignores `Range` and replies `200`
  is detected and restarted rather than appended into.
- **Verify before finalise.** Bytes land in `file.MP4.part`, renamed only once
  the size matches. A final-named file is never partial.
- **Chaptered recordings** (one item, several `GX01/GX02/...` files) are tracked
  per physical file, so a long recording resumes chapter by chapter.
- **Circuit breaker.** Systemic failures pause and probe instead of marking
  thousands of files failed.
- **Politeness.** 3 concurrent downloads, exponential backoff with jitter,
  `Retry-After` honoured on 429s.
- **Pre-flight.** Token, writability and free space checked before the first
  byte — sized to what *this* run fetches, so `--limit`/`--since` runs still
  work on a disk smaller than the library.
- **Ctrl-C is safe.** Partial files are kept; re-run `sync` to continue.

### Filename collisions

Two clips can share a filename on one date. The second gets its media id
appended (`GX010123_a1b2c3.MP4`). The id is immutable and the assignment is
recorded in the manifest, so a file lands at the same path on every future run
— which is what keeps resume matching.

---

[← Documentation index](README.md) · [Project README](../README.md)
