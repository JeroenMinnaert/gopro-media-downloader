# File integrity

**Size** — the listing's `file_size` is the baseline (for a chaptered
recording, the sum across chapters; per-chapter sizes come from response
headers). A file is renamed from `.part` only once the byte count matches. An
item-level check confirms the chapters sum to the listed total, catching a
whole chapter going missing — each file can verify fine while the recording is
incomplete.

**Content** — GoPro's API exposes no checksum, but their CDN is S3 behind
CloudFront and the `ETag` is an origin-side content hash. Files are hashed
*while streaming* (free — the bytes are already passing through) and compared.
A mismatch fails and deletes the file, so the next run refetches it.

### How the ETag works

An S3 multipart ETag is **not** the MD5 of the file:

```
etag = md5(concat of each part's raw md5 digest) + "-" + <part count>
```

So a single-part object is `md5(md5_raw(file))-1`. Verified against a real file:

```
S3 etag        : 1955982c4db2859ebaee6bb5885e7e8d-1
md5(file)      : faaf8b8e2ad35c0dad129d1201089078   wrong
md5(md5(file)) : 1955982c4db2859ebaee6bb5885e7e8d   correct
```

S3 doesn't publish the part size and it varies per file (100 MiB and 20 MiB
both appear in one library). The part count constrains it: for N parts covering
`total` bytes, `(N-1)*S < total <= N*S`. Uploaders use round sizes, so the MiB
multiples in that window are the candidates — usually one, sometimes a few. The
downloader hashes against **every** candidate at once, which keeps the verdict
honest in both directions:

| outcome | meaning |
|---|---|
| a candidate matches | conclusive proof the bytes match the origin |
| no match, one candidate | real corruption — failed and refetched |
| no match, several candidates | reported `unverified`, never as corruption |

Resumed downloads can't be hashed from byte zero, so they're recorded
`unverified` and confirmed later by `verify --deep`.

```bash
gopro-dl status                # counts by integrity state
gopro-dl backfill-etags        # fetch checksums for files downloaded earlier
gopro-dl verify --deep         # re-read from disk and check against the origin
```

`backfill-etags` costs one API call per item plus one HEAD per file and moves
no media. `verify --deep` re-reads everything (~3 hours per TiB over gigabit) —
run it at the end, not during a download, or the two compete for bandwidth.

---

[← Documentation index](README.md) · [Project README](../README.md)
