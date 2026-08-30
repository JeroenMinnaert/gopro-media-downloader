"""Where gopro-dl keeps its own, global, per-user state.

This is deliberately separate from `config.py`'s manifest/log directory,
which stays colocated with a specific `--dest` on purpose (see CLAUDE.md) so
that a downloads folder can be moved or copied as a self-contained unit. What
lives here instead is state that belongs to the *tool*, not to any one
destination -- the saved token, the persisted browser-login profile, and the
default destination itself -- so it follows the OS's standard locations (via
`platformdirs`) rather than loose dotfiles or a bare relative path that
depends on the caller's current directory.

In a source checkout (an editable `pip install -e .`), all of it instead
lives under `<repo>/.dev-state/` automatically -- so running the CLI while
developing never touches your real ~/Downloads or ~/Library/Application
Support. Detected by this module's own __file__ resolving inside a checkout
(a sibling `pyproject.toml` + `src/gopro_dl`), which is only ever true for an
editable install -- a real install (pipx, or `pip install` of a built wheel)
always resolves into site-packages instead, so end users are never affected.
Set GOPRO_DL_HOME to override the location explicitly either way, or
GOPRO_DL_FORCE_REAL=1 to use the real OS locations from an editable install
(e.g. to test real-user behavior without a separate install). Either can also
be set in a plain `.env` at the repo root (gitignored) instead of exporting
it every time -- loaded here, before GOPRO_DL_HOME/GOPRO_DL_FORCE_REAL are
read, since they decide where the *app's own* config file lives and so can't
live inside that file themselves.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import platformdirs
from dotenv import load_dotenv


def _source_checkout_root() -> Path | None:
    """The repo root if this file is running from an editable install."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "gopro_dl").is_dir():
            return candidate
    return None


_checkout_for_dotenv = _source_checkout_root()
if _checkout_for_dotenv is not None:
    load_dotenv(_checkout_for_dotenv / ".env", override=False)


def _resolve_dev_home() -> str | None:
    """GOPRO_DL_HOME if set explicitly, else <checkout>/.dev-state if this is
    an editable install, else None (use the real OS locations).

    GOPRO_DL_FORCE_REAL=1 skips both and always returns None -- for testing
    real-user behavior from an editable install without a separate install.
    Calls _source_checkout_root() itself (rather than reusing the module
    load above) so tests can monkeypatch that one function to control both.
    """
    if os.environ.get("GOPRO_DL_FORCE_REAL"):
        return None
    explicit = os.environ.get("GOPRO_DL_HOME")
    if explicit:
        return explicit
    checkout = _source_checkout_root()
    return str(checkout / ".dev-state") if checkout is not None else None


_DEV_HOME = _resolve_dev_home()

if _DEV_HOME:
    CONFIG_DIR = DATA_DIR = Path(_DEV_HOME).expanduser()
else:
    CONFIG_DIR = Path(platformdirs.user_config_dir("gopro-dl"))
    DATA_DIR = Path(platformdirs.user_data_dir("gopro-dl"))

DEFAULT_TOKEN_FILE = CONFIG_DIR / "token"
DEFAULT_ENV_FILE = CONFIG_DIR / "config.env"


def default_dest() -> Path:
    """~/Downloads/GoPro (or GOPRO_DL_HOME/downloads in dev mode), computed
    on demand rather than at import time.

    Unlike user_config_dir/user_data_dir above (cheap env-var reads),
    platformdirs' user_downloads_dir() does real I/O on Linux -- it parses
    ~/.config/user-dirs.dirs -- so this stays a function, called only from
    the one place that needs it (config.py's fallback when no --dest/
    GOPRO_DEST is set), instead of paying that cost on every invocation
    regardless of whether the fallback is even used.
    """
    if _DEV_HOME:
        return Path(_DEV_HOME).expanduser() / "downloads"
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
