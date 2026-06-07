"""Local Proxmox helpers.

The panel runs **on** the Proxmox node (the ``pct`` connection plugin shells out
to ``pct exec`` locally), so we can enumerate the node's LXC containers directly
with ``pct list`` and let the user pick a VMID instead of typing it by hand.

Everything here degrades gracefully: off a Proxmox node, without root, or if
``pct`` is missing, the helpers return empty/False rather than raising.
"""
from __future__ import annotations

import os
import shutil
import subprocess


def pct_path() -> str | None:
    """Absolute path to the ``pct`` binary, or None when not present."""
    return shutil.which("pct") or next(
        (p for p in ("/usr/sbin/pct", "/usr/bin/pct") if shutil.which(p)), None
    )


def _pct_argv(pct: str, *args: str) -> list[str]:
    """Build a pct command line, prefixing ``sudo -n`` when not running as root.

    ``pct`` requires root; the panel runs as the unprivileged ``hac`` user and is
    granted the command via /etc/sudoers.d/hac. The CLI/root path is unchanged.
    """
    base = ["sudo", "-n", pct] if os.geteuid() != 0 else [pct]
    return base + list(args)


def list_containers() -> list[dict[str, str]]:
    """Return the node's LXC containers as ``[{vmid, name, status}]``.

    Parsed by the column positions of the ``pct list`` header so an empty
    ``Lock`` column never shifts the fields. Returns ``[]`` on any failure.
    """
    pct = pct_path()
    if not pct:
        return []
    try:
        proc = subprocess.run(
            _pct_argv(pct, "list"), capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    lines = proc.stdout.splitlines()
    header = lines[0]
    cv, cs, cl, cn = (header.find(c) for c in ("VMID", "Status", "Lock", "Name"))
    if cv < 0 or cs < 0 or cn < 0:
        return []
    # When the (optional) Lock column is absent from the header, treat Status as
    # spanning up to Name.
    status_end = cl if cl >= 0 else cn

    out: list[dict[str, str]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        vmid = line[cv:cs].strip()
        status = line[cs:status_end].strip()
        name = line[cn:].strip()
        if vmid:
            out.append({"vmid": vmid, "name": name, "status": status})
    out.sort(key=lambda c: int(c["vmid"]) if c["vmid"].isdigit() else 0)
    return out
