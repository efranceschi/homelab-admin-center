"""Translate DB plugin configuration into per-job Ansible extra-vars.

Non-secret values go to ``extra-vars.yml`` (highest precedence, layered on top
of the unchanged group_vars). Secret values are written to a vault-encrypted
``extra-vars-secret.yml`` using the project's existing vault password — the
committed group_vars/vault.yml is never modified.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..models import Credential, Plugin, PluginConfig
from ..plugins import registry, resolve_config


def resolve_host_vars(db: Session, plugin_ids: list[str], server_id: int) -> dict:
    """Effective non-secret config for one host across the selected plugins.

    Merges each plugin's resolved overlay (defaults < global < group(s) < host),
    so the result can be written as the host's inventory hostvars — making
    per-group/per-host configuration apply on multi-host runs too.
    """
    merged: dict[str, object] = {}
    for pid in plugin_ids:
        if registry.get(pid) is None:
            continue
        merged.update(resolve_config(db, pid, server_id))
    return merged


def build_secret_vars(
    db: Session, run_dir: Path, plugin_ids: list[str]
) -> Path | None:
    """Write the vault-encrypted secrets file (global scope) or return None."""
    secrets: dict[str, str] = {}
    for pid in plugin_ids:
        if registry.get(pid) is None:
            continue
        secrets.update(_collect_secrets(db, pid))
    if not secrets:
        return None
    return _write_vault_file(run_dir, secrets)


def build_extra_vars(
    db: Session, run_dir: Path, plugin_ids: list[str], server_id: int | None
) -> tuple[Path | None, Path | None]:
    """Return (extra_vars_path, secret_vars_path); either may be None."""
    merged: dict[str, object] = {}
    secrets: dict[str, str] = {}

    for pid in plugin_ids:
        lp = registry.get(pid)
        if lp is None:
            continue
        merged.update(resolve_config(db, pid, server_id))
        secrets.update(_collect_secrets(db, pid))

    extra_path: Path | None = None
    if merged:
        extra_path = run_dir / "extra-vars.yml"
        extra_path.write_text(yaml.safe_dump(merged, default_flow_style=False))

    secret_path: Path | None = None
    if secrets:
        secret_path = _write_vault_file(run_dir, secrets)

    return extra_path, secret_path


def _collect_secrets(db: Session, plugin_id: str) -> dict[str, str]:
    """Read a plugin's secret values from its global config row.

    Secret form fields store a credential reference (``{"credential_id": N}``)
    inside the plugin's global config under a ``__secrets__`` key.
    """
    plugin_row = db.scalar(select(Plugin).where(Plugin.key == plugin_id))
    if plugin_row is None:
        return {}
    cfg = db.scalar(
        select(PluginConfig).where(
            PluginConfig.plugin_id == plugin_row.id,
            PluginConfig.scope == "global",
            PluginConfig.scope_ref_id.is_(None),
        )
    )
    if cfg is None:
        return {}
    data = json.loads(cfg.config_json or "{}")
    refs = data.get("__secrets__", {})
    out: dict[str, str] = {}
    from .. import crypto

    box = crypto.get_box()
    for var, cred_id in refs.items():
        cred = db.get(Credential, int(cred_id))
        if cred is not None:
            out[var] = box.decrypt(cred.secret_ciphertext)
    return out


def _write_vault_file(run_dir: Path, secrets: dict[str, str]) -> Path:
    plain = run_dir / "extra-vars-secret.plain.yml"
    plain.write_text(yaml.safe_dump(secrets, default_flow_style=False))
    enc = run_dir / "extra-vars-secret.yml"
    if not config.VAULT_PASSWORD_FILE.exists():
        # No vault password available: fall back to plaintext (0600) so the run
        # still works in dev. Documented behavior; production should provide it.
        plain.replace(enc)
        enc.chmod(0o600)
        return enc
    subprocess.run(
        [
            str(config.ANSIBLE_ROOT / ".venv" / "bin" / "ansible-vault"),
            "encrypt",
            "--vault-password-file",
            str(config.VAULT_PASSWORD_FILE),
            "--output",
            str(enc),
            str(plain),
        ],
        check=True,
        capture_output=True,
    )
    plain.unlink(missing_ok=True)
    return enc
