"""Integration tests for the schedules router."""

from __future__ import annotations

from app.db import session_scope
from app.models import Schedule
from sqlalchemy import select


def test_admin_creates_daily_schedule(admin_client, csrf):
    token = csrf(admin_client, "/schedules")
    resp = admin_client.post(
        "/schedules",
        data={"name": "nightly", "kind": "daily", "daily_time": "02:00", "mode": "check"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope() as db:
        sched = db.scalar(select(Schedule).where(Schedule.name == "nightly"))
        assert sched is not None
        assert sched.kind == "daily"
        assert sched.daily_time == "02:00"


def test_toggle_and_delete_schedule(admin_client, csrf):
    token = csrf(admin_client, "/schedules")
    admin_client.post(
        "/schedules",
        data={"name": "temp", "kind": "interval", "interval_minutes": "30", "mode": "apply"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    with session_scope() as db:
        sched = db.scalar(select(Schedule).where(Schedule.name == "temp"))
        sid, enabled_before = sched.id, sched.enabled

    admin_client.post(
        f"/schedules/{sid}/toggle", headers={"x-csrf-token": token}, follow_redirects=False
    )
    with session_scope() as db:
        assert db.get(Schedule, sid).enabled is (not enabled_before)

    resp = admin_client.post(
        f"/schedules/{sid}/delete", headers={"x-csrf-token": token}, follow_redirects=False
    )
    assert resp.status_code == 303
    with session_scope() as db:
        assert db.get(Schedule, sid) is None


def test_viewer_cannot_create_schedule(viewer_client, csrf):
    token = csrf(viewer_client, "/schedules")
    resp = viewer_client.post(
        "/schedules",
        data={"name": "nope", "kind": "daily", "daily_time": "01:00"},
        headers={"x-csrf-token": token},
    )
    assert resp.status_code == 403
