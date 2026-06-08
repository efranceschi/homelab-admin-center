"""Security-focused tests: blanket auth coverage, secret non-leakage, cookie flags."""

from __future__ import annotations

import pytest
from app.db import session_scope
from app.main import app
from app.models import Server
from sqlalchemy import select

PUBLIC_PATHS = {
    "/",
    "/login",
    "/setup",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/docs/oauth2-redirect",
}


def _protected_get_paths() -> list[str]:
    paths = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if (
            "GET" in methods
            and "{" not in path  # skip parameterised routes
            and path not in PUBLIC_PATHS
            and not path.startswith("/static")
        ):
            paths.append(path)
    return sorted(set(paths))


@pytest.mark.parametrize("path", _protected_get_paths())
def test_protected_get_routes_redirect_anonymous_to_login(client, path):
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 303, f"{path} did not redirect (got {resp.status_code})"
    assert resp.headers["location"] == "/login", (
        f"{path} redirected to {resp.headers.get('location')}"
    )


def test_credential_secret_never_appears_in_responses(admin_client, csrf):
    secret = "LEAK-CANARY-9f3a2b"
    token = csrf(admin_client, "/hosts")
    admin_client.post(
        "/hosts/credentials",
        data={"name": "canary", "type": "password", "secret": secret},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    # The secret must not be rendered anywhere on the hosts page.
    assert secret not in admin_client.get("/hosts").text


def test_session_cookie_flags(client, csrf, admin_creds):
    token = csrf(client, "/setup")
    resp = client.post(
        "/setup",
        data={**admin_creds, "password2": admin_creds["password"]},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    set_cookie = resp.headers.get("set-cookie", "").lower()
    assert "hack_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    # PANEL_HTTPS_ONLY is unset in tests, so the cookie must not be Secure-only.
    assert "secure" not in set_cookie


def test_viewer_may_run_check(viewer_client, csrf, no_real_jobs):
    with session_scope() as db:
        db.add(Server(name="checkhost", connection_type="local", enabled=True))
        db.flush()
        sid = db.scalar(select(Server).where(Server.name == "checkhost")).id

    token = csrf(viewer_client, "/hosts")
    resp = viewer_client.post(
        f"/hosts/{sid}/check", headers={"x-csrf-token": token}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert no_real_jobs and no_real_jobs[-1]["mode"] == "check"
