"""FastAPI application factory for HomeLab Admin Center (hac)."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import config, crypto
from .auth import install_redirect_handler
from .db import init_db, session_scope
from .plugins import registry, sync_to_db
from .routers import dashboard, hosts, jobs as jobs_router, plugins as plugins_router
from .routers import auth as auth_router
from .routers import schedules as schedules_router
from .routers import settings as settings_router
from .scheduler import manager as scheduler_manager
from .templating import templates


def create_app() -> FastAPI:
    app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION)

    app.add_middleware(
        SessionMiddleware,
        secret_key=crypto.get_session_secret(),
        session_cookie="hac_session",
        same_site="lax",
        https_only=False,  # set True behind TLS
    )

    app.mount(
        "/static",
        StaticFiles(directory=str(config.APP_DIR / "static")),
        name="static",
    )

    install_redirect_handler(app)

    app.include_router(auth_router.router)
    app.include_router(dashboard.router)
    app.include_router(hosts.router)
    app.include_router(plugins_router.router)
    app.include_router(jobs_router.router)
    app.include_router(schedules_router.router)
    app.include_router(settings_router.router)

    @app.on_event("startup")
    def _startup() -> None:
        init_db()
        warnings = registry.load()
        for w in warnings:
            print(f"[hac] plugin warning: {w}")
        with session_scope() as db:
            sync_to_db(db)
        # Scheduling is owned by the app via a separate child process (no cron).
        scheduler_manager.ensure_running()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        scheduler_manager.stop()

    @app.get("/")
    def index(request: Request):
        if request.session.get("user_id"):
            return RedirectResponse("/dashboard", status_code=303)
        with session_scope() as db:
            from .auth import is_setup_complete

            if not is_setup_complete(db):
                return RedirectResponse("/setup", status_code=303)
        return RedirectResponse("/login", status_code=303)

    return app


app = create_app()
