"""Self-update and self-restart for the running panel.

Designed for the app running as the ``hac`` systemd service: restart is a
detached ``systemctl restart hac``. When not under systemd (e.g. a foreground
dev run), it falls back to re-executing the current process.
"""
from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
import threading
import time

from . import config


def under_systemd() -> bool:
    return os.path.exists("/run/systemd/system") and shutil.which("systemctl") is not None


def service_user() -> dict:
    """The OS user/uid the panel process runs as (shown in Settings > System)."""
    uid = os.geteuid()
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        name = str(uid)
    return {"name": name, "uid": uid, "is_root": uid == 0}


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
    """Schedule a restart shortly after the current response is sent.

    The panel owns its own process, so it never needs root (nor `systemctl`) to
    restart. Under systemd it simply exits and is respawned by ``Restart=always``
    — a fresh ``run-panel.sh`` that rebuilds the venv if needed and re-execs
    uvicorn. Off systemd (foreground dev) it re-executes itself in place.
    """
    def _respawn() -> None:
        time.sleep(delay)
        if under_systemd():
            os._exit(0)  # systemd (Restart=always) brings us back fresh
        try:
            # nosemgrep: python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args -- self-respawn: argv is our own process argv, not external input
            os.execv(sys.executable, [sys.executable, sys.argv[0], *sys.argv[1:]])
        except Exception:
            os._exit(0)  # let any supervisor bring it back

    threading.Thread(target=_respawn, daemon=True).start()
    return "Restarting (process will respawn)…"


def force_restart() -> None:
    """Restart immediately, in-line, without the delayed background thread.

    Used by the SIGHUP graceful-restart path when the drain finishes (or its
    timeout expires / a second HUP arrives): we are already off the request path
    and want the respawn to happen now, not after a delay. Same mechanics as
    :func:`request_restart` (systemd respawn vs. self re-exec)."""
    if under_systemd():
        os._exit(0)  # systemd (Restart=always) brings us back fresh
    try:
        # nosemgrep: python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args -- self-respawn: argv is our own process argv, not external input
        os.execv(sys.executable, [sys.executable, sys.argv[0], *sys.argv[1:]])
    except Exception:
        os._exit(0)  # let any supervisor bring it back
