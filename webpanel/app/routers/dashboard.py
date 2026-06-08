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
    recent = db.scalars(select(Job).order_by(Job.id.desc()).limit(12)).all()
    plugins = db.scalars(select(Plugin).order_by(Plugin.order)).all()

    # Settled host-state distribution across ALL hosts (no state row => unknown).
    cfg = {"ok": 0, "pending": 0, "failed": 0, "unknown": 0}
    last = {"ok": 0, "changed": 0, "failed": 0, "none": 0}
    attention: list[dict] = []
    for s in servers:
        st = states.get(s.id)
        status = (st.config_status if st else None) or "unknown"
        if status not in cfg:
            status = "unknown"
        cfg[status] += 1
        ls = (st.last_status if st else None) or "none"
        last[ls if ls in last else "none"] += 1
        if status == "pending":
            attention.append({"name": s.name, "kind": "pending",
                              "detail": (st.pending_changes if st else 0)})
        elif status == "failed":
            attention.append({"name": s.name, "kind": "failed", "detail": None})
        elif st and st.reboot_required:
            attention.append({"name": s.name, "kind": "reboot", "detail": None})

    total = len(servers)
    jobs_counts = {
        "success": sum(1 for j in recent if j.status == "success"),
        "failed": sum(1 for j in recent if j.status == "failed"),
        "running": sum(1 for j in recent if j.status == "running"),
    }
    counts = {
        "servers": total,
        "enabled": sum(1 for s in servers if s.enabled),
        "plugins_enabled": sum(1 for p in plugins if p.enabled),
        "plugins_total": len(plugins),
        "reboot": sum(1 for st in states.values() if st.reboot_required),
        "failed": cfg["failed"],
        "ok": cfg["ok"],
        "pending": cfg["pending"],
        "unknown": cfg["unknown"],
        "health_pct": round(cfg["ok"] / total * 100) if total else 0,
    }
    config_segments = [
        {"label": "OK", "value": cfg["ok"], "color": "#198754"},
        {"label": "Pending Updates", "value": cfg["pending"], "color": "#ffc107"},
        {"label": "Failed", "value": cfg["failed"], "color": "#dc3545"},
        {"label": "Unknown", "value": cfg["unknown"], "color": "#adb5bd"},
    ]
    status_segments = [
        {"label": "OK", "value": last["ok"], "color": "#198754"},
        {"label": "Changed", "value": last["changed"], "color": "#fd7e14"},
        {"label": "Failed", "value": last["failed"], "color": "#dc3545"},
        {"label": "Never run", "value": last["none"], "color": "#adb5bd"},
    ]
    return render(
        request,
        "dashboard.html",
        servers=servers,
        states=states,
        recent=recent,
        plugins=plugins,
        counts=counts,
        config_segments=config_segments,
        status_segments=status_segments,
        jobs_counts=jobs_counts,
        attention=attention,
    )
