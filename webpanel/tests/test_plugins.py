"""Unit tests for plugin registry sync and config resolution cascade."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app import plugins as plugins_mod
from app.models import HostGroup, HostGroupMember, Plugin, PluginConfig, Server
from sqlalchemy import select


def _demo_plugin():
    return plugins_mod.LoadedPlugin(
        id="demo",
        name="Demo",
        version="1.0",
        description="",
        ansible_role="",
        tags=["demo"],
        enable_var=None,
        supported_connections=["local"],
        order=50,
        supports_check_mode=True,
        role_path=None,
        fields=[
            plugins_mod.FormField(var="tz", label="TZ", default="UTC"),
            plugins_mod.FormField(var="opt", label="Opt", default="a"),
            plugins_mod.FormField(var="pw", label="Password", secret=True),
        ],
        path=Path("."),
    )


@pytest.fixture
def registry_with_demo(monkeypatch):
    reg = plugins_mod.registry
    monkeypatch.setattr(reg, "_plugins", {**reg._plugins, "demo": _demo_plugin()})
    return reg


def test_sync_to_db_upserts_plugin_row(db, registry_with_demo):
    plugins_mod.sync_to_db(db)
    row = db.scalar(select(Plugin).where(Plugin.key == "demo"))
    assert row is not None
    assert row.name == "Demo"
    assert row.ansible_tags == "demo"
    schema = json.loads(row.schema_json)
    assert any(f["var"] == "pw" and f["secret"] for f in schema)


def test_resolve_config_defaults_exclude_secrets(db, registry_with_demo):
    plugins_mod.sync_to_db(db)
    cfg = plugins_mod.resolve_config(db, "demo")
    assert cfg == {"tz": "UTC", "opt": "a"}  # "pw" (secret) is excluded


def test_resolve_config_cascade_global_group_host(db, registry_with_demo):
    plugins_mod.sync_to_db(db)
    plugin = db.scalar(select(Plugin).where(Plugin.key == "demo"))

    server = Server(name="s1", connection_type="local")
    group = HostGroup(name="g1")
    db.add_all([server, group])
    db.flush()
    db.add(HostGroupMember(host_group_id=group.id, server_id=server.id))

    db.add_all(
        [
            PluginConfig(
                plugin_id=plugin.id,
                scope="global",
                scope_ref_id=None,
                config_json=json.dumps({"tz": "Europe/Lisbon"}),
            ),
            PluginConfig(
                plugin_id=plugin.id,
                scope="group",
                scope_ref_id=group.id,
                config_json=json.dumps({"opt": "b"}),
            ),
        ]
    )
    db.flush()

    # Global overrides default; group overrides global for the member host.
    assert plugins_mod.resolve_config(db, "demo", server.id) == {"tz": "Europe/Lisbon", "opt": "b"}

    # Host scope wins last.
    db.add(
        PluginConfig(
            plugin_id=plugin.id,
            scope="host",
            scope_ref_id=server.id,
            config_json=json.dumps({"opt": "c"}),
        )
    )
    db.flush()
    assert plugins_mod.resolve_config(db, "demo", server.id)["opt"] == "c"


def test_resolve_config_unknown_plugin_returns_empty(db):
    assert plugins_mod.resolve_config(db, "does-not-exist") == {}
