# Plugin Ideas & Technical Specification

This document is the roadmap and the implementation contract for **web-panel
plugins**. Part 1 specifies how a plugin is built and wired so future plugins
stay consistent and safe. Part 2 is a prioritized backlog of candidate plugins.
Part 3 tracks core (non-plugin) panel features on the roadmap.

A "plugin" is a thin manifest + form that maps UI fields to the variables of an
**idempotent Ansible role**. The panel runs roles over Proxmox LXC containers
(via the vendored `pct` connection), selecting functionality with `--tags`,
targeting hosts with `--limit`, and layering configuration with `--extra-vars`.

---

## Part 1 — Technical specification

### 1.1 Architecture recap (how a run is assembled)

For each panel job the orchestrator (`webpanel/app/ansible_layer/`) does:

1. Build a per-job inventory (`run_dirs/job-<id>/inventory.json`).
2. Resolve every selected plugin's form values into `extra-vars.yml`
   (`vars_builder.build_extra_vars`) and its secrets into a vault-encrypted
   `extra-vars-secret.yml`.
3. Build the command (`runner.build_command`):

   ```
   ansible-playbook playbooks/webpanel.yml -i <inventory> \
     --tags <plugin tags> --limit <hosts> [--check --diff] \
     -e @extra-vars.yml -e @extra-vars-secret.yml
   ```

   - **check (dry run)** adds `--check --diff`; **apply** omits them.
   - extra-vars have the **highest precedence**, layered on top of any
     `group_vars` (which the panel never edits).

`playbooks/webpanel.yml` is the parametric playbook the panel always runs. It
mirrors `site.yml` (the cron path) but is decoupled from it. **A plugin's role
must be registered in `webpanel.yml` (and usually `site.yml`) — see §1.6.**

### 1.2 Plugin file layout

```
webpanel/plugins/<id>/
  plugin.yml          # manifest
  form.schema.yml     # UI fields -> role variables
roles/<ansible_role>/ # the idempotent role the plugin drives
```

Plugins are discovered on disk and synced to the DB by the registry
(`webpanel/app/plugins.py`); admins can re-scan from **Settings → Reload
plugins**. There is no build step.

### 1.3 `plugin.yml` manifest

| Key | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Stable slug; matches the directory name. |
| `name` | string | yes | Display name in the UI. |
| `version` | string | yes | SemVer string, informational. |
| `description` | string | yes | One line shown on the Plugins page. |
| `ansible_role` | string | yes | Role under `roles/`. If the role is missing on disk the plugin becomes "tags-only" (the run still passes `--tags`). |
| `tags` | list[str] | yes | Ansible tags this plugin selects. Must match the `tags:` on the role entry in the playbook. |
| `enable_var` | string \| null | no | If set, the role is gated by `when: <enable_var> | default(false) | bool`. Use for opt-in integrations. `null` = always runs when its tag is selected. |
| `supported_connections` | list[str] | no | Subset of `[local, ssh, proxmox]`. Defaults to all three. Use to hide a plugin from connection types it can't support. |
| `order` | int | no | Execution + UI ordering (ascending). Defaults to 100. |
| `supports_check_mode` | bool | no | Defaults to `true`. Must be honestly `true` for the plugin to appear in drift checks (`--check`). |

Reference: existing orders are `timezone:10`, `ssh:20`, `sssd:30`, `apt:40`.
Leave gaps of 10 and group new plugins by category (see §2).

### 1.4 `form.schema.yml`

Two lists: `fields` (non-secret) and `secret_fields` (routed to the vault).

```yaml
fields:
  - var: my_role_enabled      # the EXACT Ansible variable name
    label: "Enable feature"    # UI label
    type: bool                 # see field types below
    default: false             # optional; seeds the form + role default
    help: "Shown under the field."   # optional
  - var: my_role_mode
    label: "Mode"
    type: select
    choices: ["a", "b", "c"]   # required for type: select
    default: "a"
secret_fields:
  - var: my_role_token
    label: "API token"
    type: password             # or textarea for multi-line (PEM, keys)
```

**Field types**

| `type` | UI control | Stored / passed as |
|---|---|---|
| `string` (default) | text input | string |
| `bool` | switch | `true`/`false` |
| `yesno` | switch | `"yes"`/`"no"` (for roles that expect yes/no strings) |
| `select` | dropdown (needs `choices`) | the chosen string |
| `textarea` | multi-line text | string |
| `password` | masked input (secret only) | string, via vault |

- `var` **must** equal the role variable name — that is the entire contract.
- Non-secret fields → `extra-vars.yml`. Secret fields → a credential reference
  (`__secrets__: {var: credential_id}`) decrypted at run time into the
  vault-encrypted `extra-vars-secret.yml`. The committed `group_vars/vault.yml`
  is never modified.
- Secret fields render with the placeholder *"(stored in the vault : keep blank
  to keep)"*; leaving them blank preserves the stored value.

### 1.5 Role requirements (MANDATORY)

The panel runs roles with **extra-vars only** and supports **dry runs**. A role
that ignores either will break in the panel. Every plugin role MUST:

