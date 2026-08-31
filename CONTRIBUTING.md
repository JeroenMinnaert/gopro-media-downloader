# Contributing

Bug reports, reproductions, and small focused pull requests are all welcome.
For anything large, open an issue first — this tool is deliberately narrow in
scope and it's cheaper to agree on the shape before you write it.

## Getting set up

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

The suite is fully mocked (respx for HTTP): no network access, no GoPro
account, and no token needed to run it. No test ever launches a real browser.
The checked-in `.envrc` keeps your token, config, and browser profile inside
the repo's gitignored `.dev-state/` instead of your real application-support
directory — see the Development section of the README for the details.

## Before you open a pull request

- `ruff check src tests` is clean. CI fails otherwise.
- The suite passes. CI runs it on Python 3.11/3.12/3.13 across Linux **and**
  macOS; the macOS leg is not incidental (the free-space check parses `df`
  because smbfs truncates `statvfs`), so don't assume a Linux-only pass is
  enough.
- New behaviour comes with a test. The parsing and path layers (`models.py`,
  `paths.py`) are pure and have no excuse not to be covered directly.

## Things worth knowing before you edit

Read `CLAUDE.md` — it's the architecture tour: the pipeline stage by stage,
the concurrency model, and the cross-cutting invariants that are easy to break
by accident (which variation is the original file, why a `.part` file is only
renamed once its size matches, why `fix-dates` rebuilds JPEGs but only ever
patches MP4s in place). Several of those rules exist because a real
multi-terabyte run found the bug the hard way.

If you're fixing something you hit against the live API, please include the
real values that exposed it — sizes, ETags, a redacted response body. The
existing regression tests are built out of exactly that, and it's the most
useful thing a report can carry.

## Reporting bugs

Use the issue templates. For a security problem, don't open an issue at all —
see [SECURITY.md](SECURITY.md).
