# Connection plugin: executa tasks Ansible dentro de containers LXC do Proxmox
# usando `pct exec` / `pct push` / `pct pull`. Roda como root no nó Proxmox,
# dispensa SSH e funciona em containers privilegiados e unprivileged.
from __future__ import annotations

import os
import subprocess

from ansible.errors import AnsibleError, AnsibleFileNotFound
from ansible.module_utils.common.text.converters import to_bytes, to_native
from ansible.plugins.connection import ConnectionBase
from ansible.utils.display import Display

DOCUMENTATION = """
    name: pct
    short_description: Executa via `pct exec` em containers LXC do Proxmox
    description:
        - Usa o utilitário `pct` do Proxmox para executar comandos e transferir
          arquivos para dentro de containers LXC, sem necessidade de SSH.
    author: lxc-ansible
    options:
      remote_addr:
        description: VMID do container (vem de ansible_host no inventário pct.py).
        default: inventory_hostname
        vars:
          - name: inventory_hostname
          - name: ansible_host
          - name: pct_vmid
      executable:
        description: Shell usado para executar os comandos dentro do container.
        default: /bin/sh
        vars:
          - name: ansible_executable
        ini:
          - section: defaults
            key: executable
      pct_cmd:
        description: Caminho do binário pct no host.
        default: pct
        vars:
          - name: pct_cmd
"""

display = Display()


class Connection(ConnectionBase):
    transport = "pct"
    has_pipelining = True

    def _connect(self):
        if not self._connected:
            self._vmid = str(self.get_option("remote_addr"))
            display.vvv("ESTABLISH PCT CONNECTION FOR VMID: {0}".format(self._vmid))
            self._connected = True
        return self

    def _pct(self):
        return self.get_option("pct_cmd")

    def exec_command(self, cmd, in_data=None, sudoable=True):
        super().exec_command(cmd, in_data=in_data, sudoable=sudoable)
        executable = self.get_option("executable")
        local_cmd = [
            self._pct(),
            "exec",
            self._vmid,
            "--",
            executable,
            "-c",
            cmd,
        ]
        display.vvv("EXEC {0}".format(" ".join(local_cmd)), host=self._vmid)
        proc = subprocess.Popen(
            local_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = proc.communicate(in_data)
        return (proc.returncode, stdout, stderr)

    def put_file(self, in_path, out_path):
        super().put_file(in_path, out_path)
        display.vvv("PUT {0} TO {1}".format(in_path, out_path), host=self._vmid)
        if not os.path.exists(to_bytes(in_path, errors="surrogate_or_strict")):
            raise AnsibleFileNotFound(
                "arquivo de origem não encontrado: {0}".format(in_path)
            )
        cmd = [self._pct(), "push", self._vmid, in_path, out_path]
        self._run_transfer(cmd, "push")

    def fetch_file(self, in_path, out_path):
        super().fetch_file(in_path, out_path)
        display.vvv("FETCH {0} TO {1}".format(in_path, out_path), host=self._vmid)
        cmd = [self._pct(), "pull", self._vmid, in_path, out_path]
        self._run_transfer(cmd, "pull")

    def _run_transfer(self, cmd, action):
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            raise AnsibleError(
                "pct {0} falhou (rc={1}): {2}".format(
                    action, proc.returncode, to_native(stderr)
                )
            )

    def close(self):
        self._connected = False
