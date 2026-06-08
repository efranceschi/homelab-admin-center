"""FastAPI application factory for HomeLab Admin Center (hac)."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import config, crypto
from .auth import install_redirect_handler
from .console import CONSOLE_RUN_ROOT
from .console import manager as console_manager
from .db import init_db, session_scope
from .jobs import PanelRestarting
from .jobs import manager as job_manager
from .plugins import registry, sync_to_db
from .templating import render
from .routers import auth as auth_router
from .routers import dashboard, hosts
from .routers import credentials as credentials_router
from .routers import groups as groups_router
from .routers import jobs as jobs_router
from .routers import plugins as plugins_router
from .routers import console as console_router
from .routers import power as power_router
from .routers import schedules as schedules_router
from .routers import settings as settings_router
from .scheduler import manager as scheduler_manager
from .templating import set_auto_refresh_seconds, set_instance_name


def _ensure_default_schedules(db) -> None:
    """Seed a daily drift-check schedule once, so config state stays fresh.

    Guarded by a Setting flag: if the user later edits, disables, or deletes the
    schedule, we never recreate it. ``created_by`` is nullable (system-owned).
    """
    from .models import Schedule, Setting

    if db.get(Setting, "drift_schedule_seeded") is None:
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

    # A daily network scan, so registered subnets are swept out of the box. A
    # no-op until the user adds a subnet; same one-shot guard as drift-check.
    if db.get(Setting, "netscan_schedule_seeded") is None:
        db.add(Schedule(
            name="network-scan",
            kind="daily",
            daily_time="04:45",
            action="network_scan",
            enabled=True,
            created_by=None,
        ))
        db.add(Setting(key="netscan_schedule_seeded", value="1", value_type="str"))


def _write_pidfile() -> None:
    """Record the main process PID so operators can `kill -HUP $(cat hac.pid)`.
    Mirrors the scheduler's pidfile handling (same RUN_DIRS)."""
    config.RUN_DIRS.mkdir(parents=True, exist_ok=True)
    config.HAC_PIDFILE.write_text(str(os.getpid()))


def _install_sighup_handler() -> None:
    """Install the graceful-restart SIGHUP handler on the MAIN process only.

    Registered on the asyncio loop so the callback runs on the loop (not inside
    the C signal handler), making it safe to touch the JobManager and schedule a
    task. The scheduler child and job subprocesses must NOT run this: the
    scheduler child resets SIGHUP to SIG_DFL in run_scheduler(), and job
    subprocesses are spawned via create_subprocess_exec which exec()s a new
    program (signal dispositions don't carry across exec), so neither inherits
    this handler — a HUP to a child can never trigger an app restart loop."""
    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGHUP, _graceful_restart)
    except (NotImplementedError, RuntimeError):
        # add_signal_handler is unavailable (non-Unix / no running loop): fall
        # back to signal.signal, scheduling the drain back onto the loop so no
        # heavy work runs inside the signal handler itself.
        def _handler(_sig, _frm):
            loop.call_soon_threadsafe(_graceful_restart)

        signal.signal(signal.SIGHUP, _handler)


def _graceful_restart() -> None:
    """SIGHUP handler body (runs on the loop). Consoles are interactive and
    unbounded, so they NEVER participate in the job drain: kill them immediately
    (each child's death tears down its WebSocket bridge), then drain jobs as
    before. A second SIGHUP still reaches ``job_manager.begin_drain`` to force an
    immediate restart."""
    console_manager.begin_drain()
    job_manager.begin_drain()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown (replaces the deprecated on_event hooks)."""
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
    # Console sessions are purely in-memory; after a restart none survive, so any
    # leftover per-session run dir is stale key material — sweep the whole tree.
    shutil.rmtree(CONSOLE_RUN_ROOT, ignore_errors=True)
    # Scheduling is owned by the app via a separate child process (no cron).
    # Suppressed under tests/DAST (PANEL_DISABLE_SCHEDULER=1) so booting the
    # app never spawns the scheduler child.
    if not config._envflag("PANEL_DISABLE_SCHEDULER"):
        scheduler_manager.ensure_running()

    # Main-process-only: pidfile + SIGHUP graceful-restart handler.
    _write_pidfile()
    _install_sighup_handler()

    yield

    config.HAC_PIDFILE.unlink(missing_ok=True)
    scheduler_manager.stop()


def create_app() -> FastAPI:
    app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, lifespan=lifespan)

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

    @app.exception_handler(PanelRestarting)
    def _panel_restarting(request: Request, exc: PanelRestarting):
        """A submission hit a draining panel: refuse with HTTP 503. JSON for
        fetch/XHR callers (incl. the scheduler's on-demand run), an HTML notice
        otherwise."""
        msg = str(exc) or "Panel is restarting; new jobs are refused."
        accept = request.headers.get("accept", "")
        wants_json = (
            request.headers.get("x-requested-with") == "fetch"
            or "application/json" in accept
        )
        if wants_json:
            return JSONResponse({"detail": msg}, status_code=503)
        return render(request, "error.html", message=msg, status_code=503)

    app.include_router(auth_router.router)
    app.include_router(dashboard.router)
    app.include_router(hosts.router)
    app.include_router(credentials_router.router)
    app.include_router(groups_router.router)
    app.include_router(plugins_router.router)
    app.include_router(jobs_router.router)
    app.include_router(console_router.router)
    app.include_router(power_router.router)
    app.include_router(schedules_router.router)
    app.include_router(settings_router.router)

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
