"""LXC / QEMU / Docker power actions (start / stop / restart).

Admin-only lifecycle control surfaced from the Hosts tree. Each action becomes a
power Job that streams its output over SSE (same UX as check/apply); the command
building lives in :mod:`app.power` and the job creation in
:func:`app.ansible_layer.service.start_power_job`.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..ansible_layer.service import JobBusyError, PanelRestarting, start_power_job
from ..auth import require_admin, verify_csrf
from ..db import db_dependency
from ..models import AuditLog, User
from ..power import ACTIONS
from ..templating import render

router = APIRouter(prefix="/power")


async def _launch_power(
    request: Request, db: Session, user: User, *,
    kind: str, target_id, action: str, force: bool,
) -> RedirectResponse:
    """Create + submit a power job, audit it, and land on its live log."""
    if action not in ACTIONS:
        return render(request, "error.html", message=f"Unknown power action: {action!r}")
    try:
        job = await start_power_job(
            db, user_id=user.id, kind=kind, target_id=target_id, action=action, force=force,
        )
    except (JobBusyError, PanelRestarting, ValueError) as exc:
        return render(request, "error.html", message=str(exc))
    db.add(AuditLog(
        user_id=user.id, action=f"power.{action}", target=job.target_ref,
        detail_json=json.dumps({"force": force, "kind": kind}),
    ))
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/server/{server_id}/{action}", dependencies=[Depends(verify_csrf)])
async def power_server(
    server_id: int,
    action: str,
    request: Request,
    force: str = Form(""),
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    """Start/stop/restart one LXC or QEMU guest."""
    return await _launch_power(
        request, db, user, kind="server", target_id=server_id,
        action=action, force=force == "1",
    )


@router.post("/docker/{container_id}/{action}", dependencies=[Depends(verify_csrf)])
async def power_docker(
    container_id: int,
    action: str,
    request: Request,
    force: str = Form(""),
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    """Start/stop/restart one Docker container (DockerContainer primary key)."""
    return await _launch_power(
        request, db, user, kind="docker", target_id=container_id,
        action=action, force=force == "1",
    )


@router.post("/stack/{host_server_id}/{project}/{action}", dependencies=[Depends(verify_csrf)])
async def power_stack(
    host_server_id: int,
    project: str,
    action: str,
    request: Request,
    force: str = Form(""),
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    """Start/stop/restart every container of a Docker Compose stack."""
    return await _launch_power(
        request, db, user, kind="stack", target_id=(host_server_id, project),
        action=action, force=force == "1",
    )


@router.post("/node/{server_id}/{action}", dependencies=[Depends(verify_csrf)])
async def power_node(
    server_id: int,
    action: str,
    request: Request,
    force: str = Form(""),
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    """Start/stop/restart all guests + containers of a node (high blast radius)."""
    return await _launch_power(
        request, db, user, kind="node", target_id=server_id,
        action=action, force=force == "1",
    )
