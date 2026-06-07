"""Integration tests for login, logout, and auth redirects."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_unauthenticated_dashboard_redirects_to_login(client):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_success(admin_client, csrf, admin_creds):
    # admin_client logged in via /setup; log out then back in to exercise /login.
    token = csrf(admin_client, "/settings")
    admin_client.post("/logout", headers={"x-csrf-token": token}, follow_redirects=False)

    login_token = csrf(admin_client, "/login")
    resp = admin_client.post(
        "/login",
        data=admin_creds,
        headers={"x-csrf-token": login_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"


def test_login_wrong_password_shows_error(admin_client, csrf, admin_creds):
    token = csrf(admin_client, "/settings")
    admin_client.post("/logout", headers={"x-csrf-token": token}, follow_redirects=False)

    login_token = csrf(admin_client, "/login")
    resp = admin_client.post(
        "/login",
        data={"username": admin_creds["username"], "password": "wrongpass"},
        headers={"x-csrf-token": login_token},
    )
    assert resp.status_code == 200
    assert "invalid credentials" in resp.text.lower()


def test_logout_clears_session(admin_client, csrf):
    token = csrf(admin_client, "/settings")
    resp = admin_client.post("/logout", headers={"x-csrf-token": token}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    # Session cleared: dashboard now redirects to login.
    assert admin_client.get("/dashboard", follow_redirects=False).status_code == 303


def test_account_locks_after_repeated_failures(admin_client, csrf, admin_creds):
    from app.main import app

    # Fresh anonymous client; admin already exists from the fixture.
    attacker = TestClient(app)
    for _ in range(5):
        t = csrf(attacker, "/login")
        attacker.post(
            "/login",
            data={"username": admin_creds["username"], "password": "bad"},
            headers={"x-csrf-token": t},
        )
    # Even the correct password is now refused (account locked).
    t = csrf(attacker, "/login")
    resp = attacker.post(
        "/login",
        data=admin_creds,
        headers={"x-csrf-token": t},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "locked" in resp.text.lower() or "invalid" in resp.text.lower()
