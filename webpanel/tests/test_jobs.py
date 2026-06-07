"""Integration tests for the jobs router and startup job reconciliation."""

from __future__ import annotations

from app.db import session_scope
from app.models import Job
from fastapi.testclient import TestClient


def test_jobs_page_requires_auth(client):
    resp = client.get("/jobs", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_jobs_page_renders_for_admin(admin_client):
    assert admin_client.get("/jobs").status_code == 200


def test_orphaned_jobs_marked_failed_on_startup():
    """A job left 'running'/'queued' by a crashed process must be reconciled."""
    from app.main import app

    with session_scope() as db:
        running = Job(status="running", mode="check")
        queued = Job(status="queued", mode="apply")
        db.add_all([running, queued])
        db.flush()
        running_id, queued_id = running.id, queued.id

    # Entering the TestClient context triggers the startup reconciliation.
    with TestClient(app):
        pass

    with session_scope() as db:
        assert db.get(Job, running_id).status == "failed"
        assert db.get(Job, queued_id).status == "failed"
        assert db.get(Job, running_id).pid is None
