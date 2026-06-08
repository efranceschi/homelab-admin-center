"""Build the unified Hosts forest the panel renders as a collapsible tree.

The forest mixes two kinds of parent:

* **Virtualization hosts** — a host that runs guests (a Proxmox node today; a
  Docker host later) nests its physically-coupled VMs/containers. This is the
  canonical physical inventory: every managed host appears here exactly once,
  guests under their node, everything else at the root.
* **Host groups** — the logical, arbitrarily-nestable groups, rendered as
  overlays below the physical section. A host that is a group member therefore
  appears both under its node and under each group — that duplication is the
  nature of a logical grouping and is intentional.

IMPORTANT: the physical parent/child link (``Server.parent_server_id``) is
presentation-only. It is NEVER expanded into a job's target set (contrast with
groups, which DO expand via :func:`app.groups.expand_group_hosts`). A node's
Check/Apply targets only that node — see ``ansible_layer/service.py``.

Nodes are plain dicts so the Jinja macro (`_host_tree.html`) stays simple.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .groups import effective_group_ids_for_host, expand_group_hosts
from .models import DockerContainer, HostGroup, Server


class _Ctx:
    """Pre-computed lookups shared across the recursive builders (one pass)."""

    def __init__(self, db: Session, states: dict, live: dict, power: dict) -> None:
        self.db = db
        self.states = states or {}
        self.live = live or {}
        # Proxmox power state (running/stopped/…) keyed by VMID, from `pct/qm list`.
        self.power = power or {}
        self.servers = list(db.scalars(select(Server)).all())
        self.children_map: dict[int, list[Server]] = {}
        for s in self.servers:
            if s.parent_server_id is not None:
                self.children_map.setdefault(s.parent_server_id, []).append(s)
        # Display-only Docker containers, grouped by the host that runs them.
        # They are not in `servers` (never a job target), so they are attached
        # purely as leaf nodes in the physical tree (see _server_node).
        self.docker_map: dict[int, list[DockerContainer]] = {}
        for c in db.scalars(select(DockerContainer)).all():
            self.docker_map.setdefault(c.host_server_id, []).append(c)
        # Effective group ids (direct + ancestors) per server, for the dropdown
        # filter — a row matches whichever group it belongs to, anywhere it shows.
        self.eff_groups: dict[int, list[int]] = {
            s.id: effective_group_ids_for_host(db, s.id) for s in self.servers
        }


def _is_guest(s: Server) -> bool:
    return bool(s.guest_type) or s.connection_type == "proxmox"


def _server_node(
    s: Server, depth: int, parent_node_id: str | None, kind: str,
    ctx: _Ctx, *, nest_guests: bool,
) -> dict:
    node_id = (f"{parent_node_id}/" if parent_node_id else "") + f"h{s.id}"
    children: list[dict] = []
    if nest_guests:
        for g in sorted(ctx.children_map.get(s.id, []), key=lambda x: x.name.lower()):
            children.append(_server_node(g, depth + 1, node_id, "guest", ctx, nest_guests=True))
        # Docker containers, grouped by compose project (stack); containers with
        # no project (e.g. `docker run`) render as leaves directly under the host.
        stacks: dict[str, list[DockerContainer]] = {}
        standalone: list[DockerContainer] = []
        for c in ctx.docker_map.get(s.id, []):
            proj = (c.compose_project or "").strip()
            (stacks.setdefault(proj, []) if proj else standalone).append(c)
        for proj in sorted(stacks, key=str.lower):
            children.append(_docker_stack_node(proj, stacks[proj], depth + 1, node_id, s.id))
        for c in sorted(standalone, key=lambda x: (x.name or x.container_id).lower()):
            children.append(_docker_node(c, depth + 1, node_id))
    # A virtualization host shows the expander even before a guest exists; inside
    # a group (nest_guests=False) members render as leaves regardless.
    has_children = bool(children) or (nest_guests and bool(s.virt_kind))
    return {
        "kind": kind,
        "depth": depth,
        "node_id": node_id,
        "parent_id": parent_node_id,
        "has_children": has_children,
        "label": s.name,
        "server": s,
        "state": ctx.states.get(s.id),
        "live": ctx.live.get(s.id),
        "virt_kind": s.virt_kind,
        "guest_type": s.guest_type,
        # Power state (running/stopped) for a controllable guest, by VMID.
        "power": ctx.power.get(s.proxmox_vmid) if s.proxmox_vmid else None,
        "is_guest": s.parent_server_id is not None,
        "synthetic": False,
        "data_search": s.name.lower(),
        "data_groups": " " + " ".join(str(g) for g in ctx.eff_groups.get(s.id, ())) + " ",
        "children": children,
    }


def _docker_stack_node(
    project: str, containers: list[DockerContainer], depth: int, parent_node_id: str,
    host_server_id: int,
) -> dict:
    """A grouping band for a Docker Compose stack (compose project).

    Synthetic and display-only: its container leaves point their ``data-parent``
    at this node. Carries a running/total rollup for an at-a-glance health read.
    """
    node_id = f"{parent_node_id}/stack:{project}"
    ordered = sorted(containers, key=lambda x: (x.name or x.container_id).lower())
    children = [_docker_node(c, depth + 1, node_id) for c in ordered]
    # Search text spans the project and every member so a container-name search
    # still matches the (possibly collapsed) stack and the filter reveals it.
    terms = [project] + [(c.name or "") + " " + (c.image or "") for c in containers]
    return {
        "kind": "stack",
        "depth": depth,
        "node_id": node_id,
        "parent_id": parent_node_id,
        "has_children": True,
        "label": project,
        "server": None,
        "project": project,
        "host_server_id": host_server_id,
        "stack_total": len(containers),
        "stack_running": sum(1 for c in containers if c.state == "running"),
        "virt_kind": None,
        "guest_type": None,
        "is_guest": True,
        "synthetic": True,
        "data_search": " ".join(terms).lower(),
        "data_groups": "  ",
        "children": children,
    }


def _docker_node(c: DockerContainer, depth: int, parent_node_id: str) -> dict:
    """A display-only leaf for one Docker container under its host."""
    return {
        "kind": "docker",
        "depth": depth,
        "node_id": f"{parent_node_id}/d{c.id}",
        "parent_id": parent_node_id,
        "has_children": False,
        "label": c.name or c.container_id[:12],
        "server": None,
        "docker": c,
        "virt_kind": None,
        "guest_type": None,
        "is_guest": True,
        "synthetic": True,
        "data_search": ((c.name or "") + " " + (c.image or "")).lower(),
        "data_groups": "  ",
        "children": [],
    }


def _synthetic_node(node_name: str, guests: list[Server], ctx: _Ctx) -> dict:
    """A label-only band for guests whose Proxmox node isn't a managed host."""
    children = [
        _server_node(g, 1, f"node:{node_name}", "guest", ctx, nest_guests=True)
        for g in sorted(guests, key=lambda x: x.name.lower())
    ]
    return {
        "kind": "vhost",
        "depth": 0,
        "node_id": f"node:{node_name}",
        "parent_id": None,
        "has_children": True,
        "label": node_name or "Unmanaged node",
        "server": None,
        "virt_kind": "proxmox",
        "guest_type": None,
        "is_guest": False,
        "synthetic": True,
        "data_search": (node_name or "").lower(),
        "data_groups": "  ",
        "children": children,
    }


