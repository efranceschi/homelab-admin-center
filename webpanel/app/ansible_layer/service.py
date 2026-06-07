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

__all__ = ["start_job", "JobBusyError", "recover_selection"]


def recover_selection(db: Session, job: Job) -> tuple[list[int], list[str], list[int]]:
    """Reconstruct the (server_ids, plugin_ids, group_ids) a job was launched with.

    ``server_ids`` is the DIRECT host selection and ``group_ids`` the selected
    groups (expanded to hosts at run time, so a retry re-resolves current
    membership). For older jobs (pre-migration) it falls back to resolving server
    names from ``target_ref`` and reverse-mapping ``plugin_tags`` to the plugins
    whose tags they fully cover.
    """
    server_ids = [
        int(x) for x in (job.server_ids or "").split(",") if x.strip().isdigit()
    ]
    plugin_ids = [k.strip() for k in (job.plugin_ids or "").split(",") if k.strip()]
    group_ids = [
        int(x) for x in (job.group_ids or "").split(",") if x.strip().isdigit()
    ]

    if not server_ids and not group_ids and job.target_ref:
        names = [n.strip() for n in job.target_ref.split(",") if n.strip()]
        if names:
            rows = db.scalars(select(Server).where(Server.name.in_(names))).all()
            server_ids = [s.id for s in rows]

    if not plugin_ids and job.plugin_tags:
        tagset = {t.strip() for t in job.plugin_tags.split(",") if t.strip()}
        if tagset:
            plugin_ids = [
                lp.id for lp in registry.all() if lp.tags and set(lp.tags) <= tagset
            ]
    return server_ids, plugin_ids, group_ids


async def start_job(
    db: Session,
    *,
    user_id: int | None,
    server_ids: list[int],
    plugin_ids: list[str],
    mode: str,
    group_ids: list[int] | None = None,
) -> Job:
    """Create a job and submit it to the manager.

    ``server_ids`` are directly-selected hosts; ``group_ids`` are groups expanded
    (recursively, de-duplicated) to their member hosts. The direct selection and
    the groups are persisted separately so a retry re-resolves group membership.

    Never rejects for concurrency: if the running pool is at capacity (or the
    scheduler holds the shared lock) the job is persisted as ``queued`` and the
    manager starts it when a slot frees.
    """
    from ..groups import expand_group_hosts

    group_ids = group_ids or []
    direct_ids = list(server_ids or [])
    target_ids = set(direct_ids) | expand_group_hosts(db, group_ids)
    servers = list(db.scalars(select(Server).where(Server.id.in_(target_ids))).all())
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
        server_ids=",".join(str(i) for i in direct_ids),
        plugin_ids=",".join(plugin_ids),
        group_ids=",".join(str(i) for i in group_ids),
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
    manager.submit(job_id, cmd, env, run_dir, [s.id for s in servers])
    return job
