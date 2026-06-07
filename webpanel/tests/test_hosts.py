"""Integration tests for host and credential management."""

from __future__ import annotations

from app import crypto
from app.db import session_scope
from app.models import Credential, Server
from sqlalchemy import select


def test_add_ssh_host(admin_client, csrf):
    token = csrf(admin_client, "/hosts")
    resp = admin_client.post(
        "/hosts",
        data={
            "name": "remote1",
            "connection_type": "ssh",
            "address": "10.0.0.9",
            "port": "22",
            "ssh_user": "ops",
        },
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope() as db:
        srv = db.scalar(select(Server).where(Server.name == "remote1"))
        assert srv is not None
        assert srv.connection_type == "ssh"
        assert srv.address == "10.0.0.9"
        assert srv.port == 22


def test_credential_secret_is_encrypted_at_rest(admin_client, csrf):
    secret_value = "TOP-SECRET-KEY-MATERIAL"
    token = csrf(admin_client, "/hosts")
    resp = admin_client.post(
        "/hosts/credentials",
        data={"name": "ssh-key-1", "type": "ssh_key", "secret": secret_value, "meta": "lab"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope() as db:
        cred = db.scalar(select(Credential).where(Credential.name == "ssh-key-1"))
        assert cred is not None
        # Stored ciphertext must NOT contain the plaintext...
        assert secret_value not in cred.secret_ciphertext
        # ...but must decrypt back to it.
        assert crypto.get_box().decrypt(cred.secret_ciphertext) == secret_value


def test_delete_host(admin_client, csrf):
    token = csrf(admin_client, "/hosts")
    admin_client.post(
        "/hosts",
        data={"name": "todelete", "connection_type": "local"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    with session_scope() as db:
        sid = db.scalar(select(Server).where(Server.name == "todelete")).id

    resp = admin_client.post(
        f"/hosts/{sid}/delete", headers={"x-csrf-token": token}, follow_redirects=False
    )
    assert resp.status_code == 303
    with session_scope() as db:
        assert db.get(Server, sid) is None


def test_toggle_host_enabled(admin_client, csrf):
    token = csrf(admin_client, "/hosts")
    admin_client.post(
        "/hosts",
        data={"name": "toggleme", "connection_type": "local"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    with session_scope() as db:
        srv = db.scalar(select(Server).where(Server.name == "toggleme"))
        sid, before = srv.id, srv.enabled

    admin_client.post(
        f"/hosts/{sid}/toggle", headers={"x-csrf-token": token}, follow_redirects=False
    )
    with session_scope() as db:
        assert db.get(Server, sid).enabled is (not before)
