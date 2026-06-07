"""Integration tests for the first-run setup flow."""

from __future__ import annotations


def test_root_redirects_to_setup_when_no_users(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup"


def test_setup_form_renders(client):
    resp = client.get("/setup")
    assert resp.status_code == 200
    assert "csrf_token" in resp.text


def test_setup_creates_admin_and_logs_in(client, csrf, admin_creds):
    token = csrf(client, "/setup")
    resp = client.post(
        "/setup",
        data={**admin_creds, "password2": admin_creds["password"]},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    # Session is now authenticated: the dashboard renders.
    assert client.get("/dashboard").status_code == 200


def test_second_setup_is_blocked(admin_client, csrf):
    # An admin already exists (fixture); /setup must bounce to /login.
    resp = admin_client.get("/setup", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_setup_rejects_password_mismatch(client, csrf):
    token = csrf(client, "/setup")
    resp = client.post(
        "/setup",
        data={"username": "bob", "password": "longenough1", "password2": "different1"},
        headers={"x-csrf-token": token},
    )
    assert resp.status_code == 200
    assert "passwords must match" in resp.text.lower()


def test_setup_rejects_short_password(client, csrf):
    token = csrf(client, "/setup")
    resp = client.post(
        "/setup",
        data={"username": "bob", "password": "short", "password2": "short"},
        headers={"x-csrf-token": token},
    )
    assert resp.status_code == 200
    assert "min 8" in resp.text.lower()
