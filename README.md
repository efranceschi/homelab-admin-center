# lxc-ansible

Idempotent automation for the LXC containers of a Proxmox node — driven from the host via
`pct exec` (no SSH bootstrap required). Recurring runs are scheduled by the web panel's own
scheduler (a child process); the CLI can also be run manually or from any external trigger.

This repository contains two things:

1. **The Ansible framework** (`roles/`, `inventory/`, `playbooks/`, `run.sh`) — the
   production automation that standardizes every running container.
2. **The web control panel** (`webpanel/`) — an optional, modern web UI to configure,
   monitor, and apply the same Ansible functionalities visually, and to manage hosts over
   **Local**, **SSH**, and **Proxmox** connections.

> The previous Portuguese README is preserved as [`README.pt.md`](./README.pt.md).

---

## What it does (per run)

| Functionality | Role | Tags | Summary |
|---------------|------|------|---------|
| **timezone**   | `common_timezone` | `timezone`     | Normalizes the system timezone (default `America/Sao_Paulo`). |
| **ssh baseline** | `ssh_baseline`  | `ssh`          | Installs/standardizes `sshd` via a drop-in and distributes the host public key. |
| **Authentik SSSD** | `authentik_sssd` | `auth`, `sssd` | Integrates the Authentik LDAP outpost via SSSD, with local fallback. Gated by `authentik_sssd_enabled`. |
| **apt maintenance** | `apt_maintenance` | `apt`, `updates` | `update` + `full-upgrade` + `autoremove`/`autoclean` (never reboots). |

---

## Architecture

- **Dynamic inventory** — `inventory/pct.py` lists only *running* containers via `pct list`,
  with no credentials. Each host gets `ansible_connection=pct`, `ansible_host=<VMID>`, and is
  grouped by `ostype` (`ostype_ubuntu`, `ostype_debian`, …).
- **Custom connection plugin** — `plugins/connection/pct.py` runs tasks through
  `pct exec` / `pct push` / `pct pull`. No SSH; works in privileged and unprivileged
  containers; runs as root on the Proxmox node.
- **Runtime** — a persistent virtualenv (`.venv`) recreated only when `requirements.txt`
  changes; collections vendored under `collections/`.
- **Entrypoint** — `run.sh` (flock-guarded against overlapping runs) for manual/CLI use.
  Recurring execution is handled by the web panel's scheduler child process (see below),
  which shares the same flock so panel, scheduler, and CLI runs never overlap.
- **Secrets** — non-secret variables in `inventory/group_vars/all/main.yml`; encrypted
  secrets in `inventory/group_vars/all/vault.yml` (Ansible Vault). The vault password lives
  at `/etc/lxc-ansible/vault-pass` (mode `0600`, never versioned).

---

## Requirements

- A Proxmox VE node (the project runs **on the host** as `root`, because it shells out to `pct`).
- Python 3 (`python3 -m venv`).
- The Ansible Vault password file at `/etc/lxc-ansible/vault-pass` (only needed if you use the
  encrypted `vault.yml`).

---

## Usage (CLI)

```bash
# Apply all roles to every running container (creates/updates the venv as needed)
/opt/lxc-ansible/run.sh

# Dry-run with diff (no changes applied)
/opt/lxc-ansible/run.sh --check --diff

# Only a subset of functionalities
/opt/lxc-ansible/run.sh --tags timezone,apt

# Ad-hoc
cd /opt/lxc-ansible && source .venv/bin/activate
ansible all -m ping
```

Any extra arguments to `run.sh` are passed straight through to `ansible-playbook`.

---

## Web control panel — HomeLab Admin Center (`hac`)

`webpanel/` contains **HomeLab Admin Center** (short name `hac`), a self-contained FastAPI
application that provides a visual dashboard over the same Ansible roles. It is **additive**:
it never edits `ansible.cfg`, `run.sh`, `site.yml`, the roles, the connection plugin, or
`group_vars`, so the existing CLI/automation keeps working unchanged.

### Features

- Username/password login with a first-run admin setup (no default password).
- SQLite database; secrets (SSH keys, Proxmox tokens, vault password) encrypted at rest.
- **Modular plugin system** — each functionality is a folder under `webpanel/plugins/`
  (`timezone`, `ssh`, `sssd`, `apt`). New functionality can be added in the future by simply
  dropping a plugin folder into that directory; the panel auto-discovers it on startup.
