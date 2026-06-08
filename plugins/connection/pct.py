# Connection plugin: runs Ansible tasks inside Proxmox LXC containers using
# `pct exec` / `pct push` / `pct pull`. Runs as root on the Proxmox node, needs
# no SSH and works on both privileged and unprivileged containers.
from __future__ import annotations

import os
import subprocess

from ansible.errors import AnsibleError, AnsibleFileNotFound
from ansible.module_utils.common.text.converters import to_bytes, to_native
from ansible.plugins.connection import ConnectionBase
from ansible.utils.display import Display

DOCUMENTATION = """
    name: pct
    short_description: Runs via `pct exec` in Proxmox LXC containers
    description:
        - Uses the Proxmox `pct` utility to run commands and transfer files
          into LXC containers, with no SSH required.
    author: hack
    options:
      remote_addr:
        description: Container VMID (comes from ansible_host in the pct.py inventory).
        default: inventory_hostname
        vars:
          - name: inventory_hostname
          - name: ansible_host
          - name: pct_vmid
      executable:
        description: Shell used to run the commands inside the container.
        default: /bin/sh
        vars:
          - name: ansible_executable
        ini:
          - section: defaults
            key: executable
      pct_cmd:
        description: Path to the pct binary on the host.
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

    def _pct_base(self):
        """pct argv prefix, resolved to an absolute path and routed through
        ``sudo -n`` when not running as root.

        ``pct`` needs root; under the web panel this plugin runs as the
        unprivileged ``hack`` user, which is granted ``pct`` via
        /etc/sudoers.d/hack. Run directly as root (the CLI path) it is unchanged.
        """
        import shutil

        pct = self.get_option("pct_cmd")
        pct = shutil.which(pct) or next(
            (p for p in ("/usr/sbin/pct", "/usr/bin/pct") if os.path.exists(p)),
            pct,
        )
        return ["sudo", "-n", pct] if os.geteuid() != 0 else [pct]

    def exec_command(self, cmd, in_data=None, sudoable=True):
        super().exec_command(cmd, in_data=in_data, sudoable=sudoable)
        executable = self.get_option("executable")
        local_cmd = self._pct_base() + [
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
                "source file not found: {0}".format(in_path)
            )
        cmd = self._pct_base() + ["push", self._vmid, in_path, out_path]
        self._run_transfer(cmd, "push")

    def fetch_file(self, in_path, out_path):
        super().fetch_file(in_path, out_path)
        display.vvv("FETCH {0} TO {1}".format(in_path, out_path), host=self._vmid)
        cmd = self._pct_base() + ["pull", self._vmid, in_path, out_path]
        self._run_transfer(cmd, "pull")

    def _run_transfer(self, cmd, action):
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            raise AnsibleError(
                "pct {0} failed (rc={1}): {2}".format(
                    action, proc.returncode, to_native(stderr)
                )
            )

    def close(self):
        self._connected = False
