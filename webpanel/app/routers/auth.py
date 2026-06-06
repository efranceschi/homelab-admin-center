"""Login, logout, and first-run admin setup."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import (
    authenticate,
    create_user,
    is_setup_complete,
    login_session,
    logout_session,
    verify_csrf,
)
from ..db import db_dependency
from ..models import AuditLog
from ..templating import render

router = APIRouter()


@router.get("/setup")
def setup_form(request: Request, db: Session = Depends(db_dependency)):
    if is_setup_complete(db):
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html")


@router.post("/setup", dependencies=[Depends(verify_csrf)])
def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    db: Session = Depends(db_dependency),
):
    if is_setup_complete(db):
        return RedirectResponse("/login", status_code=303)
    if not username.strip() or len(password) < 8 or password != password2:
        return render(
            request,
            "setup.html",
            error="Username required and passwords must match (min 8 chars).",
        )
    user = create_user(db, username.strip(), password, role="admin")
    db.add(AuditLog(user_id=user.id, action="setup", target=username))
    login_session(request, user)
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/login")
def login_form(request: Request, db: Session = Depends(db_dependency)):
    if not is_setup_complete(db):
        return RedirectResponse("/setup", status_code=303)
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "login.html")


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(db_dependency),
):
    user = authenticate(db, username.strip(), password)
    if user is None:
        return render(request, "login.html", error="Invalid credentials or account locked.")
    db.add(AuditLog(user_id=user.id, action="login", target=user.username))
    login_session(request, user)
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    logout_session(request)
    return RedirectResponse("/login", status_code=303)
