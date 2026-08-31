"""Post-hoc verification of files already on disk."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .integrity import MIB, etag_for_file
from .manifest import Manifest


@dataclass
class VerifyReport:
    checked: int = 0
    ok: int = 0
    missing: list[str] = field(default_factory=list)
    wrong_size: list[tuple[str, int, int]] = field(default_factory=list)
    bad_checksum: list[str] = field(default_factory=list)
    unverifiable: list[str] = field(default_factory=list)

    @property
    def problems(self) -> int:
        return len(self.missing) + len(self.wrong_size) + len(self.bad_checksum)


def file_hash(path: Path, algo: str) -> str | None:
    try:
        digest = hashlib.new(algo)
    except ValueError:
        return None
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(manifest: Manifest, dest: Path, deep: bool = False, fix: bool = False) -> VerifyReport:
    """Re-check every file the manifest believes is done."""
    report = VerifyReport()

    def requeue(row, path: Path | None = None) -> None:
        if not fix:
            return
        if path is not None:
            path.unlink()
        manifest.reset_file(row["id"])
        manifest.refresh_item_state(row["media_id"])

    for row in manifest.all_files(states=["done"]):
        report.checked += 1
        path = dest / row["target_path"]
        if not path.exists():
            report.missing.append(row["target_path"])
            requeue(row)
            continue

        actual = path.stat().st_size
        expected = row["expected_size"]
        if expected and actual != expected:
            report.wrong_size.append((row["target_path"], actual, expected))
            requeue(row, path)
            continue

        # "unverifiable" and "ok" are exclusive: a file nothing could be
        # checked against has not passed, it simply has no verdict.
        unverifiable = not expected

        if deep and row["checksum"] and row["checksum_algo"] in ("s3-etag", "etag"):
            # Re-hash against the origin's ETag. Inconclusive (None) means the
            # upload used a part size we cannot pin down -- not corruption.
            verdict = etag_for_file(path, row["checksum"])
            if verdict == "mismatch":
                report.bad_checksum.append(row["target_path"])
                requeue(row, path)
                continue
            unverifiable = unverifiable or verdict is None
        elif deep and row["checksum"] and row["checksum_algo"] is not None:
            digest = file_hash(path, row["checksum_algo"])
            if digest is None:
                unverifiable = True  # an algorithm this Python cannot hash
            elif digest.lower() != str(row["checksum"]).lower():
                report.bad_checksum.append(row["target_path"])
                requeue(row, path)
                continue

        if unverifiable:
            report.unverifiable.append(row["target_path"])
        else:
            report.ok += 1
    return report