def _group_node(
    g: HostGroup, depth: int, parent_node_id: str | None, ctx: _Ctx, visited: frozenset
) -> dict | None:
    if g.id in visited:  # cycle guard, same discipline as app/groups.py
        return None
    visited = visited | {g.id}
    node_id = (f"{parent_node_id}/" if parent_node_id else "") + f"g{g.id}"
    children: list[dict] = []
    for child in sorted(g.children, key=lambda x: x.name.lower()):
        cn = _group_node(child, depth + 1, node_id, ctx, visited)
        if cn is not None:
            children.append(cn)
    for s in sorted(g.servers, key=lambda x: x.name.lower()):
        children.append(_server_node(s, depth + 1, node_id, "host", ctx, nest_guests=False))
    direct = len(g.servers)
    effective = len(expand_group_hosts(ctx.db, [g.id]))
    return {
        "kind": "group",
        "depth": depth,
        "node_id": node_id,
        "parent_id": parent_node_id,
        "has_children": bool(children),
        "label": g.name,
        "group": g,
        "member_summary": f"{direct} direct · {effective} effective host(s)",
        "synthetic": False,
        "data_search": g.name.lower(),
        "data_groups": "  ",
        "children": children,
    }


def build_host_forest(
    db: Session,
    states: dict | None = None,
    live: dict | None = None,
    power: dict | None = None,
) -> list[dict]:
    """Return the top-level forest: physical hosts first, then group overlays.

    ``power`` maps a Proxmox VMID -> power state (running/stopped) from
    ``pct/qm list``, used to drive guest start/stop/restart buttons in the tree.
    """
    ctx = _Ctx(db, states or {}, live or {}, power or {})
    forest: list[dict] = []

    # --- Physical section: every host with no managed parent ---
    roots = [s for s in ctx.servers if s.parent_server_id is None]
    orphan_guests: dict[str, list[Server]] = {}
    plain_roots: list[Server] = []
    for s in roots:
        if _is_guest(s) and s.proxmox_node:
            # A guest whose node isn't managed as a host — band it by node name.
            orphan_guests.setdefault(s.proxmox_node, []).append(s)
        else:
            plain_roots.append(s)
    for s in sorted(plain_roots, key=lambda x: x.name.lower()):
        has_guests = bool(ctx.children_map.get(s.id))
        kind = "vhost" if (s.virt_kind or has_guests) else "host"
        forest.append(_server_node(s, 0, None, kind, ctx, nest_guests=True))
    for node_name in sorted(orphan_guests):
        forest.append(_synthetic_node(node_name, orphan_guests[node_name], ctx))

    # --- Logical section: root groups (those with no parent group) ---
    groups = db.scalars(select(HostGroup).order_by(HostGroup.name)).all()
    for g in groups:
        if not g.parents:
            node = _group_node(g, 0, None, ctx, frozenset())
            if node is not None:
                forest.append(node)
    return forest
