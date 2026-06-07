"""Unit tests for config env-var overrides and runtime-dir fallback."""

from __future__ import annotations

import os
from pathlib import Path

from app import config


def test_env_overrides_applied():
    # conftest set these to the temp dir before import; verify they took effect.
    assert str(config.STATE_DIR).endswith("state")
    assert config.DB_PATH == Path(os.environ["PANEL_DB_PATH"])
    assert config.MASTER_KEY_PATH == Path(os.environ["PANEL_MASTER_KEY"])


def test_envflag_parsing(monkeypatch):
    monkeypatch.setenv("HAC_TEST_FLAG", "yes")
    assert config._envflag("HAC_TEST_FLAG") is True
    monkeypatch.setenv("HAC_TEST_FLAG", "off")
    assert config._envflag("HAC_TEST_FLAG") is False
    monkeypatch.delenv("HAC_TEST_FLAG", raising=False)
    assert config._envflag("HAC_TEST_FLAG", default=True) is True


def test_ensure_runtime_dirs_creates_paths():
    config.ensure_runtime_dirs()
    assert config.RUN_DIRS.is_dir()
    assert config.STATE_DIR.is_dir()


def test_ensure_runtime_dirs_falls_back_when_state_unwritable(monkeypatch, tmp_path):
    fake_panel = tmp_path / "panel"
    fake_panel.mkdir()
    blocked = tmp_path / "blocked-state"

    # Make mkdir on the primary STATE_DIR raise PermissionError, portably.
    real_mkdir = Path.mkdir

    def fake_mkdir(self, *args, **kwargs):
        if self == blocked:
            raise PermissionError("simulated read-only state dir")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    # Snapshot mutated globals so monkeypatch restores them after the test.
    monkeypatch.setattr(config, "PANEL_DIR", fake_panel)
    monkeypatch.setattr(config, "STATE_DIR", blocked)
    monkeypatch.setattr(config, "DB_PATH", config.DB_PATH)
    monkeypatch.delenv("PANEL_DB_PATH", raising=False)

    config.ensure_runtime_dirs()

    assert config.STATE_DIR == fake_panel / "data"
    assert config.STATE_DIR.is_dir()
    assert config.DB_PATH == fake_panel / "data" / "panel.sqlite3"
