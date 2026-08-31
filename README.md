# gopro-media-downloader

[![CI](https://github.com/JeroenMinnaert/gopro-media-downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/JeroenMinnaert/gopro-media-downloader/actions/workflows/ci.yml)

Downloads your entire GoPro Plus cloud library in **original quality** into
flat `YYYY-MM-DD/` folders named by **capture date** — resumable, and built for
a multi-hour, multi-terabyte run straight to a NAS.

The GoPro website caps bulk downloads and hands you transcodes. This uses the
same API the site does, picks the `source` variation (the original file off the
camera), verifies every file against the origin's own checksum, and keeps a
manifest so an interrupted run resumes exactly where it stopped. Exercised end
to end against a multi-terabyte library of over a thousand items, including a
13.6 GiB 4-chapter clip.

> **Not affiliated with, endorsed by, or supported by GoPro, Inc.** "GoPro" and
> "GoPro Plus" are trademarks of their respective owner and are used here only
> to say what this tool talks to. It drives the same undocumented internal API
> the GoPro web app uses, which can change or disappear without notice and take
> this tool with it — see [API notes](#api-notes-things-that-will-bite-you) for
> the parts already known to be sharp. Intended for downloading media from your
> own account. Provided as-is under the MIT licence, with no warranty; you are
> responsible for your own use of it and for your own backups.

```
~/Downloads/GoPro/            # the default destination -- override with --dest
├── 2023-07-14/
│   ├── GX010123.MP4
│   └── GOPR0456.JPG
├── 2023-07-15/
│   └── GX010124.MP4
└── .gopro-dl/          # manifest.db + run logs (exclude from your NAS copy)
```

## Quickstart

```bash
pipx install gopro-media-downloader   # 1. install (alternatives below)
gopro-dl setup                        # 2. log into GoPro, pick a destination
gopro-dl sync --dry-run               # 3. enumerate the library, download nothing
gopro-dl sync --limit 5               # 4. fetch five real files as a smoke test
gopro-dl sync                         # 5. the real run -- Ctrl-C is safe
```

`setup` opens GoPro's own login page in a browser, takes the session token, and
saves your destination and timezone; afterwards every command is a bare
one-liner from any directory. It never sees your password.

Steps 3 and 4 earn their place: `--dry-run` pages the library into the manifest
and stops, telling you how many items and bytes are coming, and `--limit 5`
puts five real files on disk. Between them you learn about a wrong destination,
bad timezone or full disk in two minutes rather than four hours.

The real run expects to be interrupted — Ctrl-C, then re-run `sync` to continue
from the exact byte. The token expiring partway through is normal and handled
([Troubleshooting](#troubleshooting)).

## Install

Needs Python 3.11 or newer. Tested on macOS and Linux.

```bash
pipx install gopro-media-downloader   # once published to PyPI
gopro-dl --help
```

`pipx` keeps it in its own virtualenv and puts `gopro-dl` on your `PATH`; plain
`pip install gopro-media-downloader` works too if you'd rather manage that
yourself. From source:

```bash
git clone https://github.com/JeroenMinnaert/gopro-media-downloader
cd gopro-media-downloader
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/gopro-dl --help
```

The first run that needs a browser login downloads Chromium on its own
(one-time, ~250 MB) — nothing to install by hand. To run headless on a NAS or
server instead, see [Running in Docker](#running-in-docker).

## Getting a token

**No username/password handling in this tool** — GoPro's sign-in involves OAuth
and CAPTCHAs, and this tool never touches your credentials. It uses the
`gp_access_token` session cookie, which expires periodically; that's handled
mid-run without losing progress.

`gopro-dl setup` gets a token, validates it, and saves your settings:

```
Checking for a saved GoPro browser session...
Downloading the browser used for GoPro login (one-time, ~250MB)...
A browser window has opened. Log into GoPro there.
Token OK - you@example.com (via browser login)
Wrote ~/Library/Application Support/gopro-dl/token (chmod 600)
Destination for media (Enter to accept ~/Downloads/GoPro):
Detected timezone: Europe/Paris (override with --timezone if wrong)
```

A real Chromium window opens at GoPro's own login page — your password goes
into that page, never through this tool. The wizard then takes the session
cookie and closes it. Chromium is downloaded once, automatically; if that
fails it tells you to run `playwright install chromium` and falls back to
manual paste.

The token and browser profile live in the OS's per-app location — macOS
`~/Library/Application Support/gopro-dl/`, Linux `~/.config/gopro-dl/` — at
`chmod 600`/`700`. That one root holds the token, config file, browser profile
and NAS-redirected manifests. It is unrelated to `<dest>/.gopro-dl/`, which is
per-destination and sits with the media on purpose. Later runs, including the
mid-run expiry prompt, usually find a valid session there and skip the window.

Destination defaults to `~/Downloads/GoPro` (the OS's real Downloads folder,
not a relative path); timezone is auto-detected from `/etc/localtime`. The
wizard asks about both, so Enter accepts and typing overrides.

It writes nothing until the token validates, and never silently overwrites an
existing token or config. Flags to skip the prompts for a scripted run:

```bash
gopro-dl setup --token "$TOKEN" --token-file ~/mytoken --dest ~/gopro-backup --force
```

`--no-browser` skips straight to pasting a token by hand.

<details>
<summary><b>Getting the token by hand</b> (what the wizard automates)</summary>

1. Open <https://gopro.com/media-library/> in Chrome and log in.
2. DevTools → **Application** → **Cookies** → `https://gopro.com`.
3. Copy the full value of **`gp_access_token`** — a long JWT starting `eyJ...`.

Or via **Network**: filter `api.gopro.com`, reload, click any `media/search`
request, and copy everything after `Authorization: Bearer `.
</details>

Use `--token-file` rather than `--token` for long runs — the file can be
updated while the downloader is running. `gopro-dl token` checks validity; with
no `--token-file` set it reads the same default location `setup` wrote to.

## Everyday use

Every command is a one-liner from any directory once `setup` has run. Without
it, pass `--dest`, `--token-file` and `--timezone` on each call (`.env.example`
lists every key).

```bash
gopro-dl sync --dry-run         # plan only, downloads nothing
gopro-dl sync --limit 5         # smoke test
gopro-dl sync                   # the real run, resumable
gopro-dl status                 # counts, bytes and integrity state
gopro-dl report --failed-only   # what failed and why
gopro-dl report --csv out.csv   # full per-file detail
gopro-dl verify                 # re-check sizes of everything marked done
gopro-dl verify --deep          # re-hash and compare against the origin ETag
gopro-dl verify --deep --fix    # ... and re-queue anything that fails
gopro-dl backfill-etags         # fetch checksums for files downloaded earlier
gopro-dl fix-dates --dry-run    # what capture dates are wrong or missing?
gopro-dl fix-dates              # ... repair them in the files themselves
gopro-dl retry                  # reset failed files, then sync again
gopro-dl token                  # is the current token still valid?

# unattended: polls the token file instead of prompting on expiry
nohup gopro-dl sync --non-interactive --quiet > ~/gopro-sync.log 2>&1 &
```

Useful flags: `--since 2022-01-01`, `--until 2022-12-31`, `--types Video,Photo`,
`--timezone Europe/Paris`, `--concurrency 3` (max 8), `--limit N`,
`--retry-failed`, `--quiet`, `--non-interactive`, `--no-manifest-refresh`.

Config precedence is flag → environment variable → config file → default.

## Troubleshooting

### The token expired mid-run

Expected on a long run. Downloads pause, workers park, and the tool first
checks the saved browser profile for a refreshed session — if that works the
run continues with no input from you. Otherwise:

```
GoPro token expired or rejected.
Press Enter once updated, paste a token, type b to log into GoPro in a
browser, or Ctrl-C to stop:
```

Type `b` for the browser login, paste a token, or overwrite the token file and
press Enter. Either way the run resumes from the exact byte it reached.
`--non-interactive` polls the token file instead of prompting, which suits
`nohup`/`screen` runs.

A **403 from GoPro's CDN is not** token expiry — signed media URLs are
time-limited and expire mid-file routinely. Those refresh silently.

### `database is locked`, or the manifest looks corrupt

The manifest is on an SMB/NFS share. `gopro-dl` moves it to local disk
automatically when it detects a network `--dest`; if detection failed, pass
`--manifest-dir` yourself. See [NAS](#downloading-straight-to-a-nas).

### It refuses to start: not enough free space

Pre-flight sizes the run and exits 1 rather than filling your disk halfway.
Free up space, or work in date ranges — see
[If the destination is smaller than the library](#if-the-destination-is-smaller-than-the-library).

### Everything is dated today in Photos, Lightroom or my NAS app

The folders are right but the date *inside* the file is missing — many GoPro
JPEGs carry no `DateTimeOriginal`, so apps fall back to your download time.
`gopro-dl fix-dates --dry-run`, then `gopro-dl fix-dates`. See
[Capture dates](#capture-dates-inside-the-files).

### Files show as `unverified` in `status`

Not corruption — a resumed download can't be hashed from byte zero, and some
files have several plausible S3 part sizes, so no verdict is claimed that
can't be proven. Run `gopro-dl backfill-etags` then `gopro-dl verify --deep`
after the sync. See [File integrity](#file-integrity).

### A file failed

```bash
gopro-dl report --failed-only   # what failed and why
gopro-dl retry                  # reset those to pending
gopro-dl sync                   # fetch them again
```

### It's much slower than my connection

Downloading to a NAS over WiFi sends every byte across the air twice (CDN →
machine, machine → NAS) on a half-duplex medium. Measured 32.5 MiB/s over
802.11ax vs 75 MiB/s over gigabit ethernet. Concurrency defaults to 3 and caps
at 8; raising it will not beat your link.

### Ctrl-C seems to hang

The first press finishes the chunks in flight, which can take a minute if a
worker is blocked on a socket read. Press again to stop immediately — `.part`
files and the manifest both survive either way.

## How it stays safe on a multi-terabyte run

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

## File integrity

**Size** — the listing's `file_size` is the baseline (for a chaptered
recording, the sum across chapters; per-chapter sizes come from response
headers). A file is renamed from `.part` only once the byte count matches. An
item-level check confirms the chapters sum to the listed total, catching a
whole chapter going missing — each file can verify fine while the recording is
incomplete.

**Content** — GoPro's API exposes no checksum, but their CDN is S3 behind
CloudFront and the `ETag` is an origin-side content hash. Files are hashed
*while streaming* (free — the bytes are already passing through) and compared.
A mismatch fails and deletes the file, so the next run refetches it.

### How the ETag works

An S3 multipart ETag is **not** the MD5 of the file:

```
etag = md5(concat of each part's raw md5 digest) + "-" + <part count>
```

So a single-part object is `md5(md5_raw(file))-1`. Verified against a real file:

```
S3 etag        : 1955982c4db2859ebaee6bb5885e7e8d-1
md5(file)      : faaf8b8e2ad35c0dad129d1201089078   wrong
md5(md5(file)) : 1955982c4db2859ebaee6bb5885e7e8d   correct
```

S3 doesn't publish the part size and it varies per file (100 MiB and 20 MiB
both appear in one library). The part count constrains it: for N parts covering
`total` bytes, `(N-1)*S < total <= N*S`. Uploaders use round sizes, so the MiB
multiples in that window are the candidates — usually one, sometimes a few. The
downloader hashes against **every** candidate at once, which keeps the verdict
honest in both directions:

| outcome | meaning |
|---|---|
| a candidate matches | conclusive proof the bytes match the origin |
| no match, one candidate | real corruption — failed and refetched |
| no match, several candidates | reported `unverified`, never as corruption |

Resumed downloads can't be hashed from byte zero, so they're recorded
`unverified` and confirmed later by `verify --deep`.

```bash
gopro-dl status                # counts by integrity state
gopro-dl backfill-etags        # fetch checksums for files downloaded earlier
gopro-dl verify --deep         # re-read from disk and check against the origin
```

`backfill-etags` costs one API call per item plus one HEAD per file and moves
no media. `verify --deep` re-reads everything (~3 hours per TiB over gigabit) —
run it at the end, not during a download, or the two compete for bandwidth.

## Capture dates inside the files

The `YYYY-MM-DD/` folders come from GoPro's `captured_at`, but the date stored
*inside* a file often doesn't match — and that's the date Photos, Lightroom and
NAS photo apps sort by. `fix-dates` handles both failure modes:

* **Photos usually have no capture date at all.** Many GoPro JPEGs carry an
  Exif block holding only orientation and pixel dimensions — no
  `DateTimeOriginal`. Apps then fall back to the file's mtime, which is *when
  you downloaded it*, so a 2015 clip shows up as today.
* **Videos are usually fine**, but a wrong `mvhd`/`tkhd`/`mdhd` creation time
  is corrected in place, without rewriting the container.

```bash
gopro-dl fix-dates --dry-run          # report only, changes nothing
gopro-dl fix-dates --since 2024-01-01 # bound it to a date range
gopro-dl fix-dates                    # repair
```

No API calls, no media transferred — it reads the manifest and the files on
disk. Pass the same `--timezone` your sync used, or photos GoPro gives no
timezone for get an Exif time disagreeing with their folder.

| Where | Value | Why |
| --- | --- | --- |
| Exif `DateTimeOriginal`, `DateTimeDigitized`, `DateTime` | capture-**local** wall time | Exif dates carry no zone; same conversion that names the folder |
| Exif `GPSDateStamp` / `GPSTimeStamp` | UTC | the spec defines these as UTC |
| `mvhd` / `tkhd` / `mdhd` creation time | UTC | ISO-BMFF defines these as UTC |
| file modification time | the capture instant | what apps fall back to |

Photos are **rebuilt** (new Exif segment spliced in, other bytes copied
through, temp file + atomic rename) because adding a tag changes the file size;
the compressed image is untouched. Videos are **only ever patched in place** —
a few bytes, never a container rewrite, so a 3 GB clip costs a handful of
writes rather than 3 GB of NAS traffic. A photo whose Exif holds a MakerNote or
thumbnail IFD is skipped, since a rebuild would invalidate their offsets.

**This intentionally changes the bytes**, so a repaired file no longer matches
the origin's ETag. `fix-dates` moves the origin size and hash into
`origin_size`/`origin_checksum` and records a local md5, so `verify --deep`
keeps working and `verify --fix` won't delete a repaired file to re-download
the dateless one. That md5 means reading each repaired file end to end, so a
run repairing large videos is bound by your link — use `--since`/`--until` to
batch it. Re-running is idempotent.

### When the camera clock was reset

A GoPro that loses power resets its clock, so a morning's filming comes back
dated `2015-01-01` with the clips' *relative* times intact. GoPro knows the
true date but reports one timestamp for the whole batch, so writing it verbatim
would collapse the session onto a single second and lose the ordering.

When a folder's clips carry distinct embedded times, `fix-dates` slides the
whole folder by the one offset landing its earliest clip on GoPro's time:

```
2015-01-01 02:43:01  ->  2017-02-11 04:49:12
2015-01-01 03:05:26  ->  2017-02-11 05:11:37
2015-01-01 03:19:13  ->  2017-02-11 05:25:24
```

Folders whose clips share one identical timestamp have no ordering to protect
and take GoPro's value; `--flatten-to-api` forces that everywhere.

Cameras commonly write capture-*local* time into the video field the spec calls
UTC — a convention, not damage, so a video matching either reading is left
alone. `--video-utc` normalises those, `--keep-mtime` leaves mtimes untouched,
`--tolerance N` sets the drift accepted before rewriting (default 120s).

## Downloading straight to a NAS

Media can go straight to an SMB/NFS mount — **the manifest can't**. SMB
silently refuses SQLite's WAL journal (degrading to `journal_mode=delete`) and
network file locking is unreliable, risking `database is locked` or corruption.
Media files are fine: chunked writes, `fsync`, atomic `os.replace` and
seek/truncate resume were all verified over SMB.

`gopro-dl` handles this itself. If `--dest` looks like a network mount and you
haven't passed `--manifest-dir`, the manifest and logs go to local disk under
`~/Library/Application Support/gopro-dl/manifests/<name>-<hash>/` (macOS) or
`~/.config/gopro-dl/manifests/<name>-<hash>/` (Linux), keyed by destination
path so a re-run finds it again:

```
/Volumes/GoPro looks like a network mount -- keeping the manifest and logs
locally at ~/Library/Application Support/gopro-dl/manifests/GoPro-3f9a1c2b
instead (SMB/NFS can corrupt SQLite's WAL journal). Override with --manifest-dir.
```

Detection is best-effort (macOS `mount`, Linux `df --output=fstype`) and only
steers the manifest, never blocks a run. Pass `--manifest-dir` to choose the
location yourself:

```bash
gopro-dl sync --dest /Volumes/GoPro \
  --manifest-dir ~/gopro-backup/.gopro-dl \
  --token-file ~/gopro-backup/token --timezone Europe/Paris
```

Or run `gopro-dl setup --dest /Volumes/GoPro` once to save it and just
`gopro-dl sync` thereafter.

macOS smbfs truncates `statvfs` block counts to 32 bits, so any SMB volume over
4 TiB under-reports free space by exactly 2**32 blocks through
`shutil.disk_usage`. Pre-flight reads `df` instead, so a large share is measured
correctly rather than wrongly refused.

To stage locally and copy afterwards, `rsync -a --progress /local/gopro/
/Volumes/gopro-backup/` — and run `gopro-dl verify` before and after any copy.

### If the destination is smaller than the library

Pre-flight refuses a run that cannot fit (exit 1). Work through the library in
date ranges, copying each batch off before the next:

```bash
gopro-dl sync --until 2019-12-31          # oldest batch
# ... move/archive that batch elsewhere, then:
gopro-dl sync --since 2020-01-01 --until 2022-12-31
```

The manifest tracks what's done, so batches never overlap.

## Running in Docker

A `Dockerfile` and `docker-compose.example.yml` are included, aimed at a NAS
with Docker support, though any host works the same.

```bash
cp docker-compose.example.yml docker-compose.yml
# edit the two host paths: your config dir and your NAS media path
```

The `setup` wizard needs a browser, which a headless container doesn't have.
Run it on a machine with a display, then copy what it wrote into the folder
you're about to mount:

```bash
gopro-dl setup --dest /path/that/matches/your/GOPRO_DEST
cp ~/Library/Application\ Support/gopro-dl/{token,config.env} ./gopro-dl-config/
docker compose up -d --build
```

By default the container runs `gopro-dl sync --non-interactive` once and exits.
Set `GOPRO_DL_CRON_SCHEDULE` (5-field cron, e.g. `"0 3 * * *"`) to have it
install that as a cron job and stay running as a scheduler; `docker compose
logs -f` shows each run. That's a container-only setting read by the entrypoint,
not one of `gopro-dl`'s own `GOPRO_*` variables — everything else is configured
via compose environment variables or the `config.env` you copied in
(`.env.example` lists every key).

No public prebuilt image exists: `docker-publish.yml` pushes to whatever account
the repo's own `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` secrets name, so a fork
without them fails at the login step. Build locally as above, or set those two
secrets on your fork.

## API notes (things that will bite you)

From inspecting live API responses. Each caused a bug that looked like success:

1. **`_embedded.files` is a trap.** For videos it points at
   `high_res_proxy_mp4`, a 1080p transcode at roughly half the original's
   bytes; for photos it points at the real source, so it looks plausible. Only
   `variations[label == "source"]` is trustworthy — the proxy yields complete,
   valid, *wrong* files.
2. **Chapters share the label `"source"`.** A long recording exposes several
   variations all labelled `source`, distinguished only by the trailing number
   in the URL (`/source/default/1.mp4`, `2.mp4`, …). Taking the first match
   silently discards the rest of the recording.
3. **The listing `file_size` is the sum across chapters**, and exact — it
   matched summed chapter sizes byte-for-byte on every chaptered item tested.
   Individual variations carry no size at all.
4. **`captured_at_timezone` is essentially always absent** (all but one item in
   a library of over a thousand). Without `--timezone`, folder dates fall back
   to UTC and evening clips land in the previous day. DST is applied per clip.
5. **No checksums anywhere in the API.** The CDN's `ETag` is the only content
   hash available.
6. **`MultiClipEdit` items** are GoPro-generated edits, not originals —
   excluded from the request and filtered again on the response.

### macOS + SMB

1. **SQLite WAL is silently unavailable on SMB**, degrading to
   `journal_mode=delete`, and network file locking is unreliable. Keep the
   manifest local with `--manifest-dir`.
2. **statvfs truncates to 32 bits on smbfs**, so `shutil.disk_usage`
   under-reports any share over 4 TiB by exactly 2**32 blocks. Pre-flight reads
   `df` instead.
3. **WiFi halves throughput** on a download-to-NAS run — every byte crosses the
   air twice on a half-duplex medium. 32.5 MiB/s over 802.11ax vs 75 MiB/s over
   gigabit.

## Development

Setup, house rules, and what to put in a bug report:
[CONTRIBUTING.md](CONTRIBUTING.md). Found a way to leak the token or write
outside `--dest`? Report it privately — [SECURITY.md](SECURITY.md), not an
issue.

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

196 tests, fully mocked — no network, no token, no browser. CI runs ruff then
the suite across Python 3.11/3.12/3.13 on Linux *and* macOS; the macOS leg
matters because the free-space check parses `df` precisely because smbfs
truncates `statvfs`.

### Releasing

The version lives in `src/gopro_dl/__init__.py:__version__`. Bump it, commit,
then push a matching tag:

```bash
git tag v0.2.0 && git push origin v0.2.0
```

`release.yml` runs the full matrix, then builds and publishes to PyPI via
[trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC, no
stored token). One-time PyPI setup: add this repo as a trusted publisher for
`gopro-media-downloader`, workflow `release.yml`, environment `pypi`.
