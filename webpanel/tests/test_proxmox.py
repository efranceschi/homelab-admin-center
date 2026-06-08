"""Unit tests for the local Proxmox `pct list` parsing helpers."""

from __future__ import annotations

import subprocess

from app import proxmox


def _col(s: str, n: int) -> str:
    return s.ljust(n)


# Header + rows built with consistent column widths so str.find() alignment
# (the parser keys off header column positions) holds.
_HEADER = _col("VMID", 11) + _col("Status", 11) + _col("Lock", 12) + "Name"
_ROW1 = _col("100", 11) + _col("running", 11) + _col("", 12) + "web01"
_ROW2 = _col("101", 11) + _col("stopped", 11) + _col("", 12) + "db01"
_SAMPLE = "\n".join([_HEADER, _ROW1, _ROW2]) + "\n"


def test_list_containers_parses_pct_output(monkeypatch):
    monkeypatch.setattr(proxmox, "pct_path", lambda: "/usr/sbin/pct")

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=_SAMPLE, stderr="")

    monkeypatch.setattr(proxmox.subprocess, "run", fake_run)

    out = proxmox.list_containers()
    assert out == [
        {"vmid": "100", "name": "web01", "status": "running"},
        {"vmid": "101", "name": "db01", "status": "stopped"},
    ]


def test_list_containers_empty_when_pct_absent(monkeypatch):
    monkeypatch.setattr(proxmox, "pct_path", lambda: None)
    assert proxmox.list_containers() == []


def test_list_containers_handles_subprocess_error(monkeypatch):
    monkeypatch.setattr(proxmox, "pct_path", lambda: "/usr/sbin/pct")

    def boom(argv, **kwargs):
        raise OSError("pct not executable")

    monkeypatch.setattr(proxmox.subprocess, "run", boom)
    assert proxmox.list_containers() == []


def test_list_containers_nonzero_rc(monkeypatch):
    monkeypatch.setattr(proxmox, "pct_path", lambda: "/usr/sbin/pct")
    monkeypatch.setattr(
        proxmox.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="denied"),
    )
    assert proxmox.list_containers() == []


# `qm list` has a different column order/case than `pct list`: VMID NAME STATUS …
_VM_HEADER = _col("VMID", 11) + _col("NAME", 21) + _col("STATUS", 11) + "MEM(MB)"
_VM_ROW1 = _col("200", 11) + _col("web-vm", 21) + _col("running", 11) + "2048"
_VM_ROW2 = _col("201", 11) + _col("db-vm", 21) + _col("stopped", 11) + "4096"
_VM_SAMPLE = "\n".join([_VM_HEADER, _VM_ROW1, _VM_ROW2]) + "\n"


def test_list_vms_parses_qm_output(monkeypatch):
    monkeypatch.setattr(proxmox, "qm_path", lambda: "/usr/sbin/qm")
    monkeypatch.setattr(
        proxmox.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=_VM_SAMPLE, stderr=""),
    )
    assert proxmox.list_vms() == [
        {"vmid": "200", "name": "web-vm", "status": "running"},
        {"vmid": "201", "name": "db-vm", "status": "stopped"},
    ]


def test_list_vms_empty_when_qm_absent(monkeypatch):
    monkeypatch.setattr(proxmox, "qm_path", lambda: None)
    assert proxmox.list_vms() == []


def test_list_vms_handles_subprocess_error(monkeypatch):
    monkeypatch.setattr(proxmox, "qm_path", lambda: "/usr/sbin/qm")

    def boom(argv, **kwargs):
        raise OSError("qm not executable")

    monkeypatch.setattr(proxmox.subprocess, "run", boom)
    assert proxmox.list_vms() == []
