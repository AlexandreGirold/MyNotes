"""Runtime discovery file for MCP bridge.

Atomic write / read / replace / remove with strict permissions.

Schema::  {"url": str, "token": str (tool_token), "pid": int}
File mode::  0o600
Directory mode::  0o700
Only the owning pid may remove its entry."""
from __future__ import annotations

import json
import logging
import os
import pathlib
import secrets
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_RUNTIME_DIR = pathlib.Path.home() / ".local" / "state" / "mynotes"
_DEFAULT_RUNTIME_PATH = _DEFAULT_RUNTIME_DIR / "runtime.json"


class RuntimeFile:
    """Manage the ephemeral runtime discovery file."""

    def __init__(self, path: pathlib.Path | None = None, owner_token: str | None = None) -> None:
        self._path = pathlib.Path(
            os.environ.get("MYNOTES_RUNTIME_FILE", str(path or _DEFAULT_RUNTIME_PATH))
        )
        # Reuse the same owner token across write/remove calls.
        # If the caller didn't provide one, derive it from PID so it's
        # stable for the life of this process (and avoids cross-process
        # collision from repeated __init__).
        self._owner_token = owner_token or str(os.getpid())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.parent.chmod(0o700)

    def write(self, url: str, tool_token: str) -> None:
        """Atomically write ``{url, token, pid}`` — replaces existing entry.

        Uses ``O_EXCL`` on a random-temp file so no transient plaintext is
        ever readable, then renames into place (atomic on POSIX).
        """
        data = {"url": url, "token": tool_token, "pid": int(self._owner_token)}
        tmp = self._path.with_name(
            f"{self._path.name}.tmp.{secrets.token_hex(8)}"
        )
        fd = os.open(
            str(tmp),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(fd, json.dumps(data).encode())
        finally:
            os.close(fd)
        tmp.replace(self._path)

    def read(self) -> dict[str, Any] | None:
        """Return parsed JSON or ``None`` when absent / corrupt."""
        if not self._path.exists():
            return None
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def remove(self) -> None:
        """Remove the runtime file **only** if the stored pid matches ours."""
        entry = self.read()
        if entry and entry.get("pid") == int(self._owner_token):
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass

    @property
    def path(self) -> pathlib.Path:
        return self._path
