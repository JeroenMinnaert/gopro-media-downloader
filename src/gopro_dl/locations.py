"""Where gopro-dl's own per-user state lives -- the token, its config file,
the browser-login profile, and any NAS-redirected manifests.

Deliberately separate from `config.py`'s manifest/log directory, which stays
colocated with a specific `--dest` on purpose (see CLAUDE.md) so a downloads
folder can be moved or copied as a self-contained unit.

One rule: GOPRO_DL_HOME set -> use it; unset -> the OS's standard per-user
app directory (via `platformdirs`). Nothing here inspects how the package
was installed or loads any file -- for local development, export
GOPRO_DL_HOME yourself (e.g. from a checked-in `.envrc`, see CLAUDE.md) so
what you get is exactly what a real user gets, just relocated.

Resolved once, explicitly, by whoever needs it (`cli.py: main()`) and carried
on `Config` from there on -- never as a module-level constant computed at
import time, so there is nothing to reload or monkeypatch four different
ways in tests.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import platformdirs


def _resolve_root() -> Path:
    """The GOPRO_DL_HOME/platformdirs rule, as a plain function so tests can
    exercise it directly -- AppDirs.resolve() itself gets monkeypatched
    wholesale in the test suite for isolation (see conftest.py)."""
    home = os.environ.get("GOPRO_DL_HOME")
    return Path(home).expanduser() if home else Path(platformdirs.user_config_dir("gopro-dl"))


@dataclass(frozen=True)
class AppDirs:
    """One root holding everything gopro-dl keeps about *you*, not about any
    one destination."""

    root: Path

    @property
    def token_file(self) -> Path:
        return self.root / "token"

    @property
    def config_file(self) -> Path:
        return self.root / "config.env"

    @property
    def browser_profile(self) -> Path:
        return self.root / "browser-profile"

    def manifest_dir_for(self, dest: Path) -> Path:
        """Deterministic local manifest/log location for a --dest that turns
        out to live on a network mount (see preflight.is_network_filesystem)
        -- SMB in particular silently refuses SQLite's WAL journal,
        corrupting or locking the manifest if left there. Keyed by the
        resolved destination path so re-running against the same NAS
        destination finds it again.
        """
        resolved = dest.resolve()
        digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:8]
        return self.root / "manifests" / f"{resolved.name or 'root'}-{digest}"

    @classmethod
    def resolve(cls) -> AppDirs:
        return cls(root=_resolve_root())


def default_dest() -> Path:
    """~/Downloads/GoPro, computed on demand rather than at import time --
    platformdirs' user_downloads_dir() does real I/O on Linux (it parses
    ~/.config/user-dirs.dirs), so this stays a function, called only from
    the one place that needs it (config.py's fallback when no --dest/
    GOPRO_DEST is set).

    Unrelated to GOPRO_DL_HOME: that only relocates the tool's own state
    (token, config, browser profile). Set GOPRO_DEST explicitly (e.g. in a
    local .envrc) to also relocate where media lands.
    """
    return Path(platformdirs.user_downloads_dir()) / "GoPro"
