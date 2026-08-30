"""Content verification against S3/CloudFront ETags.

GoPro's API exposes no checksum, but the CDN serves an S3 ETag, which for a
multipart object is:

    md5(concat of each part's raw md5 digest) + "-" + <part count>

A single-part object is therefore md5(md5_raw(file))-1, not md5(file). The part
size is not published, but it is constrained: for N parts of size S covering
`total` bytes, (N-1)*S < total <= N*S. Uploaders use round sizes, so the MiB
multiples in that window are the realistic candidates -- usually a handful.

We hash against every candidate at once while streaming, which costs nothing
extra because the bytes are already passing through. A match is conclusive: it
proves the bytes we wrote are the bytes S3 holds.
"""

from __future__ import annotations

import hashlib
import math
import re

MIB = 1024 * 1024
MAX_CANDIDATES = 8

_ETAG_RE = re.compile(r'^"?([0-9a-f]{32})(?:-(\d+))?"?$')


def etag_header(headers) -> str | None:
    """Unquoted ETag from a response's headers, or None if absent."""
    return (headers.get("ETag") or "").strip('"') or None


def parse_etag(header: str | None) -> tuple[str, int] | None:
    """('<hex>', parts) from an ETag header, or None if it is not one."""
    if not header:
        return None
    match = _ETAG_RE.match(header.strip())
    if not match:
        return None
    return match.group(1), int(match.group(2) or 1)


def candidate_part_sizes(total: int, parts: int) -> list[int]:
    """Plausible part sizes for a multipart object of `total` bytes.

    Returns the MiB multiples consistent with the part count, smallest first.
    """
    if parts <= 1 or total <= 0:
        return [max(total, 1)]
    low = math.ceil(total / parts)
    high = (total - 1) // (parts - 1)
    if high < low:
        return []
    first = math.ceil(low / MIB) * MIB
    return list(range(first, high + 1, MIB))[:MAX_CANDIDATES]


class MultipartHasher:
    """Incremental S3 multipart ETag for one candidate part size."""

    def __init__(self, part_size: int) -> None:
        self.part_size = max(int(part_size), 1)
        self._part_digests: list[bytes] = []
        self._current = hashlib.md5()
        self._in_part = 0
        self._empty = True

    def update(self, chunk: bytes) -> None:
        self._empty = False
        view = memoryview(chunk)
        while view:
            room = self.part_size - self._in_part
            take = view[:room]
            self._current.update(take)
            self._in_part += len(take)
            view = view[len(take):]
            if self._in_part == self.part_size:
                self._part_digests.append(self._current.digest())
                self._current = hashlib.md5()
                self._in_part = 0

    def hexdigest(self) -> str:
        digests = list(self._part_digests)
        if self._in_part or not digests:
            digests.append(self._current.digest())
        if len(digests) == 1 and self._empty:
            return hashlib.md5(b"").hexdigest()
        combined = hashlib.md5(b"".join(digests)).hexdigest()
        return f"{combined}-{len(digests)}"


class EtagVerifier:
    """Hashes a stream against every plausible part size at once."""

    def __init__(self, etag: str | None, total: int | None) -> None:
        self.expected = etag
        self.parsed = parse_etag(etag)
        self.hashers: list[MultipartHasher] = []
        self.plain = None
        if not self.parsed or not total:
            return
        digest, parts = self.parsed
        if parts <= 1:
            # A non-multipart ETag is a plain md5; a 1-part multipart one is
            # md5(md5_raw(file)). Track both so either shape verifies.
            self.plain = hashlib.md5()
            self.hashers = [MultipartHasher(max(total, 1))]
        else:
            self.hashers = [MultipartHasher(s) for s in candidate_part_sizes(total, parts)]

    @property
    def active(self) -> bool:
        return bool(self.hashers or self.plain is not None)

    @property
    def ambiguous(self) -> bool:
        """True when several part sizes fit, so a non-match proves nothing."""
        return len(self.hashers) > 1

    def update(self, chunk: bytes) -> None:
        if self.plain is not None:
            self.plain.update(chunk)
        for hasher in self.hashers:
            hasher.update(chunk)

    def result(self) -> str | None:
        """'ok', 'mismatch', or None when no conclusion can be drawn."""
        if not self.expected or not self.active:
            return None
        want = self.expected.strip('"')
        if self.plain is not None and self.plain.hexdigest() == want:
            return "ok"
        for hasher in self.hashers:
            if hasher.hexdigest() == want:
                return "ok"
        # With one candidate the comparison is decisive; with several, a
        # non-match may just mean the uploader used an unusual part size.
        return "mismatch" if not self.ambiguous else None


def etag_for_file(path, etag: str | None) -> str | None:
    """Re-verify a file already on disk. Returns 'ok'/'mismatch'/None."""
    import os

    parsed = parse_etag(etag)
    if not parsed:
        return None
    total = os.path.getsize(path)
    verifier = EtagVerifier(etag, total)
    if not verifier.active:
        return None
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 * MIB), b""):
            verifier.update(chunk)
    return verifier.result()
