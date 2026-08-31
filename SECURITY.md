# Security Policy

## Supported versions

This is a single-maintainer project with no long-term support branches. Fixes
land on `main` and go out in the next release; please reproduce on the latest
version before reporting.

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Use one of:

- GitHub's [private vulnerability reporting](https://github.com/JeroenMinnaert/gopro-media-downloader/security/advisories/new)
  (preferred — it keeps the discussion attached to the repo)
- Email <2262843+JeroenMinnaert@users.noreply.github.com> with `gopro-media-downloader` in the subject

Expect an acknowledgement within about a week. There is no bounty; credit in
the release notes if you'd like it.

## What is in scope

This tool handles a GoPro Plus bearer token and, optionally, a persisted
browser-login profile. Things worth reporting:

- The saved token, `config.env`, or the browser profile being written or left
  with permissions wider than owner-only, or landing somewhere unexpected.
- The token leaking into logs, `report` output, tracebacks, or an HTTP request
  to any host other than GoPro's own API/CDN.
- A path in `GOPRO_DL_HOME` / `.envrc` resolution that lets another local user
  on a shared machine redirect where the token or cookies are written (the
  `_envrc_is_trustworthy()` ownership check exists for exactly this).
- Any way a crafted API response causes a write outside the destination
  directory — filename or date-folder construction in `paths.py` / `models.py`.

## What is not in scope

- The GoPro API itself, or anything that requires a GoPro account you do not
  control. Report those to GoPro.
- Token expiry, rate limiting, or CAPTCHAs — those are normal operation, see
  the README.
- Findings that require an attacker who already has read access to your user
  account on the machine running the tool. A local token file is readable by
  its owner by design.
