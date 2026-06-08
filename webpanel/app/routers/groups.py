"""Host groups: nested membership of hosts and other groups (Infrastructure)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user, require_admin, verify_csrf
from ..db import db_dependency
from ..groups import (
    descendant_group_ids,
    expand_group_hosts,
    would_create_cycle,
)
from ..models import AuditLog, HostGroup, HostGroupChild, HostGroupMember, Server, User
from ..templating import render

router = APIRouter(prefix="/groups")


@router.get("")
def list_groups(request: Request):
    # The group listing is now folded into the unified Hosts tree; keep this
    # path as a redirect for old bookmarks / Referer-based returns.
    return RedirectResponse("/hosts", status_code=303)


@router.post("", dependencies=[Depends(verify_csrf)])
def add_group(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    name = name.strip()
    if not name:
        return render(request, "error.html", message="Group name is required.")
    if db.scalar(select(HostGroup).where(HostGroup.name == name)):
        return render(request, "error.html", message="A group with that name already exists.")
    g = HostGroup(name=name, description=description.strip() or None)
    db.add(g)
    db.flush()
    db.add(AuditLog(user_id=user.id, action="group.add", target=name))
    return RedirectResponse(f"/groups/{g.id}", status_code=303)


@router.get("/{group_id}")
def edit_group(
    group_id: int,
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    g = db.get(HostGroup, group_id)
    if g is None:
        return RedirectResponse("/hosts", status_code=303)
    servers = db.scalars(select(Server).order_by(Server.name)).all()
    member_ids = {s.id for s in g.servers}
    child_ids = {c.id for c in g.children}
    # Candidate child groups: every other group that wouldn't form a cycle.
    blocked = descendant_group_ids(db, group_id) | {group_id}
    candidates = [
        cg
        for cg in db.scalars(select(HostGroup).order_by(HostGroup.name)).all()
        if cg.id not in blocked
    ]
    effective = expand_group_hosts(db, [group_id])
    eff_servers = [s for s in servers if s.id in effective]
    return render(
        request,
        "group_edit.html",
        g=g,
        servers=servers,
        member_ids=member_ids,
        candidates=candidates,
        child_ids=child_ids,
        eff_servers=eff_servers,
    )


@router.post("/{group_id}", dependencies=[Depends(verify_csrf)])
async def update_group(
    group_id: int,
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    g = db.get(HostGroup, group_id)
    if g is None:
        return RedirectResponse("/hosts", status_code=303)
    form = await request.form()
    name = (form.get("name") or "").strip()
    if name:
        clash = db.scalar(select(HostGroup).where(HostGroup.name == name))
        if clash and clash.id != group_id:
            return render(request, "error.html", message="A group with that name already exists.")
        g.name = name
    g.description = (form.get("description") or "").strip() or None

    # Replace host membership with the submitted set.
    want_hosts = {int(v) for v in form.getlist("servers") if str(v).isdigit()}
    db.query(HostGroupMember).filter(
        HostGroupMember.host_group_id == group_id
    ).delete()
    for sid in want_hosts:
        db.add(HostGroupMember(host_group_id=group_id, server_id=sid))

    # Replace child-group edges with the submitted set, skipping any that would
    # create a cycle (defensive — the picker already hides those).
    want_children = {int(v) for v in form.getlist("children") if str(v).isdigit()}
    db.query(HostGroupChild).filter(
        HostGroupChild.parent_group_id == group_id
    ).delete()
    db.flush()
    for cid in want_children:
        if cid == group_id or would_create_cycle(db, group_id, cid):
            continue
        db.add(HostGroupChild(parent_group_id=group_id, child_group_id=cid))

    db.add(AuditLog(user_id=user.id, action="group.update", target=g.name))
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@router.post("/{group_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_group(
    group_id: int,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    g = db.get(HostGroup, group_id)
    if g:
        # Remove membership + parent/child edges referencing this group.
        db.query(HostGroupMember).filter(
            HostGroupMember.host_group_id == group_id
        ).delete()
        db.query(HostGroupChild).filter(
            (HostGroupChild.parent_group_id == group_id)
            | (HostGroupChild.child_group_id == group_id)
        ).delete()
        db.add(AuditLog(user_id=user.id, action="group.delete", target=g.name))
        db.delete(g)
    return RedirectResponse("/hosts", status_code=303)
