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
3.11/3.12/3.13/3.14 on **both** ubuntu-latest and macos-latest — macOS is not
incidental, see Architecture below. It also smoke-tests the packaged entry
point from outside the source tree and checks that a clean environment (no
`GOPRO_*` vars, no config file) yields no usable token.

Publishing is a separate concern with gates that are not all visible in the
YAML — see **[docs/releasing.md](docs/releasing.md)** for the version bump, what
each job in `release.yml` does, why Docker images are cut from releases rather
than from `main`, and what a release that stops half way is telling you. Read it
before changing anything under `.github/workflows/` or the version string.

## Architecture

See **[ARCHITECTURE.md](ARCHITECTURE.md)** — the pipeline stage by stage, the
concurrency model, and the cross-cutting invariants that are easy to break by
accident. Read it before editing anything in `src/`.
