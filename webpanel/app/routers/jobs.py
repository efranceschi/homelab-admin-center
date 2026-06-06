"""Run jobs (check/apply), stream live logs over SSE, view history."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ansible_layer.service import JobBusyError, start_job
from ..auth import current_user, require_admin, verify_csrf
from ..db import db_dependency
from ..jobs import manager
from ..models import Job, Plugin, Server, User
from ..plugins import registry
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
    return render(
        request,
        "jobs.html",
        servers=servers,
        plugins=plugins,
        history=history,
        busy=manager.is_busy(),
    )


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
    server_ids = [int(v) for v in form.getlist("servers")]
    plugin_ids = [v for v in form.getlist("plugins")]
    if not server_ids or not plugin_ids:
        return RedirectResponse("/jobs", status_code=303)

    try:
        job = await start_job(
            db,
            user_id=user.id,
            server_ids=server_ids,
            plugin_ids=plugin_ids,
            mode=mode,
        )
    except JobBusyError as exc:
        return render(request, "error.html", message=str(exc))
    except ValueError as exc:
        return render(request, "error.html", message=str(exc))
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


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
    live = manager.active is not None and manager.active.job_id == job_id
    return render(
        request, "job_detail.html", job=job, log_text=log_text, live=live
    )


@router.get("/{job_id}/stream")
async def job_stream(
    job_id: int,
    request: Request,
    user: User = Depends(current_user),
):
    rt = manager.active
    if rt is None or rt.job_id != job_id:
        async def _empty():
            yield "event: done\ndata: not-live\n\n"

        return StreamingResponse(_empty(), media_type="text/event-stream")

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
                payload = line.rstrip("\n")
                yield f"data: {payload}\n\n"
        finally:
            rt.unsubscribe(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.post("/{job_id}/cancel", dependencies=[Depends(verify_csrf)])
async def cancel_job(
    job_id: int,
    user: User = Depends(require_admin),
):
    await manager.cancel(job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)
