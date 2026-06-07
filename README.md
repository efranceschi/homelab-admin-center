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

## Quick install (one line)

On the Proxmox node, as root:

```bash
curl -fsSL https://raw.githubusercontent.com/efranceschi/homelab-admin-center/prod/install.sh | sudo bash
```

This pulls the **production branch (`prod`)**, installs prerequisites, clones the project
to `/opt/hac`, seeds config from the bundled examples, creates the unprivileged **`hac`**
service user with a least-privilege sudo policy, and installs + starts the **`hac`** systemd
service (running as that user). When it finishes, open `http://<host>:8910` and create the
first admin account.

Optional environment overrides:

```bash
# custom location / branch, or install without starting
HAC_INSTALL_DIR=/opt/hac HAC_BRANCH=prod HAC_NO_START=1 \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/efranceschi/homelab-admin-center/prod/install.sh)"
```

Re-running the same command updates an existing install. (You can also update from the
panel itself via **Administration → Update & restart**.)

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
  containers. `pct` requires root: run directly it calls `pct`; run by the unprivileged
  panel it transparently prefixes `sudo -n` (granted via `/etc/sudoers.d/hac`).
- **Runtime** — a persistent virtualenv (`.venv`) recreated only when `requirements.txt`
  changes; collections vendored under `collections/`.
- **Entrypoint** — `run.sh` (flock-guarded against overlapping runs) for manual/CLI use.
  Recurring execution is handled by the web panel's scheduler child process (see below),
  which shares the same flock so panel, scheduler, and CLI runs never overlap.
- **Secrets** — non-secret variables in `inventory/group_vars/all/main.yml`; encrypted
  secrets in `inventory/group_vars/all/vault.yml` (Ansible Vault). The vault password lives
  at `/etc/hac/vault-pass` (mode `0600`, never versioned).

---

## Requirements

