"""Host discovery scans.

A *discovery* is any change the panel detects about the fleet that may warrant an
action, recorded in the ``discoveries`` table so the panel can offer it for an
explicit decision (confirm / ignore). Two kinds exist today:

* ``new_host``    — a server that exists but isn't registered yet (Proxmox LXC
  containers via ``pct list``; an SSH/TCP sweep can be layered on later).
* ``name_change`` — a managed host whose live hostname no longer matches its
  recorded name. Proxmox container renames are caught from ``pct list``; ssh/local
  hosts surface theirs from the inventory probe (gathered ``ansible_hostname``).

Confirming a ``name_change`` renames the host; confirming a ``new_host`` registers
it. Ignoring records the decision so an identical recurring event is a no-op.
Confirmed/ignored rows are retained as history.

``run_discovery`` opens its own session and is safe to call from both the
scheduler child process and a request worker thread (``asyncio.to_thread``);
SQLAlchemy sessions are not thread-safe, so it never touches a request session.
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import proxmox
from .db import session_scope
from .models import Discovery, HostState, Server, utcnow


def run_discovery() -> dict[str, int]:
    """Scan for unmanaged hosts and reconcile the ``discoveries`` table.

    Returns ``{"found", "new", "pending", "renamed"}`` — total containers seen,
    new_host rows newly inserted, pending discoveries now outstanding, and
    name_change discoveries emitted this pass. A no-op (returns zeros) when
    ``pct`` is absent or returns nothing, so a transient failure never wipes
    existing rows (notably ignored ones).
    """
    stats = {"found": 0, "new": 0, "pending": 0, "renamed": 0}
    # Scan LXC containers (pct) and QEMU VMs (qm) independently — each tagged
    # with its guest_type. A missing binary or an empty result for one source
    # never suppresses the other; an empty result is ambiguous (none vs. failure)
    # so we skip that source rather than risk pruning good rows.
    guests: list[dict[str, str]] = []
    if proxmox.pct_path() is not None:
        guests += [dict(c, guest_type="lxc") for c in proxmox.list_containers()]
    if proxmox.qm_path() is not None:
        guests += [dict(v, guest_type="qemu") for v in proxmox.list_vms()]
    if not guests:
        return stats
    stats["found"] = len(guests)

    with session_scope() as db:
        stats["renamed"] = len(detect_proxmox_name_changes(db, guests))
        servers = db.scalars(select(Server)).all()
        # Any host carrying a vmid is a registered guest (LXC via pct, QEMU via
        # ssh), so dedup on vmid alone — it is globally unique across CT and VM.
        registered_vmids = {s.proxmox_vmid for s in servers if s.proxmox_vmid}
        node = next((s.proxmox_node for s in servers if s.proxmox_node), None)
        existing = {
            d.proxmox_vmid: d
            for d in db.scalars(
                select(Discovery).where(
                    Discovery.kind == "new_host", Discovery.source == "proxmox"
                )
            ).all()
        }

        for c in guests:
            vmid = c.get("vmid")
            if not vmid or vmid in registered_vmids:
                continue
            row = existing.get(vmid)
            if row is None:
                db.add(Discovery(
                    kind="new_host",
                    status="pending",
                    source="proxmox",
                    proxmox_node=node,
                    proxmox_vmid=vmid,
                    guest_type=c.get("guest_type"),
                    name=(c.get("name") or "").strip() or None,
                    status_text=c.get("status"),
                ))
                stats["new"] += 1
            elif row.status == "pending":
                # Refresh a still-pending row; ignored/confirmed rows are left
                # untouched so a decision sticks across scans.
                row.name = (c.get("name") or "").strip() or None
                row.status_text = c.get("status")
                row.guest_type = c.get("guest_type")
                row.last_seen = utcnow()
                if node and not row.proxmox_node:
                    row.proxmox_node = node

        # A pending new_host whose container has since become a managed host
        # (e.g. added via the picker) is now resolved — record it as confirmed
        # history rather than deleting it.
        for vmid, row in existing.items():
            if vmid in registered_vmids and row.status == "pending":
                row.status = "confirmed"
                row.resolved_at = utcnow()

        db.flush()
        stats["pending"] = len(
            db.scalars(
                select(Discovery.id).where(Discovery.status == "pending")
            ).all()
        )
    return stats


def detect_proxmox_name_changes(
    db: Session, guests: list[dict[str, str]]
) -> list[tuple[str, str]]:
    """Surface managed Proxmox guests whose CT/VM was renamed as discoveries.

    Matched by VMID against the node's ``pct list`` + ``qm list`` enumeration, so
    it covers both LXC containers (``connection_type='proxmox'``) and QEMU VMs
    (managed over ``ssh`` but still carrying a ``proxmox_vmid``). The node's guest
    list is the naming authority for any host with a vmid (see
    :func:`record_probe_hostnames`, which defers OS-hostname renames for them).
    Unlike the old auto-sync, this never mutates ``Server.name`` — it emits a
    *pending* ``name_change`` discovery. Returns ``[(old, new)]`` for the new
    pending discoveries created this pass. Caller owns the session/commit.
    """
    if not guests:
        return []
    by_vmid = {c["vmid"]: c for c in guests if c.get("vmid")}
    servers = db.scalars(select(Server)).all()
    by_server = _name_change_rows_by_server(db)
    emitted: list[tuple[str, str]] = []

    for srv in servers:
        if not srv.proxmox_vmid:
            continue
        c = by_vmid.get(srv.proxmox_vmid)
        if c is None:
            continue
        live = (c.get("name") or "").strip()
        if _reconcile_name(db, srv, live, "proxmox", by_server.get(srv.id, [])):
            emitted.append((srv.name, live))
    return emitted


def record_probe_hostnames(
    db: Session, servers: list[Server], hostnames: dict[str, str]
) -> int:
    """Store probed facts and surface ssh/local hostname changes as discoveries.

    ``hostnames`` maps inventory host name (== ``Server.name``) to the live OS
    hostname gathered by the inventory probe (or any check/apply run). The live
    hostname is cached in ``HostState.facts_json`` for future use; for ssh/local
    hosts where it differs from ``Server.name`` a pending ``name_change`` is
    emitted. Proxmox guests get their facts stored but no name_change here — their
    name authority is ``pct list``/``qm list`` (see
    :func:`detect_proxmox_name_changes`). A QEMU VM is managed over ssh but still
    carries a ``proxmox_vmid``; it is deferred to that authority too.
    Returns the count of new pending name_change discoveries created.
    """
    if not hostnames:
        return 0
    by_server = _name_change_rows_by_server(db)
    changed = 0
    for srv in servers:
        live = hostnames.get(srv.name)
        if not live:
            continue
        _store_hostname_fact(db, srv.id, live)
        if srv.connection_type not in ("ssh", "local") or srv.proxmox_vmid:
            continue
        if _reconcile_name(db, srv, live, srv.connection_type, by_server.get(srv.id, [])):
            changed += 1
    return changed


def run_inventory_probe() -> dict[str, int]:
    """Gather each enabled host's live hostname (and facts) and reconcile names.

    Reuses the ansible layer (a read-only ``--check`` facts pass under the run
    flock) so it never overlaps a real job. Returns ``{"probed", "renamed"}``.
    """
    from .ansible_layer import headless

    stats = {"probed": 0, "renamed": 0}
    with session_scope() as db:
        servers = list(
            db.scalars(select(Server).where(Server.enabled.is_(True))).all()
        )
    if not servers:
        return stats
    hostnames = headless.gather_hostnames(servers)
    if not hostnames:
        return stats
    stats["probed"] = len(hostnames)
    with session_scope() as db:
        servers = list(
            db.scalars(select(Server).where(Server.enabled.is_(True))).all()
        )
        stats["renamed"] = record_probe_hostnames(db, servers, hostnames)
    return stats


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _name_change_rows_by_server(db: Session) -> dict[int, list[Discovery]]:
    """All name_change discoveries grouped by ``server_id`` (one query)."""
    out: dict[int, list[Discovery]] = {}
    for r in db.scalars(
        select(Discovery).where(Discovery.kind == "name_change")
    ).all():
        out.setdefault(r.server_id, []).append(r)
    return out


def _reconcile_name(
    db: Session,
    server: Server,
    live: str,
    source: str,
    rows: list[Discovery],
) -> bool:
    """Reconcile one host against its current live hostname.

    Keeps at most one *pending* name_change per host, always reflecting the
    latest live target. Only a transition the user explicitly *ignored* (exact
    ``old -> new``) suppresses re-emission. Returns True iff a brand-new pending
    row was created.
    """
    old = server.name
    if not live or live == old:
        # No discrepancy: a stale pending rename (e.g. the container was renamed
        # back) is obsolete — drop it. Ignored/confirmed history is kept.
        for r in rows:
            if r.status == "pending":
                db.delete(r)
        return False
    # The user explicitly ignored exactly this transition — do nothing.
    for r in rows:
        if r.status == "ignored" and r.old_name == old and r.new_name == live:
            return False
    pending = next((r for r in rows if r.status == "pending"), None)
    if pending is not None:
        # Refresh the single pending row to the current live target.
        pending.old_name = old
        pending.new_name = live
        pending.name = live
        pending.last_seen = utcnow()
        return False
    db.add(Discovery(
        kind="name_change",
        status="pending",
        source=source,
        server_id=server.id,
        old_name=old,
        new_name=live,
        name=live,
    ))
    return True


def _store_hostname_fact(db: Session, server_id: int, hostname: str) -> None:
    """Cache the live OS hostname in ``HostState.facts_json`` for future use."""
    st = db.scalar(select(HostState).where(HostState.server_id == server_id))
    if st is None:
        st = HostState(server_id=server_id)
        db.add(st)
    try:
        facts = json.loads(st.facts_json or "{}")
    except (ValueError, TypeError):
        facts = {}
    facts["hostname"] = hostname
    st.facts_json = json.dumps(facts)
