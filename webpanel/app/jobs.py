"""Background job manager: runs ansible-playbook and streams live output.

Design constraints (see plan):
  - Single Uvicorn worker -> this in-memory registry is authoritative.
  - One job at a time, guarded by the SAME flock file (/run/hac.lock),
    shared with the scheduler child process so runs never overlap.
  - Live logs streamed to the browser via SSE (an asyncio.Queue per subscriber).
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import signal
from datetime import datetime, timezone
from pathlib import Path

from . import config, housekeeping
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


# Per-host fan-out turns a "Check All" into N jobs; run several at once so the
# burst doesn't serialize (still capped, and never overlaps a scheduler run).
DEFAULT_MAX_CONCURRENT = 5
MAX_MAX_CONCURRENT = 10
_RETRY_SECONDS = 5  # re-check the flock this often while runs are queued


class JobManager:
    """Runs panel jobs with a bounded pool + FIFO queue.

    Concurrency is capped by the ``max_concurrent_jobs`` setting (default 1).
    Extra runs are queued (DB status ``queued``) instead of rejected and started
    as slots free. A single flock (shared with the scheduler child) is held with
    reference-counting while ANY panel job runs, so panel jobs may overlap each
    other but never overlap a scheduler/cron run; when the scheduler holds the
    lock, queued jobs simply wait and a timer retries.
    """

    def __init__(self) -> None:
        self._active: dict[int, JobRuntime] = {}
        self._queue: list[tuple[int, list[str], dict[str, str], Path, list[int]]] = []
        self._lock_fd: int | None = None
        self._retry_handle: asyncio.TimerHandle | None = None

    # --- introspection ------------------------------------------------------
    @property
    def active(self) -> JobRuntime | None:
        """Back-compat: the most recently started running job, if any."""
        if not self._active:
            return None
        return next(reversed(list(self._active.values())))

    def get_runtime(self, job_id: int) -> JobRuntime | None:
        return self._active.get(job_id)

    def active_job_ids(self) -> list[int]:
        """Job ids currently running (may be more than one with concurrency)."""
        return list(self._active.keys())

    def running_count(self) -> int:
        return len(self._active)

    def queued_count(self) -> int:
        return len(self._queue)

    def queued_jobs(self) -> list[tuple[int, list[int]]]:
        """(job_id, target server ids) for each pending (not yet started) job."""
        return [(item[0], list(item[4])) for item in self._queue]

    def max_concurrent(self) -> int:
        from .models import Setting

        try:
            with session_scope() as db:
                row = db.get(Setting, "max_concurrent_jobs")
                n = int(row.value) if row and str(row.value).strip() else DEFAULT_MAX_CONCURRENT
        except (ValueError, TypeError, Exception):
            n = DEFAULT_MAX_CONCURRENT
        return max(1, min(MAX_MAX_CONCURRENT, n))

    def is_busy(self) -> bool:
        """True when the running pool is at capacity (further runs would queue)."""
        return len(self._active) >= self.max_concurrent()

    # --- flock interlock (shared with the scheduler) ------------------------
    def _acquire_flock(self) -> bool:
        """Ensure the shared run-lock is held. Returns False if another process
        (the scheduler / a cron run) currently holds it."""
        if self._lock_fd is not None:
            return True
        config.RUN_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(config.RUN_LOCK_FILE), os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def _release_flock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            finally:
                self._lock_fd = None

    # --- submit / dispatch --------------------------------------------------
    def submit(
        self,
        job_id: int,
        cmd: list[str],
        env: dict[str, str],
        run_dir: Path,
        target_server_ids: list[int],
    ) -> None:
        """Enqueue a job (already persisted as ``queued``) and try to dispatch."""
        self._queue.append((job_id, cmd, env, run_dir, target_server_ids))
        self._dispatch()

    def _arm_retry(self) -> None:
        if self._retry_handle is not None:
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return

        def _retry() -> None:
            self._retry_handle = None
            self._dispatch()

        self._retry_handle = loop.call_later(_RETRY_SECONDS, _retry)

    def _dispatch(self) -> None:
        """Start queued jobs up to the concurrency limit, if the lock is free."""
        if self._retry_handle is not None:
            self._retry_handle.cancel()
            self._retry_handle = None
        limit = self.max_concurrent()
        while self._queue and len(self._active) < limit:
            if not self._acquire_flock():
                # Scheduler/cron holds the lock — wait and retry shortly.
                self._arm_retry()
                return
            job_id, cmd, env, run_dir, server_ids = self._queue.pop(0)
            self._start(job_id, cmd, env, run_dir, server_ids)

    def _start(
        self,
        job_id: int,
        cmd: list[str],
        env: dict[str, str],
        run_dir: Path,
        target_server_ids: list[int],
    ) -> None:
        log_path = run_dir / "stdout.log"
        rt = JobRuntime(job_id, log_path)
        self._active[job_id] = rt
        with session_scope() as db:
            job = db.get(Job, job_id)
            if job:
                job.status = "running"
                job.started_at = datetime.now(timezone.utc)
                job.log_path = str(log_path)
        asyncio.create_task(self._run(rt, cmd, env, run_dir, target_server_ids))

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
            # Preserve an explicit cancellation; otherwise derive from rc.
            if rt.status != "cancelled":
                rt.status = "success" if rc == 0 else "failed"
            self._finalize(rt, rc, target_server_ids)
            self._cleanup_secrets(run_dir)
            housekeeping.run_housekeeping()
            rt.publish(f"\n[panel] job finished rc={rc} ({rt.status})\n")
            rt.publish("__PANEL_JOB_DONE__")
            rt.done.set()
            # Free the slot, start any queued job (keeps the lock if work
            # remains), and only release the shared lock once fully idle.
            self._active.pop(rt.job_id, None)
            self._dispatch()
            if not self._active and not self._queue:
                self._release_flock()

    # --- post-processing ----------------------------------------------------
    def _finalize(self, rt: JobRuntime, rc: int, server_ids: list[int]) -> None:
        from sqlalchemy import select

        text = "".join(rt.lines)
        recap = results.parse_recap(text)
        reboot_hosts = results.parse_reboot(text)
        finished = datetime.now(timezone.utc)
        with session_scope() as db:
            job = db.get(Job, rt.job_id)
            mode = job.mode if job else "check"
            if job:
                job.status = rt.status
                job.return_code = rc
                job.finished_at = finished
                job.pid = None
                # Persist the full log so it survives run-dir housekeeping.
                job.log_text = text
            for sid in server_ids:
                srv = db.get(Server, sid)
                if srv is None:
                    continue
                stats = recap.get(srv.name)
                state = db.scalar(select(HostState).where(HostState.server_id == sid))
                if state is None:
                    state = HostState(server_id=sid)
                    db.add(state)
                # Only the most recent run wins: never let an older job (a
                # straggler that finished after a newer one) clobber a fresher
                # per-host state.
                if state.last_job_id is not None and rt.job_id < state.last_job_id:
                    continue
                state.last_job_id = rt.job_id
                new_status = results.status_from_stats(stats)
                if new_status is not None:
                    state.last_status = new_status
                state.reboot_required = srv.name in reboot_hosts
                cfg_status, pending = results.derive_config_state(
                    mode, stats, reachable=stats is not None
                )
                state.config_status = cfg_status
                state.config_checked_at = finished
                state.pending_changes = pending

    @staticmethod
    def _cleanup_secrets(run_dir: Path) -> None:
        for name in ("extra-vars-secret.yml", "extra-vars-secret.plain.yml"):
            p = run_dir / name
            p.unlink(missing_ok=True)
        for key in run_dir.glob("id_*"):
            key.unlink(missing_ok=True)

    # --- cancel -------------------------------------------------------------
    async def cancel(self, job_id: int) -> bool:
        # Running job: signal the process; status is set here and preserved by _run.
        rt = self._active.get(job_id)
        if rt is not None and rt.process is not None:
            rt.status = "cancelled"
            rt.process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(rt.process.wait(), timeout=10)
            except asyncio.TimeoutError:
                rt.process.send_signal(signal.SIGKILL)
            with session_scope() as db:
                job = db.get(Job, job_id)
                if job:
                    job.status = "cancelled"
            return True
        # Still queued: drop it from the queue and mark cancelled.
        for i, spec in enumerate(self._queue):
            if spec[0] == job_id:
                self._queue.pop(i)
                with session_scope() as db:
                    job = db.get(Job, job_id)
                    if job:
                        job.status = "cancelled"
                return True
        return False


manager = JobManager()
