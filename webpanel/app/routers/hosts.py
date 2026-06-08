"""Host (server) and credential management."""
from __future__ import annotations

import asyncio
import socket

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import discovery, proxmox
from ..ansible_layer.service import JobBusyError, start_jobs
from ..auth import current_user, require_admin, verify_csrf
from ..db import db_dependency
from ..jobs import manager
from ..models import (
    AuditLog,
    Credential,
    Discovery,
    HostEvent,
    HostGroup,
    HostInventory,
    HostState,
    Job,
    Plugin,
    Server,
    User,
    utcnow,
)
from ..tree import build_host_forest

DISCOVERY_STATUSES = ("pending", "ignored", "confirmed")
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
    """Per-host transient activity from queued and running jobs.

    Maps server id -> {"activity": "queued"|"checking"|"applying", "job_id": <id>}
    for every host targeted by a pending or currently-running job
    (concurrency-aware), so the hosts table can show what each host is doing and
    link its status to the job. Empty when no job is queued or active.
    """
    out: dict[int, dict] = {}
    # Queued first; a running job for the same host overrides it below.
    for jid, server_ids in manager.queued_jobs():
        for sid in server_ids:
            out[sid] = {"activity": "queued", "job_id": jid}
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
    disc_status: str = "pending",
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    # One pct/qm pass serves both rename detection and the add-host picker below.
    # Renames surface as pending name_change discoveries (not auto-applied).
    containers = proxmox.list_containers()
    vms = proxmox.list_vms()
    guests = [dict(c, guest_type="lxc") for c in containers] + [
        dict(v, guest_type="qemu") for v in vms
    ]
    discovery.detect_proxmox_name_changes(db, guests)
    # Power state (running/stopped) per VMID, to drive the tree's power buttons.
    power_by_vmid = {g["vmid"]: g["status"] for g in guests if g.get("vmid")}
    servers = db.scalars(select(Server).order_by(Server.name)).all()
    credentials = db.scalars(select(Credential).order_by(Credential.name)).all()
    states = {st.server_id: st for st in db.scalars(select(HostState)).all()}
    # Only offer containers that aren't registered yet, so the picker (and the
    # "All" switch) never re-adds an existing host. vmid is unique across CT/VM.
    registered = {s.proxmox_vmid for s in servers if s.proxmox_vmid}
    available = [c for c in containers if c["vmid"] not in registered]
    if disc_status not in DISCOVERY_STATUSES:
        disc_status = "pending"
    discovered = db.scalars(
        select(Discovery)
        .where(Discovery.status == disc_status)
        .order_by(Discovery.last_seen.desc())
    ).all()
    # The tab badge always reflects outstanding (pending) discoveries, regardless
    # of which status the user is currently filtering on.
    pending_count = db.scalar(
        select(func.count()).select_from(Discovery).where(Discovery.status == "pending")
    )
    ood_count = sum(
        1
        for s in servers
        if s.enabled
        and states.get(s.id)
        and states[s.id].config_status == "pending"
    )
    live_status = _live_host_status(db)
    return render(
        request,
        "hosts.html",
        servers=servers,
        tree=build_host_forest(db, states, live_status, power_by_vmid),
        groups=db.scalars(select(HostGroup).order_by(HostGroup.name)).all(),
        credentials=credentials,
        states=states,
        live_status=live_status,
        pct_containers=available,
        pct_available=proxmox.pct_path() is not None,
        discovered=discovered,
        discovered_count=pending_count,
        disc_status=disc_status,
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
            if st and st.config_status in ("ok", "pending", "failed")
            else "unknown"
        )
    return JSONResponse({"busy": bool(live), "cells": cells, "states": drift})


