"""Per-host fan-out and the monotonic 'last execution wins' state guard.

Covers the change that turns every multi-host trigger (Check All / group /
multi-select / scheduled run) into one job per host, and the guard that keeps a
host's config_state pinned to its most recent run so stragglers can't clobber it.
"""

from __future__ import annotations

import pytest

from app.ansible_layer import service
from app.db import session_scope
from app.models import HostGroup, HostGroupChild, HostGroupMember, HostState, Job, Server


@pytest.fixture
def graph(db):
    """root ─┬─ web ─ srvA / └─ db ─ srvB ; root also holds srvC."""
    root, web, dbg = HostGroup(name="root"), HostGroup(name="web"), HostGroup(name="db")
    db.add_all([root, web, dbg])
    db.flush()
    a = Server(name="srvA", connection_type="local")
    b = Server(name="srvB", connection_type="local")
    c = Server(name="srvC", connection_type="local")
    db.add_all([a, b, c])
    db.flush()
    db.add_all(
        [
            HostGroupChild(parent_group_id=root.id, child_group_id=web.id),
            HostGroupChild(parent_group_id=root.id, child_group_id=dbg.id),
            HostGroupMember(host_group_id=web.id, server_id=a.id),
            HostGroupMember(host_group_id=dbg.id, server_id=b.id),
            HostGroupMember(host_group_id=root.id, server_id=c.id),
        ]
    )
    db.flush()
    return {"root": root.id, "A": a.id, "B": b.id, "C": c.id}


@pytest.mark.asyncio
async def test_start_jobs_one_per_host_dedup(db, graph, monkeypatch):
    """Direct hosts + expanded groups collapse to a deduped set, one job each."""
    seen: list[list[int]] = []

    async def _fake_start_job(db, *, user_id, server_ids, plugin_ids, mode, group_ids=None):
        seen.append(list(server_ids))
        return Job(status="queued", mode="check")

    monkeypatch.setattr(service, "start_job", _fake_start_job)

    # srvC is both directly selected and a member of root -> must not double-run.
    jobs = await service.start_jobs(
        db,
        user_id=1,
        server_ids=[graph["C"]],
        plugin_ids=["p"],
        mode="check",
        group_ids=[graph["root"]],
    )

    assert len(jobs) == 3  # A, B, C exactly once
    assert all(len(ids) == 1 for ids in seen)  # every job targets a single host
    assert sorted(i[0] for i in seen) == sorted([graph["A"], graph["B"], graph["C"]])


@pytest.mark.asyncio
async def test_start_jobs_empty_raises(db, monkeypatch):
    async def _fake_start_job(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("start_job should not run with no targets")

    monkeypatch.setattr(service, "start_job", _fake_start_job)
    with pytest.raises(ValueError):
        await service.start_jobs(
            db, user_id=1, server_ids=[], plugin_ids=["p"], mode="check", group_ids=[]
        )


def test_finalize_older_job_does_not_clobber(db, tmp_path):
    """A stale job finishing after a newer one must not overwrite host state."""
    from sqlalchemy import select

    from app.jobs import JobManager, JobRuntime

    srv = Server(name="srvX", connection_type="local")
    db.add(srv)
    db.flush()
    sid = srv.id

    # Job ids are monotonic: the later-created job (higher id) is the newer run.
    older = Job(status="success", mode="check")
    newer = Job(status="success", mode="check")
    db.add_all([older, newer])
    db.flush()
    older_id, newer_id = older.id, newer.id
    assert newer_id > older_id
    db.commit()

    mgr = JobManager()

    # Newer job finalizes first -> host becomes 'updated' tied to newer_id.
    rt_new = JobRuntime(newer_id, tmp_path / "new.log")
    rt_new.lines = ["PLAY RECAP\nsrvX : ok=5 changed=0 unreachable=0 failed=0\n"]
    rt_new.status = "success"
    mgr._finalize(rt_new, 0, [sid])

    # Older straggler finalizes afterwards with drift -> must be ignored.
    rt_old = JobRuntime(older_id, tmp_path / "old.log")
    rt_old.lines = ["PLAY RECAP\nsrvX : ok=5 changed=3 unreachable=0 failed=0\n"]
    rt_old.status = "success"
    mgr._finalize(rt_old, 0, [sid])

    with session_scope() as s:
        st = s.scalar(select(HostState).where(HostState.server_id == sid))
        assert st.last_job_id == newer_id
        assert st.config_status == "updated"
        assert st.pending_changes == 0
