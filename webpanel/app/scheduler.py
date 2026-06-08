"""Application-owned scheduler.

Scheduling is NOT handled by cron. Instead the web app spawns this module as a
separate child process (`python -m app.scheduler`). The child polls the
``schedules`` table and runs due schedules via the headless executor, sharing
the same flock as panel-triggered runs so nothing overlaps.

The ``SchedulerProcess`` manager (used by the web app) starts/stops/restarts the
child and reports its status. A pidfile lets a fresh app process re-attach to an
already-running scheduler (e.g. after a self-restart).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import config, discovery, housekeeping
from .ansible_layer import headless
from .db import init_engine, session_scope
from .models import Plugin, Schedule, Server
from .plugins import registry

POLL_SECONDS = 30
# How often to run log/run-dir housekeeping (independent of any schedule).
HOUSEKEEP_SECONDS = 3600
# How often to scan for unmanaged hosts (independent of any schedule). Also runs
# once on startup, so a fresh deploy populates the discovered-hosts table soon.
DISCOVERY_SECONDS = 86400
PIDFILE = config.RUN_DIRS / "scheduler.pid"


# --------------------------------------------------------------------------- #
# Next-run computation
# --------------------------------------------------------------------------- #
def compute_next_run(sched: Schedule, now: datetime) -> datetime:
    """Return the next UTC datetime this schedule should fire after ``now``."""
    if sched.kind == "interval" and sched.interval_minutes:
        return now + timedelta(minutes=sched.interval_minutes)
    # daily at HH:MM local time
    hhmm = (sched.daily_time or "03:30").split(":")
    hour, minute = int(hhmm[0]), int(hhmm[1])
    local_now = now.astimezone()
    target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local_now:
        target = target + timedelta(days=1)
    return target.astimezone(timezone.utc)


def _resolve_targets(db, sched: Schedule) -> list[int]:
    from .groups import expand_group_hosts

    direct = [int(x) for x in sched.server_ids.split(",") if x.strip().isdigit()]
    group_ids = [
        int(x) for x in (sched.group_ids or "").split(",") if x.strip().isdigit()
    ]
    if direct or group_ids:
        return list(set(direct) | expand_group_hosts(db, group_ids))
    return [s.id for s in db.scalars(select(Server).where(Server.enabled.is_(True))).all()]


def _resolve_plugins(db, sched: Schedule) -> list[str]:
    if sched.plugin_ids.strip():
        keys = [p.strip() for p in sched.plugin_ids.split(",") if p.strip()]
    else:
        keys = [
            p.key
            for p in db.scalars(
                select(Plugin).where(Plugin.enabled.is_(True)).order_by(Plugin.order)
            ).all()
        ]
    # In check mode, drop plugins that can't be dry-run safely: their tasks
    # report spurious `changed` under --check and flap the host to pending,
    # which a later panel-triggered check (check-safe surface only) then clears.
    # Mirror that surface here so both paths agree. See _enabled_plugin_keys.
    if sched.mode == "check":
        safe = {
            p.key
            for p in db.scalars(
                select(Plugin).where(Plugin.supports_check_mode.is_(True))
            ).all()
        }
        keys = [k for k in keys if k in safe]
    return keys


# --------------------------------------------------------------------------- #
# The loop (runs in the child process)
# --------------------------------------------------------------------------- #
def run_scheduler() -> None:
    init_engine()
    registry.load()  # the child process has its own (empty) registry to populate
    _write_pidfile()
    print(f"[scheduler] started pid={os.getpid()} poll={POLL_SECONDS}s", flush=True)
    running = {"stop": False}

    def _term(_sig, _frm):
        running["stop"] = True

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    last_housekeep = 0.0  # 0 => run once on the first iteration
    last_discovery = 0.0  # 0 => run once on the first iteration
    while not running["stop"]:
        try:
            _tick()
        except Exception as exc:  # never let the loop die
            print(f"[scheduler] tick error: {exc}", flush=True)
        now_mono = time.monotonic()
        if now_mono - last_housekeep >= HOUSEKEEP_SECONDS:
            last_housekeep = now_mono
            try:
                stats = housekeeping.run_housekeeping()
                if stats["run_dirs_removed"] or stats["logs_cleared"]:
                    print(f"[scheduler] housekeeping {stats}", flush=True)
            except Exception as exc:
                print(f"[scheduler] housekeeping error: {exc}", flush=True)
        if now_mono - last_discovery >= DISCOVERY_SECONDS:
            last_discovery = now_mono
            try:
                stats = discovery.run_discovery()
                print(f"[scheduler] discovery {stats}", flush=True)
            except Exception as exc:
                print(f"[scheduler] discovery error: {exc}", flush=True)
        for _ in range(POLL_SECONDS):
            if running["stop"]:
                break
            time.sleep(1)
    PIDFILE.unlink(missing_ok=True)
    print("[scheduler] stopped", flush=True)


def _tick() -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        schedules = db.scalars(select(Schedule).where(Schedule.enabled.is_(True))).all()
        for sched in schedules:
            if sched.next_run_at is None:
                sched.next_run_at = compute_next_run(sched, now)
                continue
            nra = sched.next_run_at
            if nra.tzinfo is None:
                nra = nra.replace(tzinfo=timezone.utc)
            if nra <= now:
                targets = _resolve_targets(db, sched)
                plugins = _resolve_plugins(db, sched)
                print(f"[scheduler] firing '{sched.name}' ({sched.mode})", flush=True)
                if targets and plugins:
                    # One job per host: keeps each host's config_state tied to its
                    # own run, consistent with panel-triggered fan-out.
                    for sid in targets:
                        headless.run_now(
                            db, server_ids=[sid], plugin_ids=plugins,
                            mode=sched.mode, triggered_by=sched.created_by,
                        )
                sched.last_run_at = now
                sched.next_run_at = compute_next_run(sched, datetime.now(timezone.utc))


def _write_pidfile() -> None:
    config.RUN_DIRS.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()))


# --------------------------------------------------------------------------- #
# Manager (used by the web app process)
# --------------------------------------------------------------------------- #
class SchedulerProcess:
    """Spawns and supervises the scheduler child process."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def _pidfile_pid(self) -> int | None:
        try:
            pid = int(PIDFILE.read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if _pid_alive(pid) else None

    def is_running(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        return self._pidfile_pid() is not None

    def status(self) -> dict:
        pid = (self._proc.pid if self._proc and self._proc.poll() is None else None) or self._pidfile_pid()
        return {"running": pid is not None, "pid": pid}

    def ensure_running(self) -> None:
        if self.is_running():
            return
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "app.scheduler"],
            cwd=str(config.PANEL_DIR),
            start_new_session=True,
        )

    def stop(self) -> None:
        pid = self.status()["pid"]
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        self._proc = None
        PIDFILE.unlink(missing_ok=True)

    def restart(self) -> None:
        self.stop()
        time.sleep(1)
        self.ensure_running()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


manager = SchedulerProcess()


if __name__ == "__main__":
    run_scheduler()
