from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

LOG = logging.getLogger(__name__)

# How often to check the strings file for changes (seconds)
_POLL_INTERVAL = 30


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten nested TOML dict to dotted keys, leaf values only."""
    out: dict[str, str] = {}
    for k, v in d.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, full_key))
        else:
            out[full_key] = str(v)
    return out


class StringCatalog:
    """Hot-reloadable catalog of user-visible BBS strings.

    Strings are stored in a TOML file and accessed by dotted key::

        catalog.get("error.unknown_cmd")
        catalog.get("prompt.connected", node="N0BBS-1", call="W1AW")

    The file is polled every :data:`_POLL_INTERVAL` seconds and reloaded
    transparently — no service restart needed for string changes.
    """

    def __init__(self, path: str | None = None) -> None:
        self._path: Path | None = Path(path) if path else None
        self._lock = threading.RLock()
        self._strings: dict[str, str] = {}
        self._mtime: float = 0.0
        self._last_poll: float = 0.0

        if self._path and self._path.exists():
            self._reload()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get(self, key: str, **kwargs: Any) -> str:
        """Return the string for *key*, formatted with *kwargs*.

        Returns ``key`` verbatim if the key is not found, so callers always
        get something printable even during development.
        """
        self._poll_if_due()
        with self._lock:
            template = self._strings.get(key, key)
        if kwargs:
            try:
                return template.format_map(kwargs)
            except (KeyError, ValueError):
                return template
        return template

    def get_all(self) -> dict[str, str]:
        """Return a snapshot of all strings (for the sysop console)."""
        self._poll_if_due()
        with self._lock:
            return dict(self._strings)

    def reload(self) -> bool:
        """Force an immediate reload from disk.  Returns True if reloaded."""
        return self._reload()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _poll_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_poll < _POLL_INTERVAL:
            return
        self._last_poll = now
        if self._path is None:
            return
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            return
        if mtime != self._mtime:
            self._reload()

    def _reload(self) -> bool:
        if self._path is None or not self._path.exists():
            return False
        try:
            data = _load_toml(self._path)
            flat = _flatten(data)
            mtime = os.path.getmtime(self._path)
            with self._lock:
                self._strings = flat
                self._mtime = mtime
            self._last_poll = time.monotonic()
            LOG.debug("strings: loaded %d entries from %s", len(flat), self._path)
            return True
        except Exception as exc:
            LOG.warning("strings: failed to reload %s: %s", self._path, exc)
            return False
