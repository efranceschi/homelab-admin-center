"""Unit tests for secrets-at-rest (Fernet SecretBox) and key file handling."""

from __future__ import annotations

import os
import stat

import pytest
from app import config, crypto
from cryptography.fernet import Fernet


def test_secretbox_roundtrip():
    box = crypto.SecretBox(Fernet.generate_key())
    token = box.encrypt("super-secret-value")
    assert token != "super-secret-value"
    assert box.decrypt(token) == "super-secret-value"


def test_secretbox_rejects_tampered_token():
    box = crypto.SecretBox(Fernet.generate_key())
    token = box.encrypt("payload")
    tampered = token[:-4] + "AAAA"
    with pytest.raises(ValueError):
        box.decrypt(tampered)


def test_secretbox_rejects_wrong_key():
    token = crypto.SecretBox(Fernet.generate_key()).encrypt("payload")
    other = crypto.SecretBox(Fernet.generate_key())
    with pytest.raises(ValueError):
        other.decrypt(token)


def test_master_key_file_created_with_0600():
    box = crypto.get_box()  # triggers key creation at config.MASTER_KEY_PATH
    assert box is crypto.get_box()  # process-wide singleton
    path = config.MASTER_KEY_PATH
    assert path.exists()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_session_secret_persisted_and_stable():
    first = crypto.get_session_secret()
    assert config.SESSION_SECRET_PATH.exists()
    assert crypto.get_session_secret() == first  # re-read returns same value
