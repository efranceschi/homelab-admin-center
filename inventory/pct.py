#!/usr/bin/env python3
"""Ansible dynamic inventory for Proxmox LXC containers (PRIMARY path).

Enumerates containers via `pct list`, filters the `running` ones and emits a
JSON inventory where each host uses the `pct` connection plugin (pct
exec/push/pull). Requires no token/API — runs locally as root on the Proxmox node.

Each host receives:
  ansible_connection = pct
  ansible_host       = <VMID>     (used by the connection plugin as the pct target)
  pct_vmid           = <VMID>
  pct_name           = <container hostname>
  pct_ostype         = ubuntu|debian|...

Groups:
  running            : all powered-on containers
  ostype_<type>      : grouped by ostype (ostype_ubuntu, ostype_debian, ...)

Usage:
  ./pct.py --list
  ./pct.py --host <name>   (returns {} — all vars already come from --list)
"""
import json
import subprocess
import sys


def run(cmd):
    return subprocess.run(
        cmd, capture_output=True, text=True, check=True
    ).stdout


def list_containers():
    """Return a list of dicts {vmid, status, name} from `pct list`."""
    out = run(["pct", "list"])
    lines = out.splitlines()
    if not lines:
        return []
    # Header: VMID  Status  Lock  Name  -> column parsing via simple split.
    containers = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        vmid, status = parts[0], parts[1]
        # Name is the last column; Lock is optional in the middle.
        name = parts[-1] if len(parts) >= 3 else vmid
        containers.append({"vmid": vmid, "status": status, "name": name})
    return containers


def ostype_of(vmid):
    """Read the container ostype via `pct config`. Defaults to 'debian' if absent."""
    try:
        out = run(["pct", "config", vmid])
    except subprocess.CalledProcessError:
        return "unknown"
    for line in out.splitlines():
        if line.startswith("ostype:"):
            return line.split(":", 1)[1].strip()
    return "debian"


def build_inventory():
    inv = {
        "_meta": {"hostvars": {}},
        "all": {"children": ["running", "ungrouped"]},
        "running": {"hosts": []},
    }
    for ct in list_containers():
        if ct["status"] != "running":
            continue
        host = ct["name"]
        vmid = ct["vmid"]
        ostype = ostype_of(vmid)
        inv["_meta"]["hostvars"][host] = {
            "ansible_connection": "pct",
            "ansible_host": vmid,
            "pct_vmid": vmid,
            "pct_name": ct["name"],
            "pct_ostype": ostype,
        }
        inv["running"]["hosts"].append(host)
        group = "ostype_{}".format(ostype)
        inv.setdefault(group, {"hosts": []})["hosts"].append(host)
        if group not in inv["all"]["children"]:
            inv["all"]["children"].append(group)
    return inv


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "--list"
    if arg == "--list":
        print(json.dumps(build_inventory(), indent=2))
    elif arg == "--host":
        # All vars come via _meta in --list.
        print(json.dumps({}))
    else:
        sys.stderr.write("usage: pct.py --list | --host <name>\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