@router.get("/{server_id}")
def host_detail(
    server_id: int,
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    """Per-host detail page: connection, config state, and a history timeline."""
    srv = db.get(Server, server_id)
    if srv is None:
        return RedirectResponse("/hosts", status_code=303)
    state = db.scalar(select(HostState).where(HostState.server_id == server_id))
    events = db.scalars(
        select(HostEvent)
        .where(HostEvent.server_id == server_id)
        .order_by(HostEvent.created_at.desc())
        .limit(200)
    ).all()
    inventory = {
        r.key: r
        for r in db.scalars(
            select(HostInventory).where(HostInventory.server_id == server_id)
        ).all()
    }
    return render(
        request,
        "host_detail.html",
        server=srv,
        state=state,
        events=events,
        inventory=inventory,
    )


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
        await start_jobs(
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
        .where(Server.enabled.is_(True), HostState.config_status == "pending")
        .order_by(Server.name)
    ).all()
    server_ids = [s.id for s in rows]
    plugin_ids = _all_enabled_plugin_keys(db)
    if not server_ids or not plugin_ids:
        return RedirectResponse("/hosts", status_code=303)
    try:
        await start_jobs(
            db, user_id=user.id, server_ids=server_ids, plugin_ids=plugin_ids, mode="apply"
        )
    except (JobBusyError, ValueError) as exc:
        return render(request, "error.html", message=str(exc))
    db.add(AuditLog(user_id=user.id, action="host.apply_all", target="pending"))
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
        await start_jobs(
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
        await start_jobs(
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
    proxmox_vmid: str = Form(""),
    proxmox_node: str = Form(""),
    guest_type: str = Form(""),
    from_discovery: str = Form(""),
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
    # A QEMU VM is registered over ssh but still nests under its node: carry the
    # vmid/node link and parent it. This is the completion of a VM discovery — the
    # admin supplies the SSH address/credential the node enumeration can't.
    vmid = proxmox_vmid.strip()
    srv = Server(
        name=name.strip(),
        connection_type=connection_type,
        address=address.strip() or None,
        port=int(port) if port.strip().isdigit() else None,
        ssh_user=ssh_user.strip() or None,
        credential_id=int(credential_id) if credential_id.strip().isdigit() else None,
        proxmox_vmid=vmid or None,
        proxmox_node=(proxmox_node.strip() or socket.gethostname()) if vmid else None,
        guest_type=(guest_type.strip() or "qemu") if vmid else None,
        parent_server_id=_node_server_id(db, proxmox_node.strip() or None) if vmid else None,
        enabled=True,
    )
    db.add(srv)
    if vmid:
        _mark_virt_host(db, srv.parent_server_id)
        _confirm_discovery_for_vmid(db, vmid, from_discovery)
    db.add(AuditLog(user_id=user.id, action="host.add", target=srv.name))
    return RedirectResponse("/hosts", status_code=303)


def _confirm_discovery_for_vmid(db: Session, vmid: str, disc_id: str) -> None:
    """Mark the pending new_host discovery for ``vmid`` confirmed once its guest
    is registered (the VM-via-ssh completion path). Resolves by the explicit
    discovery id when given, else by vmid."""
    disc = None
    if disc_id.strip().isdigit():
        disc = db.get(Discovery, int(disc_id))
    if disc is None:
        disc = db.scalar(
            select(Discovery).where(
                Discovery.kind == "new_host",
                Discovery.proxmox_vmid == vmid,
                Discovery.status == "pending",
            )
        )
    if disc is not None and disc.status == "pending":
        disc.status = "confirmed"
        disc.resolved_at = utcnow()


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
    taken_vmids = {s.proxmox_vmid for s in existing if s.proxmox_vmid}
    parent_id = _node_server_id(db)

    for c in selected:
        _create_guest_server(
            db, user, c["vmid"], c.get("name"), taken_names, taken_vmids,
            guest_type="lxc", parent_id=parent_id,
        )

    return RedirectResponse("/hosts", status_code=303)


def _node_server_id(db: Session, node_name: str | None = None) -> int | None:
    """Id of the managed host that represents the Proxmox node guests run on.

    Prefers an explicit virtualization host (``virt_kind`` set), matched by name
    when ``node_name`` is given (multi-node ready); otherwise the single
    ``local`` host (the box the panel runs on). Returns None when the node isn't
    managed as a host — guests then render at the tree's top level.
    """
    if node_name:
        srv = db.scalar(
            select(Server).where(
                Server.name == node_name, Server.virt_kind.is_not(None)
            )
        )
        if srv is not None:
            return srv.id
    srv = db.scalar(
        select(Server).where(Server.virt_kind.is_not(None)).order_by(Server.id)
    )
    if srv is None:
        srv = db.scalar(
            select(Server).where(Server.connection_type == "local").order_by(Server.id)
        )
    return srv.id if srv is not None else None


def _mark_virt_host(db: Session, server_id: int | None) -> None:
    """Flag a host as a Proxmox virtualization host once it gains a guest.

    Idempotent — only sets ``virt_kind`` when still NULL, so the tree shows the
    expander/badge even before the node's first facts run populates it.
    """
    if server_id is None:
        return
    node = db.get(Server, server_id)
    if node is not None and node.virt_kind is None:
        node.virt_kind = "proxmox"


def _create_guest_server(
    db: Session,
    user: User,
    vmid: str,
    name: str | None,
    taken_names: set[str],
    taken_vmids: set[str],
    *,
    guest_type: str = "lxc",
    parent_id: int | None = None,
) -> Server | None:
    """Register one Proxmox guest as a host, naming it after the guest.

    LXC containers (``guest_type='lxc'``) are reached locally via ``pct exec`` —
    ``connection_type='proxmox'``, enabled. QEMU VMs (``guest_type='qemu'``) have
    no ``pct exec``; they are registered over ``ssh`` but kept **disabled** until
    SSH connection details are supplied (the primary VM flow is the prefilled
    Add-host modal, which sets the address up front). Both carry the
    ``proxmox_vmid`` and a ``parent_server_id`` link to their node. Falls back to
    ``ct{vmid}``/``vm{vmid}`` and a ``-{vmid}`` suffix on name collisions; skips
    (returns None) when ``vmid`` is missing or already registered. Mutates
    ``taken_names``/``taken_vmids`` so a batch caller stays collision-free.
    """
    if not vmid or vmid in taken_vmids:
        return None
    prefix = "vm" if guest_type == "qemu" else "ct"
    base = (name or "").strip() or f"{prefix}{vmid}"
    hostname = base if base not in taken_names else f"{base}-{vmid}"
    srv = Server(
        name=hostname,
        connection_type="ssh" if guest_type == "qemu" else "proxmox",
        proxmox_node=socket.gethostname(),
        proxmox_vmid=vmid,
        guest_type=guest_type,
        parent_server_id=parent_id,
        enabled=guest_type != "qemu",
    )
    db.add(srv)
    _mark_virt_host(db, parent_id)
    db.add(AuditLog(user_id=user.id, action="host.add", target=hostname))
    taken_names.add(hostname)
    taken_vmids.add(vmid)
    return srv


@router.post("/{server_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_host(
    server_id: int,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    srv = db.get(Server, server_id)
    if srv:
        # Detach guests so they outlive their node (still linked by proxmox_node
        # string, they resurface at the tree's top level). The ON DELETE SET NULL
        # is declared on the model for fresh DBs, but SQLite cannot add the FK
        # action via ALTER on an upgraded DB — so enforce it here too.
        for child in db.scalars(
            select(Server).where(Server.parent_server_id == server_id)
        ).all():
            child.parent_server_id = None
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
# Discovery
# --------------------------------------------------------------------------- #
@router.post("/discover", dependencies=[Depends(verify_csrf)])
async def discover_hosts(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    """Run a discovery scan now (also runs every 24h via the scheduler).

    Off-loaded to a thread because ``pct list`` shells out and can block for a
    few seconds; ``run_discovery`` manages its own session."""
    stats = await asyncio.to_thread(discovery.run_discovery)
    db.add(AuditLog(
        user_id=user.id, action="host.discover", target=f"new={stats['new']}"
    ))
    return _stay(request)


@router.post("/discovered/{disc_id}/confirm", dependencies=[Depends(verify_csrf)])
def confirm_discovery(
    disc_id: int,
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    """Apply a pending discovery: register a new host, or rename an existing one."""
    disc = db.get(Discovery, disc_id)
    if disc is None or disc.status != "pending":
        return _stay(request)
    if disc.kind == "new_host":
        existing = db.scalars(select(Server)).all()
        taken_names = {s.name for s in existing}
        taken_vmids = {s.proxmox_vmid for s in existing if s.proxmox_vmid}
        _create_guest_server(
            db, user, disc.proxmox_vmid, disc.name, taken_names, taken_vmids,
            guest_type=disc.guest_type or "lxc",
            parent_id=_node_server_id(db, disc.proxmox_node),
        )
    elif disc.kind == "name_change":
        _apply_name_change(db, user, disc)
    disc.status = "confirmed"
    disc.resolved_at = utcnow()
    db.add(AuditLog(
        user_id=user.id, action="discovery.confirm", target=f"{disc.kind}:{disc.id}"
    ))
    return _stay(request)


@router.post("/discovered/{disc_id}/ignore", dependencies=[Depends(verify_csrf)])
def ignore_discovery(
    disc_id: int,
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    """Record an ignore decision so this discovery stops surfacing (kept as history)."""
    disc = db.get(Discovery, disc_id)
    if disc and disc.status == "pending":
        disc.status = "ignored"
        disc.resolved_at = utcnow()
        db.add(AuditLog(
            user_id=user.id, action="discovery.ignore", target=f"{disc.kind}:{disc.id}"
        ))
    return _stay(request)


def _apply_name_change(db: Session, user: User, disc: Discovery) -> None:
    """Rename the host a name_change discovery points at to its new name.

    Resolves a unique-name collision the same way new hosts do (a ``-{vmid}`` or
    ``-{server_id}`` suffix) and records a ``name_sync`` event on the host timeline.
    """
    srv = db.get(Server, disc.server_id) if disc.server_id else None
    if srv is None:
        return
    new = (disc.new_name or "").strip() or srv.name
    taken = {s.name for s in db.scalars(select(Server)).all() if s.id != srv.id}
    suffix = srv.proxmox_vmid or srv.id
    candidate = new if new not in taken else f"{new}-{suffix}"
    old = srv.name
    if candidate == old:
        return
    srv.name = candidate
    db.add(HostEvent(
        server_id=srv.id,
        kind="name_sync",
        status=None,
        message=f"Host renamed: {old} → {candidate}",
    ))