- **Host management** across three connection types: **Local**, **SSH**, and **Proxmox**
  (the existing `pct` path).
- **Configure** plugin variables through auto-generated forms.
- **Apply** or **dry-run** (`--check --diff`) selected plugins against selected hosts, with
  **live log streaming** (Server-Sent Events).
- **Dashboard** — host inventory, last-run status, reboot-required flags, drift detection,
  recent jobs.
- **Scheduling without cron** — recurring runs (daily at a time, or every N minutes) are
  executed by the application's own **scheduler child process**, managed from the UI
  (start/stop/restart). It shares the run flock so scheduled and manual runs never overlap.
- **Self-update / self-restart** — Settings has **Update & restart** (git pull + reinstall
  deps + restart) and **Restart** buttons. These work when the panel runs as the `hac`
  systemd service (`Restart=always`).

### Install & run

Foreground (development):

```bash
cd /opt/lxc-ansible/webpanel
./run-panel.sh        # creates webpanel/.venv-web, installs requirements-web.txt, starts uvicorn
```

As a managed service (production), install the `hac` systemd unit:

```bash
cd /opt/lxc-ansible/webpanel
sudo ./install-service.sh     # installs, enables, and starts hac.service
systemctl status hac
journalctl -u hac -f          # live logs
```

Then open `http://<host>:8910`. On first visit you'll be sent to `/setup` to create the
admin account.

> The panel uses its **own** virtualenv (`webpanel/.venv-web`) and `requirements-web.txt`,
> separate from the Ansible `requirements.txt`, so it never disturbs the automation venv. It
> invokes the project's existing `ansible-playbook` (from `.venv`) for actual runs.

### Connection types

| Type | How it connects | Notes |
|------|-----------------|-------|
| **Local** | `ansible_connection=local` | Runs against the panel host itself. |
| **SSH** | `ansible_connection=ssh` | Uses an SSH credential (key) stored encrypted in the panel DB. |
| **Proxmox** | `ansible_connection=pct` | Reuses `plugins/connection/pct.py`; targets a container VMID. Requires running on the Proxmox node as root. |

### Plugin layout

```
webpanel/plugins/<id>/
├── plugin.yml         # manifest: id, name, ansible_role, tags, supported_connections, …
└── form.schema.yml    # UI form fields mapped to the role's variables
```

A plugin references an existing role in `roles/` by name (`ansible_role:`). Future
third-party plugins may instead bundle their own `role/` directory inside the plugin folder;
the loader supports both.

---

## Authentik (SSSD) integration

The `authentik_sssd` role integrates containers with an Authentik LDAP outpost over verified
LDAPS, with local fallback (so root/local users always keep priority). Full operational
details — provider/outpost setup, certificate pinning, the `ldap-bind` service account, and
how to edit credentials — are documented in [`README.pt.md`](./README.pt.md).

To edit credentials/parameters:

```bash
cd /opt/lxc-ansible && source .venv/bin/activate
ansible-vault edit inventory/group_vars/all/vault.yml   # URI, base DN, bind DN/password
# object classes / attributes / TLS mode -> inventory/group_vars/all/main.yml
./run.sh --check --diff --tags auth                     # validate
```

---

## Security notes

- The automation and the web panel both run as **root on the Proxmox node** (required for
  `pct`). The panel binds to `0.0.0.0:8910` by default for LAN access — because it runs as
  root, restrict access at the firewall layer or front it with an authenticated reverse
  proxy, and do not expose it to untrusted networks. Override the bind with the
  `PANEL_HOST`/`PANEL_PORT` env vars (or `systemctl edit hac`).
- `inventory/group_vars/all/vault.yml` is safe to commit (encrypted). The vault password
  (`/etc/lxc-ansible/vault-pass`) and the panel master key (`/etc/lxc-ansible/panel.key`) must
  **never** be versioned — both are in `.gitignore`.
- The panel stores SSH keys, Proxmox tokens, and the vault password encrypted with a
  host-local master key.

---

## Versioning

```bash
cd /opt/lxc-ansible && git init && git add -A && git commit -m "baseline lxc-ansible"
```

`.gitignore` already excludes the venvs, vendored collections, logs, the vault password, the
panel master key, the panel database, and per-job run directories.
