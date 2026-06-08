"""Read-only sync of Docker containers into the Hosts tree.

A *docker host* is any managed host that runs a Docker daemon (e.g. the LXC
``komodo``). Its containers are gathered from the playbook's ``PANEL_DOCKER``
probe (``docker ps``, parsed by :func:`app.ansible_layer.results.parse_docker`)
and mirrored into the ``docker_containers`` table for display only — they are
NOT Ansible-managed (no check/apply, credentials, groups, or facts) and never
enter the ``servers`` table, so they can never become a job target.

The sync is idempotent and read-only: rows are upserted by ``container_id`` and
pruned when a container disappears. A host that reports Docker is auto-marked
``virt_kind='docker'`` (unless it already carries another virt kind, e.g. a
Proxmox node that also runs Docker keeps ``'proxmox'`` and still shows its
containers). The mark — and the containers — are cleared only for a host that is
*reachable* yet no longer reports Docker, so a transient unreachability never
wipes the tree.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DockerContainer, Server, utcnow


def _compose_project(labels: str) -> str | None:
    """Extract ``com.docker.compose.project`` from docker's CSV label string."""
    for pair in (labels or "").split(","):
        key, _, value = pair.partition("=")
        if key.strip() == "com.docker.compose.project":
            return value.strip() or None
    return None


def _state(c: dict) -> str:
    """The container state, falling back to deriving it from the status text."""
    state = (c.get("State") or "").strip().lower()
    if state:
        return state
    status = (c.get("Status") or "").strip().lower()
    if status.startswith("up"):
        return "running"
    if status.startswith("exited") or status.startswith("dead"):
        return "exited"
    return status.split()[0] if status else ""


def _apply_fields(row: DockerContainer, c: dict, now) -> None:
    row.name = (c.get("Names") or "").strip()
    row.image = (c.get("Image") or "").strip()
    row.state = _state(c)
    row.status = (c.get("Status") or "").strip()
    row.ports = (c.get("Ports") or "").strip()
    row.compose_project = _compose_project(c.get("Labels", ""))
    row.last_seen = now


def sync_containers(
    db: Session,
    servers: Iterable[Server],
    docker_by_host: dict[str, list[dict]],
    reachable_hosts: Iterable[str],
) -> int:
    """Mirror probed Docker containers into ``docker_containers``.

    ``docker_by_host`` maps ``Server.name`` to its ``docker ps`` rows (a host
    present ran Docker; absent means no Docker). ``reachable_hosts`` is the set
    of hosts whose facts probe succeeded (``PANEL_HOSTNAME``) — only these are
    eligible for the clear path, so an unreachable host keeps its last-known
    containers. Returns the number of containers currently tracked across the
    hosts touched. Caller owns the session/commit.
    """
    by_name = {s.name: s for s in servers}
    reachable = set(reachable_hosts)
    tracked = 0

    for name, containers in docker_by_host.items():
        srv = by_name.get(name)
        if srv is None:
            continue
        if not srv.virt_kind:
            srv.virt_kind = "docker"
        now = utcnow()
        existing = {
            r.container_id: r
            for r in db.scalars(
                select(DockerContainer).where(
                    DockerContainer.host_server_id == srv.id
                )
            ).all()
        }
        seen: set[str] = set()
        for c in containers:
            cid = (c.get("ID") or "").strip()
            if not cid:
                continue
            seen.add(cid)
            row = existing.get(cid)
            if row is None:
                row = DockerContainer(
                    host_server_id=srv.id, container_id=cid,
                    first_seen=now, last_seen=now,
                )
                db.add(row)
            _apply_fields(row, c, now)
        for cid, row in existing.items():
            if cid not in seen:
                db.delete(row)
        tracked += len(seen)

    # A host that is reachable but no longer reports Docker has stopped running
    # it — drop the auto mark and its (now stale) containers. Only 'docker'
    # marks are cleared, so a Proxmox node's 'proxmox' kind is preserved.
    for srv in by_name.values():
        if (
            srv.name in reachable
            and srv.name not in docker_by_host
            and srv.virt_kind == "docker"
        ):
            srv.virt_kind = None
            for row in db.scalars(
                select(DockerContainer).where(
                    DockerContainer.host_server_id == srv.id
                )
            ).all():
                db.delete(row)

    return tracked
