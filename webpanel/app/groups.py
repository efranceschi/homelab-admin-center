"""Host-group graph helpers: recursive expansion and cycle prevention.

Groups may contain hosts (HostGroupMember) and other groups (HostGroupChild).
The graph is a DAG; these helpers walk it loop-safely (a `visited` set guards
against any accidental cycle that slipped past validation).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import HostGroupChild, HostGroupMember


def _child_edges(db: Session) -> dict[int, set[int]]:
    """parent_group_id -> set(child_group_id) for the whole graph."""
    edges: dict[int, set[int]] = {}
    for parent, child in db.execute(
        select(HostGroupChild.parent_group_id, HostGroupChild.child_group_id)
    ):
        edges.setdefault(parent, set()).add(child)
    return edges


def reachable_group_ids(db: Session, group_ids: list[int]) -> set[int]:
    """All groups reachable from ``group_ids`` following child edges (inclusive)."""
    edges = _child_edges(db)
    seen: set[int] = set()
    stack = list(group_ids)
    while stack:
        gid = stack.pop()
        if gid in seen:
            continue
        seen.add(gid)
        stack.extend(edges.get(gid, set()) - seen)
    return seen


def descendant_group_ids(db: Session, group_id: int) -> set[int]:
    """Groups contained transitively under ``group_id`` (excludes itself)."""
    return reachable_group_ids(db, [group_id]) - {group_id}


def ancestor_group_ids(db: Session, group_id: int) -> set[int]:
    """Groups that transitively contain ``group_id`` (excludes itself)."""
    edges = _child_edges(db)
    # Reverse the edges once, then walk up.
    parents: dict[int, set[int]] = {}
    for parent, children in edges.items():
        for child in children:
            parents.setdefault(child, set()).add(parent)
    seen: set[int] = set()
    stack = [group_id]
    while stack:
        gid = stack.pop()
        for p in parents.get(gid, ()):
            if p not in seen:
                seen.add(p)
                stack.append(p)
    return seen


def would_create_cycle(db: Session, parent_id: int, child_id: int) -> bool:
    """True if making ``child_id`` a child of ``parent_id`` introduces a cycle."""
    return child_id == parent_id or parent_id in reachable_group_ids(db, [child_id])


def expand_group_hosts(db: Session, group_ids: list[int]) -> set[int]:
    """Server ids of every host in ``group_ids`` and all nested subgroups."""
    if not group_ids:
        return set()
    all_groups = reachable_group_ids(db, list(group_ids))
    if not all_groups:
        return set()
    rows = db.scalars(
        select(HostGroupMember.server_id).where(
            HostGroupMember.host_group_id.in_(all_groups)
        )
    ).all()
    return set(rows)


def effective_group_ids_for_host(db: Session, server_id: int) -> list[int]:
    """Every group a host belongs to, directly or via ancestors.

    Ordered ancestors-first then direct (tie-break by id) so that, when used as
    plugin-config overlays, a more specific (child) group wins over its parents.
    """
    direct = set(
        db.scalars(
            select(HostGroupMember.host_group_id).where(
                HostGroupMember.server_id == server_id
            )
        ).all()
    )
    effective: set[int] = set(direct)
    for gid in direct:
        effective |= ancestor_group_ids(db, gid)
    # depth = number of ancestors; fewer ancestors (closer to root) overlay first.
    return sorted(effective, key=lambda g: (len(ancestor_group_ids(db, g)), g))
