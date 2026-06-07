"""Host (server) and credential management."""
from __future__ import annotations

import json
import socket

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto, proxmox
from ..ansible_layer.service import JobBusyError, start_job
from ..auth import current_user, require_admin, verify_csrf
from ..db import db_dependency
from ..jobs import manager
from ..models import AuditLog, Credential, HostState, Job, Plugin, Server, User
from ..templating import render, templates

router = APIRouter(prefix="/hosts")


def _enabled_plugin_keys(db: Session) -> list[str]:
    """Enabled plugins that support check mode — the surface a drift check covers."""
    rows = db.scalars(
        select(Plugin).where(Plugin.enabled.is_(True)).order_by(Plugin.order)
    ).all()
    return [p.key for p in rows if p.supports_check_mode]


def _all_enabled_plugin_keys(db: Session) -> list[str]:
    """Every enabled plugin — the surface an apply converges to config."""
    rows = db.scalars(
        select(Plugin).where(Plugin.enabled.is_(True)).order_by(Plugin.order)
    ).all()
    return [p.key for p in rows]


def _live_host_status(db: Session) -> dict[int, dict]:
    """Per-host transient activity from running jobs.

    Maps server id -> {"activity": "checking"|"applying", "job_id": <id>} for
    every host targeted by a currently-running job (concurrency-aware), so the
    hosts table can show what each host is doing and link its status to the live
    job. Empty when no job is active.
    """
    out: dict[int, dict] = {}
    for jid in manager.active_job_ids():
        rt = manager.get_runtime(jid)
        if rt is None or rt.done.is_set():
            continue
        job = db.get(Job, jid)
        if job is None or job.status != "running":
            continue
        activity = "applying" if job.mode == "apply" else "checking"
        for x in (job.server_ids or "").split(","):
            if x.strip().isdigit():
                out[int(x)] = {"activity": activity, "job_id": jid}
    return out


def _stay(request: Request, default: str = "/hosts") -> RedirectResponse:
    """Redirect back to the page the action was triggered from (the Referer),
    so starting a check/apply keeps the user on the current screen instead of
    jumping to the job. Falls back to ``default``."""
    return RedirectResponse(request.headers.get("referer") or default, status_code=303)


