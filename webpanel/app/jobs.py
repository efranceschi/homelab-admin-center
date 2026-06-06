"""Background job manager: runs ansible-playbook and streams live output.

Design constraints (see plan):
  - Single Uvicorn worker -> this in-memory registry is authoritative.
  - One job at a time, guarded by the SAME flock file (/run/lxc-ansible.lock),
    shared with the scheduler child process so runs never overlap.
  - Live logs streamed to the browser via SSE (an asyncio.Queue per subscriber).
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import shutil
import signal
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .ansible_layer import results
from .db import session_scope
from .models import HostState, Job, Server


class JobBusyError(RuntimeError):
    """Raised when a job is already running (panel-level or via the flock)."""


class JobRuntime:
    def __init__(self, job_id: int, log_path: Path) -> None:
        self.job_id = job_id
        self.log_path = log_path
        self.process: asyncio.subprocess.Process | None = None
        self.subscribers: set[asyncio.Queue[str]] = set()
        self.lines: list[str] = []
        self.done = asyncio.Event()
        self.status = "running"

    def publish(self, line: str) -> None:
        self.lines.append(line)
        for q in list(self.subscribers):
            q.put_nowait(line)

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue()
        for line in self.lines:  # replay backlog to late subscribers
            q.put_nowait(line)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self.subscribers.discard(q)


class JobManager:
    def __init__(self) -> None:
        self._active: JobRuntime | None = None
        self._lock_fd: int | None = None

    @property
    def active(self) -> JobRuntime | None:
        return self._active

    def is_busy(self) -> bool:
        return self._active is not None and not self._active.done.is_set()

    # --- flock interlock (shared with run.sh) -------------------------------
    def _acquire_flock(self) -> None:
        config.RUN_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(config.RUN_LOCK_FILE), os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise JobBusyError(
                "another lxc-ansible run is in progress (cron or panel); try again later"
            )
        self._lock_fd = fd

    def _release_flock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            finally:
                self._lock_fd = None

    # --- launch -------------------------------------------------------------
    async def launch(
        self,
        job_id: int,
        cmd: list[str],
        env: dict[str, str],
        run_dir: Path,
        target_server_ids: list[int],
    ) -> JobRuntime:
        if self.is_busy():
            raise JobBusyError("a panel job is already running")
        self._acquire_flock()

        log_path = run_dir / "stdout.log"
        rt = JobRuntime(job_id, log_path)
        self._active = rt

        with session_scope() as db:
            job = db.get(Job, job_id)
            if job:
                job.status = "running"
                job.started_at = datetime.now(timezone.utc)
                job.log_path = str(log_path)

        asyncio.create_task(self._run(rt, cmd, env, run_dir, target_server_ids))
        return rt

    async def _run(
        self,
        rt: JobRuntime,
        cmd: list[str],
        env: dict[str, str],
        run_dir: Path,
        target_server_ids: list[int],
    ) -> None:
        rc = 1
        log_file = rt.log_path.open("w")
        try:
            rt.publish(f"$ {' '.join(cmd)}\n")
            log_file.write(f"$ {' '.join(cmd)}\n")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(config.ANSIBLE_ROOT),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            rt.process = proc
            with session_scope() as db:
                job = db.get(Job, rt.job_id)
                if job:
                    job.pid = proc.pid

            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace")
                log_file.write(line)
                log_file.flush()
                rt.publish(line)
            rc = await proc.wait()
        except Exception as exc:  # pragma: no cover - defensive
            rt.publish(f"\n[panel] runner error: {exc}\n")
        finally:
            log_file.close()
            rt.status = "success" if rc == 0 else "failed"
            self._finalize(rt, rc, target_server_ids)
            self._cleanup_secrets(run_dir)
            self._rotate_logs()
            self._release_flock()
            rt.publish(f"\n[panel] job finished rc={rc} ({rt.status})\n")
            rt.publish("__PANEL_JOB_DONE__")
            rt.done.set()
            self._active = None

    # --- post-processing ----------------------------------------------------
    def _finalize(self, rt: JobRuntime, rc: int, server_ids: list[int]) -> None:
        from sqlalchemy import select

        text = "".join(rt.lines)
        recap = results.parse_recap(text)
        reboot_hosts = results.parse_reboot(text)
        with session_scope() as db:
            job = db.get(Job, rt.job_id)
            if job:
                job.status = rt.status
                job.return_code = rc
                job.finished_at = datetime.now(timezone.utc)
                job.pid = None
            for sid in server_ids:
                srv = db.get(Server, sid)
                if srv is None:
                    continue
                stats = recap.get(srv.name)
                state = db.scalar(select(HostState).where(HostState.server_id == sid))
                if state is None:
                    state = HostState(server_id=sid)
                    db.add(state)
                state.last_job_id = rt.job_id
                new_status = results.status_from_stats(stats)
                if new_status is not None:
                    state.last_status = new_status
                state.reboot_required = srv.name in reboot_hosts

    @staticmethod
    def _cleanup_secrets(run_dir: Path) -> None:
        for name in ("extra-vars-secret.yml", "extra-vars-secret.plain.yml"):
            p = run_dir / name
            p.unlink(missing_ok=True)
        for key in run_dir.glob("id_*"):
            key.unlink(missing_ok=True)

    @staticmethod
    def _rotate_logs() -> None:
        runs = sorted(
            config.RUN_DIRS.glob("*/"),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for old in runs[config.JOB_LOG_RETENTION:]:
            shutil.rmtree(old, ignore_errors=True)

    # --- cancel -------------------------------------------------------------
    async def cancel(self, job_id: int) -> bool:
        rt = self._active
        if rt is None or rt.job_id != job_id or rt.process is None:
            return False
        rt.process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(rt.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            rt.process.send_signal(signal.SIGKILL)
        with session_scope() as db:
            job = db.get(Job, job_id)
            if job:
                job.status = "cancelled"
        rt.status = "cancelled"
        return True


manager = JobManager()
