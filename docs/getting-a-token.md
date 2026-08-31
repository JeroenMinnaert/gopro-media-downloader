# Getting a token

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

---

[← Documentation index](README.md) · [Project README](../README.md)
