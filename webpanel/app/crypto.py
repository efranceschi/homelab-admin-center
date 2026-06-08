"""Secrets-at-rest for the control panel.

Secrets (SSH private keys, Proxmox API tokens, the Ansible vault password) are
encrypted with Fernet (AES-128-CBC + HMAC-SHA256) before being stored in SQLite.
The master key is a host-local file (``/etc/hack/panel.key``, mode 0600),
auto-generated on first run and excluded from git — mirroring the existing
``vault-pass`` trust model. This protects DB backups/exports and accidental
commits; it is not a defense against a root compromise of the host itself.
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import config


def _read_or_create_key(path: Path) -> bytes:
    """Return the key bytes at ``path``, generating a 0600 file if absent."""
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Write atomically with restrictive permissions.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


class SecretBox:
    """Thin Fernet wrapper used to (de)serialize secret strings."""

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:  # pragma: no cover - defensive
            raise ValueError("secret could not be decrypted (wrong master key?)") from exc


_box: SecretBox | None = None


def get_box() -> SecretBox:
    """Process-wide singleton SecretBox backed by the master key file."""
    global _box
    if _box is None:
        _box = SecretBox(_read_or_create_key(config.MASTER_KEY_PATH))
    return _box


def get_session_secret() -> str:
    """Return (creating if needed) the session-cookie signing secret."""
    return _read_or_create_key(config.SESSION_SECRET_PATH).decode("ascii")
