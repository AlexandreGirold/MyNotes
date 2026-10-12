"""Credential storage with optional Fernet encryption.

Persistent encrypted credentials are stored only when a Fernet-compatible
key is available.  Without a key the store falls back to in-memory only —
no key file is ever written to disk in plaintext.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

from cryptography.fernet import InvalidToken

logger = logging.getLogger(__name__)

# Filename used for encrypted credentials — excluded from backups.
_CREDENTIALS_FILENAME = "credentials.enc"


class CredentialStore:
    """Persist API credentials, optionally encrypted at rest.

    Parameters
    ----------
    data_dir:
        Directory where the encrypted file lives (e.g.
        ``~/.local/share/mynotes``).
    key:
        A Fernet-compatible key (32-url-safe-base64-encoded bytes or
        a 32-byte ``bytes`` object).  If ``None`` the store uses
        in-memory only.
    """

    def __init__(self, data_dir: Path, key: str | None = None) -> None:
        self.data_dir = data_dir
        self._key: bytes | None = None
        self._memory: dict[str, str] = {}
        self.storage_error: str | None = None

        if key is not None:
            self._key = self._parse_key(key)
            if self._key is not None:
                # Re-load persisted credentials if encryption is available
                self._reload()
        # else: memory-only, no encryption.

    # -- public API --------------------------------------------------------

    def set(self, api_key: str) -> None:
        """Store *api_key* under the ``"default"`` credential name.

        If encryption is configured the value is written to disk
        immediately.  Otherwise it stays in memory only.
        """
        self._memory["default"] = api_key
        if self._key is not None:
            self._persist()

    def get(self) -> str | None:
        """Return the stored API key, or ``None``."""
        return self._memory.get("default")

    @property
    def persistent(self) -> bool:
        return self._key is not None

    # -- internal ----------------------------------------------------------

    @staticmethod
    def _parse_key(key: str | None) -> bytes | None:
        """Convert *key* to a Fernet-compatible key.

        Fernet expects 32 url-safe base64-encoded bytes (44 bytes
        after decoding).  We store the key in exactly the form
        ``Fernet`` needs so ``_fernet()`` can use it directly.
        """
        if key is None:
            return None
        try:
            if isinstance(key, bytes) and len(key) == 32:
                return base64.urlsafe_b64encode(key)
            text = key.decode("ascii") if isinstance(key, bytes) else key
            if len(text) == 64:
                raw = bytes.fromhex(text)
                if len(raw) == 32:
                    return base64.urlsafe_b64encode(raw)
            encoded = text.encode("ascii")
            if len(base64.b64decode(encoded, altchars=b"-_", validate=True)) != 32:
                raise ValueError("Invalid key length")
            return encoded
        except (ValueError, TypeError, UnicodeError):
            logger.warning("Could not parse credential encryption key; falling back to memory-only")
            return None

    def _reload(self) -> None:
        """Load encrypted credentials from disk."""
        target = self.data_dir / _CREDENTIALS_FILENAME
        if not target.exists():
            return
        try:
            data = self._read_encrypted(target)
            if data:
                self._memory.update(data)
        except (InvalidToken, ValueError, OSError, TypeError):
            self.storage_error = "The saved API key could not be unlocked. Check your system keyring or re-enter the key."
            logger.warning("Failed to load encrypted credentials; starting fresh")

    def _persist(self) -> None:
        """Persist in-memory credentials to disk (encrypted)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        target = self.data_dir / _CREDENTIALS_FILENAME
        try:
            self._write_encrypted(target, self._memory)
            self.storage_error = None
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            self.storage_error = "The encrypted API key could not be saved. Check available disk space and file permissions."
            logger.exception("Failed to persist encrypted credentials")
            raise OSError(self.storage_error) from error

    def _fernet(self) -> Any:
        """Return a Fernet instance or raise."""
        from cryptography.fernet import Fernet
        if self._key is None:
            raise RuntimeError("Fernet key not configured")
        return Fernet(self._key)

    def _write_encrypted(self, path: Path, data: dict[str, Any]) -> None:
        """Write *data* dict as Fernet-encrypted bytes."""
        fernet = self._fernet()
        plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
        ciphertext = fernet.encrypt(plaintext)
        # Write to temp then rename for atomicity
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(ciphertext)
        tmp.chmod(0o600)
        tmp.replace(path)

    def _read_encrypted(self, path: Path) -> dict[str, Any] | None:
        """Read and decrypt the credential file, returning parsed JSON."""
        fernet = self._fernet()
        ciphertext = path.read_bytes()
        plaintext = fernet.decrypt(ciphertext)
        return json.loads(plaintext.decode("utf-8"))

    def clear(self) -> None:
        """Remove the stored API key and delete the on-disk file."""
        self._memory.pop("default", None)
        target = self.data_dir / _CREDENTIALS_FILENAME
        try:
            if target.exists():
                target.unlink()
        except Exception:  # noqa: BLE001
            logger.warning("Failed to delete credential file")
