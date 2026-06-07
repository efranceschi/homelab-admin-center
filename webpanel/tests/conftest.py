"""Shared pytest fixtures for the HAC web-panel test suite.

The panel derives every writable path from ``PANEL_*`` env vars (see
``app/config.py``). We point them all at a throwaway temp dir **before** the app
package is imported, so each test run boots on an isolated SQLite DB + fresh key
files and never touches /etc/hac or /var/lib/hac. ``PANEL_DISABLE_SCHEDULER``
stops the startup hook from spawning the scheduler child process.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Environment isolation — MUST run before importing app.config (module-level
# reads of these vars happen at import time).
# --------------------------------------------------------------------------- #
_TMP = Path(tempfile.mkdtemp(prefix="hac-tests-"))
os.environ.update(
    {
        "PANEL_STATE_DIR": str(_TMP / "state"),
        "PANEL_DB_PATH": str(_TMP / "state" / "panel.sqlite3"),
        "PANEL_MASTER_KEY": str(_TMP / "etc" / "panel.key"),
        "PANEL_SESSION_SECRET": str(_TMP / "etc" / "panel.session"),
        "PANEL_VAULT_PASSWORD_FILE": str(_TMP / "etc" / "vault-pass"),
        "PANEL_RUN_LOCK": str(_TMP / "run" / "hac.lock"),
        "PANEL_DISABLE_SCHEDULER": "1",
    }
)
(_TMP / "etc").mkdir(parents=True, exist_ok=True)
(_TMP / "run").mkdir(parents=True, exist_ok=True)

CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')

ADMIN_USER = "admin"
ADMIN_PASS = "adminpass1"
VIEWER_USER = "viewer1"
VIEWER_PASS = "viewerpass1"


def _reset_schema() -> None:
    """Drop and recreate every table so each test starts from a clean DB."""
    from app.db import init_engine
    from app.models import Base

    engine = init_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@pytest.fixture
def admin_creds():
    """First-run admin credentials used by the auth fixtures and tests."""
    return {"username": ADMIN_USER, "password": ADMIN_PASS}


@pytest.fixture(autouse=True)
def _clean_db():
    """Fresh schema before each test (autouse so unit tests get it too)."""
    _reset_schema()
    yield


@pytest.fixture
def db():
    """A SQLAlchemy session bound to the freshly-reset DB (for unit tests)."""
    from app.db import get_session

    session = get_session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def csrf():
    """Return ``get(client, url) -> token`` scraping the per-session CSRF token.

    A GET renders a page through ``templating.render``, which both seeds the
    session CSRF token and embeds it as a hidden field we parse back out.
    """

    def _get(client, url: str = "/login") -> str:
        resp = client.get(url)
        match = CSRF_RE.search(resp.text)
        assert match, f"no csrf_token field found at {url} (status {resp.status_code})"
        return match.group(1)

    return _get


@pytest.fixture
def client():
    """A TestClient whose context runs the startup hook (plugin sync, seeding)."""
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_client(client, csrf):
    """A client authenticated as the first-run admin (via /setup)."""
    token = csrf(client, "/setup")
    resp = client.post(
        "/setup",
        data={"username": ADMIN_USER, "password": ADMIN_PASS, "password2": ADMIN_PASS},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return client


@pytest.fixture
def no_real_jobs(monkeypatch):
    """Stub ``start_job`` in the routers so check/apply never launch ansible.

    The endpoints only use the return value's truthiness, so an async no-op that
    records its calls is enough to exercise the routing/auth/audit paths.
    """
    calls: list[dict] = []

    async def _fake_start_job(db, *, user_id, server_ids, plugin_ids, mode, group_ids=None):
        calls.append(
            {
                "user_id": user_id,
                "server_ids": list(server_ids),
                "plugin_ids": list(plugin_ids),
                "mode": mode,
                "group_ids": group_ids,
            }
        )
        return object()  # truthy sentinel; routers don't inspect it

    import app.routers.hosts as hosts_router
    import app.routers.jobs as jobs_router

    monkeypatch.setattr(hosts_router, "start_job", _fake_start_job)
    monkeypatch.setattr(jobs_router, "start_job", _fake_start_job)
    return calls


@pytest.fixture
def viewer_client(admin_client, csrf):
    """A separate client logged in as a non-admin viewer.

    The admin creates the viewer account, then a fresh client logs in as it. The
    admin's TestClient context keeps the app started, so a bare TestClient works.
    """
    from app.main import app
    from fastapi.testclient import TestClient

    token = csrf(admin_client, "/settings")
    admin_client.post(
        "/settings/users",
        data={"username": VIEWER_USER, "password": VIEWER_PASS, "role": "viewer"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )

    viewer = TestClient(app)
    login_token = csrf(viewer, "/login")
    resp = viewer.post(
        "/login",
        data={"username": VIEWER_USER, "password": VIEWER_PASS},
        headers={"x-csrf-token": login_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return viewer
