"""Host discovery scans.

A scan looks for servers that exist but aren't registered as managed hosts yet,
recording each in the ``discovered_hosts`` table so the panel can offer them for
explicit confirmation. Today only Proxmox LXC containers (via ``pct list``) are
discovered; an SSH/TCP sweep can be layered on later.

``run_discovery`` opens its own session and is safe to call from both the
scheduler child process and a request worker thread (``asyncio.to_thread``);
SQLAlchemy sessions are not thread-safe, so it never touches a request session.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import proxmox
from .db import session_scope
from .models import DiscoveredHost, HostEvent, Server, utcnow


def run_discovery() -> dict[str, int]:
    """Scan for unmanaged hosts and reconcile the ``discovered_hosts`` table.

    Returns ``{"found", "new", "pending"}`` — total containers seen, rows newly
    inserted, and non-dismissed rows now outstanding. A no-op (returns zeros)
    when ``pct`` is absent or returns nothing, so a transient failure never
    wipes existing rows (notably dismissals).
    """
    stats = {"found": 0, "new": 0, "pending": 0, "renamed": 0}
    if proxmox.pct_path() is None:
        return stats
    containers = proxmox.list_containers()
    # An empty result is ambiguous (no containers vs. pct failure); skip rather
    # than risk pruning good rows. With no containers there is nothing to add.
    if not containers:
        return stats
    stats["found"] = len(containers)

    with session_scope() as db:
        stats["renamed"] = len(reconcile_proxmox_names(db, containers))
        servers = db.scalars(select(Server)).all()
        registered_vmids = {
            s.proxmox_vmid
            for s in servers
            if s.connection_type == "proxmox" and s.proxmox_vmid
        }
        node = next(
            (s.proxmox_node for s in servers if s.proxmox_node), None
        )
        existing = {
            d.proxmox_vmid: d
            for d in db.scalars(
                select(DiscoveredHost).where(DiscoveredHost.source == "proxmox")
            ).all()
        }

        for c in containers:
            vmid = c.get("vmid")
            if not vmid or vmid in registered_vmids:
                continue
            row = existing.get(vmid)
            if row is None:
                db.add(DiscoveredHost(
                    source="proxmox",
                    proxmox_node=node,
                    proxmox_vmid=vmid,
                    name=(c.get("name") or "").strip() or None,
                    status=c.get("status"),
                ))
                stats["new"] += 1
            else:
                row.name = (c.get("name") or "").strip() or None
                row.status = c.get("status")
                row.last_seen = utcnow()
                if node and not row.proxmox_node:
                    row.proxmox_node = node

        # Prune only rows that have since become managed hosts. Rows merely
        # absent from this scan are left alone (the empty-result guard above
        # already protects against a failed pct call deleting everything).
        for vmid, row in existing.items():
            if vmid in registered_vmids:
                db.delete(row)

        db.flush()
        stats["pending"] = len(
            db.scalars(
                select(DiscoveredHost.id).where(DiscoveredHost.dismissed.is_(False))
            ).all()
        )
    return stats


def reconcile_proxmox_names(
    db: Session, containers: list[dict[str, str]]
) -> list[tuple[str, str]]:
    """Auto-sync managed Proxmox host names to their live container names.

    A host name always follows the container it maps to (matched by VMID); when
    the container is renamed in Proxmox the host is renamed to match, with a
    ``-{vmid}`` suffix on a name collision. Each rename is recorded as a
    ``name_sync`` :class:`HostEvent`. Returns ``[(old, new)]`` for the renames
    applied. Caller owns the session/commit, so this is safe with either the
    request session or the scheduler's. No-op on an empty container list.
    """
    if not containers:
        return []
    by_vmid = {c["vmid"]: c for c in containers if c.get("vmid")}
    servers = db.scalars(select(Server)).all()
    taken = {s.name for s in servers}
    renamed: list[tuple[str, str]] = []

    for srv in servers:
        if srv.connection_type != "proxmox" or not srv.proxmox_vmid:
            continue
        c = by_vmid.get(srv.proxmox_vmid)
        if c is None:
            continue
        live = (c.get("name") or "").strip()
        if not live or live == srv.name:
            continue
        candidate = live if live not in taken else f"{live}-{srv.proxmox_vmid}"
        if candidate == srv.name:
            continue
        old = srv.name
        taken.discard(old)
        srv.name = candidate
        taken.add(candidate)
        db.add(HostEvent(
            server_id=srv.id,
            kind="name_sync",
            status=None,
            message=f"Proxmox container renamed: {old} → {candidate}",
        ))
        renamed.append((old, candidate))
    return renamed
