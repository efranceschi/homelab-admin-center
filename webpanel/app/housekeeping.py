"""Disk and database housekeeping for job logs.

Job output is kept two ways: a per-job run directory on disk
(``run_dirs/job-N/``, holding inventory + ``stdout.log``) and, once a job
finishes, the full captured log persisted into ``jobs.log_text`` so it survives
the rotation of the on-disk run directory.

Housekeeping bounds both. Only the ``JOB_LOG_RETENTION`` most recent jobs keep
their run directory and their stored ``log_text``; older job rows are retained
as metadata-only history (status, timing, return code) with ``log_text``
cleared. It is safe to call repeatedly and from the scheduler child process —
the newest run dir (the active job, if any) is always preserved.
"""
from __future__ import annotations

import re
import shutil

from sqlalchemy import select

from . import config
from .db import session_scope
from .models import Job

_JOB_DIR_RE = re.compile(r"^job-\d+$")


def purge_run_dirs(keep: int) -> int:
    """Delete run dirs beyond the newest ``keep`` (by mtime). Returns count removed."""
    if not config.RUN_DIRS.exists():
        return 0
    dirs = sorted(
        (
            d
            for d in config.RUN_DIRS.iterdir()
            if d.is_dir() and _JOB_DIR_RE.match(d.name)
        ),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for old in dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        removed += 1
    return removed


def purge_job_logs(keep: int) -> int:
    """Clear ``log_text`` on all but the newest ``keep`` jobs. Returns rows cleared."""
    cleared = 0
    with session_scope() as db:
        ids = db.scalars(select(Job.id).order_by(Job.id.desc())).all()
        for jid in ids[keep:]:
            job = db.get(Job, jid)
            if job is not None and job.log_text is not None:
                job.log_text = None
                cleared += 1
    return cleared


def run_housekeeping(keep: int | None = None) -> dict[str, int]:
    """Run every housekeeping step; safe to call periodically."""
    keep = config.JOB_LOG_RETENTION if keep is None else keep
    keep = max(1, keep)
    return {
        "run_dirs_removed": purge_run_dirs(keep),
        "logs_cleared": purge_job_logs(keep),
    }
