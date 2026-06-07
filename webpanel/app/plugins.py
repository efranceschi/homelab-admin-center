"""Plugin discovery, registry, and config resolution.

A plugin is a folder under ``webpanel/plugins/<id>/`` containing:
  - ``plugin.yml``      : manifest (id, name, ansible_role, tags, ...)
  - ``form.schema.yml`` : UI form fields mapped to the role's variables

The wrapped Ansible role is resolved with this precedence:
  1. ``webpanel/plugins/<id>/role/``  (self-contained, future drop-ins)
  2. ``roles/<ansible_role>``         (existing production roles)

Discovery runs at startup; each plugin is upserted into the ``plugins`` table.
Enable/disable and configuration are stored in the DB, never on the filesystem.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .models import Plugin, PluginConfig


@dataclass
class FormField:
    var: str
    label: str
    type: str = "string"
    default: object = None
    choices: list[str] = field(default_factory=list)
    help: str = ""
    secret: bool = False


@dataclass
class LoadedPlugin:
    id: str
    name: str
    version: str
    description: str
    ansible_role: str
    tags: list[str]
    enable_var: str | None
    supported_connections: list[str]
    order: int
    supports_check_mode: bool
    role_path: Path | None
    fields: list[FormField]
    path: Path

    @property
    def tags_csv(self) -> str:
        return ",".join(self.tags)

    def defaults(self) -> dict:
        return {f.var: f.default for f in self.fields if f.default is not None}

    def secret_vars(self) -> set[str]:
        return {f.var for f in self.fields if f.secret}


class PluginRegistry:
    """In-memory registry of discovered plugins, keyed by plugin id."""

    def __init__(self) -> None:
        self._plugins: dict[str, LoadedPlugin] = {}

    def all(self) -> list[LoadedPlugin]:
        return sorted(self._plugins.values(), key=lambda p: p.order)

    def get(self, plugin_id: str) -> LoadedPlugin | None:
        return self._plugins.get(plugin_id)

    def load(self) -> list[str]:
        """(Re)scan the plugins directory. Returns list of warning messages."""
        self._plugins.clear()
        warnings: list[str] = []
        if not config.PLUGINS_DIR.is_dir():
            return [f"plugins dir not found: {config.PLUGINS_DIR}"]
        for entry in sorted(config.PLUGINS_DIR.iterdir()):
            manifest = entry / "plugin.yml"
            if not (entry.is_dir() and manifest.is_file()):
                continue
            try:
                self._plugins_add(entry, manifest)
            except Exception as exc:  # never let one bad plugin crash startup
                warnings.append(f"skipped plugin {entry.name}: {exc}")
        return warnings

    def _plugins_add(self, entry: Path, manifest: Path) -> None:
        data = yaml.safe_load(manifest.read_text()) or {}
        pid = str(data.get("id") or entry.name)

        role_name = str(data.get("ansible_role") or "")
        # Resolve role: self-contained dir wins, else shared roles/ dir.
        local_role = entry / "role"
        if local_role.is_dir():
            role_path: Path | None = local_role
        elif role_name and (config.ROLES_PATH / role_name).is_dir():
            role_path = config.ROLES_PATH / role_name
        else:
            role_path = None  # tags-only plugin; runner still passes --tags

        fields = self._load_fields(entry / "form.schema.yml")

        self._plugins[pid] = LoadedPlugin(
            id=pid,
            name=str(data.get("name") or pid),
            version=str(data.get("version") or "0.0.0"),
            description=str(data.get("description") or ""),
            ansible_role=role_name,
            tags=[str(t) for t in (data.get("tags") or [])],
            enable_var=(data.get("enable_var") or None),
            supported_connections=[
                str(c) for c in (data.get("supported_connections") or ["local", "ssh", "proxmox"])
            ],
            order=int(data.get("order") or 100),
            supports_check_mode=bool(data.get("supports_check_mode", True)),
            role_path=role_path,
            fields=fields,
            path=entry,
        )

    @staticmethod
    def _load_fields(schema_path: Path) -> list[FormField]:
        if not schema_path.is_file():
            return []
        schema = yaml.safe_load(schema_path.read_text()) or {}
        fields: list[FormField] = []
        for raw in schema.get("fields", []) or []:
            fields.append(
                FormField(
                    var=raw["var"],
                    label=raw.get("label", raw["var"]),
                    type=raw.get("type", "string"),
                    default=raw.get("default"),
                    choices=[str(c) for c in raw.get("choices", [])],
                    help=raw.get("help", ""),
                )
            )
        for raw in schema.get("secret_fields", []) or []:
            fields.append(
                FormField(
                    var=raw["var"],
                    label=raw.get("label", raw["var"]),
                    type=raw.get("type", "password"),
                    secret=True,
                )
            )
        return fields


# Process-wide registry singleton.
registry = PluginRegistry()


def sync_to_db(db: Session) -> None:
    """Upsert discovered plugins into the ``plugins`` table.

    Preserves the operator's enabled flag across restarts; updates metadata.
    """
    seen: set[str] = set()
    for lp in registry.all():
        seen.add(lp.id)
        row = db.scalar(select(Plugin).where(Plugin.key == lp.id))
        schema_json = json.dumps(
            [
                {
                    "var": f.var,
                    "label": f.label,
                    "type": f.type,
                    "default": f.default,
                    "choices": f.choices,
                    "help": f.help,
                    "secret": f.secret,
                }
                for f in lp.fields
            ]
        )
        if row is None:
            db.add(
                Plugin(
                    key=lp.id,
                    name=lp.name,
                    ansible_tags=lp.tags_csv,
                    role_name=lp.ansible_role,
                    description=lp.description,
                    schema_json=schema_json,
                    supported_connections=",".join(lp.supported_connections),
                    enable_var=lp.enable_var,
                    supports_check_mode=lp.supports_check_mode,
                    order=lp.order,
                    enabled=True,
                    version=lp.version,
                )
            )
        else:
            row.name = lp.name
            row.ansible_tags = lp.tags_csv
            row.role_name = lp.ansible_role
            row.description = lp.description
            row.schema_json = schema_json
            row.supported_connections = ",".join(lp.supported_connections)
            row.enable_var = lp.enable_var
            row.supports_check_mode = lp.supports_check_mode
            row.order = lp.order
            row.version = lp.version


def resolve_config(db: Session, plugin_id: str, server_id: int | None = None) -> dict:
    """Resolve effective variables: defaults < global < group < host.

    Group scope is resolved for every group the server belongs to (in id order),
    then the host scope wins last. Secrets are excluded (handled separately).
    """
    lp = registry.get(plugin_id)
    if lp is None:
        return {}
    plugin_row = db.scalar(select(Plugin).where(Plugin.key == plugin_id))
    if plugin_row is None:
        return dict(lp.defaults())

    values: dict = dict(lp.defaults())

    def overlay(scope: str, ref: int | None) -> None:
        stmt = select(PluginConfig).where(
            PluginConfig.plugin_id == plugin_row.id,
            PluginConfig.scope == scope,
        )
        stmt = stmt.where(PluginConfig.scope_ref_id == ref) if ref is not None else stmt.where(
            PluginConfig.scope_ref_id.is_(None)
        )
        cfg = db.scalar(stmt)
        if cfg:
            values.update(json.loads(cfg.config_json or "{}"))

    overlay("global", None)
    if server_id is not None:
        from .groups import effective_group_ids_for_host

        # Ancestor groups first, then the host's direct groups, so a more
        # specific (child) group overrides its parents; host scope wins last.
        for gid in effective_group_ids_for_host(db, server_id):
            overlay("group", gid)
        overlay("host", server_id)

    # Drop secret vars — they never live in plugin_configs.
    for s in lp.secret_vars():
        values.pop(s, None)
    return values
