"""Unit tests for the Ansible integration layer: inventory, results, command, env."""

from __future__ import annotations

import json

from app.ansible_layer import inventory_builder, results, runner
from app.models import Server


def test_build_inventory_maps_connection_types(tmp_path):
    local = Server(name="node", connection_type="local")
    local.id = 1
    pmx = Server(name="ct100", connection_type="proxmox", proxmox_vmid="100")
    pmx.id = 2
    ssh = Server(
        name="remote", connection_type="ssh", address="10.0.0.5", port=2222, ssh_user="ops"
    )
    ssh.id = 3

    inv_path = inventory_builder.build_inventory(
        tmp_path, [local, pmx, ssh], host_vars={2: {"extra_var": "x"}}
    )
    data = json.loads(inv_path.read_text())
    hosts = data["all"]["hosts"]

    assert hosts["node"]["ansible_connection"] == "local"
    assert hosts["node"]["ansible_become"] is True

    assert hosts["ct100"]["ansible_connection"] == "pct"
    assert hosts["ct100"]["ansible_host"] == "100"
    assert hosts["ct100"]["pct_vmid"] == "100"
    assert hosts["ct100"]["extra_var"] == "x"  # host_vars merged

    assert hosts["remote"]["ansible_connection"] == "ssh"
    assert hosts["remote"]["ansible_host"] == "10.0.0.5"
    assert hosts["remote"]["ansible_port"] == 2222
    assert hosts["remote"]["ansible_user"] == "ops"


def test_build_inventory_rejects_unknown_connection(tmp_path):
    bad = Server(name="weird", connection_type="telnet")
    bad.id = 9
    try:
        inventory_builder.build_inventory(tmp_path, [bad])
    except ValueError as exc:
        assert "unknown connection_type" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


RECAP = """
PLAY RECAP *********************************************************************
web01                      : ok=5    changed=2    unreachable=0    failed=0
db01                       : ok=4    changed=0    unreachable=0    failed=1
"""


def test_parse_recap():
    stats = results.parse_recap(RECAP)
    assert stats["web01"] == {"ok": 5, "changed": 2, "failed": 0}
    assert stats["db01"]["failed"] == 1


def test_status_from_stats():
    assert results.status_from_stats(None) is None
    assert results.status_from_stats({"ok": 1, "changed": 0, "failed": 0}) == "ok"
    assert results.status_from_stats({"ok": 1, "changed": 3, "failed": 0}) == "changed"
    assert results.status_from_stats({"ok": 1, "changed": 0, "failed": 2}) == "failed"


def test_derive_config_state():
    # Check mode with pending changes => out of date.
    assert results.derive_config_state("check", {"changed": 3, "failed": 0}, True) == (
        "out_of_date",
        3,
    )
    # Check mode, nothing to change => up to date.
    assert results.derive_config_state("check", {"changed": 0, "failed": 0}, True) == ("updated", 0)
    # Apply mode that succeeded => converged/updated regardless of changes.
    assert results.derive_config_state("apply", {"changed": 4, "failed": 0}, True) == ("updated", 0)
    # Unreachable / failed => unknown.
    assert results.derive_config_state("check", None, False) == ("unknown", 0)
    assert results.derive_config_state("check", {"changed": 1, "failed": 2}, True) == ("unknown", 0)


def test_build_command_flags(tmp_path):
    inv = tmp_path / "inventory.json"
    secret = tmp_path / "secret.yml"
    cmd = runner.build_command(
        inventory_path=inv,
        tags=["timezone", "apt"],
        limit_hosts=["web01", "db01"],
        check=True,
        extra_vars_path=None,
        secret_vars_path=secret,
    )
    assert "--tags" in cmd and "timezone,apt" in cmd
    assert "--limit" in cmd and "web01,db01" in cmd
    assert "--check" in cmd and "--diff" in cmd
    assert f"@{secret}" in cmd
    assert str(inv) in cmd


def test_build_env_sets_ansible_paths():
    env = runner.build_env()
    assert "ANSIBLE_ROLES_PATH" in env
    assert "ANSIBLE_COLLECTIONS_PATH" in env
    assert env["ANSIBLE_HOST_KEY_CHECKING"] == "False"