@router.get("")
def list_hosts(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    servers = db.scalars(select(Server).order_by(Server.name)).all()
    credentials = db.scalars(select(Credential).order_by(Credential.name)).all()
    states = {st.server_id: st for st in db.scalars(select(HostState)).all()}
    # Only offer containers that aren't registered yet, so the picker (and the
    # "All" switch) never re-adds an existing host.
    registered = {
        s.proxmox_vmid for s in servers if s.connection_type == "proxmox" and s.proxmox_vmid
    }
    available = [c for c in proxmox.list_containers() if c["vmid"] not in registered]
    ood_count = sum(
        1
        for s in servers
        if s.enabled
        and states.get(s.id)
        and states[s.id].config_status == "out_of_date"
    )
    return render(
        request,
        "hosts.html",
        servers=servers,
        credentials=credentials,
        states=states,
        live_status=_live_host_status(db),
        pct_containers=available,
        pct_available=proxmox.pct_path() is not None,
        ood_count=ood_count,
    )


@router.get("/states")
def host_states(
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    """Live config-state cells for every host, for the hosts page to poll.

    Returns rendered HTML per host (identical markup to the table) plus the
    settled drift state, so the client can refresh the Config-state column in
    place — showing Checking…/Applying… while a job runs against each host."""
    live = _live_host_status(db)
    states = {st.server_id: st for st in db.scalars(select(HostState)).all()}
    tmpl = templates.get_template("_host_state.html")
    cells: dict[str, str] = {}
    drift: dict[str, str] = {}
    for srv in db.scalars(select(Server)).all():
        st = states.get(srv.id)
        cells[str(srv.id)] = tmpl.render(st=st, live=live.get(srv.id))
        drift[str(srv.id)] = (
            st.config_status
            if st and st.config_status in ("updated", "out_of_date")
            else "unknown"
        )
    return JSONResponse({"busy": bool(live), "cells": cells, "states": drift})


@router.post("/check-all", dependencies=[Depends(verify_csrf)])
async def check_all_hosts(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    """Run a configuration drift check (--check) against all enabled hosts."""
    server_ids = [
        s.id for s in db.scalars(select(Server).where(Server.enabled.is_(True))).all()
    ]
    plugin_ids = _enabled_plugin_keys(db)
    if not server_ids or not plugin_ids:
        return RedirectResponse("/hosts", status_code=303)
    try:
        job = await start_job(
            db, user_id=user.id, server_ids=server_ids, plugin_ids=plugin_ids, mode="check"
        )
    except (JobBusyError, ValueError) as exc:
        return render(request, "error.html", message=str(exc))
    db.add(AuditLog(user_id=user.id, action="host.check_all", target="all"))
    return _stay(request)


@router.post("/apply-all", dependencies=[Depends(verify_csrf)])
async def apply_all_hosts(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    """Apply configuration to every enabled host the last check flagged as out
    of date, converging the whole fleet in a single apply job."""
    # Viewer may run check mode; only admin may apply (matches the run endpoint).
    if user.role != "admin":
        return render(request, "error.html", message="Only admins can apply changes.")
    rows = db.scalars(
        select(Server)
        .join(HostState, HostState.server_id == Server.id)
        .where(Server.enabled.is_(True), HostState.config_status == "out_of_date")
        .order_by(Server.name)
    ).all()
    server_ids = [s.id for s in rows]
    plugin_ids = _all_enabled_plugin_keys(db)
    if not server_ids or not plugin_ids:
        return RedirectResponse("/hosts", status_code=303)
    try:
        job = await start_job(
            db, user_id=user.id, server_ids=server_ids, plugin_ids=plugin_ids, mode="apply"
        )
    except (JobBusyError, ValueError) as exc:
        return render(request, "error.html", message=str(exc))
    db.add(AuditLog(user_id=user.id, action="host.apply_all", target="out_of_date"))
    return _stay(request)


@router.post("/{server_id}/check", dependencies=[Depends(verify_csrf)])
async def check_host(
    server_id: int,
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    """Run a configuration drift check (--check) against a single host."""
    srv = db.get(Server, server_id)
    plugin_ids = _enabled_plugin_keys(db)
    if srv is None or not plugin_ids:
        return RedirectResponse("/hosts", status_code=303)
    try:
        job = await start_job(
            db, user_id=user.id, server_ids=[server_id], plugin_ids=plugin_ids, mode="check"
        )
    except (JobBusyError, ValueError) as exc:
        return render(request, "error.html", message=str(exc))
    db.add(AuditLog(user_id=user.id, action="host.check", target=srv.name))
    return _stay(request)


@router.post("/{server_id}/apply", dependencies=[Depends(verify_csrf)])
async def apply_host(
    server_id: int,
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    """Apply configuration to a single host, converging it back to date."""
    # Viewer may run check mode; only admin may apply (matches the run endpoint).
    if user.role != "admin":
        return render(request, "error.html", message="Only admins can apply changes.")
    srv = db.get(Server, server_id)
    plugin_ids = _all_enabled_plugin_keys(db)
    if srv is None or not plugin_ids:
        return RedirectResponse("/hosts", status_code=303)
    try:
        job = await start_job(
            db, user_id=user.id, server_ids=[server_id], plugin_ids=plugin_ids, mode="apply"
        )
    except (JobBusyError, ValueError) as exc:
        return render(request, "error.html", message=str(exc))
    db.add(AuditLog(user_id=user.id, action="host.apply", target=srv.name))
    return _stay(request)


@router.post("", dependencies=[Depends(verify_csrf)])
def add_host(
    request: Request,
    name: str = Form(""),
    connection_type: str = Form(...),
    address: str = Form(""),
    port: str = Form(""),
    ssh_user: str = Form(""),
    credential_id: str = Form(""),
    proxmox_vmids: list[str] = Form(default=[]),
    proxmox_all: str = Form(""),
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    # Proxmox hosts are reached locally via `pct exec` on this node, so the user
    # picks one or more containers (or "all") and each becomes its own host,
    # named after the container. The node name (this host) is recorded for display.
    if connection_type == "proxmox":
        return _add_proxmox_hosts(db, user, proxmox_vmids, bool(proxmox_all.strip()))

    if not name.strip():
        return RedirectResponse("/hosts", status_code=303)
    srv = Server(
        name=name.strip(),
        connection_type=connection_type,
        address=address.strip() or None,
        port=int(port) if port.strip().isdigit() else None,
        ssh_user=ssh_user.strip() or None,
        credential_id=int(credential_id) if credential_id.strip().isdigit() else None,
        enabled=True,
    )
    db.add(srv)
    db.add(AuditLog(user_id=user.id, action="host.add", target=srv.name))
    return RedirectResponse("/hosts", status_code=303)


def _add_proxmox_hosts(
    db: Session, user: User, vmids: list[str], add_all: bool
) -> RedirectResponse:
    """Create one proxmox host per selected container (or all of them)."""
    containers = proxmox.list_containers()
    by_vmid = {c["vmid"]: c for c in containers}

    if add_all:
        selected = list(containers)
    else:
        seen: set[str] = set()
        selected = []
        for raw in vmids:
            v = raw.strip()
            if v and v not in seen:
                seen.add(v)
                selected.append(by_vmid.get(v, {"vmid": v, "name": ""}))

    existing = db.scalars(select(Server)).all()
    taken_names = {s.name for s in existing}
    taken_vmids = {s.proxmox_vmid for s in existing if s.connection_type == "proxmox"}
    node = socket.gethostname()

    for c in selected:
        vmid = c["vmid"]
        if not vmid or vmid in taken_vmids:
            continue  # already registered — skip
        base = (c.get("name") or "").strip() or f"ct{vmid}"
        hostname = base if base not in taken_names else f"{base}-{vmid}"
        db.add(Server(
            name=hostname,
            connection_type="proxmox",
            proxmox_node=node,
            proxmox_vmid=vmid,
            enabled=True,
        ))
        db.add(AuditLog(user_id=user.id, action="host.add", target=hostname))
        taken_names.add(hostname)
        taken_vmids.add(vmid)

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
