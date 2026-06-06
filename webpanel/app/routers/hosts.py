"""Host (server) and credential management."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto
from ..auth import current_user, require_admin, verify_csrf
from ..db import db_dependency
from ..models import AuditLog, Credential, Server, User
from ..templating import render

router = APIRouter(prefix="/hosts")


@router.get("")
def list_hosts(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    servers = db.scalars(select(Server).order_by(Server.name)).all()
    credentials = db.scalars(select(Credential).order_by(Credential.name)).all()
    return render(
        request, "hosts.html", servers=servers, credentials=credentials
    )


@router.post("", dependencies=[Depends(verify_csrf)])
def add_host(
    request: Request,
    name: str = Form(...),
    connection_type: str = Form(...),
    address: str = Form(""),
    port: str = Form(""),
    ssh_user: str = Form(""),
    credential_id: str = Form(""),
    proxmox_node: str = Form(""),
    proxmox_vmid: str = Form(""),
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    srv = Server(
        name=name.strip(),
        connection_type=connection_type,
        address=address.strip() or None,
        port=int(port) if port.strip().isdigit() else None,
        ssh_user=ssh_user.strip() or None,
        credential_id=int(credential_id) if credential_id.strip().isdigit() else None,
        proxmox_node=proxmox_node.strip() or None,
        proxmox_vmid=proxmox_vmid.strip() or None,
        enabled=True,
    )
    db.add(srv)
    db.add(AuditLog(user_id=user.id, action="host.add", target=srv.name))
    return RedirectResponse("/hosts", status_code=303)


@router.post("/{server_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_host(
    server_id: int,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    srv = db.get(Server, server_id)
    if srv:
        db.add(AuditLog(user_id=user.id, action="host.delete", target=srv.name))
        db.delete(srv)
    return RedirectResponse("/hosts", status_code=303)


@router.post("/{server_id}/toggle", dependencies=[Depends(verify_csrf)])
def toggle_host(
    server_id: int,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    srv = db.get(Server, server_id)
    if srv:
        srv.enabled = not srv.enabled
    return RedirectResponse("/hosts", status_code=303)


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
@router.post("/credentials", dependencies=[Depends(verify_csrf)])
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
    return RedirectResponse("/hosts", status_code=303)


@router.post("/credentials/{cred_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_credential(
    cred_id: int,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    cred = db.get(Credential, cred_id)
    if cred:
        db.add(AuditLog(user_id=user.id, action="credential.delete", target=cred.name))
        db.delete(cred)
    return RedirectResponse("/hosts", status_code=303)
