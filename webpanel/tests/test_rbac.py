"""Integration tests for role-based access control (admin vs viewer)."""

from __future__ import annotations

from app.db import session_scope
from app.models import Server
from sqlalchemy import select


def _make_server(name="host1"):
    with session_scope() as db:
        db.add(Server(name=name, connection_type="local", enabled=True))
        db.flush()
        return db.scalar(select(Server).where(Server.name == name)).id


def test_viewer_can_view_settings_page(viewer_client):
    # settings_home only requires an authenticated user.
    assert viewer_client.get("/settings").status_code == 200


def test_viewer_cannot_add_user(viewer_client, csrf):
    token = csrf(viewer_client, "/settings")
    resp = viewer_client.post(
        "/settings/users",
        data={"username": "x", "password": "longenough1", "role": "viewer"},
        headers={"x-csrf-token": token},
    )
    assert resp.status_code == 403


def test_viewer_cannot_add_host(viewer_client, csrf):
    token = csrf(viewer_client, "/hosts")
    resp = viewer_client.post(
        "/hosts",
        data={"name": "h", "connection_type": "local"},
        headers={"x-csrf-token": token},
    )
    assert resp.status_code == 403


def test_viewer_cannot_restart_system(viewer_client, csrf):
    token = csrf(viewer_client, "/settings")
    resp = viewer_client.post("/settings/system/restart", headers={"x-csrf-token": token})
    assert resp.status_code == 403


def test_viewer_apply_is_refused_with_message(viewer_client, csrf, no_real_jobs):
    sid = _make_server("applyhost")
    token = csrf(viewer_client, "/hosts")
    resp = viewer_client.post(
        f"/hosts/{sid}/apply",
        data={},
        headers={"x-csrf-token": token},
    )
    assert resp.status_code == 200
    assert "only admins can apply" in resp.text.lower()
    assert no_real_jobs == []  # no job was started


def test_admin_can_add_user(admin_client, csrf):
    token = csrf(admin_client, "/settings")
    resp = admin_client.post(
        "/settings/users",
        data={"username": "newadmin", "password": "longenough1", "role": "admin"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
