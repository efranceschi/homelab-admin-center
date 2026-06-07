"""Tests for the Proxmox pct connection plugin (plugins/connection/pct.py)."""

from __future__ import annotations

import pytest
from ansible.errors import AnsibleError, AnsibleFileNotFound


def _make_conn(mod, options=None):
    """Build a Connection bypassing ConnectionBase.__init__ for unit testing."""
    conn = mod.Connection.__new__(mod.Connection)
    conn._connected = True
    conn._vmid = "100"
    opts = {"pct_cmd": "pct", "executable": "/bin/sh", "remote_addr": "100"}
    opts.update(options or {})
    conn.get_option = lambda name: opts[name]
    return conn


class FakePopen:
    """Records argv and returns canned (rc, stdout, stderr)."""

    last_args = None

    def __init__(self, args, **kwargs):
        FakePopen.last_args = args
        self.args = args
        self.returncode = 0

    def communicate(self, in_data=None):
        return (b"stdout-data", b"stderr-data")


def test_pct_base_root_no_sudo(monkeypatch, pct_connection_module):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/sbin/pct")
    monkeypatch.setattr("os.geteuid", lambda: 0)
    conn = _make_conn(pct_connection_module)
    assert conn._pct_base() == ["/usr/sbin/pct"]


def test_pct_base_unprivileged_uses_sudo(monkeypatch, pct_connection_module):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/sbin/pct")
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    conn = _make_conn(pct_connection_module)
    assert conn._pct_base() == ["sudo", "-n", "/usr/sbin/pct"]


def test_exec_command_builds_pct_exec_argv(monkeypatch, pct_connection_module):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/sbin/pct")
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr(pct_connection_module.subprocess, "Popen", FakePopen)

    conn = _make_conn(pct_connection_module)
    rc, out, err = conn.exec_command("echo hi")

    assert FakePopen.last_args == [
        "/usr/sbin/pct",
        "exec",
        "100",
        "--",
        "/bin/sh",
        "-c",
        "echo hi",
    ]
    assert rc == 0
    assert out == b"stdout-data"
    assert err == b"stderr-data"


def test_put_file_missing_source_raises(monkeypatch, pct_connection_module):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/sbin/pct")
    monkeypatch.setattr("os.geteuid", lambda: 0)
    conn = _make_conn(pct_connection_module)
    with pytest.raises(AnsibleFileNotFound):
        conn.put_file("/does/not/exist/anywhere", "/tmp/dest")


def test_put_file_propagates_transfer_failure(monkeypatch, tmp_path, pct_connection_module):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/sbin/pct")
    monkeypatch.setattr("os.geteuid", lambda: 0)

    class FailingPopen(FakePopen):
        def __init__(self, args, **kwargs):
            super().__init__(args, **kwargs)
            self.returncode = 1

    monkeypatch.setattr(pct_connection_module.subprocess, "Popen", FailingPopen)

    src = tmp_path / "payload"
    src.write_text("data")
    conn = _make_conn(pct_connection_module)
    with pytest.raises(AnsibleError):
        conn.put_file(str(src), "/tmp/dest")
