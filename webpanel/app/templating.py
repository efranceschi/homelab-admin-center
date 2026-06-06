"""Shared Jinja2 templates instance and render helper."""
from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import config
from .auth import get_csrf_token

templates = Jinja2Templates(directory=str(config.APP_DIR / "templates"))
templates.env.globals["app_name"] = config.APP_NAME       # long: HomeLab Admin Center
templates.env.globals["app_short"] = config.APP_SHORT     # short: HAC
templates.env.globals["app_slug"] = config.APP_SLUG       # sigla: hac
templates.env.globals["app_version"] = config.APP_VERSION


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
