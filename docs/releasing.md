# Cutting a release

Everything a release touches — PyPI, Docker Hub, the tag, the changelog — plus
what to do when one stops half way.

## The steps

```bash
# 1. bump the version the artifacts are built from
#    src/gopro_dl/__init__.py: __version__ = "0.1.2"

# 2. move the Unreleased section of CHANGELOG.md into a dated 0.1.2 section,
#    and add its compare link at the bottom

# 3. merge that, then tag the merge commit
git checkout main && git pull
git tag -a v0.1.2 -m "v0.1.2"
git push origin v0.1.2
```

The tag name does not set the version: hatch reads `__version__`. They are
compared for you (below), but the bump is a real step, not a formality.

## What runs, in order

`release.yml`, on a `v*` tag push only:

| job | what it does |
| --- | --- |
| `version` | fails in seconds if the tag and `__version__` disagree |
| `test` | calls `ci.yml`, so a tag gets the same lint and full OS/Python matrix as a push to `main` |
| `build` | sdist + wheel |
| `publish` | uploads to PyPI via trusted publishing (OIDC, no token) |

Each job `needs:` the one above, so a failure anywhere stops the rest —
`publish` reports `skipped` rather than running.

`docker-publish.yml` then triggers on **Release** completing successfully, and
builds `linux/amd64` + `linux/arm64`. Images are therefore cut from releases,
never from `main`: an image exists exactly when a version does, and has been
through the same matrix, build and PyPI publish. Tags come from the version —
`v0.1.2` publishes `:0.1.2`, `:0.1` and `:latest`, so `:latest` means the newest
release rather than the newest commit. A pre-release (`v0.2.0-rc1`) publishes
only its exact version, so it never becomes what `:latest` or `:0.2` resolve to.

## The gates that are not in the YAML

Two of them, and both have to be right or the release stops at the publish
step with an error that makes no sense from the workflow file alone:

- **PyPI's trusted publisher is constrained to the `pypi` environment.** A job
  that does not opt into that environment cannot mint the OIDC token, however
  green everything else is. Configured at the project's publishing settings on
  PyPI.
- **The `pypi` GitHub environment accepts only `v*` tags** — one deployment
  policy entry, of type *tag*, with branch policies off. No branch can reach
  it, which is why `release.yml` offers no `workflow_dispatch`: such a run
  could only ever be turned away after paying for the whole matrix.

Together they mean a version tag is the only thing that can publish.

## Secrets

`DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (an access token, not the account
password) are repository secrets. Create the target repository on Docker Hub
**before** the first push, with the visibility you want — Docker Hub does not
necessarily default an auto-created repository to private. PyPI needs no
secret at all.

## When it goes wrong

| symptom | what it means |
| --- | --- |
| `version` fails immediately | the tag and `__version__` disagree. Delete the tag, bump, re-tag |
| `publish` blocked on the environment | the ref is not a `v*` tag, or the environment policy no longer matches |
| `publish` fails with "file already exists" | that version is on PyPI already. Version numbers are permanent — yank, then release the next patch; never re-upload |
| `build` never starts | usually an Actions billing or spending-limit problem, not your code |
| Docker login fails | the two secrets are missing or expired |
| PyPI still shows the old version | index and JSON caches lag by a minute or two. Check `https://pypi.org/pypi/<name>/<version>/json`, or install with `--no-cache-dir` |

A release that failed for an infrastructure reason does not need a new tag:
`gh run rerun <id> --failed` re-runs only the failed jobs and keeps the
original tag context.

---

[← Documentation index](README.md) · [Project README](../README.md)
