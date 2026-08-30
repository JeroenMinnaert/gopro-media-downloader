"""Where gopro-dl keeps its own, global, per-user state.

This is deliberately separate from `config.py`'s manifest/log directory,
which stays colocated with a specific `--dest` on purpose (see CLAUDE.md) so
that a downloads folder can be moved or copied as a self-contained unit. What
lives here instead is state that belongs to the *tool*, not to any one
destination -- the saved token, the persisted browser-login profile, and the
default destination itself -- so it follows the OS's standard locations (via
`platformdirs`) rather than loose dotfiles or a bare relative path that
depends on the caller's current directory.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import platformdirs

CONFIG_DIR = Path(platformdirs.user_config_dir("gopro-dl"))
DATA_DIR = Path(platformdirs.user_data_dir("gopro-dl"))

DEFAULT_TOKEN_FILE = CONFIG_DIR / "token"


def default_dest() -> Path:
    """~/Downloads/GoPro, computed on demand rather than at import time.

    Unlike user_config_dir/user_data_dir above (cheap env-var reads),
    platformdirs' user_downloads_dir() does real I/O on Linux -- it parses
    ~/.config/user-dirs.dirs -- so this stays a function, called only from
    the one place that needs it (config.py's fallback when no --dest/
    GOPRO_DEST is set), instead of paying that cost on every invocation
    regardless of whether the fallback is even used.
    """
    return Path(platformdirs.user_downloads_dir()) / "GoPro"


def manifest_dir_for(dest: Path) -> Path:
    """Deterministic local manifest/log location for a --dest that turns out
    to live on a network mount (see preflight.is_network_filesystem) -- SMB
    in particular silently refuses SQLite's WAL journal, corrupting or
    locking the manifest if left there. Keyed by the resolved destination
    path so re-running against the same NAS destination finds it again.
    """
    resolved = dest.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:8]
    return DATA_DIR / "manifests" / f"{resolved.name or 'root'}-{digest}"
