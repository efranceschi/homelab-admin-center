"""Orchestrate a panel job: prepare run dir, inventory, vars, command, launch."""
from __future__ import annotations

import shlex
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..jobs import JobBusyError, PanelRestarting, manager
from ..models import Job, Server
from ..plugins import registry
from . import inventory_builder, runner, vars_builder

__all__ = [
    "start_job", "start_jobs", "start_power_job", "JobBusyError", "PanelRestarting",
    "recover_selection",
]


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
    # Targets = explicit hosts ∪ recursively-expanded GROUP members. The physical
    # virtualization tree (Server.parent_server_id) is deliberately NOT expanded
    # here: a node's Check/Apply targets only that node, never its guests (their
    # config is independent). See app/tree.py — that link is presentation-only.
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

    # Resolve each host's effective config (defaults < global < group(s) < host)
    # into inventory hostvars, so per-group/per-host config applies even on
    # multi-host runs. Secrets stay global, in a vault-encrypted extra-vars file.
    host_vars = {
        s.id: vars_builder.resolve_host_vars(db, plugin_ids, s.id) for s in servers
    }
    inv_path = inventory_builder.build_inventory(run_dir, servers, host_vars)
    secret_path = vars_builder.build_secret_vars(db, run_dir, plugin_ids)
    cmd = runner.build_command(
        inventory_path=inv_path,
        tags=tags,
        limit_hosts=[s.name for s in servers],
        check=check,
        extra_vars_path=None,
        secret_vars_path=secret_path,
    )
    env = runner.build_env()

    db.commit()  # persist the job row before the async task touches it
    manager.submit(job_id, cmd, env, run_dir, [s.id for s in servers])
    return job


def _join_commands(cmds: list[list[str]]) -> list[str]:
    """Collapse one or more argv lists into a single command to run.

    A single command runs directly (so cancel can SIGTERM it cleanly). Multiple
    commands (a forced restart, or any bulk stack/node action) run under
    ``bash -lc`` joined with ``;`` so an already-stopped member doesn't abort the
    rest of the batch."""
    if len(cmds) == 1:
        return cmds[0]
    return ["bash", "-lc", " ; ".join(shlex.join(c) for c in cmds)]


async def start_power_job(
    db: Session,
    *,
    user_id: int | None,
    kind: str,
    target_id,
    action: str,
    force: bool,
) -> Job:
    """Create a power job (a pct/qm/docker lifecycle action) and submit it.

    Reuses the job manager end-to-end: the command(s) stream over SSE exactly
    like a check/apply run, but the job is tagged ``kind="power"`` so
    :meth:`JobManager._finalize` skips the ansible-recap path (no config-state
    rewrite, no docker resync). Any SSH key files are written into the job's run
    dir so ``_cleanup_secrets`` removes them after the run.
    """
    from .. import power

    job = Job(
        status="queued",
        kind="power",
        mode="check",  # unused for power jobs, but the column is NOT NULL
        target_type="host",
        triggered_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.flush()  # assign job.id
    job_id = job.id

    run_dir = config.RUN_DIRS / f"job-{job_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        cmds, label, server_ids = power.build_power_commands(
            db, kind=kind, target_id=target_id, action=action, force=force, run_dir=run_dir,
        )
    except Exception:
        # Don't leave an orphan queued row when command-building fails — the
        # route catches the error (so db_dependency commits) and renders it.
        db.delete(job)
        db.flush()
        raise

    job.target_ref = f"{action}: {label}"
    job.server_ids = ",".join(str(i) for i in server_ids)

    cmd = _join_commands(cmds)
    env = runner.build_env()
    db.commit()  # persist before the async task touches the row
    manager.submit(job_id, cmd, env, run_dir, server_ids)
    return job


async def start_jobs(
    db: Session,
    *,
    user_id: int | None,
    server_ids: list[int],
    plugin_ids: list[str],
    mode: str,
    group_ids: list[int] | None = None,
) -> list[Job]:
    """Fan out a multi-host trigger to ONE job per host.

    Resolves the direct ``server_ids`` plus the recursively-expanded
    ``group_ids`` into a deduplicated host set, then starts a single-host
    :func:`start_job` for each. This keeps every host's ``config_state`` tied to
    its own job (no shared ``last_job_id`` across hosts), so the live status and
    drift state stay individual and consistent. Returns the created jobs.
    """
    from ..groups import expand_group_hosts

    target_ids = set(server_ids or []) | expand_group_hosts(db, group_ids or [])
    if not target_ids:
        raise ValueError("no valid target servers selected")
    jobs: list[Job] = []
    for sid in sorted(target_ids):
        jobs.append(
            await start_job(
                db,
                user_id=user_id,
                server_ids=[sid],
                plugin_ids=plugin_ids,
                mode=mode,
                group_ids=[],
            )
        )
    return jobs