- A Proxmox VE node (the project runs **on the host**, shelling out to `pct`). The panel runs
  as the unprivileged `hac` user and escalates to root only via `/etc/sudoers.d/hac` (see
  [Security notes](#security-notes)); the CLI `run.sh` path still runs as `root`.
- Python 3 (`python3 -m venv`).
- The Ansible Vault password file at `/etc/hac/vault-pass` (only needed if you use the
  encrypted `vault.yml`).

---

## Usage (CLI)

```bash
# Apply all roles to every running container (creates/updates the venv as needed)
/opt/hac/run.sh

# Dry-run with diff (no changes applied)
/opt/hac/run.sh --check --diff

# Only a subset of functionalities
/opt/hac/run.sh --tags timezone,apt

# Ad-hoc
cd /opt/hac && source .venv/bin/activate
ansible all -m ping
```

Any extra arguments to `run.sh` are passed straight through to `ansible-playbook`.

---

## Web control panel — HomeLab Admin Center (`hac`)

`webpanel/` contains **HomeLab Admin Center** (short name **HAC**; technical slug `hac`),
a self-contained FastAPI
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
cd /opt/hac/webpanel
./run-panel.sh        # creates webpanel/.venv-web, installs requirements-web.txt, starts uvicorn
```

As a managed service (production), install the `hac` systemd unit:

```bash
cd /opt/hac/webpanel
sudo ./install-service.sh     # installs, enables, and starts hac.service
systemctl status hac
journalctl -u hac -f          # live logs
```

Then open `http://<host>:8910`. On first visit you'll be sent to `/setup` to create the
admin account.

> The panel uses its **own** virtualenv (`webpanel/.venv-web`) and `requirements-web.txt`,
> separate from the Ansible `requirements.txt`, so it never disturbs the automation venv. It
> invokes the project's existing `ansible-playbook` (from `.venv`) for actual runs.

### Behind a reverse proxy (TLS)

To serve the panel over HTTPS from a TLS-terminating reverse proxy at its own
hostname (e.g. `https://hac.example.com/`), set two environment variables (in
`systemctl edit hac`, or the shell when running `./run-panel.sh`):

| Variable | Value | Purpose |
|----------|-------|---------|
| `PANEL_HTTPS_ONLY` | `1` | Marks the session cookie `Secure` so it's only sent over HTTPS. |
| `PANEL_FORWARDED_ALLOW_IPS` | proxy IP, list, or `*` | Trust `X-Forwarded-Proto`/`X-Forwarded-For` from the proxy so the app sees the real client IP and the original `https` scheme. Defaults to `127.0.0.1` (proxy on the same host). |

`run-panel.sh` already launches uvicorn with `--proxy-headers`, so the original
scheme/client IP are honoured for whichever IPs you trust above. Example Nginx
server block:

```nginx
server {
    listen 443 ssl;
    server_name hac.example.com;
    # ssl_certificate / ssl_certificate_key ...

    location / {
        proxy_pass http://127.0.0.1:8910;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

> Only enable `PANEL_HTTPS_ONLY=1` once the panel is actually reached over HTTPS —
> over plain `http://<host>:8910` the browser would drop the `Secure` cookie and
> login would silently fail. This setup assumes the proxy serves the panel at the
> **root** path; subpath mounting (`/hac/`) would need additional URL-prefix work.

### Connection types

| Type | How it connects | Notes |
|------|-----------------|-------|
| **Local** | `ansible_connection=local` | Runs against the panel host (the Proxmox node) itself. Tasks escalate via ansible `become` (sudo), since the panel runs unprivileged. |
| **SSH** | `ansible_connection=ssh` | Uses an SSH credential (key) stored encrypted in the panel DB. |
| **Proxmox** | `ansible_connection=pct` | Reuses `plugins/connection/pct.py`; targets a container VMID. Runs on the Proxmox node; `pct` is invoked via `sudo` (granted to the `hac` user in `/etc/sudoers.d/hac`). |

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
cd /opt/hac && source .venv/bin/activate
ansible-vault edit inventory/group_vars/all/vault.yml   # URI, base DN, bind DN/password
# object classes / attributes / TLS mode -> inventory/group_vars/all/main.yml
./run.sh --check --diff --tags auth                     # validate
```

---

## Security notes

- The web panel runs as the **unprivileged `hac` system user**, not root. The installer
  creates the `hac` user/group, owns the checkout (`chown -R hac:hac /opt/hac`), and installs
  a **least-privilege sudo policy** at `/etc/sudoers.d/hac` (mode `0440`, validated with
  `visudo -cf`). The panel obtains root **only** for these operations:
  - `pct` — Proxmox container management (host picker + the `pct` connection plugin).
  - `/bin/sh` via ansible `become` — applying roles to the Proxmox **node itself** (the
    *Local* connection: timezone/apt/ssh/sssd). Drop this rule if you don't manage the node
    from the panel, for a tighter footprint.

  > The in-app **Restart** / **Update & restart** needs **no** sudo rule: the panel owns its
  > own process, so it simply exits and systemd (`Restart=always`) respawns it fresh.
  > Self-update (`git pull` + `pip install`) also needs no sudo — it runs as the `hac` owner
  > of the checkout and venvs.
  >
  > `pct exec` and the `become` grant are each root-equivalent in effect. The hard win is
  > that the **network-facing process, its database, secrets, and files are no longer root** —
  > the escalation surface is reduced to those two audited sudoers entries. The unit must
  > **not** set `NoNewPrivileges=yes` (it would block sudo).

- The panel binds to `0.0.0.0:8910` by default for LAN access. It has no built-in IP
  allowlist, so restrict access at the firewall layer or front it with an authenticated
  reverse proxy, and do not expose it to untrusted networks. Override the bind with the
  `PANEL_HOST`/`PANEL_PORT` env vars (or `systemctl edit hac`).

- To develop as the service user: `sudo -u hac -i` (the account is password-locked but has a
  shell and owns `/opt/hac`).

- The CLI entrypoint `run.sh` (the daily/manual Ansible path) still expects to run as root;
  the panel and `run.sh` share the same advisory lock (`/run/hac.lock`, pre-created `hac:hac`
  by a `tmpfiles.d` entry) so their runs never overlap.
- `inventory/group_vars/all/vault.yml` is safe to commit (encrypted). The vault password
  (`/etc/hac/vault-pass`) and the panel master key (`/etc/hac/panel.key`) must
  **never** be versioned — both are in `.gitignore`.
- The panel stores SSH keys, Proxmox tokens, and the vault password encrypted with a
  host-local master key.

---

## Versioning

`.gitignore` already excludes the venvs, vendored collections, logs, the vault password, the
panel master key, the panel database, and per-job run directories.

### Branches

- **`main`** — default / development branch (latest work).
- **`prod`** — production / stable branch. The one-line installer and the in-app
  **Update & restart** pull from `prod` by default.

Promote a tested `main` to production with a fast-forward:

```bash
git checkout prod && git merge --ff-only main && git push origin prod
git checkout main
```
