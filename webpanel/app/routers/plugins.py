"""Plugin registry views: list, configure (global defaults), enable/disable."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto
from ..auth import current_user, require_admin, verify_csrf
from ..db import db_dependency
from ..models import AuditLog, Credential, HostGroup, Plugin, PluginConfig, User
from ..plugins import registry, resolve_config
from ..templating import render

router = APIRouter(prefix="/plugins")


@router.get("")
def list_plugins(
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    rows = {p.key: p for p in db.scalars(select(Plugin)).all()}
    items = []
    for lp in registry.all():
        items.append({"lp": lp, "row": rows.get(lp.id)})
    return render(request, "plugins.html", items=items)


@router.get("/{plugin_id}")
def configure_form(
    plugin_id: str,
    request: Request,
    scope: str = "global",
    ref: int | None = None,
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    lp = registry.get(plugin_id)
    if lp is None:
        return RedirectResponse("/plugins", status_code=303)
    row = db.scalar(select(Plugin).where(Plugin.key == plugin_id))
    groups = db.scalars(select(HostGroup).order_by(HostGroup.name)).all()
    valid_ref = ref in {g.id for g in groups}
    scope = "group" if (scope == "group" and ref and valid_ref) else "global"
    inherited: dict = {}
    if scope == "group":
        # Editing a group's sparse overlay: show only what's explicitly set here;
        # the inherited effective value (global) is shown as placeholder context.
        inherited = resolve_config(db, plugin_id, None)
        cfg = db.scalar(
            select(PluginConfig).where(
                PluginConfig.plugin_id == row.id,
                PluginConfig.scope == "group",
                PluginConfig.scope_ref_id == ref,
            )
        ) if row else None
        values = json.loads(cfg.config_json) if cfg else {}
        values.pop("__secrets__", None)
        secret_set: set[str] = set()
    else:
        values = resolve_config(db, plugin_id, None)
        secret_set = _stored_secret_vars(db, row)
    return render(
        request,
        "plugin_config.html",
        lp=lp,
        row=row,
        values=values,
        secret_set=secret_set,
        groups=groups,
        scope=scope,
        ref=ref,
        inherited=inherited,
    )


@router.post("/{plugin_id}", dependencies=[Depends(verify_csrf)])
async def save_config(
    plugin_id: str,
    request: Request,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    lp = registry.get(plugin_id)
    row = db.scalar(select(Plugin).where(Plugin.key == plugin_id))
    if lp is None or row is None:
        return RedirectResponse("/plugins", status_code=303)

    form = await request.form()
    scope = form.get("scope") or "global"
    ref = form.get("ref")
    ref_id = int(ref) if (scope == "group" and ref and str(ref).isdigit()) else None

    # --- Group scope: sparse, non-secret overlay (secrets stay global). ---
    if scope == "group" and ref_id is not None:
        sparse: dict[str, object] = {}
        for f in lp.fields:
            if f.secret:
                continue  # secrets are configured at the Global scope only
            raw = form.get(f.var)
            if f.type in ("bool", "yesno"):
                # tri-state select: "" = inherit, else explicit value
                if raw in (None, ""):
                    continue
                sparse[f.var] = (raw == "true") if f.type == "bool" else raw
            else:
                if raw is not None and str(raw).strip() != "":
                    sparse[f.var] = raw
        payload = json.dumps(sparse)
        cfg = db.scalar(
            select(PluginConfig).where(
                PluginConfig.plugin_id == row.id,
                PluginConfig.scope == "group",
                PluginConfig.scope_ref_id == ref_id,
            )
        )
        if cfg is None:
            db.add(PluginConfig(
                plugin_id=row.id, scope="group", scope_ref_id=ref_id,
                config_json=payload, updated_by=user.id,
            ))
        else:
            cfg.config_json = payload
            cfg.updated_by = user.id
        db.add(AuditLog(user_id=user.id, action="plugin.config", target=f"{plugin_id}@group:{ref_id}"))
        return RedirectResponse(f"/plugins/{plugin_id}?scope=group&ref={ref_id}", status_code=303)

    # --- Global scope: full config + secrets (unchanged behaviour). ---
    non_secret: dict[str, object] = {}
    box = crypto.get_box()

    # Load existing global config (to preserve secret refs).
    cfg = db.scalar(
        select(PluginConfig).where(
            PluginConfig.plugin_id == row.id,
            PluginConfig.scope == "global",
            PluginConfig.scope_ref_id.is_(None),
        )
    )
    existing = json.loads(cfg.config_json) if cfg else {}
    secret_refs: dict[str, int] = dict(existing.get("__secrets__", {}))

    for f in lp.fields:
        raw = form.get(f.var)
        if f.secret:
            if raw:  # only update when a new secret was entered
                cred = Credential(
                    name=f"{plugin_id}:{f.var}",
                    type="password",
                    secret_ciphertext=box.encrypt(str(raw)),
                    created_by=user.id,
                )
                # Replace any prior credential for this var.
                _delete_secret_credential(db, plugin_id, f.var)
                db.add(cred)
                db.flush()
                secret_refs[f.var] = cred.id
            continue
        if f.type == "bool":
            non_secret[f.var] = f.var in form
        elif f.type == "yesno":
            non_secret[f.var] = "yes" if f.var in form else (raw or "no")
        else:
            if raw is not None:
                non_secret[f.var] = raw

    non_secret["__secrets__"] = secret_refs
    payload = json.dumps(non_secret)

    if cfg is None:
        cfg = PluginConfig(
            plugin_id=row.id, scope="global", scope_ref_id=None, config_json=payload,
            updated_by=user.id,
        )
        db.add(cfg)
    else:
        cfg.config_json = payload
        cfg.updated_by = user.id
    db.add(AuditLog(user_id=user.id, action="plugin.config", target=plugin_id))
    return RedirectResponse("/plugins", status_code=303)


@router.post("/{plugin_id}/toggle", dependencies=[Depends(verify_csrf)])
def toggle_plugin(
    plugin_id: str,
    db: Session = Depends(db_dependency),
    user: User = Depends(require_admin),
):
    row = db.scalar(select(Plugin).where(Plugin.key == plugin_id))
    if row:
        row.enabled = not row.enabled
        db.add(AuditLog(user_id=user.id, action="plugin.toggle", target=plugin_id))
    return RedirectResponse("/plugins", status_code=303)


# --------------------------------------------------------------------------- #
def _stored_secret_vars(db: Session, row: Plugin | None) -> set[str]:
    if row is None:
        return set()
    cfg = db.scalar(
        select(PluginConfig).where(
            PluginConfig.plugin_id == row.id,
            PluginConfig.scope == "global",
            PluginConfig.scope_ref_id.is_(None),
        )
    )
    if cfg is None:
        return set()
    return set(json.loads(cfg.config_json).get("__secrets__", {}).keys())


def _delete_secret_credential(db: Session, plugin_id: str, var: str) -> None:
    name = f"{plugin_id}:{var}"
    for cred in db.scalars(select(Credential).where(Credential.name == name)).all():
        db.delete(cred)
