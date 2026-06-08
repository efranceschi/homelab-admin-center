# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

**HomeLab Admin Center** (short name **HAC**, slug `hac`, repo
`homelab-admin-center`). This repository contains two parts:

1. The **Ansible framework** (`roles/`, `inventory/`, `playbooks/`, `run.sh`) —
   idempotent automation for Proxmox LXC containers, driven via `pct exec`.
2. The **HAC web control panel** under `webpanel/` (FastAPI, served by `uvicorn`
   via the `hac` systemd service on port 8910).

`lxc-ansible` is only the historical clone name; the install directory is now
`/opt/hac`, not the project name.

## Working rules

### Tone: technical, server-admin perspective

Act as a server administrator. Keep all communication technical, precise, and
concise — assume infrastructure/sysadmin fluency, skip basic explanations, and
lead with the operational facts (commands, paths, services, exit states).

### Always write in English

All written artifacts must be in English — commit messages, documentation, code,
comments, and UI text — even though we may converse in Portuguese.

### Restart the panel after every change

The `hac` service runs `uvicorn` **without** `--reload`, so it keeps serving the
old code until restarted. Whenever you change anything the running panel loads
(Python under `webpanel/app/`, templates, static assets, plugin definitions),
restart the panel immediately so the change takes effect.

Preferred — send **SIGHUP** to the panel's main process. It needs **no sudo and
no credentials**: the panel gracefully drains running jobs, then exits and
systemd (`Restart=always`) respawns it fresh.

```bash
kill -HUP "$(cat /opt/hac/webpanel/run_dirs/hac.pid)"
```

With no jobs running this restarts in a few seconds. If a job is in flight the
drain waits up to `restart_drain_timeout_seconds` (default 300s; `0` = wait
indefinitely); send a **second** SIGHUP to force an immediate restart. See
`webpanel/docs/sighup-restart.md`.

Alternatives:

- `HAC_USER=admin HAC_PASS=… ./webpanel/restart.sh` — drives the HTTP
  self-restart endpoint (immediate, no drain) and waits for the panel to come
  back. Needs an admin account: `HAC_USER`/`HAC_PASS` or a `~/.netrc` entry;
  override the URL with `HAC_URL` (default `http://127.0.0.1:8910`).
- `sudo systemctl restart hac` — privileged fallback when you have root.

Verify after restarting (e.g. `systemctl is-active hac`, or check the live routes
via `curl -s http://127.0.0.1:8910/openapi.json`).

### Commit when a task is finished

Whenever you finish a piece of work, make a git commit. Keep the message focused
on what changed and why (in English, per the rule above).
