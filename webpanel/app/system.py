"""Self-update and self-restart for the running panel.

Designed for the app running as the ``hac`` systemd service: restart is a
detached ``systemctl restart hac``. When not under systemd (e.g. a foreground
dev run), it falls back to re-executing the current process.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time

from . import config


def under_systemd() -> bool:
    return os.path.exists("/run/systemd/system") and shutil.which("systemctl") is not None


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    head = f"$ {' '.join(cmd)}\n"
    return head + (proc.stdout or "") + (proc.stderr or "")


def run_update() -> str:
    """Pull the latest code and reinstall web deps. Returns combined output."""
    out: list[str] = []
    git = shutil.which("git") or "git"
    out.append(_run([git, "-C", str(config.ANSIBLE_ROOT), "pull", "--ff-only"]))
    pip = config.PANEL_DIR / ".venv-web" / "bin" / "pip"
    if pip.exists():
        out.append(_run([str(pip), "install", "-q", "-r",
                         str(config.PANEL_DIR / "requirements-web.txt")]))
    return "\n".join(out)


def request_restart(delay: float = 1.0) -> str:
    """Schedule a restart shortly after the current response is sent."""
    if under_systemd():
        systemctl = shutil.which("systemctl") or "systemctl"
        # The service runs as the unprivileged `hac` user; restarting the unit
        # needs root, granted via /etc/sudoers.d/hac. Run directly as root (dev)
        # no sudo is used.
        sudo = "sudo -n " if os.geteuid() != 0 else ""
        subprocess.Popen(
            ["bash", "-c", f"sleep {delay}; {sudo}{systemctl} restart hac"],
            start_new_session=True,
        )
        return "Restarting via systemd (hac.service)…"

    def _reexec() -> None:
        time.sleep(delay)
        try:
            os.execv(sys.executable, [sys.executable, sys.argv[0], *sys.argv[1:]])
        except Exception:
            os._exit(0)  # let any supervisor bring it back

    threading.Thread(target=_reexec, daemon=True).start()
    return "Re-executing process…"
