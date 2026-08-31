# Capture dates inside the files

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

---

[← Documentation index](README.md) · [Project README](../README.md)
