"""Dashboard: inventory overview, host state, recent jobs."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import db_dependency
from ..models import HostState, Job, Plugin, Server, User
from ..templating import render

router = APIRouter()


@router.get("/dashboard")
def dashboard(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    servers = db.scalars(select(Server).order_by(Server.name)).all()
    states = {s.server_id: s for s in db.scalars(select(HostState)).all()}
    recent = db.scalars(select(Job).order_by(Job.id.desc()).limit(10)).all()
    plugins = db.scalars(select(Plugin).order_by(Plugin.order)).all()

    counts = {
        "servers": len(servers),
        "plugins_enabled": sum(1 for p in plugins if p.enabled),
        "reboot": sum(1 for st in states.values() if st.reboot_required),
        "failed": sum(1 for st in states.values() if st.last_status == "failed"),
    }
    return render(
        request,
        "dashboard.html",
        servers=servers,
        states=states,
        recent=recent,
        plugins=plugins,
        counts=counts,
    )
