# API notes (things that will bite you)

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

---

[← Documentation index](README.md) · [Project README](../README.md)
