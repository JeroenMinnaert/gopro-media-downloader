# gopro-media-downloader

[![CI](https://github.com/JeroenMinnaert/gopro-media-downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/JeroenMinnaert/gopro-media-downloader/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Download your entire GoPro Plus cloud library in **original quality**, into
flat `YYYY-MM-DD/` folders named by **capture date** — resumable, verified, and
built for a multi-terabyte run straight to a NAS.

The GoPro website caps bulk downloads and hands you transcodes. This uses the
same API the site does, picks the `source` variation (the original file off the
camera), verifies every file against the origin's own checksum, and keeps a
manifest so an interrupted run resumes at the exact byte.

```
~/Downloads/GoPro/            # default destination -- override with --dest
├── 2023-07-14/
│   ├── GX010123.MP4
│   └── GOPR0456.JPG
├── 2023-07-15/
│   └── GX010124.MP4
└── .gopro-dl/                # manifest.db + run logs
```

> **Not affiliated with, endorsed by, or supported by GoPro, Inc.** "GoPro" and
> "GoPro Plus" are trademarks of their respective owner, used here only to say
> what this tool talks to. It drives the same undocumented internal API the
> GoPro web app uses, which can change or disappear without notice and take
> this tool with it ([known sharp edges](docs/api-notes.md)). Intended for
> downloading media from your own account. Provided as-is under the MIT
> licence, with no warranty.

## Install

Requires Python 3.11+. Tested on macOS and Linux.

```bash
pipx install gopro-media-downloader
```

Chromium is downloaded once, automatically, the first time a browser login is
needed. To run headless on a NAS or server, see [Docker](docs/docker.md).

## Quickstart

```bash
gopro-dl setup                  # log into GoPro, pick a destination
gopro-dl sync --dry-run         # enumerate the library, download nothing
gopro-dl sync --limit 5         # fetch five real files as a smoke test
gopro-dl sync                   # the real run -- Ctrl-C is safe
```

`setup` opens GoPro's own login page in a browser, takes the session token, and
saves your destination and timezone; afterwards every command is a bare
one-liner from any directory. **It never sees your password** — there is no
username/password handling in this tool. See
[Getting a token](docs/getting-a-token.md) for the manual route and for where
credentials are stored.

Steps 2 and 3 earn their place: `--dry-run` tells you how many items and bytes
are coming, and `--limit 5` puts five real files on disk — so a wrong
destination or full disk surfaces in two minutes rather than four hours.

## Usage

```bash
gopro-dl sync                   # the real run, resumable
gopro-dl status                 # counts, bytes and integrity state
gopro-dl report --failed-only   # what failed and why
gopro-dl verify --deep          # re-hash and compare against the origin ETag
gopro-dl backfill-etags         # fetch checksums for files downloaded earlier
gopro-dl fix-dates              # repair capture dates inside the files
gopro-dl retry                  # reset failed files, then sync again
gopro-dl token                  # is the current token still valid?

# unattended: polls the token file instead of prompting on expiry
nohup gopro-dl sync --non-interactive --quiet > ~/gopro-sync.log 2>&1 &
```

Common flags: `--since` / `--until`, `--types Video,Photo`, `--timezone`,
`--concurrency N` (max 8), `--limit N`, `--quiet`, `--non-interactive`,
`--retry-failed`, `--no-manifest-refresh`, `--manifest-dir`.
Config precedence is flag → environment variable → config file → default;
`.env.example` lists every key. Full reference: `gopro-dl --help`.

## How it works

- **Manifest first** — the whole library is enumerated into SQLite before
  anything downloads, so progress and ETA are meaningful from the first byte.
- **Resume at two levels** — correctly sized files are skipped; `.part` files
  resume via HTTP `Range`, with servers that ignore it detected and restarted.
- **Verified against the origin** — every file is hashed while streaming and
  checked against the CDN's S3 ETag; a mismatch is refetched, never silently
  kept. ([how](docs/file-integrity.md))
- **Chapter-aware** — one long recording is several `GX01/GX02/...` files;
  each is tracked separately so a recording resumes chapter by chapter.
- **Built to be interrupted** — Ctrl-C keeps partial files; re-run `sync` to
  continue. Token expiry mid-run pauses rather than fails.
- **Circuit breaker and pre-flight** — an outage pauses and probes instead of
  failing thousands of files; token, writability and free space are checked
  before the first byte.
- **Capture dates repaired in the files** — GoPro serves many JPEGs with no
  `DateTimeOriginal`, so Photos and Lightroom sort them as downloaded today.
  `fix-dates` writes the real date. ([how](docs/capture-dates.md))

## Documentation

| Page | What's in it |
| --- | --- |
| [Getting a token](docs/getting-a-token.md) | The `setup` wizard, credential storage, manual cookie route |
| [Troubleshooting](docs/troubleshooting.md) | Expired tokens, `database is locked`, free-space refusals, wrong dates, slow transfers |
| [How it stays safe](docs/reliability.md) | Resume, pre-flight, circuit breaker, filename collisions |
| [File integrity](docs/file-integrity.md) | Size and content verification, and the S3 multipart ETag maths |
| [Capture dates](docs/capture-dates.md) | `fix-dates` in detail, including the camera-clock-reset case |
| [Downloading to a NAS](docs/nas.md) | SMB/NFS destinations, why the manifest goes local, batching |
| [Running in Docker](docs/docker.md) | Bundled `Dockerfile`, headless setup, scheduled runs |
| [API notes](docs/api-notes.md) | The GoPro API's sharp edges |

## Contributing

Setup, house rules and what to put in a bug report:
[CONTRIBUTING.md](CONTRIBUTING.md). How the code fits together:
[ARCHITECTURE.md](ARCHITECTURE.md). Release notes: [CHANGELOG.md](CHANGELOG.md).

```bash
git clone https://github.com/JeroenMinnaert/gopro-media-downloader
cd gopro-media-downloader
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q && .venv/bin/ruff check src tests
```

196 tests, fully mocked — no network, no token, no browser. CI runs across
Python 3.11/3.12/3.13 on Linux and macOS.

Found a way to leak the token or write outside `--dest`? Report it privately
via [SECURITY.md](SECURITY.md), not an issue.

## License

MIT — see [LICENSE](LICENSE).
