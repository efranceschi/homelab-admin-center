"""Unit tests for the extra-vars builder (non-ansible paths)."""

from __future__ import annotations

from app.ansible_layer import vars_builder


def test_resolve_host_vars_skips_unknown_plugins(db):
    assert vars_builder.resolve_host_vars(db, ["does-not-exist"], 1) == {}


def test_build_secret_vars_returns_none_without_secrets(db, tmp_path):
    # No plugins selected => nothing to encrypt => no file.
    assert vars_builder.build_secret_vars(db, tmp_path, []) is None


def test_build_extra_vars_empty_when_nothing_configured(db, tmp_path):
    extra, secret = vars_builder.build_extra_vars(db, tmp_path, ["does-not-exist"], None)
    assert extra is None
    assert secret is None
