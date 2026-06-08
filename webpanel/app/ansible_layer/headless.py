"""Synchronous run executor used by the scheduler child process.

Mirrors the async JobManager but runs ansible-playbook with a blocking
subprocess (no SSE), recording a Job row and updating host state. Shares the
same flock file as the panel so a scheduled run and a panel run never overlap.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..models import HostState, Job, Server
from ..plugins import registry
from . import inventory_builder, results, runner, vars_builder


class _Flock:
    def __init__(self, path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self):
        config.RUN_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_WRONLY | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises OSError if held
        self._fd = fd
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def _reconcile_probed_hostnames(db: Session, servers, text: str) -> None:
    """Feed the run's gathered hostnames into discovery (opportunistic refresh).

    Every panel run emits the facts-probe marker (tagged ``always``), so each run
    doubles as a hostname probe. Imported lazily to avoid an import cycle.
    """
    from .. import discovery

    discovery.record_probe_hostnames(db, servers, results.parse_hostnames(text))


def run_facts_probe(servers) -> str:
    """Run a read-only facts-only pass and return the raw stdout log text.

    Selects only the ``facts`` tag (roles are skipped) under ``--check``, sharing
    the run flock so it never overlaps a real job. Returns ``""`` if there are no
    servers, the lock is held, or ansible is unavailable — the markers it carries
    (``PANEL_HOSTNAME``/``PANEL_FACTS``/``PANEL_DOCKER``) are parsed by the caller.
    """
    if not servers:
        return ""
    run_dir = config.RUN_DIRS / "facts-probe"
    run_dir.mkdir(parents=True, exist_ok=True)
    host_vars = {s.id: {} for s in servers}
    inv = inventory_builder.build_inventory(run_dir, servers, host_vars)
    cmd = runner.build_command(inv, ["facts"], [s.name for s in servers], True, None, None)
    env = runner.build_env()
    log_path = run_dir / "stdout.log"
    try:
        with _Flock(config.RUN_LOCK_FILE):
            with log_path.open("w") as log:
                subprocess.run(
                    cmd, cwd=str(config.ANSIBLE_ROOT), env=env,
                    stdout=log, stderr=subprocess.STDOUT,
                )
    except OSError:
        return ""
    finally:
        for key in run_dir.glob("id_*"):
            key.unlink(missing_ok=True)
    return log_path.read_text(errors="replace")


def gather_hostnames(servers) -> dict[str, str]:
    """Run a read-only facts pass and return ``{inventory_host: hostname}``."""
    return results.parse_hostnames(run_facts_probe(servers))


def run_now(
    db: Session,
    *,
    server_ids: list[int],
    plugin_ids: list[str],
    mode: str,
    triggered_by: int | None = None,
) -> Job | None:
    """Execute a run synchronously. Returns the Job, or None if no targets."""
    servers = list(db.scalars(select(Server).where(Server.id.in_(server_ids))).all())
    if not servers:
        return None

    tags: list[str] = []
    for pid in plugin_ids:
        lp = registry.get(pid)
        if lp:
            tags.extend(lp.tags)
    check = mode != "apply"

    job = Job(
        status="queued",
        mode="apply" if mode == "apply" else "check",
        target_type="group" if len(servers) > 1 else "host",
        target_ref=",".join(s.name for s in servers),
        plugin_tags=",".join(tags),
        server_ids=",".join(str(s.id) for s in servers),
        plugin_ids=",".join(plugin_ids),
        triggered_by=triggered_by,
        created_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.flush()
    job_id = job.id

    run_dir = config.RUN_DIRS / f"job-{job_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    host_vars = {
        s.id: vars_builder.resolve_host_vars(db, plugin_ids, s.id) for s in servers
    }
    inv = inventory_builder.build_inventory(run_dir, servers, host_vars)
    secret = vars_builder.build_secret_vars(db, run_dir, plugin_ids)
    cmd = runner.build_command(inv, tags, [s.name for s in servers], check, None, secret)
    env = runner.build_env()
    log_path = run_dir / "stdout.log"

    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    job.log_path = str(log_path)
    db.commit()

    rc = 1
    try:
        with _Flock(config.RUN_LOCK_FILE):
            with log_path.open("w") as log:
                log.write(f"$ {' '.join(cmd)}\n")
                log.flush()
                proc = subprocess.run(
                    cmd, cwd=str(config.ANSIBLE_ROOT), env=env,
                    stdout=log, stderr=subprocess.STDOUT,
                )
                rc = proc.returncode
    except OSError:
        with log_path.open("a") as log:
            log.write("\n[scheduler] another run holds the lock; skipped.\n")
        job.status = "cancelled"
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
        return job
    finally:
        for name in ("extra-vars-secret.yml", "extra-vars-secret.plain.yml"):
            (run_dir / name).unlink(missing_ok=True)
        for key in run_dir.glob("id_*"):
            key.unlink(missing_ok=True)

    text = log_path.read_text(errors="replace")
    recap = results.parse_recap(text)
    reboot = results.parse_reboot(text)
    _reconcile_probed_hostnames(db, servers, text)
    from .. import docker, inventory

    inventory.store_facts(db, servers, results.parse_facts(text))
    docker.sync_containers(
        db, servers, results.parse_docker(text),
        results.parse_hostnames(text).keys(),
    )
    finished = datetime.now(timezone.utc)
    norm_mode = "apply" if mode == "apply" else "check"
    job.status = "success" if rc == 0 else "failed"
    job.return_code = rc
    job.finished_at = finished
    for srv in servers:
        state = db.scalar(select(HostState).where(HostState.server_id == srv.id))
        if state is None:
            state = HostState(server_id=srv.id)
            db.add(state)
        # Only the most recent run wins: never let an older job (a straggler that
        # finished after a newer one) clobber a fresher per-host state.
        if state.last_job_id is not None and job_id < state.last_job_id:
            continue
        state.last_job_id = job_id
        stats = recap.get(srv.name)
        st = results.status_from_stats(stats)
        if st is not None:
            state.last_status = st
        state.reboot_required = srv.name in reboot
        cfg_status, pending = results.derive_host_state(
            norm_mode, stats, reachable=stats is not None
        )
        state.config_status = cfg_status
        state.config_checked_at = finished
        state.pending_changes = pending
    db.commit()
    return job
