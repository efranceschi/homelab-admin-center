"""Integration tests for the host-groups router (CRUD + nesting + cycle guard)."""

from __future__ import annotations

from app.db import session_scope
from app.models import HostGroup, HostGroupChild
from sqlalchemy import select


def _create_group(client, csrf, name):
    token = csrf(client, "/groups")
    resp = client.post(
        "/groups", data={"name": name}, headers={"x-csrf-token": token}, follow_redirects=False
    )
    assert resp.status_code == 303
    with session_scope() as db:
        return db.scalar(select(HostGroup).where(HostGroup.name == name)).id


def test_create_group(admin_client, csrf):
    gid = _create_group(admin_client, csrf, "prod")
    assert gid > 0


def test_duplicate_group_name_rejected(admin_client, csrf):
    _create_group(admin_client, csrf, "dup")
    token = csrf(admin_client, "/groups")
    resp = admin_client.post("/groups", data={"name": "dup"}, headers={"x-csrf-token": token})
    assert resp.status_code == 200
    assert "already exists" in resp.text.lower()


def test_nest_child_group(admin_client, csrf):
    parent = _create_group(admin_client, csrf, "parent")
    child = _create_group(admin_client, csrf, "child")
    token = csrf(admin_client, f"/groups/{parent}")
    resp = admin_client.post(
        f"/groups/{parent}",
        data={"name": "parent", "children": [str(child)]},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope() as db:
        edge = db.scalar(
            select(HostGroupChild).where(
                HostGroupChild.parent_group_id == parent,
                HostGroupChild.child_group_id == child,
            )
        )
        assert edge is not None


def test_cycle_is_rejected(admin_client, csrf):
    parent = _create_group(admin_client, csrf, "p2")
    child = _create_group(admin_client, csrf, "c2")
    # parent -> child
    token = csrf(admin_client, f"/groups/{parent}")
    admin_client.post(
        f"/groups/{parent}",
        data={"name": "p2", "children": [str(child)]},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    # Attempt child -> parent (would close a cycle); endpoint must skip the edge.
    token = csrf(admin_client, f"/groups/{child}")
    admin_client.post(
        f"/groups/{child}",
        data={"name": "c2", "children": [str(parent)]},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    with session_scope() as db:
        bad = db.scalar(
            select(HostGroupChild).where(
                HostGroupChild.parent_group_id == child,
                HostGroupChild.child_group_id == parent,
            )
        )
        assert bad is None  # cycle-forming edge was not created


def test_delete_group(admin_client, csrf):
    gid = _create_group(admin_client, csrf, "trash")
    token = csrf(admin_client, "/groups")
    resp = admin_client.post(
        f"/groups/{gid}/delete", headers={"x-csrf-token": token}, follow_redirects=False
    )
    assert resp.status_code == 303
    with session_scope() as db:
        assert db.get(HostGroup, gid) is None
