"""Credential management (Infrastructure): secrets encrypted at rest."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto
from ..auth import current_user, require_admin, verify_csrf
from ..db import db_dependency
from ..models import AuditLog, Credential, User
from ..templating import render

router = APIRouter(prefix="/credentials")


@router.get("")
def list_credentials(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    credentials = db.scalars(select(Credential).order_by(Credential.name)).all()
    return render(request, "credentials.html", credentials=credentials)


@router.post("", dependencies=[Depends(verify_csrf)])
def add_credential(
    request: Request,
    name: str = Form(...),
    type: str = Form(...),
    secret: str = Form(...),
    meta: str = Form(""),
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    cred = Credential(
        name=name.strip(),
        type=type,
        secret_ciphertext=crypto.get_box().encrypt(secret),
        meta_json=json.dumps({"note": meta.strip()}) if meta.strip() else "{}",
        created_by=user.id,
    )
    db.add(cred)
    db.add(AuditLog(user_id=user.id, action="credential.add", target=cred.name))
    return RedirectResponse("/credentials", status_code=303)


@router.post("/{cred_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_credential(
    cred_id: int,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    cred = db.get(Credential, cred_id)
    if cred:
        db.add(AuditLog(user_id=user.id, action="credential.delete", target=cred.name))
        db.delete(cred)
    return RedirectResponse("/credentials", status_code=303)
