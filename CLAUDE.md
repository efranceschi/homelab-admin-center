# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

`lxc-ansible` — Ansible roles/playbooks for managing Proxmox LXC containers, plus
the **HomeLab Admin Center (hac)** web panel under `webpanel/` (FastAPI, served by
`uvicorn` via the `hac` systemd service on port 8910).

## Working rules

### Restart the panel after every change

The `hac` service runs `uvicorn` **without** `--reload`, so it keeps serving the
old code until restarted. Whenever you change anything the running panel loads
(Python under `webpanel/app/`, templates, static assets, plugin definitions),
restart the service immediately so the change takes effect:

```bash
systemctl restart hac
```

Verify after restarting (e.g. `systemctl is-active hac`, or check the live routes
via `curl -s http://127.0.0.1:8910/openapi.json`).

### Commit when a task is finished

Whenever you finish a piece of work, make a git commit. **Write commit messages
in English**, even though we converse in Portuguese. Keep the message focused on
what changed and why.
