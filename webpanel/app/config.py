"""Central configuration and filesystem paths for the web control panel.

All paths are derived from the panel's location inside the Ansible project so the
panel stays self-contained and never hard-codes assumptions beyond the repo root.
"""
from __future__ import annotations

import os
from pathlib import Path

# Branding — three canonical forms used throughout the project:
#   APP_NAME  : long display name  -> "HomeLab Admin Center"
#   APP_SHORT : short display name  -> "HAC"
#   APP_SLUG  : technical id / sigla (systemd unit, paths) -> "hac"
APP_NAME = "HomeLab Admin Center"
APP_SHORT = "HAC"
APP_SLUG = "hac"
APP_VERSION = "0.1.0"
APP_REPO_URL = "https://github.com/efranceschi/homelab-admin-center"

# webpanel/app/config.py -> webpanel/app -> webpanel -> <ANSIBLE_ROOT>
APP_DIR = Path(__file__).resolve().parent
PANEL_DIR = APP_DIR.parent
ANSIBLE_ROOT = PANEL_DIR.parent

# Existing Ansible assets the panel reuses (read-only; never modified).
ROLES_PATH = ANSIBLE_ROOT / "roles"
COLLECTIONS_PATH = ANSIBLE_ROOT / "collections"
CONNECTION_PLUGINS_PATH = ANSIBLE_ROOT / "plugins" / "connection"
INVENTORY_DIR = ANSIBLE_ROOT / "inventory"
WEBPANEL_PLAYBOOK = ANSIBLE_ROOT / "playbooks" / "webpanel.yml"
ANSIBLE_PLAYBOOK_BIN = ANSIBLE_ROOT / ".venv" / "bin" / "ansible-playbook"
VAULT_PASSWORD_FILE = Path(
    os.environ.get("PANEL_VAULT_PASSWORD_FILE", "/etc/hac/vault-pass")
)

# Shared advisory lock — the SAME file run.sh uses, so panel runs and the daily
# cron run can never overlap on the same containers.
RUN_LOCK_FILE = Path(os.environ.get("PANEL_RUN_LOCK", "/run/hac.lock"))

# Panel's own plugin directory (auto-discovered at startup).
PLUGINS_DIR = PANEL_DIR / "plugins"

# Per-job working directories (inventory, extra-vars, logs).
RUN_DIRS = PANEL_DIR / "run_dirs"

# PID of the main panel process, written at startup / removed on clean shutdown.
# Enables a sudo-free, HTTP-free graceful restart: `kill -HUP $(cat hac.pid)`.
# Mirrors the scheduler's scheduler.pid (same RUN_DIRS, created by ensure_*dirs).
HAC_PIDFILE = RUN_DIRS / "hac.pid"

# Persistent state. Defaults live under /var/lib so the DB is outside the git
# tree; falls back to the panel dir for unprivileged/dev runs.
_DEFAULT_STATE_DIR = "/var/lib/hac"
STATE_DIR = Path(os.environ.get("PANEL_STATE_DIR", _DEFAULT_STATE_DIR))
DB_PATH = Path(os.environ.get("PANEL_DB_PATH", str(STATE_DIR / "panel.sqlite3")))

# Master key for secrets-at-rest (Fernet). Mirrors the vault-pass trust model.
MASTER_KEY_PATH = Path(
    os.environ.get("PANEL_MASTER_KEY", "/etc/hac/panel.key")
)

# Session signing secret (persisted next to the master key).
SESSION_SECRET_PATH = Path(
    os.environ.get("PANEL_SESSION_SECRET", "/etc/hac/panel.session")
)

# Log retention for job logs (mirrors run.sh keeping the 30 most recent).
JOB_LOG_RETENTION = int(os.environ.get("PANEL_JOB_LOG_RETENTION", "30"))


def _envflag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


# Mark the session cookie Secure (HTTPS-only). Enable when the panel is fronted
# by a TLS-terminating reverse proxy (uvicorn must run with --proxy-headers so
# the original https scheme is seen). Leave off for plain-http/direct access,
# otherwise the browser drops the cookie and login silently fails.
HTTPS_ONLY = _envflag("PANEL_HTTPS_ONLY", default=False)


def ensure_runtime_dirs() -> None:
    """Create the writable runtime directories the panel needs.

    Falls back to a panel-local state dir when /var/lib is not writable (e.g.
    running as a non-root developer), so the app still boots.
    """
    global STATE_DIR, DB_PATH
    RUN_DIRS.mkdir(parents=True, exist_ok=True)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        STATE_DIR = PANEL_DIR / "data"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if "PANEL_DB_PATH" not in os.environ:
            DB_PATH = STATE_DIR / "panel.sqlite3"
