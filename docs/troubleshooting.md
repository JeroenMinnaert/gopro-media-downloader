# Troubleshooting

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
`--manifest-dir` yourself. See [NAS](nas.md).

### It refuses to start: not enough free space

Pre-flight sizes the run and exits 1 rather than filling your disk halfway.
Free up space, or work in date ranges — see
[If the destination is smaller than the library](nas.md#if-the-destination-is-smaller-than-the-library).

### Everything is dated today in Photos, Lightroom or my NAS app

The folders are right but the date *inside* the file is missing — many GoPro
JPEGs carry no `DateTimeOriginal`, so apps fall back to your download time.
`gopro-dl fix-dates --dry-run`, then `gopro-dl fix-dates`. See
[Capture dates](capture-dates.md).

### Files show as `unverified` in `status`

Not corruption — a resumed download can't be hashed from byte zero, and some
files have several plausible S3 part sizes, so no verdict is claimed that
can't be proven. Run `gopro-dl backfill-etags` then `gopro-dl verify --deep`
after the sync. See [File integrity](file-integrity.md).

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

---

[← Documentation index](README.md) · [Project README](../README.md)
