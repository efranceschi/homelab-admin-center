# Functional Spec — SIGHUP-triggered graceful restart

Status: approved for implementation. Open items in §6 are resolved.

## 1. Summary

Install a `SIGHUP` handler on the **main panel process** (the single-worker
uvicorn process that hosts the in-memory `JobManager`). On `SIGHUP`, the panel
performs a **graceful drain** of running jobs and then restarts via the existing
self-restart path (`os._exit(0)` under systemd, which respawns it). This adds a
sudo-free, HTTP-free restart trigger alongside the existing
`POST /settings/system/restart` endpoint.

## 2. Motivation

A signal-based restart works without auth/CSRF and without the HTTP stack being
healthy. It is a clean integration point for deploy scripts and operators
(`kill -HUP $(cat run_dirs/hac.pid)`), complementing — not replacing — the current
restart mechanisms.

## 3. Decisions (from interview)

| Decision | Outcome |
|---|---|
| Signal target | Main uvicorn process (not job subprocesses) |
| In-flight jobs | Graceful drain — wait for `running` jobs to finish |
| Queue handling | Freeze the queue — start no new queued jobs during drain |
| New submissions during drain | Refuse with explicit error |
| Drain timeout | Bounded — on expiry, force immediate restart |
| Visibility | Reflect "restarting/draining" in UI and health |
| PID discovery | App writes a pidfile |
| Relationship to HTTP restart | Coexist — reuse `request_restart()`, leave the endpoint and `restart.sh` unchanged |

## 4. Behavior

### 4.1 Trigger
- The main process installs a `signal.SIGHUP` handler at startup
  (`app/main.py` lifespan / startup).
- The handler is registered only in the main process. Job subprocesses and the
  scheduler child must **not** inherit a HUP-restart handler — reset to
  `SIG_DFL` in children, or install after the fork points. This must be verified
  explicitly: child subprocesses currently inherit the parent's handlers.

### 4.2 Drain sequence (on first SIGHUP)
1. Set an internal `draining` flag in the `JobManager`.
2. **Freeze the queue**: `_dispatch()` stops promoting `queued` → `running`.
   Queued jobs are left as-is (in-memory; they become `failed` on next startup,
   consistent with the existing crash-recovery in `app/main.py`).
3. **Refuse new submissions**: `start_job()` / `manager.submit()` raise a typed
   error (`PanelRestarting`) → HTTP `503` with a clear message; the scheduler
   treats it as a transient rejection and logs/skips.
4. **Wait** for all `running` jobs (active subprocesses) to exit naturally, up to
   the drain timeout.
5. When the active set reaches zero, call `system.request_restart(delay=0)` →
   `os._exit(0)`; systemd respawns.

### 4.3 Timeout
- Bounded wait of `restart_drain_timeout_seconds` (default **300s**, configurable
  via the `Setting` table like `max_concurrent_jobs`).
- On expiry: force the restart immediately (`os._exit(0)`), aborting
  still-running children. On the next startup those jobs are marked `failed` by
  the existing recovery code. Log which job IDs were force-aborted.

### 4.4 Repeated signals
- A **second `SIGHUP` while already draining** escalates to an immediate forced
  restart (operator's "don't wait" escape hatch). The handler is re-entrant-safe:
  the first HUP records the draining state, a subsequent HUP short-circuits to
  the forced path.

### 4.5 PID file
- The main process writes its PID to `config.RUN_DIRS / "hac.pid"`
  (`/opt/hac/webpanel/run_dirs/hac.pid`) at startup and removes it on clean
  shutdown, mirroring the scheduler's `scheduler.pid`. `RUN_DIRS` is already
  created by `config.ensure_dirs()`.
- Enables `kill -HUP $(cat /opt/hac/webpanel/run_dirs/hac.pid)`.

### 4.6 Visibility
- **Health**: the health/status payload exposes a state field reporting
  `draining` while a drain is in progress, so external checks and the UI can
  react.
- **UI**: while draining, show a "panel restarting…" banner and reflect that
  submissions are temporarily refused. The existing `restart.sh`
  wait-for-comeback loop continues to work unchanged.

## 5. Out of scope
- No change to job-cancellation signals (`SIGTERM`/`SIGKILL`), the scheduler's
  `SIGTERM`/`SIGINT` handling, or the HTTP restart endpoint and `restart.sh`.
- `SIGHUP` does **not** mean "reload config" here — it means "graceful restart",
  per the request.

## 6. Resolved open items
1. **Drain timeout default** — `restart_drain_timeout_seconds` defaults to
   **300s** (5 min). Ansible playbook jobs run for minutes; 300s avoids routine
   force-aborts while still guaranteeing the restart eventually happens.
   Configurable via the `Setting` table; a value of `0` means "no timeout"
   (wait indefinitely) for operators who prefer it.
2. **Second-HUP-during-drain** — escalates to a forced immediate restart
   (§4.4), giving operators an explicit way to skip the drain.
3. **Pidfile path/permissions** — `config.RUN_DIRS / "hac.pid"` (i.e.
   `/opt/hac/webpanel/run_dirs/hac.pid`), written by the app user. No root
   needed; `RUN_DIRS` already exists and is used for `scheduler.pid`.

## 7. Affected components (implementation map)
- `app/main.py` — install/reset SIGHUP handler; write/remove pidfile; existing
  startup job-recovery already handles abandoned `running`/`queued` jobs.
- `app/jobs.py` — `draining` flag; freeze `_dispatch()`; refuse `submit()`;
  drain-wait + timeout → `request_restart`; second-HUP forced path.
- `app/ansible_layer/service.py` — `start_job()` surfaces the refusal error.
- `app/system.py` — reuse `request_restart()`; add a `force` path for
  timeout/second-HUP.
- `app/routers/settings.py` (or health router) — expose `draining` state; add
  the `restart_drain_timeout_seconds` setting (validation like
  `max_concurrent_jobs`).
- `app/config.py` — pidfile path constant alongside `RUN_DIRS`.
- Templates — UI "restarting" banner.
