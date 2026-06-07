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
from .routers import dashboard, groups as groups_router, hosts, jobs as jobs_router, plugins as plugins_router
from .routers import auth as auth_router
from .routers import schedules as schedules_router
from .routers import settings as settings_router
from .scheduler import manager as scheduler_manager
from .templating import set_auto_refresh_seconds, set_instance_name, templates


def _ensure_default_schedules(db) -> None:
    """Seed a daily drift-check schedule once, so config state stays fresh.

    Guarded by a Setting flag: if the user later edits, disables, or deletes the
    schedule, we never recreate it. ``created_by`` is nullable (system-owned).
    """
    from .models import Schedule, Setting

    if db.get(Setting, "drift_schedule_seeded") is not None:
        return
    db.add(Schedule(
        name="drift-check",
        kind="daily",
        daily_time="04:30",
        mode="check",
        server_ids="",   # all enabled hosts
        plugin_ids="",   # all enabled plugins
        enabled=True,
        created_by=None,
    ))
    db.add(Setting(key="drift_schedule_seeded", value="1", value_type="str"))


def create_app() -> FastAPI:
    app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION)

    app.add_middleware(
        SessionMiddleware,
        secret_key=crypto.get_session_secret(),
        session_cookie="hac_session",
        same_site="lax",
        https_only=config.HTTPS_ONLY,  # Secure cookie when behind a TLS proxy (PANEL_HTTPS_ONLY=1)
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
    app.include_router(groups_router.router)
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
            _ensure_default_schedules(db)
            from .models import Job, Setting

            row = db.get(Setting, "instance_name")
            set_instance_name(row.value if row else None)
            ar = db.get(Setting, "auto_refresh_seconds")
            set_auto_refresh_seconds(ar.value if ar else None)
            # Reconcile jobs orphaned by a previous process: the in-memory queue
            # and runtimes are gone after a restart, so anything left running or
            # queued can never complete — mark it failed so history is truthful.
            from sqlalchemy import update

            db.execute(
                update(Job)
                .where(Job.status.in_(("running", "queued")))
                .values(status="failed", pid=None)
            )
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
