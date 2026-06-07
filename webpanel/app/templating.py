"""Shared Jinja2 templates instance and render helper."""
from __future__ import annotations

import socket

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import config
from .auth import get_csrf_token

templates = Jinja2Templates(directory=str(config.APP_DIR / "templates"))
templates.env.globals["app_name"] = config.APP_NAME       # long: HomeLab Admin Center
templates.env.globals["app_short"] = config.APP_SHORT     # short: HAC
templates.env.globals["app_slug"] = config.APP_SLUG       # sigla: hac
templates.env.globals["app_version"] = config.APP_VERSION
templates.env.globals["app_repo_url"] = config.APP_REPO_URL
# Navbar brand: a user-customisable instance name + the node's hostname badge.
# Process-wide globals are authoritative because the panel runs a single worker.
templates.env.globals["hostname"] = socket.gethostname()
templates.env.globals["instance_name"] = config.APP_NAME  # overridden at startup
# Periodic auto-refresh of volatile views (seconds; 0 = off). Overridden at
# startup from the 'auto_refresh_seconds' setting. Single worker => global state.
templates.env.globals["auto_refresh_seconds"] = 180


def set_instance_name(value: str | None) -> None:
    """Update the navbar instance name (falls back to the app name if blank)."""
    templates.env.globals["instance_name"] = (value or "").strip() or config.APP_NAME


def set_auto_refresh_seconds(value: int | None) -> None:
    """Update the page auto-refresh interval (clamped; 0 disables)."""
    try:
        n = int(value) if value is not None else 180
    except (TypeError, ValueError):
        n = 180
    if n and n < 10:
        n = 10  # avoid hammering; below 10s is effectively a reload loop
    templates.env.globals["auto_refresh_seconds"] = max(0, min(3600, n))


def render(request: Request, name: str, **ctx):
    """Render a template with common context (csrf, current user)."""
    base = {
        "request": request,
        "csrf_token": get_csrf_token(request),
        "current_username": request.session.get("username"),
        "current_role": request.session.get("role"),
    }
    base.update(ctx)
    # Starlette >=0.29 signature: TemplateResponse(request, name, context).
    return templates.TemplateResponse(request, name, base)
