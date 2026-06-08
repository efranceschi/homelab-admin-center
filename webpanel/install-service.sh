#!/usr/bin/env bash
# ============================================================================
# Install (or update) the `hack` systemd service so Homelab Admin and Control
# Kernel starts on boot and can be managed with systemctl. Run as root.
#
#   sudo ./install-service.sh          # install/update + enable + (re)start
#   sudo ./install-service.sh --no-start
#
# Idempotent: rerunning converges to the same state. The unit file is only
# rewritten (and the daemon reloaded) when its rendered content actually
# changes; the service is enabled/started only when not already so.
# ============================================================================
set -euo pipefail

PANEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANSIBLE_ROOT="$(dirname "${PANEL_DIR}")"
UNIT_SRC="${PANEL_DIR}/systemd/hack.service"
UNIT_DST="/etc/systemd/system/hack.service"
SUDOERS_SRC="${PANEL_DIR}/systemd/hack.sudoers"
SUDOERS_DST="/etc/sudoers.d/hack"
TMPFILES_SRC="${PANEL_DIR}/systemd/hack.tmpfiles"
TMPFILES_DST="/etc/tmpfiles.d/hack.conf"
HACK_USER="hack"
HACK_GROUP="hack"

say() { echo -e "\033[1;36m[H.A.C.K.]\033[0m $*"; }
die() { echo -e "\033[1;31m[H.A.C.K.] $*\033[0m" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "This installer must run as root."
[[ -f "${UNIT_SRC}" ]] || die "Unit template not found: ${UNIT_SRC}"

NO_START=0
[[ "${1:-}" == "--no-start" ]] && NO_START=1

# --- prerequisites ----------------------------------------------------------
# run-panel.sh builds a venv on first start; python3 + the venv module are required.
command -v python3 >/dev/null 2>&1 || die "python3 is required but not found."
python3 -c 'import venv' >/dev/null 2>&1 \
    || die "python3 venv module missing — install it (e.g. apt-get install -y python3-venv)."

# The panel runs unprivileged and escalates via sudo; the sudo package (which
# also provides visudo for policy validation) must be present.
if ! command -v visudo >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        say "installing prerequisite: sudo"
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends sudo >/dev/null
    fi
    command -v visudo >/dev/null 2>&1 \
        || die "sudo/visudo not found — install the 'sudo' package (apt-get install -y sudo)."
fi

chmod +x "${PANEL_DIR}/run-panel.sh" "${PANEL_DIR}/install-service.sh"

# --- service account (least privilege) --------------------------------------
# The panel runs as an unprivileged system user; root is obtained only via
# /etc/sudoers.d/hack. Home is the checkout (/opt/hack) so `sudo -u hack -i`
# drops you straight into the project for development; the account is
# password-locked (no direct login). hack's home dotfiles/caches (.bashrc,
# .ansible, ...) are git-ignored so they never pollute the tree.
if ! getent group "${HACK_GROUP}" >/dev/null; then
    say "creating group ${HACK_GROUP}"
    groupadd --system "${HACK_GROUP}"
fi
if ! getent passwd "${HACK_USER}" >/dev/null; then
    say "creating user ${HACK_USER}"
    useradd --system --gid "${HACK_GROUP}" --home-dir "${ANSIBLE_ROOT}" \
        --shell /bin/bash "${HACK_USER}"
fi

# --- least-privilege sudoers (validate before installing) -------------------
RENDERED_SUDO="$(mktemp)"
cp "${SUDOERS_SRC}" "${RENDERED_SUDO}"
if visudo -cf "${RENDERED_SUDO}" >/dev/null; then
    if [[ ! -f "${SUDOERS_DST}" ]] || ! cmp -s "${RENDERED_SUDO}" "${SUDOERS_DST}"; then
        say "installing sudoers policy -> ${SUDOERS_DST}"
        install -m 0440 "${RENDERED_SUDO}" "${SUDOERS_DST}"
    fi
else
    die "sudoers template failed validation: ${SUDOERS_SRC}"
fi

# --- tmpfiles: pre-create the shared run lock owned by hack ------------------
install -m 0644 "${TMPFILES_SRC}" "${TMPFILES_DST}"
systemd-tmpfiles --create "${TMPFILES_DST}" >/dev/null 2>&1 || true

# --- writable runtime dirs the unit/app expect, owned by the service user ----
# /var/lib/hack and /var/log/hack are also managed by StateDirectory=/LogsDirectory=
# at start, but create them up front so a pre-start CLI run.sh works too.
install -d -o "${HACK_USER}" -g "${HACK_GROUP}" -m 0700 /etc/hack      # master key, session secret, vault-pass
install -d -o "${HACK_USER}" -g "${HACK_GROUP}" -m 0700 /var/lib/hack  # sqlite DB
install -d -o "${HACK_USER}" -g "${HACK_GROUP}" -m 0755 /var/log/hack  # job/run logs
# Re-own EXISTING contents too (a prior root run leaves panel.key / panel.session
# / panel.sqlite3 at 0600 root:root, which the hack user could not read).
chown -R "${HACK_USER}:${HACK_GROUP}" /etc/hack /var/lib/hack /var/log/hack

# --- own the checkout so the service (and `sudo -u hack` dev) can read/write --
say "setting ownership of ${ANSIBLE_ROOT} -> ${HACK_USER}:${HACK_GROUP}"
chown -R "${HACK_USER}:${HACK_GROUP}" "${ANSIBLE_ROOT}"

# --- remove the legacy cron entry (scheduling is owned by the app) ----------
for legacy in /etc/cron.d/lxc-ansible /etc/cron.d/lxc-ansible.disabled; do
    if [[ -e "${legacy}" ]]; then
        say "removing legacy cron entry ${legacy}"
        rm -f "${legacy}"
    fi
done

# --- render the unit with the ACTUAL install paths --------------------------
# The template ships with /opt/hack defaults; rewrite them so the unit
# works at any checkout location. Data paths (/var/lib, /etc, /run, /var/log)
# are intentionally left untouched.
RENDERED="$(mktemp)"
trap 'rm -f "${RENDERED}" "${RENDERED_SUDO}"' EXIT
sed -e "s#/opt/hack/webpanel#${PANEL_DIR}#g" \
    -e "s#WorkingDirectory=/opt/hack#WorkingDirectory=${ANSIBLE_ROOT}#g" \
    "${UNIT_SRC}" > "${RENDERED}"

if [[ ! -f "${UNIT_DST}" ]] || ! cmp -s "${RENDERED}" "${UNIT_DST}"; then
    say "installing unit -> ${UNIT_DST}"
    install -m 0644 "${RENDERED}" "${UNIT_DST}"
    systemctl daemon-reload
else
    say "unit already up to date -> ${UNIT_DST}"
fi

# --- enable (idempotent) ----------------------------------------------------
if [[ "$(systemctl is-enabled hack.service 2>/dev/null || true)" != "enabled" ]]; then
    say "enabling hack.service"
    systemctl enable hack.service >/dev/null 2>&1
fi

# --- start / restart --------------------------------------------------------
if [[ "${NO_START}" == "1" ]]; then
    say "skipping start (--no-start). Start later with: systemctl start hack"
else
    if [[ "$(systemctl is-active hack.service 2>/dev/null || true)" != "active" ]]; then
        say "starting service (first start builds the venv; needs network)..."
        systemctl start hack.service
    else
        # Already running: restart so a rerun after a code/unit update takes effect.
        say "restarting service to apply the latest code/unit..."
        systemctl restart hack.service
    fi

    # Give uvicorn a moment to come up, then report concrete state.
    for _ in 1 2 3 4 5; do
        [[ "$(systemctl is-active hack.service 2>/dev/null || true)" == "active" ]] && break
        sleep 1
    done
    systemctl --no-pager --full status hack.service || true
fi

PORT="$(systemctl show hack.service -p Environment --value 2>/dev/null | tr ' ' '\n' | sed -n 's/^PANEL_PORT=//p')"
PORT="${PORT:-8910}"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

cat <<EOF

[H.A.C.K.] Done. Useful commands:
  systemctl status hack        # service state
  systemctl restart hack       # restart
  systemctl stop hack          # stop
  journalctl -u hack -f        # live logs

Open http://${IP:-<host>}:${PORT} and complete the first-run admin setup.
EOF
