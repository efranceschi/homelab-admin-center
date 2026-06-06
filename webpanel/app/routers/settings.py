"""App settings and user management (admin only)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import create_user, current_user, require_admin, verify_csrf
from ..db import db_dependency
from ..models import AuditLog, User
from ..plugins import registry
from ..templating import render

router = APIRouter(prefix="/settings")


@router.get("")
def settings_home(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    users = db.scalars(select(User).order_by(User.username)).all()
    return render(
        request, "settings.html", users=users, plugins=registry.all()
    )


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
