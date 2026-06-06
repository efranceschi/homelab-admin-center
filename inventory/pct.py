#!/usr/bin/env python3
"""Inventário dinâmico Ansible para containers LXC do Proxmox (caminho PRIMÁRIO).

Enumera os containers via `pct list`, filtra os que estão `running` e emite um
inventário JSON onde cada host usa o connection plugin `pct` (pct exec/push/pull).
Não requer token/API — roda localmente como root no nó Proxmox.

Cada host recebe:
  ansible_connection = pct
  ansible_host       = <VMID>     (usado pelo connection plugin como alvo do pct)
  pct_vmid           = <VMID>
  pct_name           = <hostname do container>
  pct_ostype         = ubuntu|debian|...

Grupos:
  running            : todos os containers ligados
  ostype_<tipo>      : agrupados por ostype (ostype_ubuntu, ostype_debian, ...)

Uso:
  ./pct.py --list
  ./pct.py --host <name>   (retorna {} — todas as vars já vêm no --list)
"""
import json
import subprocess
import sys


def run(cmd):
    return subprocess.run(
        cmd, capture_output=True, text=True, check=True
    ).stdout


def list_containers():
    """Retorna lista de dicts {vmid, status, name} a partir de `pct list`."""
    out = run(["pct", "list"])
    lines = out.splitlines()
    if not lines:
        return []
    # Cabeçalho: VMID  Status  Lock  Name  -> parsing por colunas via split simples.
    containers = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        vmid, status = parts[0], parts[1]
        # Name é a última coluna; Lock é opcional no meio.
        name = parts[-1] if len(parts) >= 3 else vmid
        containers.append({"vmid": vmid, "status": status, "name": name})
    return containers


def ostype_of(vmid):
    """Lê o ostype do container via `pct config`. Default 'debian' se ausente."""
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
        # Todas as vars vêm via _meta no --list.
        print(json.dumps({}))
    else:
        sys.stderr.write("uso: pct.py --list | --host <name>\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
