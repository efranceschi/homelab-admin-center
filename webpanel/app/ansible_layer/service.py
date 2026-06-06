"""Orchestrate a panel job: prepare run dir, inventory, vars, command, launch."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..jobs import JobBusyError, manager
from ..models import Job, Server
from ..plugins import registry
from . import inventory_builder, runner, vars_builder

__all__ = ["start_job", "JobBusyError"]


async def start_job(
    db: Session,
    *,
    user_id: int | None,
    server_ids: list[int],
    plugin_ids: list[str],
    mode: str,
) -> Job:
    """Create and launch a job. Raises JobBusyError if one is already running."""
    if manager.is_busy():
        raise JobBusyError("a panel job is already running")

    servers = list(db.scalars(select(Server).where(Server.id.in_(server_ids))).all())
    if not servers:
        raise ValueError("no valid target servers selected")

    tags: list[str] = []
    for pid in plugin_ids:
        lp = registry.get(pid)
        if lp:
            tags.extend(lp.tags)

    check = mode != "apply"

    job = Job(
        status="queued",
        mode="apply" if mode == "apply" else "check",
        target_type="host" if len(servers) == 1 else "group",
        target_ref=",".join(s.name for s in servers),
        plugin_tags=",".join(tags),
        triggered_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.flush()  # assign job.id
    job_id = job.id

    run_dir = config.RUN_DIRS / f"job-{job_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    inv_path = inventory_builder.build_inventory(run_dir, servers)
    # For a single-host run we resolve per-host config; otherwise global/group.
    single_sid = servers[0].id if len(servers) == 1 else None
    extra_path, secret_path = vars_builder.build_extra_vars(
        db, run_dir, plugin_ids, single_sid
    )
    cmd = runner.build_command(
        inventory_path=inv_path,
        tags=tags,
        limit_hosts=[s.name for s in servers],
        check=check,
        extra_vars_path=extra_path,
        secret_vars_path=secret_path,
    )
    env = runner.build_env()

    db.commit()  # persist the job row before the async task touches it
    await manager.launch(job_id, cmd, env, run_dir, [s.id for s in servers])
    return job
