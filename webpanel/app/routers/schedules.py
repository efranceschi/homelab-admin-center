"""Schedule management — recurring runs executed by the scheduler child process."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ansible_layer.service import JobBusyError, start_jobs
from ..auth import current_user, require_admin, verify_csrf
from ..db import db_dependency
from ..models import AuditLog, HostGroup, Plugin, Schedule, Server, User
from ..scheduler import _resolve_plugins, _resolve_targets
from ..scheduler import manager as scheduler_manager
from ..templating import render

router = APIRouter(prefix="/schedules")


@router.get("")
def list_schedules(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    schedules = db.scalars(select(Schedule).order_by(Schedule.name)).all()
    servers = db.scalars(select(Server).order_by(Server.name)).all()
    plugins = db.scalars(select(Plugin).order_by(Plugin.order)).all()
    groups = db.scalars(select(HostGroup).order_by(HostGroup.name)).all()
    return render(
        request,
        "schedules.html",
        schedules=schedules,
        servers=servers,
        plugins=plugins,
        groups=groups,
        scheduler=scheduler_manager.status(),
    )


@router.post("", dependencies=[Depends(verify_csrf)])
async def add_schedule(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    form = await request.form()
    kind = form.get("kind", "daily")
    action = form.get("action", "ansible")
    if action not in ("ansible", "network_scan"):
        action = "ansible"
    sched = Schedule(
        name=(form.get("name") or "Schedule").strip(),
        enabled=True,
        kind=kind,
        action=action,
        interval_minutes=int(form["interval_minutes"]) if kind == "interval"
        and str(form.get("interval_minutes", "")).isdigit() else None,
        daily_time=(form.get("daily_time") or "03:30") if kind == "daily" else None,
        # A network scan ignores targets/plugins/mode; leave them at defaults.
        mode=form.get("mode", "apply") if action == "ansible" else "check",
        server_ids=",".join(form.getlist("servers")) if action == "ansible" else "",
        plugin_ids=",".join(form.getlist("plugins")) if action == "ansible" else "",
        group_ids=",".join(form.getlist("groups")) if action == "ansible" else "",
        created_by=user.id,
    )
    db.add(sched)
    db.add(AuditLog(user_id=user.id, action="schedule.add", target=sched.name))
    return RedirectResponse("/schedules", status_code=303)


@router.post("/{schedule_id}/toggle", dependencies=[Depends(verify_csrf)])
def toggle_schedule(
    schedule_id: int,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    sched = db.get(Schedule, schedule_id)
    if sched:
        sched.enabled = not sched.enabled
        sched.next_run_at = None  # recompute on next tick
    return RedirectResponse("/schedules", status_code=303)


@router.post("/{schedule_id}/run", dependencies=[Depends(verify_csrf)])
async def run_schedule(
    schedule_id: int,
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    """Fire a schedule on demand, without disturbing its cadence.

    Resolves the same targets/plugins the scheduler child would, then dispatches
    through the panel's job pool (start_jobs) instead of the child's blocking
    executor — so a manual run streams live and shares the queue. ``next_run_at``
    is left untouched: a one-off run must not shift the schedule."""
    sched = db.get(Schedule, schedule_id)
    if sched is None:
        return RedirectResponse("/schedules", status_code=303)
    if sched.action == "network_scan":
        import asyncio

        from .. import netscan

        await asyncio.to_thread(netscan.run_network_scan)
        db.add(AuditLog(user_id=user.id, action="schedule.run", target=sched.name))
        return RedirectResponse("/schedules", status_code=303)
    targets = _resolve_targets(db, sched)
    plugins = _resolve_plugins(db, sched)
    if not targets or not plugins:
        return render(request, "error.html", message="Nothing to run for this schedule.")
    try:
        await start_jobs(
            db, user_id=user.id, server_ids=targets, plugin_ids=plugins, mode=sched.mode
        )
    except (JobBusyError, ValueError) as exc:
        return render(request, "error.html", message=str(exc))
    db.add(AuditLog(user_id=user.id, action="schedule.run", target=sched.name))
    return RedirectResponse("/schedules", status_code=303)


@router.post("/{schedule_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_schedule(
    schedule_id: int,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    sched = db.get(Schedule, schedule_id)
    if sched:
        db.add(AuditLog(user_id=user.id, action="schedule.delete", target=sched.name))
        db.delete(sched)
    return RedirectResponse("/schedules", status_code=303)


@router.post("/scheduler/{action}", dependencies=[Depends(verify_csrf)])
def control_scheduler(
    action: str,
    user: User = Depends(require_admin),
):
    if action == "start":
        scheduler_manager.ensure_running()
    elif action == "stop":
        scheduler_manager.stop()
    elif action == "restart":
        scheduler_manager.restart()
    return RedirectResponse("/schedules", status_code=303)
