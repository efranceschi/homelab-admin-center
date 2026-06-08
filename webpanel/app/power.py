"""Build start/stop/restart commands for LXC, QEMU and Docker targets.

Pure command construction — no DB writes, no job logic. Each builder returns a
list of argv command lists (a forced restart and bulk actions yield more than
one), so the caller can run them as a single process.

Execution mechanism per target:

* **LXC / QEMU** run locally on the Proxmox node via ``pct`` / ``qm`` (prefixed
  with ``sudo -n`` when the panel is unprivileged). ``shutdown``/``reboot`` are
  graceful; ``stop`` is the hard kill. Neither has a force-reboot, so a forced
  restart is stop-then-start.
* **Docker** runs on the container's host: locally, over SSH (reusing the
  encrypted SSH key, written 0600 into the job run dir), or via ``pct exec`` when
  the docker host is itself an LXC guest. ``docker stop`` is graceful (``-t``
  timeout), ``docker kill`` is force; a forced restart is kill-then-start.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crypto, proxmox
from .ansible_layer.inventory_builder import _write_key_file
from .models import DockerContainer, Server

ACTIONS = ("start", "stop", "restart")

# Graceful-stop timeout handed to `docker stop`/`docker restart` (seconds).
DOCKER_STOP_TIMEOUT = 10


# --- pct / qm (local, on the Proxmox node) ----------------------------------

def _guest_cmds(binary: str, vmid: str, action: str, force: bool) -> list[list[str]]:
    if action == "start":
        return [proxmox._sudo_argv(binary, "start", vmid)]
    if action == "stop":
        return [proxmox._sudo_argv(binary, "stop" if force else "shutdown", vmid)]
    if action == "restart":
        if force:  # no force-reboot — stop then start
            return [
                proxmox._sudo_argv(binary, "stop", vmid),
                proxmox._sudo_argv(binary, "start", vmid),
            ]
        return [proxmox._sudo_argv(binary, "reboot", vmid)]
    raise ValueError(f"unknown action: {action}")


def _server_power_cmds(srv: Server, action: str, force: bool) -> list[list[str]]:
    if srv.guest_type == "lxc":
        binary = proxmox.pct_path()
        if not binary:
            raise ValueError("pct binary not found on this node")
    elif srv.guest_type == "qemu":
        binary = proxmox.qm_path()
        if not binary:
            raise ValueError("qm binary not found on this node")
    else:
        raise ValueError(
            f"{srv.name}: not a controllable guest (guest_type={srv.guest_type!r})"
        )
    if not srv.proxmox_vmid:
        raise ValueError(f"{srv.name}: missing VMID")
    return _guest_cmds(binary, srv.proxmox_vmid, action, force)


# --- docker (runs on the container's host) ----------------------------------

def _docker_cmds(action: str, force: bool, cid: str) -> list[list[str]]:
    t = str(DOCKER_STOP_TIMEOUT)
    if action == "start":
        return [["docker", "start", cid]]
    if action == "stop":
        return [["docker", "kill", cid]] if force else [["docker", "stop", "-t", t, cid]]
    if action == "restart":
        if force:  # no force-restart — kill then start
            return [["docker", "kill", cid], ["docker", "start", cid]]
        return [["docker", "restart", "-t", t, cid]]
    raise ValueError(f"unknown action: {action}")


def _ssh_key_for(host: Server, run_dir: Path) -> Path:
    """0600 key file for `host` in `run_dir`, reused across a bulk action on the
    same host (so writing twice doesn't trip _write_key_file's O_EXCL)."""
    key_path = run_dir / f"id_{host.id}"
    if key_path.exists():
        return key_path
    if host.credential is None:
        raise ValueError(f"{host.name}: no SSH credential configured")
    secret = crypto.get_box().decrypt(host.credential.secret_ciphertext)
    return _write_key_file(run_dir, host, secret)


def _wrap_for_host(host: Server, argv: list[str], run_dir: Path) -> list[str]:
    """Wrap a docker argv so it executes on `host` (local / ssh / pct exec)."""
    ct = host.connection_type
    if ct == "local":
        return proxmox._sudo_argv(*argv)
    if ct == "proxmox":  # docker inside an LXC guest, reached via `pct exec`
        if not host.proxmox_vmid:
            raise ValueError(f"{host.name}: missing VMID for pct exec")
        pct = proxmox.pct_path()
        if not pct:
            raise ValueError("pct binary not found on this node")
        return proxmox._sudo_argv(pct, "exec", host.proxmox_vmid, "--", *argv)
    if ct == "ssh":
        key_file = _ssh_key_for(host, run_dir)
        ssh = [
            "ssh", "-i", str(key_file),
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
        ]
        if host.port:
            ssh += ["-p", str(host.port)]
        target = f"{host.ssh_user}@{host.address}" if host.ssh_user else (host.address or host.name)
        return ssh + [target, "--", *argv]
    raise ValueError(f"{host.name}: unsupported connection type {ct!r}")


def _docker_host_cmds(
    host: Server, containers: list[DockerContainer], action: str, force: bool, run_dir: Path
) -> list[list[str]]:
    cmds: list[list[str]] = []
    for c in containers:
        for argv in _docker_cmds(action, force, c.container_id):
            cmds.append(_wrap_for_host(host, argv, run_dir))
    return cmds


# --- entry point ------------------------------------------------------------

def build_power_commands(
    db: Session, *, kind: str, target_id, action: str, force: bool, run_dir: Path,
) -> tuple[list[list[str]], str, list[int]]:
    """Return ``(cmds, label, affected_server_ids)`` for a power action.

    ``kind``:
      * ``"server"`` — one LXC/QEMU guest (``target_id`` = Server id).
      * ``"docker"`` — one container (``target_id`` = DockerContainer id).
      * ``"stack"``  — a whole compose stack (``target_id`` = ``(host_id, project)``).
      * ``"node"``   — every guest + container of a node (``target_id`` = Server id).

    Raises ``ValueError`` on a bad action, missing binary/credential/VMID, or an
    empty target set.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")

    if kind == "server":
        srv = db.get(Server, int(target_id))
        if srv is None:
            raise ValueError("guest not found")
        return _server_power_cmds(srv, action, force), srv.name, [srv.id]

    if kind == "docker":
        c = db.get(DockerContainer, int(target_id))
        if c is None:
            raise ValueError("container not found")
        host = db.get(Server, c.host_server_id)
        if host is None:
            raise ValueError("docker host not found")
        cmds = _docker_host_cmds(host, [c], action, force, run_dir)
        return cmds, c.name or c.container_id[:12], [host.id]

    if kind == "stack":
        host_id, project = target_id
        host = db.get(Server, int(host_id))
        if host is None:
            raise ValueError("docker host not found")
        members = db.scalars(
            select(DockerContainer)
            .where(
                DockerContainer.host_server_id == int(host_id),
                DockerContainer.compose_project == project,
            )
            .order_by(DockerContainer.name)
        ).all()
        if not members:
            raise ValueError(f"stack {project!r}: no containers")
        cmds = _docker_host_cmds(host, members, action, force, run_dir)
        return cmds, f"stack {project}", [host.id]

    if kind == "node":
        node = db.get(Server, int(target_id))
        if node is None:
            raise ValueError("node not found")
        cmds: list[list[str]] = []
        affected: set[int] = set()
        guests = db.scalars(
            select(Server)
            .where(Server.parent_server_id == node.id, Server.guest_type.isnot(None))
            .order_by(Server.name)
        ).all()
        for g in guests:
            cmds.extend(_server_power_cmds(g, action, force))
            affected.add(g.id)
        containers = db.scalars(
            select(DockerContainer)
            .where(DockerContainer.host_server_id == node.id)
            .order_by(DockerContainer.name)
        ).all()
        if containers:
            cmds.extend(_docker_host_cmds(node, list(containers), action, force, run_dir))
            affected.add(node.id)
        if not cmds:
            raise ValueError(f"{node.name}: no guests or containers to control")
        return cmds, f"all on {node.name}", sorted(affected)

    raise ValueError(f"unknown power target kind: {kind!r}")
