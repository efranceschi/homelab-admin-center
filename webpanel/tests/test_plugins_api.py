"""Integration tests for the plugins router (listing + access control)."""

from __future__ import annotations

from app.db import session_scope
from app.models import Plugin, PluginConfig
from sqlalchemy import select

# A real plugin discovered from webpanel/plugins/ at startup.
REAL_PLUGIN = "timezone"


def test_plugins_page_renders_for_admin(admin_client):
    assert admin_client.get("/plugins").status_code == 200


def test_configure_form_renders_for_real_plugin(admin_client):
    assert admin_client.get(f"/plugins/{REAL_PLUGIN}").status_code == 200


def test_admin_saves_global_plugin_config(admin_client, csrf):
    token = csrf(admin_client, "/plugins")
    resp = admin_client.post(
        f"/plugins/{REAL_PLUGIN}",
        data={"scope": "global", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/plugins"
    with session_scope() as db:
        plugin = db.scalar(select(Plugin).where(Plugin.key == REAL_PLUGIN))
        cfg = db.scalar(
            select(PluginConfig).where(
                PluginConfig.plugin_id == plugin.id, PluginConfig.scope == "global"
            )
        )
        assert cfg is not None


def test_admin_toggles_plugin(admin_client, csrf):
    token = csrf(admin_client, "/plugins")
    with session_scope() as db:
        before = db.scalar(select(Plugin).where(Plugin.key == REAL_PLUGIN)).enabled
    resp = admin_client.post(
        f"/plugins/{REAL_PLUGIN}/toggle",
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope() as db:
        assert db.scalar(select(Plugin).where(Plugin.key == REAL_PLUGIN)).enabled is (not before)


def test_plugins_page_visible_to_viewer(viewer_client):
    assert viewer_client.get("/plugins").status_code == 200


def test_configure_unknown_plugin_redirects(admin_client):
    resp = admin_client.get("/plugins/__nope__", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/plugins"


def test_viewer_cannot_save_plugin_config(viewer_client, csrf):
    token = csrf(viewer_client, "/plugins")
    resp = viewer_client.post(
        "/plugins/anything",
        data={"scope": "global"},
        headers={"x-csrf-token": token},
    )
    assert resp.status_code == 403
