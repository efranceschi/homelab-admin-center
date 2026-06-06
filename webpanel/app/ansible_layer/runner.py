"""Build the ansible-playbook invocation for a panel job.

Everything is scoped to the panel: a generated inventory, the dedicated
playbooks/webpanel.yml, selected --tags, --limit, optional --check --diff, and
extra-vars overlays. Ansible paths (roles/collections/connection plugins/vault)
are supplied via environment variables so the panel never reads or mutates the
cron-critical ansible.cfg.
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import config


def build_env() -> dict[str, str]:
    env = os.environ.copy()
    env["ANSIBLE_ROLES_PATH"] = str(config.ROLES_PATH)
    env["ANSIBLE_COLLECTIONS_PATH"] = str(config.COLLECTIONS_PATH)
    env["ANSIBLE_CONNECTION_PLUGINS"] = str(config.CONNECTION_PLUGINS_PATH)
    env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
    env["ANSIBLE_RETRY_FILES_ENABLED"] = "False"
    env["ANSIBLE_FORCE_COLOR"] = "1"
    env["ANSIBLE_INTERPRETER_PYTHON"] = "/usr/bin/python3"
    if config.VAULT_PASSWORD_FILE.exists():
        env["ANSIBLE_VAULT_PASSWORD_FILE"] = str(config.VAULT_PASSWORD_FILE)
    # Make the connection plugin importable as a module too (defensive).
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _ansible_playbook_bin() -> str:
    """Prefer the project's own venv ansible-playbook (same version as cron)."""
    if config.ANSIBLE_PLAYBOOK_BIN.exists():
        return str(config.ANSIBLE_PLAYBOOK_BIN)
    return "ansible-playbook"  # fall back to PATH


def build_command(
    inventory_path: Path,
    tags: list[str],
    limit_hosts: list[str],
    check: bool,
    extra_vars_path: Path | None,
    secret_vars_path: Path | None,
) -> list[str]:
    cmd = [
        _ansible_playbook_bin(),
        str(config.WEBPANEL_PLAYBOOK),
        "-i",
        str(inventory_path),
    ]
    if tags:
        cmd += ["--tags", ",".join(tags)]
    if limit_hosts:
        cmd += ["--limit", ",".join(limit_hosts)]
    if check:
        cmd += ["--check", "--diff"]
    if extra_vars_path is not None:
        cmd += ["-e", f"@{extra_vars_path}"]
    if secret_vars_path is not None:
        cmd += ["-e", f"@{secret_vars_path}"]
    return cmd
