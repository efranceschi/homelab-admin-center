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


def qm_path() -> str | None:
    """Absolute path to the ``qm`` binary (QEMU/KVM guests), or None."""
    return shutil.which("qm") or next(
        (p for p in ("/usr/sbin/qm", "/usr/bin/qm") if shutil.which(p)), None
    )


def _sudo_argv(binary: str, *args: str) -> list[str]:
    """Build a command line, prefixing ``sudo -n`` when not running as root.

    ``pct``/``qm`` require root; the panel runs as the unprivileged ``hac`` user
    and is granted the commands via /etc/sudoers.d/hac. The CLI/root path is
    unchanged.
    """
    base = ["sudo", "-n", binary] if os.geteuid() != 0 else [binary]
    return base + list(args)


def _pct_argv(pct: str, *args: str) -> list[str]:
    """Backwards-compatible alias for :func:`_sudo_argv` (pct callers)."""
    return _sudo_argv(pct, *args)


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


def list_vms() -> list[dict[str, str]]:
    """Return the node's QEMU/KVM VMs as ``[{vmid, name, status}]``.

    Parsed by the column positions of the ``qm list`` header (``VMID NAME STATUS
    MEM(MB) BOOTDISK(GB) PID``). Note the field order differs from ``pct list``
    (Name precedes Status). Degrades gracefully — returns ``[]`` on any failure,
    so a node without ``qm`` (or without KVM) simply yields no VMs.
    """
    qm = qm_path()
    if not qm:
        return []
    try:
        proc = subprocess.run(
            _sudo_argv(qm, "list"), capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    lines = proc.stdout.splitlines()
    header = lines[0]
    cv, cn, cs, cm = (header.find(c) for c in ("VMID", "NAME", "STATUS", "MEM"))
    if cv < 0 or cn < 0 or cs < 0:
        return []
    # Status spans up to the MEM(MB) column when present, else to end of line.
    status_end = cm if cm >= 0 else len(header) + 999

    out: list[dict[str, str]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        vmid = line[cv:cn].strip()
        name = line[cn:cs].strip()
        status = line[cs:status_end].strip()
        if vmid:
            out.append({"vmid": vmid, "name": name, "status": status})
    out.sort(key=lambda c: int(c["vmid"]) if c["vmid"].isdigit() else 0)
    return out
