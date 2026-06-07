"""Coverage for assorted small modules and read-mostly endpoints."""

from __future__ import annotations

from app import ansi, system


# --------------------------------------------------------------------------- #
# ansi.py
# --------------------------------------------------------------------------- #
def test_strip_ansi_removes_escapes():
    coloured = "\x1b[31mred\x1b[0m plain"
    assert ansi.strip_ansi(coloured) == "red plain"
    assert ansi.strip_ansi("") == ""


def test_ansi_to_html_escapes_and_colours():
    html_out = ansi.ansi_to_html("\x1b[31m<danger>\x1b[0m")
    assert "<span" in html_out
    assert "&lt;danger&gt;" in html_out  # HTML-escaped content
    assert html_out.endswith("</span>")


def test_ansi_to_html_plain_passthrough():
    assert ansi.ansi_to_html("just text") == "just text"
    assert ansi.ansi_to_html("") == ""


# --------------------------------------------------------------------------- #
# system.py
# --------------------------------------------------------------------------- #
def test_under_systemd_returns_bool():
    assert isinstance(system.under_systemd(), bool)


def test_request_restart_non_systemd_reexec(monkeypatch):
    monkeypatch.setattr(system, "under_systemd", lambda: False)
    started = {}

    class FakeThread:
        def __init__(self, target=None, daemon=None):
            started["made"] = True

        def start(self):
            started["started"] = True

    monkeypatch.setattr(system.threading, "Thread", FakeThread)
    note = system.request_restart(delay=0)
    assert "respawn" in note.lower() or "restart" in note.lower()
    assert started == {"made": True, "started": True}


# --------------------------------------------------------------------------- #
# read-mostly endpoints (exercise rendering + queries)
# --------------------------------------------------------------------------- #
def test_dashboard_renders(admin_client):
    assert admin_client.get("/dashboard").status_code == 200


def test_settings_runtime_update(admin_client, csrf):
    token = csrf(admin_client, "/settings")
    resp = admin_client.post(
        "/settings/runtime",
        data={"max_concurrent_jobs": "3", "auto_refresh_seconds": "60", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_jobs_run_endpoint_check_mode(admin_client, csrf, no_real_jobs):
    from app.db import session_scope
    from app.models import Server
    from sqlalchemy import select

    with session_scope() as db:
        db.add(Server(name="runhost", connection_type="local", enabled=True))
        db.flush()
        sid = db.scalar(select(Server).where(Server.name == "runhost")).id

    token = csrf(admin_client, "/jobs")
    resp = admin_client.post(
        "/jobs/run",
        data={"mode": "check", "servers": str(sid), "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code in (303, 200)
