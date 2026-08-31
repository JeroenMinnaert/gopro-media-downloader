# Downloading straight to a NAS

Media can go straight to an SMB/NFS mount — **the manifest can't**. SMB
silently refuses SQLite's WAL journal (degrading to `journal_mode=delete`) and
network file locking is unreliable, risking `database is locked` or corruption.
Media files are fine: chunked writes, `fsync`, atomic `os.replace` and
seek/truncate resume were all verified over SMB.

`gopro-dl` handles this itself. If `--dest` looks like a network mount and you
haven't passed `--manifest-dir`, the manifest and logs go to local disk under
`~/Library/Application Support/gopro-dl/manifests/<name>-<hash>/` (macOS) or
`~/.config/gopro-dl/manifests/<name>-<hash>/` (Linux), keyed by destination
path so a re-run finds it again:

```
/Volumes/GoPro looks like a network mount -- keeping the manifest and logs
locally at ~/Library/Application Support/gopro-dl/manifests/GoPro-3f9a1c2b
instead (SMB/NFS can corrupt SQLite's WAL journal). Override with --manifest-dir.
```

Detection is best-effort (macOS `mount`, Linux `df --output=fstype`) and only
steers the manifest, never blocks a run. Pass `--manifest-dir` to choose the
location yourself:

```bash
gopro-dl sync --dest /Volumes/GoPro \
  --manifest-dir ~/gopro-backup/.gopro-dl \
  --token-file ~/gopro-backup/token --timezone Europe/Paris
```

Or run `gopro-dl setup --dest /Volumes/GoPro` once to save it and just
`gopro-dl sync` thereafter.

macOS smbfs truncates `statvfs` block counts to 32 bits, so any SMB volume over
4 TiB under-reports free space by exactly 2**32 blocks through
`shutil.disk_usage`. Pre-flight reads `df` instead, so a large share is measured
correctly rather than wrongly refused.

To stage locally and copy afterwards, `rsync -a --progress /local/gopro/
/Volumes/gopro-backup/` — and run `gopro-dl verify` before and after any copy.

### If the destination is smaller than the library

Pre-flight refuses a run that cannot fit (exit 1). Work through the library in
date ranges, copying each batch off before the next:

```bash
gopro-dl sync --until 2019-12-31          # oldest batch
# ... move/archive that batch elsewhere, then:
gopro-dl sync --since 2020-01-01 --until 2022-12-31
```

The manifest tracks what's done, so batches never overlap.

---

[← Documentation index](README.md) · [Project README](../README.md)
