"""Build a per-job static inventory (JSON) from DB-managed servers.

Connection-type mapping:
  local   -> ansible_connection: local
  ssh     -> ansible_connection: ssh   (+ host/port/user, key written to 0600 file)
  proxmox -> ansible_connection: pct   (reuses plugins/connection/pct.py, target VMID)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .. import crypto
from ..models import Server


def _write_key_file(run_dir: Path, server: Server, secret: str) -> Path:
    key_path = run_dir / f"id_{server.id}"
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, secret.encode("utf-8"))
        if not secret.endswith("\n"):
            os.write(fd, b"\n")
    finally:
        os.close(fd)
    return key_path


def build_inventory(run_dir: Path, servers: list[Server]) -> Path:
    """Write inventory.json for the given servers and return its path."""
    hostvars: dict[str, dict] = {}
    all_hosts: list[str] = []

    for srv in servers:
        host = srv.name
        all_hosts.append(host)
        hv: dict[str, object] = {}

        if srv.connection_type == "local":
            hv["ansible_connection"] = "local"

        elif srv.connection_type == "ssh":
            hv["ansible_connection"] = "ssh"
            hv["ansible_host"] = srv.address or host
            if srv.port:
                hv["ansible_port"] = srv.port
            if srv.ssh_user:
                hv["ansible_user"] = srv.ssh_user
            hv["ansible_ssh_common_args"] = "-o StrictHostKeyChecking=no"
            if srv.credential is not None:
                secret = crypto.get_box().decrypt(srv.credential.secret_ciphertext)
                key_file = _write_key_file(run_dir, srv, secret)
                hv["ansible_ssh_private_key_file"] = str(key_file)

        elif srv.connection_type == "proxmox":
            hv["ansible_connection"] = "pct"
            hv["ansible_host"] = srv.proxmox_vmid or host
            hv["pct_vmid"] = srv.proxmox_vmid
            hv["pct_name"] = host
        else:
            raise ValueError(f"unknown connection_type: {srv.connection_type}")

        hostvars[host] = hv

    inventory = {
        "_meta": {"hostvars": hostvars},
        "all": {"hosts": all_hosts},
    }
    inv_path = run_dir / "inventory.json"
    inv_path.write_text(json.dumps(inventory, indent=2))
    return inv_path
