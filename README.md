# gopro-media-downloader

[![CI](https://github.com/JeroenMinnaert/gopro-media-downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/JeroenMinnaert/gopro-media-downloader/actions/workflows/ci.yml)

Downloads your entire GoPro Plus cloud library in **original quality**, into flat
`YYYY-MM-DD/` folders named by **capture date**, resumably — built for a
multi-hour, multi-terabyte backup run straight to a NAS.

The GoPro website caps bulk downloads and hands you transcodes. This talks to
the same API the website uses, picks the `source` variation (the original file
off the camera), verifies every file against the origin's own checksum, and
keeps a manifest so an interrupted run picks up exactly where it stopped.

Proven on a real library: 1,217 items / 1.4 TiB, including 22 chaptered
recordings and a 4-chapter 13.6 GiB clip.

```
~/Downloads/GoPro/            # the default destination -- override with --dest
├── 2023-07-14/
│   ├── GX010123.MP4
│   └── GOPR0456.JPG
├── 2023-07-15/
│   └── GX010124.MP4
└── .gopro-dl/          # manifest.db + run logs (exclude from your NAS copy)
```

## Install

```bash
pipx install gopro-media-downloader   # once published to PyPI
gopro-dl --help
```

Or from source, in a virtualenv:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/gopro-dl --help
```

## Get your token and get set up

There is deliberately **no username/password handling in this tool** — GoPro's
sign-in involves OAuth and CAPTCHAs, and this tool never touches your
credentials. Underneath, it uses a bearer token (the `gp_access_token`
session cookie) that expires after a while; the tool handles that mid-run
without losing progress (see below).

The setup wizard gets you a token, validates it, and saves your settings —
just run it, nothing to install by hand first:

```bash
gopro-dl setup
```

```
gopro-dl setup - token, destination and settings in one pass.

Checking for a saved GoPro browser session...
Downloading the browser used for GoPro login (one-time, ~250MB)...
A browser window has opened. Log into GoPro there -- this continues
automatically once you're signed in, or Ctrl-C to give up.
Token OK - jane@example.com (via browser login)

Wrote /Users/jane/Library/Application Support/gopro-dl/token (chmod 600)
Destination for media (Enter to accept /Users/jane/Downloads/GoPro): 
Destination: /Users/jane/Downloads/GoPro
Detected timezone: Europe/Brussels (override with --timezone if wrong)
Wrote /Users/jane/Library/Application Support/gopro-dl/config.env

Next: gopro-dl sync --dry-run --limit 5
```

The first time it needs the browser, it downloads Chromium itself (that
"Downloading..." line only appears once — later runs skip straight to the
window). A real, visible Chromium window then opens at GoPro's own login
page — your password goes straight into that page, never through this tool.
Once you're logged in, the wizard picks the session cookie up from the
browser and closes the window.

Both the token and that browser login live under the OS's standard per-app
location instead of loose dotfiles in your home directory — on macOS that's
`~/Library/Application Support/gopro-dl/` (`token` and `browser-profile/`,
`chmod 600`/`700`); on Linux it's `~/.config/gopro-dl/token` and
`~/.config/gopro-dl/browser-profile/` (one root holds everything: token,
config file, browser profile, and NAS-redirected manifests alike). This is
separate from
`<dest>/.gopro-dl/`, which is per-destination and colocated with the media on
purpose (see below) — the two are unrelated. Most future runs — including
re-running `setup` and the mid-run token-expiry prompt below — find a
still-valid browser session there and skip the window entirely.

(If the automatic download fails — no network, a locked-down environment —
it tells you to run `playwright install chromium` yourself and falls back to
manual paste in the meantime.)

The destination defaults to `~/Downloads/GoPro` — anchored to the OS's real
Downloads folder rather than a relative path that depends on which directory
you happened to run `gopro-dl` from — but the wizard always asks first,
showing that default so Enter accepts it and typing a path overrides it.

The timezone is auto-detected too, from the system's `/etc/localtime` — the
wizard only falls back to asking if that can't be read. Pass `--timezone`
yourself to skip or override detection.

It refuses to write anything until the token validates against the API, and
never silently overwrites an existing token file or config — it asks first
(or pass `--force`). Pass `--no-browser` to skip straight to pasting a token by
hand, or `--token`/`--token-file`/`--dest`/`--timezone` to skip their prompts
for a scripted run:

```bash
gopro-dl setup --token "$TOKEN" --token-file ~/mytoken --dest ~/gopro-backup --force
```

**To get the token value by hand** (what the wizard automates):

1. Open <https://gopro.com/media-library/> in Chrome and log in.
2. Open DevTools (`Cmd+Option+I`) → **Application** tab → **Storage → Cookies →
   `https://gopro.com`**.
3. Find the cookie named **`gp_access_token`** and copy its full **Value** — a
   long JWT starting `eyJ...`.

**Alternative (Network tab):** DevTools → **Network** → filter `api.gopro.com` →
reload the page → click any `media/search` request → **Headers** → Request
Headers → copy everything after `Authorization: Bearer `.

Use `--token-file` rather than `--token` for long runs: the file can be updated
while the downloader is running. Check it any time with:

```bash
gopro-dl token
```

With no `--token-file`/`GOPRO_TOKEN_FILE` set, this reads from the same
per-OS default location `setup` wrote to — so it works from any directory.

## Usage

Once `gopro-dl setup` has run, every command below is a one-liner from any
directory — settings are saved for you. Otherwise pass `--dest`,
`--token-file` and `--timezone` explicitly (see `.env.example` for every key).

```bash
# Plan only -- builds the full manifest, downloads nothing
gopro-dl sync --dry-run

# Smoke test before committing to the whole library
gopro-dl sync --limit 5

# The real run: safe to Ctrl-C and restart at any point
gopro-dl sync

# Unattended: polls the token file instead of prompting when the token expires
nohup gopro-dl sync --non-interactive --quiet > ~/gopro-sync.log 2>&1 &
```

```bash
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
```

A first Ctrl-C stops after the current chunks and keeps every `.part` file;
re-running resumes from the exact byte. It can take up to a minute to take
effect, because a worker may be blocked on a socket read — press it again to
stop immediately (still safe: partial files and the manifest survive).

Useful flags: `--timezone Europe/Brussels`, `--since 2022-01-01`,
`--until 2022-12-31`, `--types Video,Photo`,
`--concurrency 3` (max 8), `--limit N`, `--retry-failed`, `--quiet`,
`--non-interactive`, `--no-manifest-refresh`.

Settings written by `gopro-dl setup` live in one config file (`.env.example`
lists every key), the same for every directory. Precedence is
flag → environment variable → that config file → built-in default.

## When the token expires mid-run

Expected on a run this long. The download pauses, every worker parks, and the
tool first silently checks the saved browser profile (from `gopro-dl setup`,
see above) for a session that has since refreshed — if that works, the run
just continues with no input from you. Otherwise you get a prompt:

```
GoPro token expired or rejected.
  1. Open https://gopro.com/media-library/ in Chrome (log in if needed)
  2. DevTools -> Application -> Cookies -> gopro.com -> gp_access_token
  3. Copy the value into /Users/you/Library/Application Support/gopro-dl/token
Press Enter once updated, paste a token, type b to log into GoPro in a
browser, or Ctrl-C to stop:
```

Type `b` to pop open the same browser login the setup wizard uses, paste a
token directly, or overwrite the token file and press Enter. Either way the
run continues from the exact byte it reached — nothing is re-downloaded. With
`--non-interactive` it polls the token file instead of prompting (no browser
involved), which suits `nohup`/`screen` runs.

Note that a **403 from GoPro's CDN is not** token expiry — signed media URLs are
time-limited and routinely expire mid-file. Those are refreshed silently and
never interrupt you.

## How it stays safe on a multi-terabyte run

- **Manifest first.** The whole library is enumerated into
  `<dest>/.gopro-dl/manifest.db` before anything downloads, so the tool always
  knows the full picture and can report progress meaningfully.
- **Resume at two levels.** A correctly sized file already on disk is skipped
  outright; a partial `.part` file resumes via HTTP `Range`. Servers that ignore
  `Range` and reply `200` are detected and restarted cleanly rather than
  appending into a corrupt file.
- **Verify before finalise.** Bytes land in `file.MP4.part` and are only renamed
  to `file.MP4` after the size matches. A final-named file is never partial.
- **Chaptered recordings** (one media item, several `GX01/GX02/...` files) are
  tracked per physical file, so a long recording resumes chapter by chapter.
- **Circuit breaker.** If most requests start failing systemically, the run
  pauses and probes instead of marking thousands of files failed.
- **Politeness.** 3 concurrent downloads by default, exponential backoff with
  jitter, and `Retry-After` is honoured on 429s.
- **Content verified against the origin.** See [File integrity](#file-integrity).
- **Pre-flight.** Token validity, destination writability, and free space are
  checked before the first byte. The space check is sized to what *this* run
  will fetch, so `--dry-run` and `--limit`/`--since` runs still work on a
  scratch disk smaller than the whole library.
- **Ctrl-C is safe.** First press finishes current chunks and flushes state;
  partial files are kept. Re-run `sync` to continue.

### Filename collisions

Two different clips can share a filename on the same date. The second one gets
its media id appended (`GX010123_a1b2c3.MP4`). The suffix comes from the
immutable media id and the assignment is recorded in the manifest, so a file
always lands at the same path on every future run — which is what keeps resume
matching. Which of two colliding clips takes the plain name depends on which is
resolved first; once assigned, it never changes.

## File integrity

Every downloaded file is verified two ways.

**Size** — the listing's `file_size` is the baseline. For a chaptered recording
it is the sum across chapters, and the per-chapter sizes come from the response
headers. A file is only renamed from `.part` to its final name once the byte
count matches, so a final-named file is never partial. An item-level check also
confirms the chapters sum to the listed total, which catches a whole chapter
going missing — each individual file can verify fine while the recording is
incomplete.

**Content** — GoPro's API exposes no checksum of any kind, but their CDN is S3
behind CloudFront, and the `ETag` header is an origin-side content hash. Files
are hashed *while streaming*, which costs nothing because the bytes are already
passing through, and compared against that ETag. A mismatch fails the file and
deletes it, so the next run re-fetches it.

### How the ETag works

An S3 multipart ETag is **not** the MD5 of the file:

```
etag = md5(concat of each part's raw md5 digest) + "-" + <part count>
```

A single-part object is therefore `md5(md5_raw(file))-1`. Verified against a
real file:

```
S3 etag        : 1955982c4db2859ebaee6bb5885e7e8d-1
md5(file)      : faaf8b8e2ad35c0dad129d1201089078   wrong
md5(md5(file)) : 1955982c4db2859ebaee6bb5885e7e8d   correct
```

S3 does not publish the part size, and it varies per file (100 MiB and 20 MiB
both appear in one library). The part count constrains it: for N parts covering
`total` bytes, `(N-1)*S < total <= N*S`. Uploaders use round sizes, so the MiB
multiples in that window are the realistic candidates — usually one, sometimes
a few. The downloader hashes against **every** candidate at once.

That makes the verdict honest in both directions:

| outcome | meaning |
|---|---|
| a candidate matches | conclusive proof the bytes match the origin |
| no match, one candidate | real corruption — the file is failed and refetched |
| no match, several candidates | reported `unverified`, never as corruption |

Resumed downloads cannot be hashed from byte zero, so they are recorded as
`unverified` and confirmed later by `verify --deep`.

```bash
gopro-dl status                # counts by integrity state
gopro-dl backfill-etags        # fetch checksums for files downloaded earlier
gopro-dl verify --deep         # re-read from disk and check against the origin
```

`backfill-etags` costs one API call per item plus one HEAD per file and
transfers no media, so it is cheap to run. `verify --deep` re-reads everything,
which is bounded by your link (roughly 4 hours for 1.4 TiB over gigabit) — run
it once at the end, not during a download, or the two compete for bandwidth.

## Capture dates inside the files

The `YYYY-MM-DD/` folders always come from GoPro's `captured_at`, but the date
stored *inside* a downloaded file often does not match it — and that is the
date Photos, Lightroom and every NAS photo app actually sort by.

Two different things go wrong, and `fix-dates` handles both:

* **Photos usually have no capture date at all.** GoPro serves plenty of JPEGs
  whose Exif block contains nothing but orientation and pixel dimensions — no
  `DateTimeOriginal`. With no date to read, apps fall back to the file's
  modification time, which for a downloaded file is *when you downloaded it*,
  so a 2015 clip shows up as today. `fix-dates` adds the tag.
* **Videos are usually fine**, but a wrong `mvhd`/`tkhd`/`mdhd` creation time
  is corrected in place, without rewriting the container.

```bash
gopro-dl fix-dates --dry-run          # report only, changes nothing
gopro-dl fix-dates --since 2024-01-01 # bound it to a date range
gopro-dl fix-dates                    # repair
```

It reads the manifest and the files on disk only — no API calls, no media
transferred. Pass the same `--timezone` your sync used (or let the saved config
supply it), or photos GoPro gives no timezone for will get an Exif time that
disagrees with the folder they sit in.

What it writes:

| Where | Value | Why |
| --- | --- | --- |
| Exif `DateTimeOriginal`, `DateTimeDigitized`, `DateTime` | capture-**local** wall time | Exif dates carry no zone; this is the same conversion that names the folder |
| Exif `GPSDateStamp` / `GPSTimeStamp` | UTC | the spec defines these as UTC |
| `mvhd` / `tkhd` / `mdhd` creation time | UTC | ISO-BMFF defines these as UTC |
| file modification time | the capture instant | what apps fall back to, including for files nothing can be embedded in |

Photos are rebuilt (prefix + new Exif segment + the original bytes, written to
a temp file and atomically renamed) because adding a tag changes the file's
size; the compressed image itself is copied through untouched. Videos are only
ever patched in place — a few bytes overwritten, never a container rewrite, so
a 3 GB clip costs a handful of writes rather than 3 GB of NAS traffic. A photo
whose Exif holds a MakerNote or a thumbnail IFD is reported and skipped rather
than rebuilt, since relocating its values would invalidate those.

**This intentionally changes the bytes**, so a repaired file no longer matches
the origin's ETag. `fix-dates` moves the origin's size and hash aside into
`origin_size`/`origin_checksum` and records a local md5 in their place, so
`verify --deep` keeps working and `verify --fix` will not delete a repaired
file and download the dateless one again. Computing that md5 means reading each
repaired file end to end, so a run that repairs large videos is bound by your
link to the destination, not by the size of the edits — use `--since`/`--until`
to do it in batches. Re-running is safe and idempotent:
files that already agree with GoPro are left byte-for-byte alone.

### When the camera clock was reset

A GoPro that loses power resets its clock, so a whole morning's filming comes
back dated `2015-01-01` — but with the clips' *relative* times intact
(`02:43`, `03:05`, `04:13`). GoPro knows the true date, yet reports a single
timestamp for every clip in that batch, so writing it verbatim would fix the
date and collapse the session onto one second, losing the ordering.

So when a folder's clips carry distinct embedded times, `fix-dates` slides the
whole folder by the one offset that lands its earliest clip on GoPro's time.
The dates come out right and the gaps between clips survive:

```
2015-01-01 02:43:01  ->  2017-02-11 04:49:12
2015-01-01 03:05:26  ->  2017-02-11 05:11:37
2015-01-01 03:19:13  ->  2017-02-11 05:25:24
```

Folders whose clips already share one identical timestamp have no ordering to
protect and simply get GoPro's value. `--flatten-to-api` forces that everywhere.

Cameras commonly write capture-*local* time into the video field the spec calls
UTC. That is a convention rather than damage, so a video matching either
reading is left alone; `--video-utc` normalises those too. `--keep-mtime`
leaves modification times untouched, and `--tolerance N` sets how many seconds
of drift to accept before rewriting (default 120).

## Downloading straight to a NAS

Media can be written directly to an SMB/NFS mount — **the manifest just can't
live there**, and `gopro-dl` handles that itself: if `--dest` is detected as a
network mount and you haven't passed `--manifest-dir`, it automatically keeps
the manifest and logs on local disk instead, under
`~/Library/Application Support/gopro-dl/manifests/<name>-<hash>/` (macOS) /
`~/.config/gopro-dl/manifests/<name>-<hash>/` (Linux) — keyed by the
destination path, so re-running against the same NAS folder finds it again.
You'll see a one-line notice when this kicks in:

```bash
gopro-dl sync --dest /Volumes/GoPro --token-file ~/gopro-backup/token --timezone Europe/Brussels
```

```
/Volumes/GoPro looks like a network mount -- keeping the manifest and logs
locally at /Users/jane/Library/Application Support/gopro-dl/manifests/GoPro-3f9a1c2b
instead (SMB/NFS can corrupt SQLite's WAL journal). Override with --manifest-dir.
```

Detection is best-effort (macOS via `mount`, Linux via `df --output=fstype`)
and only used to steer the manifest, never to block a run — if it can't tell,
it just leaves the manifest colocated with `--dest` as usual. Pass
`--manifest-dir` yourself to pick an exact location instead (e.g. to keep
several NAS destinations' manifests together under one folder you control):

```bash
gopro-dl sync \
  --dest /Volumes/GoPro \
  --manifest-dir ~/gopro-backup/.gopro-dl \
  --token-file ~/gopro-backup/token \
  --timezone Europe/Brussels
```

SMB silently refuses SQLite's WAL journal (it degrades to `journal_mode=delete`)
and network file locking is unreliable, so a manifest on the share risks
`database is locked` errors or corruption. Media files themselves are fine:
chunked writes, `fsync`, atomic `os.replace` and seek/truncate resume were all
verified working over SMB.

Note also that macOS smbfs truncates `statvfs` block counts to 32 bits, so any
SMB volume over 4 TiB under-reports its free space by exactly 2**32 blocks
through `shutil.disk_usage`. The pre-flight check reads `df` instead, so a large
share is measured correctly rather than wrongly refused.

Run `gopro-dl setup --dest /Volumes/GoPro --token-file ~/gopro-backup/token`
once to save these, then just run `gopro-dl sync`.

If you would rather stage locally and copy afterwards:

```bash
rsync -a --progress /local/gopro/ /Volumes/Downloads/gopro-backup/
```

Run `gopro-dl verify` before and after any copy.

### If the destination is smaller than the library

Pre-flight refuses to start a run that cannot fit (exit code 1). Work through
the library in date ranges instead, copying each batch off before the next:

```bash
gopro-dl sync --until 2019-12-31          # oldest batch
# ... move/archive that batch elsewhere, then:
gopro-dl sync --since 2020-01-01 --until 2022-12-31
```

The manifest tracks what is already done, so batches never overlap and nothing
is fetched twice.

## Running in Docker

A `Dockerfile` and `docker-compose.example.yml` are included, aimed at
running `gopro-dl` on a NAS (this was written with a UGREEN NAS's Docker
support in mind, but any Docker host works the same way).

```bash
cp docker-compose.example.yml docker-compose.yml
# edit the two host paths in it: your config dir and your NAS media path
```

The interactive `setup` wizard opens a real browser window to log in, which
doesn't work in a headless container. Run it on a machine with a display
instead, then copy what it wrote into the folder you're about to mount:

```bash
gopro-dl setup --dest /path/that/matches/your/GOPRO_DEST
cp ~/Library/Application\ Support/gopro-dl/{token,config.env} ./gopro-dl-config/
```

Then bring the container up:

```bash
docker compose up -d --build
```

By default it runs `gopro-dl sync --non-interactive` once and exits. Set
`GOPRO_DL_CRON_SCHEDULE` (standard 5-field cron syntax, e.g. `"0 3 * * *"`
for daily at 03:00) to instead have the container install that as a cron
job and stay running as a scheduler — `docker compose logs -f` shows each
run's output. This is a container-only setting read by the entrypoint
script, not one of `gopro-dl`'s own `GOPRO_*` config variables.

Everything else is configured the normal way, via environment variables in
the compose file (`GOPRO_DEST`, `GOPRO_DL_HOME`, `GOPRO_TIMEZONE`, ...) or
the `config.env` you copied in — see `.env.example` for the full list.

## API notes (things that will bite you)

Established by inspecting a real account. Each of these caused a bug that
looked like success:

1. **`_embedded.files` is a trap.** For videos it points at
   `high_res_proxy_mp4` — a 1080p transcode at roughly half the bytes of the
   original — while for photos it points at the real source, so it looks
   plausible. Only `variations[label == "source"]` is trustworthy. Downloading
   the proxy produces complete, valid, *wrong* files.

2. **Chapters share the label `"source"`.** A long recording exposes several
   variations all labelled `source`, distinguished only by the trailing number
   in the URL path (`/source/default/1.mp4`, `2.mp4`, …). Taking the first
   match silently discards the rest of the recording.

3. **The listing `file_size` is the sum across chapters**, and it is exact —
   it matched the summed chapter sizes byte-for-byte on every chaptered item
   tested. The individual variations carry no size at all.

4. **`captured_at_timezone` is essentially always absent** (1,216 of 1,217
   items). Without `--timezone`, folder dates fall back to UTC and evening
   clips land in the previous day. DST is applied per clip.

5. **No checksums anywhere in the API** — not in the listing, the download
   response, or any variation. The CDN's `ETag` is the only content hash
   available.

6. **`MultiClipEdit` items** are GoPro-generated edits, not originals. They are
   excluded from the request and defensively filtered again on the response.

### macOS + SMB notes

1. **SQLite WAL is silently unavailable on SMB** — it degrades to
   `journal_mode=delete`, and network file locking is unreliable. Keep the
   manifest on a local disk with `--manifest-dir`.
2. **statvfs truncates to 32 bits on smbfs**, so `shutil.disk_usage`
   under-reports any share over 4 TiB by exactly 2**32 blocks. Pre-flight reads
   `df` instead, or it would refuse a destination with terabytes free.
3. **WiFi halves your throughput** on a download-to-NAS run, because every byte
   crosses the air twice (CDN → Mac, Mac → NAS) on a half-duplex medium.
   Measured 32.5 MiB/s over 802.11ax vs 75 MiB/s over gigabit ethernet — 2.3x.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

CI runs on every push and pull request: ruff, then the suite across Python
3.11/3.12/3.13 on both Linux and macOS. macOS is not incidental — the
free-space check parses `df` precisely because smbfs truncates statvfs, and
that path only exists on Darwin. Two extra guards catch what unit tests cannot:
a CLI smoke test run from outside the source tree (packaging and entry-point
breakage) and a check that a clean environment yields no usable token
(config-precedence regressions).

Note the test suite never runs `playwright install chromium` and doesn't need
to: every test that exercises `browser_login` fakes Playwright out entirely
(`tests/test_browser_login.py`), and an autouse fixture stubs the module's
two entry points everywhere else, so no test ever launches a real browser.

### Releasing

The version lives in one place, `src/gopro_dl/__init__.py:__version__`. To cut
a release: bump it, commit, then push a matching tag —

```bash
git tag v0.2.0 && git push origin v0.2.0
```

`.github/workflows/release.yml` builds the sdist/wheel and publishes to PyPI
via [trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC, no
stored token). One-time setup on PyPI: add this repo as a trusted publisher
for the `gopro-media-downloader` project, workflow `release.yml`, environment
`pypi`.

84 tests, all against mocked API responses — no network and no token needed.
They cover pagination, source-variation selection (including the proxy trap and
multi-chapter fan-out), capture-date foldering and DST boundaries, collision
stability, manifest idempotency, the resume state machine
(206 / ignored-Range 200 / 416 / expired signed URL / mid-stream interrupt),
S3 ETag verification, the token-expiry pause, the circuit breaker, disk-space
edge cases, and ETag backfill.

Several encode bugs found in live testing, with the real-world values that
exposed them — the proxy-vs-source sizes, the 39-part and 192-part ETags, and
the smbfs 2**32 truncation — so a regression would be caught immediately.
