"""Helpers for the Ansible-side Python tests (repo-root inventory + plugins).

These modules are plain scripts (not importable packages), so we load them from
their file paths via importlib and hand the resulting module objects to tests.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, relpath: str):
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def inventory_module():
    return _load("hac_inventory_pct", "inventory/pct.py")


@pytest.fixture(scope="session")
def pct_connection_module():
    return _load("hac_connection_pct", "plugins/connection/pct.py")
