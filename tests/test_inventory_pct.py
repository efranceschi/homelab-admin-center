"""Tests for the dynamic inventory script inventory/pct.py."""

from __future__ import annotations

PCT_LIST = """VMID       Status     Lock         Name
100        running                 web01
101        stopped                 db01
102        running                 cache01
"""

PCT_CONFIG = {
    "100": "arch: amd64\nostype: ubuntu\nhostname: web01\n",
    "102": "arch: amd64\nostype: debian\nhostname: cache01\n",
}


def _patch_run(monkeypatch, mod):
    def fake_run(cmd):
        if cmd[:2] == ["pct", "list"]:
            return PCT_LIST
        if cmd[:2] == ["pct", "config"]:
            return PCT_CONFIG.get(cmd[2], "ostype: debian\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(mod, "run", fake_run)


def test_list_containers(monkeypatch, inventory_module):
    _patch_run(monkeypatch, inventory_module)
    cts = inventory_module.list_containers()
    assert {"vmid": "100", "status": "running", "name": "web01"} in cts
    assert {"vmid": "101", "status": "stopped", "name": "db01"} in cts


def test_ostype_of(monkeypatch, inventory_module):
    _patch_run(monkeypatch, inventory_module)
    assert inventory_module.ostype_of("100") == "ubuntu"
    assert inventory_module.ostype_of("102") == "debian"


def test_build_inventory_only_running_with_groups(monkeypatch, inventory_module):
    _patch_run(monkeypatch, inventory_module)
    inv = inventory_module.build_inventory()

    # Stopped containers are excluded.
    assert inv["running"]["hosts"] == ["web01", "cache01"]
    assert "db01" not in inv["_meta"]["hostvars"]

    hv = inv["_meta"]["hostvars"]["web01"]
    assert hv["ansible_connection"] == "pct"
    assert hv["ansible_host"] == "100"
    assert hv["pct_vmid"] == "100"
    assert hv["pct_ostype"] == "ubuntu"

    # ostype groups created and registered under "all".
    assert inv["ostype_ubuntu"]["hosts"] == ["web01"]
    assert inv["ostype_debian"]["hosts"] == ["cache01"]
    assert "ostype_ubuntu" in inv["all"]["children"]
    assert "ostype_debian" in inv["all"]["children"]
