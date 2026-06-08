"""Run jobs (check/apply), stream live logs over SSE, view history."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ansi import ansi_to_html
from ..ansible_layer.service import JobBusyError, recover_selection, start_jobs
from ..auth import current_user, require_admin, verify_csrf
from ..db import db_dependency
from ..jobs import manager
from ..models import HostGroup, Job, Plugin, Server, User
from ..templating import render

router = APIRouter(prefix="/jobs")


@router.get("")
def jobs_home(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    servers = db.scalars(
        select(Server).where(Server.enabled.is_(True)).order_by(Server.name)
    ).all()
    plugins = db.scalars(
        select(Plugin).where(Plugin.enabled.is_(True)).order_by(Plugin.order)
    ).all()
    history = db.scalars(select(Job).order_by(Job.id.desc()).limit(25)).all()
    groups = db.scalars(select(HostGroup).order_by(HostGroup.name)).all()
    return render(
        request,
        "jobs.html",
        servers=servers,
        plugins=plugins,
        groups=groups,
        history=history,
        busy=manager.is_busy(),
        running=manager.running_count(),
        queued=manager.queued_count(),
        max_concurrent=manager.max_concurrent(),
    )


@router.get("/recent")
def jobs_recent(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    """HTML fragment for the navbar Jobs dropdown: the latest runs."""
    jobs = db.scalars(select(Job).order_by(Job.id.desc()).limit(10)).all()
    return render(request, "_recent_jobs.html", jobs=jobs)


@router.get("/queue-status")
def queue_status(user: User = Depends(current_user)):
    """JSON snapshot of the job pool, polled by the sidebar queue indicator.

    Declared before /{job_id} so the literal path is not captured by the
    path-param route."""
    return {
        "running": manager.running_count(),
        "queued": manager.queued_count(),
        "max_concurrent": manager.max_concurrent(),
        "busy": manager.is_busy(),
        "draining": manager.is_draining(),
    }


@router.post("/run", dependencies=[Depends(verify_csrf)])
async def run_job(
    request: Request,
    mode: str = Form("check"),
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    # viewer may run check mode; only admin may apply.
    if mode == "apply" and user.role != "admin":
        return render(request, "error.html", message="Only admins can apply changes.")

    form = await request.form()
    server_ids = [int(v) for v in form.getlist("servers") if str(v).isdigit()]
    group_ids = [int(v) for v in form.getlist("groups") if str(v).isdigit()]
    plugin_ids = [v for v in form.getlist("plugins")]
    if (not server_ids and not group_ids) or not plugin_ids:
        return RedirectResponse("/jobs", status_code=303)

    try:
        await start_jobs(
            db,
            user_id=user.id,
            server_ids=server_ids,
            plugin_ids=plugin_ids,
            mode=mode,
            group_ids=group_ids,
        )
    except JobBusyError as exc:
        return render(request, "error.html", message=str(exc))
    except ValueError as exc:
        return render(request, "error.html", message=str(exc))
    # Stay on the Run page; the new per-host jobs show in history (status links).
    return RedirectResponse("/jobs", status_code=303)


@router.get("/{job_id}")
def job_detail(
    job_id: int,
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    job = db.get(Job, job_id)
    if job is None:
        return RedirectResponse("/jobs", status_code=303)
    log_text = ""
    if job.log_path:
        try:
            with open(job.log_path, encoding="utf-8", errors="replace") as fh:
                log_text = fh.read()
        except OSError:
            log_text = ""
    # Fall back to the persisted copy when the run dir has been cleaned up.
    if not log_text and job.log_text:
        log_text = job.log_text
    live = manager.get_runtime(job_id) is not None
    return render(
        request,
        "job_detail.html",
        job=job,
        log_html=ansi_to_html(log_text),
        live=live,
    )


@router.get("/{job_id}/stream")
async def job_stream(
    job_id: int,
    request: Request,
    user: User = Depends(current_user),
):
    # Disable response buffering on any reverse proxy in front of the panel
    # (nginx honours X-Accel-Buffering); without this the browser only sees the
    # whole log once the connection closes, instead of line-by-line.
    sse_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    rt = manager.get_runtime(job_id)
    if rt is None:
        async def _empty():
            yield "event: done\ndata: not-live\n\n"

        return StreamingResponse(
            _empty(), media_type="text/event-stream", headers=sse_headers
        )

    async def event_gen():
        q = rt.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    line = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if line == "__PANEL_JOB_DONE__":
                    yield "event: done\ndata: done\n\n"
                    break
                payload = ansi_to_html(line.rstrip("\n"))
                yield f"data: {payload}\n\n"
        finally:
            rt.unsubscribe(q)

    return StreamingResponse(
        event_gen(), media_type="text/event-stream", headers=sse_headers
    )


@router.post("/{job_id}/retry", dependencies=[Depends(verify_csrf)])
async def retry_job(
    job_id: int,
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    """Re-run a finished job with its original target and plugin selection.

    Covers retrying a failed/cancelled run and re-running a successful one
    ("Executar Novamente"); only an in-flight (queued/running) job is rejected.
    """
    job = db.get(Job, job_id)
    if job is None:
        return RedirectResponse("/jobs", status_code=303)
    if job.status not in ("success", "failed", "cancelled"):
        return render(
            request, "error.html", message="Only finished jobs can be re-run."
        )
    # Apply mode is admin-only, matching the run endpoint.
    if job.mode == "apply" and user.role != "admin":
        return render(request, "error.html", message="Only admins can apply changes.")

    server_ids, plugin_ids, group_ids = recover_selection(db, job)
    if (not server_ids and not group_ids) or not plugin_ids:
        return render(
            request,
            "error.html",
            message="Cannot retry: the original targets or plugins are no longer available.",
        )

    try:
        new_jobs = await start_jobs(
            db,
            user_id=user.id,
            server_ids=server_ids,
            plugin_ids=plugin_ids,
            mode=job.mode,
            group_ids=group_ids,
        )
    except (JobBusyError, ValueError) as exc:
        return render(request, "error.html", message=str(exc))
    if len(new_jobs) == 1:
        return RedirectResponse(f"/jobs/{new_jobs[0].id}", status_code=303)
    return RedirectResponse("/jobs", status_code=303)


@router.post("/{job_id}/apply", dependencies=[Depends(verify_csrf)])
async def apply_from_job(
    job_id: int,
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    """Promote a successful check (dry run) into a fresh apply job.

    Starts a new, separate job in apply mode against the same targets and
    plugins as the source check — so the dry run and the real apply remain
    distinct entries in history. Apply is admin-only, matching /run.
    """
    if user.role != "admin":
        return render(request, "error.html", message="Only admins can apply changes.")
    job = db.get(Job, job_id)
    if job is None:
        return RedirectResponse("/jobs", status_code=303)
    if job.mode != "check" or job.status != "success":
        return render(
            request,
            "error.html",
            message="Apply is only offered for a successful check run.",
        )

    server_ids, plugin_ids, group_ids = recover_selection(db, job)
    if (not server_ids and not group_ids) or not plugin_ids:
        return render(
            request,
            "error.html",
            message="Cannot apply: the original targets or plugins are no longer available.",
        )

    try:
        new_jobs = await start_jobs(
            db,
            user_id=user.id,
            server_ids=server_ids,
            plugin_ids=plugin_ids,
            mode="apply",
            group_ids=group_ids,
        )
    except (JobBusyError, ValueError) as exc:
        return render(request, "error.html", message=str(exc))
    if len(new_jobs) == 1:
        return RedirectResponse(f"/jobs/{new_jobs[0].id}", status_code=303)
    return RedirectResponse("/jobs", status_code=303)


@router.post("/{job_id}/cancel", dependencies=[Depends(verify_csrf)])
async def cancel_job(
    job_id: int,
    user: User = Depends(require_admin),
):
    await manager.cancel(job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)
