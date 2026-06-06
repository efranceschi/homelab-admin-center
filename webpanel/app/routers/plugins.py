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
from ..models import AuditLog, Credential, Plugin, PluginConfig, User
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
    db: Session = Depends(db_dependency),
    user: User = Depends(current_user),
):
    lp = registry.get(plugin_id)
    if lp is None:
        return RedirectResponse("/plugins", status_code=303)
    row = db.scalar(select(Plugin).where(Plugin.key == plugin_id))
    values = resolve_config(db, plugin_id, None)
    # Determine which secret fields already have a stored credential.
    secret_set = _stored_secret_vars(db, row)
    return render(
        request,
        "plugin_config.html",
        lp=lp,
        row=row,
        values=values,
        secret_set=secret_set,
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