1. **Be idempotent** — re-running changes nothing once converged.
2. **Provide `roles/<role>/defaults/main.yml` with a default for EVERY variable
   its tasks/templates reference.** The panel does not load `group_vars`, so any
   var without a default raises `AnsibleUndefinedVariable` mid-run. (This was the
   root cause of the `authentik_ldap_user_object_class is undefined` failure.)
3. **Be check-mode safe.** Set `supports_check_mode: true` only if true. In
   particular, **guard service restarts/handlers** that depend on a package an
   earlier task would install:

   ```yaml
   - name: restart <svc>
     ansible.builtin.service: { name: <svc>, state: restarted, enabled: true }
     when: not ansible_check_mode   # the unit may not exist yet during --check
   ```

   (This was the root cause of job #26's "Could not find the requested service
   sssd" on a `--check` run.)
4. **Be LXC/unprivileged aware.** Operations that touch the host kernel/RTC are
   often no-ops or fail in unprivileged containers — make them tolerant
   (`failed_when: false`, `changed_when: false`) as `common_timezone` does, or
   restrict the plugin via `supported_connections`.
5. **Reboot signal (optional).** If the role can leave a host needing a reboot,
   rely on the playbook's post-task that emits `"<host>: REBOOT REQUIRED ..."`;
   `results.parse_reboot` flags those hosts. Never reboot automatically.
6. **Secrets** come in as plain Ansible vars at run time (already decrypted from
   the vault). Set restrictive file modes on anything written
   (`mode: "0600"`), like the SSSD config.

### 1.6 Registering the role in the playbooks

Adding a role file is not enough — it must be wired into the play(s):

- **`playbooks/webpanel.yml`** (REQUIRED — the panel's playbook): add the role
  with its `tags`, and a `when:` if it uses `enable_var`:

  ```yaml
  roles:
    - role: my_role
      tags: [myfeature]
      when: my_role_enabled | default(false) | bool   # only if enable_var set
  ```

- **`playbooks/site.yml`** (RECOMMENDED — the daily cron path): add the same
  entry if the feature should also converge on the unattended schedule.

The `tags` here MUST match `plugin.yml`'s `tags`, or the panel's `--tags`
selection will not run the role.

### 1.7 Step-by-step: add a new plugin

1. `roles/<role>/` — write `tasks/main.yml`, `defaults/main.yml` (all vars!),
   `templates/`, `handlers/` (check-mode-guarded). Idempotent. English only.
2. `webpanel/plugins/<id>/plugin.yml` and `form.schema.yml` (vars == role vars).
3. Register the role in `playbooks/webpanel.yml` (+ `site.yml`).
4. **Settings → Reload plugins** (or restart `hack`) to pick it up.
5. Verify (§1.8).

### 1.8 Verification checklist

- [ ] `ansible-playbook playbooks/site.yml --syntax-check` passes.
- [ ] `roles/<role>/defaults/main.yml` defines every referenced variable
      (grep the tasks/templates for `{{ ... }}` and `when:` vars).
- [ ] Dry run from the panel (**check**) on one host succeeds — no
      `AnsibleUndefinedVariable`, no handler failure.
- [ ] Second run reports `changed=0` (idempotent).
- [ ] Apply on one host converges; re-check shows "updated".
- [ ] Secrets land only in the vault file, never in `extra-vars.yml`.
- [ ] All text (task names, comments, labels, help) is in English.
- [ ] Restart `hack` after editing panel code/templates (uvicorn has no reload).

### 1.9 Conventions

- Variable namespace: prefix every role var with the role/feature
  (`fail2ban_*`, `nodeexp_*`) to avoid collisions in the shared extra-vars.
- One responsibility per plugin; compose at run time by selecting several.
- `order` ranges: base/identity `10–49`, hardening `50–99`, observability
  `100–149`, networking `150–199`, app platforms `200–299`, tuning `300+`.
- Keep `supports_check_mode: true`; if a step genuinely can't dry-run, isolate
  it behind `when: not ansible_check_mode` rather than disabling check for the
  whole plugin.

---

## Part 2 — Plugin backlog (candidates)

Selection criteria: popularity in the Ansible ecosystem **and** fit with this
model (idempotent, check-mode friendly, works over `pct exec`, configurable via
form fields, no `group_vars` dependency).

Existing plugins: `timezone`, `ssh_baseline (ssh)`, `apt_maintenance (apt)`,
`authentik_sssd (sssd)`.

Legend — Priority: **P1** high ROW/low risk · **P2** valuable · **P3** later.
LXC: ✅ fine in unprivileged · ⚠️ needs privileged/nesting or care.

### Tier A — Base & hardening (complement what exists)

| Plugin | What it configures | Key form fields | Pri | LXC |
|---|---|---|---|---|
| **users** | Users, groups, sudo, per-user `authorized_keys` | users[], groups[], sudo_nopasswd, keys | P1 | ✅ |
| **ntp** | Time sync (chrony / systemd-timesyncd) | ntp_servers[], implementation | P1 | ✅ |
| **unattended_upgrades** | Automatic security patching + window | origins, auto_reboot, schedule | P1 | ✅ |
| **fail2ban** | SSH/service brute-force protection | jails, bantime, maxretry, ignoreip | P1 | ✅ |
| **packages** | Install/remove an arbitrary apt package list | install[], remove[], state | P1 | ✅ |
| **apt_repos** | Add apt repositories + signing keys | repos[], keys[] | P2 | ✅ |
| **firewall** | Ingress rules (nftables / ufw) | allow_ports[], trusted_nets[], policy | P2 | ⚠️ |

### Tier B — Observability & ops (homelab favorites)

| Plugin | What it configures | Key form fields | Pri | LXC |
|---|---|---|---|---|
| **metrics_agent** | node_exporter / Telegraf / Netdata / Beszel agent | implementation, bind_addr, port, labels | P1 | ✅ |
| **log_shipping** | promtail→Loki / vector / rsyslog forward | endpoint, labels, files[] | P2 | ✅ |
| **backup_agent** | restic / borg / proxmox-backup-client | repo, schedule, retention, secret | P2 | ✅ |

### Tier C — Networking / mesh

| Plugin | What it configures | Key form fields | Pri | LXC |
|---|---|---|---|---|
| **mesh_vpn** | Tailscale / Netbird / WireGuard | auth_key(secret), routes, hostname | P2 | ⚠️ (needs `/dev/net/tun`) |
| **dns_resolv** | systemd-resolved / static resolv.conf / `/etc/hosts` | nameservers[], search, hosts[] | P2 | ✅ |

### Tier D — Application platforms

| Plugin | What it configures | Key form fields | Pri | LXC |
|---|---|---|---|---|
| **docker** | Docker engine + compose, daemon.json | version, users[], daemon_opts | P2 | ⚠️ privileged + nesting |
| **reverse_proxy** | Caddy / Traefik / nginx | sites[], tls, upstreams | P3 | ✅ |
| **database** | PostgreSQL / MariaDB / Redis | version, databases[], users[](secret) | P3 | ✅ |

### Tier E — Tuning (small, safe)

| Plugin | What it configures | Pri | LXC |
|---|---|---|---|
| **sysctl_limits** | sysctl values + `limits.conf` | P3 | ⚠️ many sysctls are read-only in unprivileged |
| **journald** | journald size/retention caps | P3 | ✅ |
| **logrotate** | logrotate policies | P3 | ✅ |
| **motd_banner** | login banner / MOTD / hostname / `/etc/hosts` | P3 | ✅ |
| **swap_zram** | swapfile / zram | P3 | ⚠️ unprivileged can't manage host swap |

### LXC caveats (call out in `plugin.yml`/help)

- **firewall (nftables)**, **docker**, **swap/zram**, many **sysctl** keys, and
  **mesh VPNs** usually need a **privileged** container and/or *nesting* /
  `/dev/net/tun`. Validate with `--check`, restrict `supported_connections`, and
  state the prerequisite in the field `help`.
- systemd-dependent steps must skip restarts in check mode (§1.5.3).

### Suggested next three (best effort/value, low risk)

1. **users** — closes the access loop alongside `ssh_baseline` + `authentik_sssd`.
2. **metrics_agent** (node_exporter / Beszel) — immediate fleet observability.
3. **unattended_upgrades** — continuous patching; natural pair of `apt_maintenance`.

Each is idempotent, check-mode friendly, and works in unprivileged LXC.

---

## Part 3 — Panel core backlog (non-plugin)

Roadmap items for the panel itself (not Ansible plugins).

### API keys for programmatic access (future)

**Motivation.** Automation currently has to authenticate like a browser:
log in with username/password and carry a per-session CSRF token (see
`webpanel/restart.sh`, which scrapes `/login` then posts to
`/settings/system/restart`). That is brittle and couples scripts to the HTML/CSRF
flow. (The *restart* case specifically already has a credential-free path —
`kill -HUP "$(cat webpanel/run_dirs/hack.pid)"`, see
`webpanel/docs/sighup-restart.md` — so this motivation now applies to the
remaining programmatic actions, not restart.)

**Idea.** Add issuable API keys (admin-managed, scoped, revocable) accepted via
an `Authorization`/`X-API-Key` header, bypassing the session+CSRF dance for
non-browser callers. Sketch:

- Storage: hashed key (never stored plaintext), label, owner, scopes, created/
  last-used/expiry; reuse the credential-encryption patterns already in
  `webpanel/app/crypto.py`.
- Auth: a dependency alongside `current_user`/`require_admin`
  (`webpanel/app/auth.py`) that resolves a key to a principal and enforces
  scope; exempt key-authenticated requests from `verify_csrf`.
- Management UI under **Settings** (issue / revoke / list), audit-logged.
- Then simplify `restart.sh` (and any future CLI/CI tooling) to a single
  authenticated request with the key.

Pri **P2** — quality-of-life + safer automation; no plugin work involved.
