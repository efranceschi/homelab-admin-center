"""App settings and user management (admin only)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import system
from ..auth import create_user, current_user, require_admin, verify_csrf
from ..db import db_dependency
from ..jobs import DEFAULT_MAX_CONCURRENT, MAX_MAX_CONCURRENT, manager as job_manager
from ..models import AuditLog, Setting, User
from ..plugins import registry
from ..scheduler import manager as scheduler_manager
from ..templating import render, set_auto_refresh_seconds, set_instance_name


def _get_setting_int(db: Session, key: str, default: int) -> int:
    row = db.get(Setting, key)
    try:
        return int(row.value) if row and str(row.value).strip() else default
    except (TypeError, ValueError):
        return default


def _set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value, value_type="int"))
    else:
        row.value = value

router = APIRouter(prefix="/settings")


@router.get("")
def settings_home(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    users = db.scalars(select(User).order_by(User.username)).all()
    return render(
        request,
        "settings.html",
        users=users,
        plugins=registry.all(),
        scheduler=scheduler_manager.status(),
        under_systemd=system.under_systemd(),
        max_concurrent_jobs=_get_setting_int(db, "max_concurrent_jobs", DEFAULT_MAX_CONCURRENT),
        max_concurrent_cap=MAX_MAX_CONCURRENT,
        auto_refresh_seconds=_get_setting_int(db, "auto_refresh_seconds", 180),
        running_jobs=job_manager.running_count(),
        queued_jobs=job_manager.queued_count(),
    )


@router.post("/instance", dependencies=[Depends(verify_csrf)])
def update_instance_name(
    request: Request,
    instance_name: str = Form(""),
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    value = instance_name.strip()
    row = db.get(Setting, "instance_name")
    if row is None:
        db.add(Setting(key="instance_name", value=value, value_type="str"))
    else:
        row.value = value
    set_instance_name(value)  # refresh the live navbar global (single worker)
    db.add(AuditLog(user_id=user.id, action="setting.update", target="instance_name"))
    return RedirectResponse("/settings", status_code=303)


@router.post("/runtime", dependencies=[Depends(verify_csrf)])
def update_runtime(
    request: Request,
    max_concurrent_jobs: int = Form(DEFAULT_MAX_CONCURRENT),
    auto_refresh_seconds: int = Form(180),
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    """Job concurrency + page auto-refresh interval (admin)."""
    mc = max(1, min(MAX_MAX_CONCURRENT, max_concurrent_jobs))
    ar = auto_refresh_seconds
    if ar < 0:
        ar = 0
    if 0 < ar < 10:
        ar = 10
    ar = min(3600, ar)
    _set_setting(db, "max_concurrent_jobs", str(mc))
    _set_setting(db, "auto_refresh_seconds", str(ar))
    # Commit BEFORE dispatching: the JobManager re-reads max_concurrent_jobs in a
    # fresh session, so the new limit must already be persisted or the immediate
    # dispatch would still see the old value (and a raised limit wouldn't start
    # queued jobs until the next event). This is what makes the limit dynamic
    # with no restart.
    db.commit()
    set_auto_refresh_seconds(ar)  # refresh the live global (single worker)
    # A freed/raised limit may let queued jobs start immediately.
    job_manager._dispatch()
    db.add(AuditLog(user_id=user.id, action="setting.update", target="runtime"))
    return RedirectResponse("/settings#runtime", status_code=303)


@router.post("/system/update", dependencies=[Depends(verify_csrf)])
def system_update(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    output = system.run_update()
    db.add(AuditLog(user_id=user.id, action="system.update"))
    db.commit()
    note = system.request_restart(delay=2.0)
    return render(request, "system_action.html", title="Update", output=output, note=note)


@router.post("/system/restart", dependencies=[Depends(verify_csrf)])
def system_restart(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    db.add(AuditLog(user_id=user.id, action="system.restart"))
    db.commit()
    note = system.request_restart(delay=1.5)
    return render(request, "system_action.html", title="Restart", output="", note=note)


@router.post("/users", dependencies=[Depends(verify_csrf)])
def add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("viewer"),
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    if not username.strip() or len(password) < 8:
        return render(request, "error.html", message="Username and 8+ char password required.")
    if db.scalar(select(User).where(User.username == username.strip())):
        return render(request, "error.html", message="Username already exists.")
    new = create_user(db, username.strip(), password, role=role if role in ("admin", "viewer") else "viewer")
    db.add(AuditLog(user_id=user.id, action="user.add", target=new.username))
    return RedirectResponse("/settings", status_code=303)


@router.post("/users/{user_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_user(
    user_id: int,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    target = db.get(User, user_id)
    # Never delete the last remaining admin / yourself into lockout.
    if target and target.id != user.id:
        admins = db.scalars(select(User).where(User.role == "admin")).all()
        if not (target.role == "admin" and len(admins) <= 1):
            db.add(AuditLog(user_id=user.id, action="user.delete", target=target.username))
            db.delete(target)
    return RedirectResponse("/settings", status_code=303)


@router.post("/plugins/reload", dependencies=[Depends(verify_csrf)])
def reload_plugins(
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    from ..plugins import sync_to_db

    registry.load()
    sync_to_db(db)
    db.add(AuditLog(user_id=user.id, action="plugins.reload"))
    return RedirectResponse("/settings", status_code=303)
