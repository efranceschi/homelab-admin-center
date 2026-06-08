"""Shared Jinja2 templates instance and render helper."""
from __future__ import annotations

import socket

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import config
from .auth import get_csrf_token


def _static_version() -> str:
    """Cache-busting token for the bundled CSS/JS: the newest mtime among the
    panel's own static assets. Changes whenever we edit them (and the panel is
    restarted), so browsers fetch the new file instead of a stale cached copy."""
    import os

    newest = 0.0
    for sub in ("css/panel.css", "js/sse.js", "js/xterm.js", "css/xterm.css"):
        try:
            newest = max(newest, (config.APP_DIR / "static" / sub).stat().st_mtime)
        except OSError:
            pass
    return str(int(newest)) or config.APP_VERSION

templates = Jinja2Templates(directory=str(config.APP_DIR / "templates"))
templates.env.globals["app_name"] = config.APP_NAME       # long: Homelab Admin and Control Kernel
templates.env.globals["app_short"] = config.APP_SHORT     # short: H.A.C.K.
templates.env.globals["app_slug"] = config.APP_SLUG       # sigla: hack
templates.env.globals["app_version"] = config.APP_VERSION
templates.env.globals["app_repo_url"] = config.APP_REPO_URL
# Navbar brand: a user-customisable instance name + the node's hostname badge.
# Process-wide globals are authoritative because the panel runs a single worker.
templates.env.globals["hostname"] = socket.gethostname()
templates.env.globals["instance_name"] = config.APP_NAME  # overridden at startup
# Periodic auto-refresh of volatile views (seconds; 0 = off). Overridden at
# startup from the 'auto_refresh_seconds' setting. Single worker => global state.
templates.env.globals["auto_refresh_seconds"] = 180
# Cache-busting query appended to bundled CSS/JS URLs (see _static_version).
templates.env.globals["static_version"] = _static_version()


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


def render(request: Request, name: str, *, status_code: int = 200, **ctx):
    """Render a template with common context (csrf, current user).

    ``status_code`` lets callers return non-200 HTML (e.g. a 503 while the panel
    is draining for restart) without bypassing the shared context seeding."""
    base = {
        "request": request,
        "csrf_token": get_csrf_token(request),
        "current_username": request.session.get("username"),
        "current_role": request.session.get("role"),
    }
    # Seed the sidebar job-pool indicator from the live manager so its first
    # paint shows the real running/max (the poller then keeps it fresh). Only
    # for authenticated renders — the indicator isn't shown to anon pages.
    if base["current_username"]:
        from .jobs import manager

        base["queue_running"] = manager.running_count()   # in-memory
        base["queue_queued"] = manager.queued_count()     # in-memory
        base["queue_max"] = manager.max_concurrent()      # one PK Setting read
    base.update(ctx)
    # Starlette >=0.29 signature: TemplateResponse(request, name, context).
    return templates.TemplateResponse(request, name, base, status_code=status_code)
