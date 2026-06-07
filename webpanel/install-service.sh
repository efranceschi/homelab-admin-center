#!/usr/bin/env bash
# ============================================================================
# Install (or update) the `hac` systemd service so HomeLab Admin Center starts
# on boot and can be managed with systemctl. Run as root.
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
UNIT_SRC="${PANEL_DIR}/systemd/hac.service"
UNIT_DST="/etc/systemd/system/hac.service"

say() { echo -e "\033[1;36m[hac]\033[0m $*"; }
die() { echo -e "\033[1;31m[hac] $*\033[0m" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "This installer must run as root."
[[ -f "${UNIT_SRC}" ]] || die "Unit template not found: ${UNIT_SRC}"

NO_START=0
[[ "${1:-}" == "--no-start" ]] && NO_START=1

# --- prerequisites ----------------------------------------------------------
# run-panel.sh builds a venv on first start; python3 + the venv module are required.
command -v python3 >/dev/null 2>&1 || die "python3 is required but not found."
python3 -c 'import venv' >/dev/null 2>&1 \
    || die "python3 venv module missing — install it (e.g. apt-get install -y python3-venv)."

chmod +x "${PANEL_DIR}/run-panel.sh" "${PANEL_DIR}/install-service.sh"

# --- writable runtime dirs the unit/app expect ------------------------------
# /var/lib/hac is handled by StateDirectory=, but /etc and /var/log are
# referenced by ReadWritePaths= and must exist before the first start, or the
# unit fails to set up its namespace. install -d is a no-op when present.
install -d -m 0700 /etc/hac      # master key, session secret, vault-pass
install -d -m 0755 /var/log/hac  # job/run logs

# --- disable the legacy cron entry (scheduling is owned by the app) ---------
LEGACY_CRON="/etc/cron.d/lxc-ansible"
if [[ -f "${LEGACY_CRON}" ]]; then
    say "disabling legacy cron entry ${LEGACY_CRON} -> ${LEGACY_CRON}.disabled"
    mv "${LEGACY_CRON}" "${LEGACY_CRON}.disabled"
fi

# --- render the unit with the ACTUAL install paths --------------------------
# The template ships with /opt/hac defaults; rewrite them so the unit
# works at any checkout location. Data paths (/var/lib, /etc, /run, /var/log)
# are intentionally left untouched.
RENDERED="$(mktemp)"
trap 'rm -f "${RENDERED}"' EXIT
sed -e "s#/opt/hac/webpanel#${PANEL_DIR}#g" \
    -e "s#WorkingDirectory=/opt/hac#WorkingDirectory=${ANSIBLE_ROOT}#g" \
    "${UNIT_SRC}" > "${RENDERED}"

UNIT_CHANGED=0
if [[ ! -f "${UNIT_DST}" ]] || ! cmp -s "${RENDERED}" "${UNIT_DST}"; then
    say "installing unit -> ${UNIT_DST}"
    install -m 0644 "${RENDERED}" "${UNIT_DST}"
    UNIT_CHANGED=1
    systemctl daemon-reload
else
    say "unit already up to date -> ${UNIT_DST}"
fi

# --- enable (idempotent) ----------------------------------------------------
if [[ "$(systemctl is-enabled hac.service 2>/dev/null || true)" != "enabled" ]]; then
    say "enabling hac.service"
    systemctl enable hac.service >/dev/null 2>&1
fi

# --- start / restart --------------------------------------------------------
if [[ "${NO_START}" == "1" ]]; then
    say "skipping start (--no-start). Start later with: systemctl start hac"
else
    if [[ "$(systemctl is-active hac.service 2>/dev/null || true)" != "active" ]]; then
        say "starting service (first start builds the venv; needs network)..."
        systemctl start hac.service
    else
        # Already running: restart so a rerun after a code/unit update takes effect.
        say "restarting service to apply the latest code/unit..."
        systemctl restart hac.service
    fi

    # Give uvicorn a moment to come up, then report concrete state.
    for _ in 1 2 3 4 5; do
        [[ "$(systemctl is-active hac.service 2>/dev/null || true)" == "active" ]] && break
        sleep 1
    done
    systemctl --no-pager --full status hac.service || true
fi

PORT="$(systemctl show hac.service -p Environment --value 2>/dev/null | tr ' ' '\n' | sed -n 's/^PANEL_PORT=//p')"
PORT="${PORT:-8910}"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

cat <<EOF

[hac] Done. Useful commands:
  systemctl status hac        # service state
  systemctl restart hac       # restart
  systemctl stop hac          # stop
  journalctl -u hac -f        # live logs

Open http://${IP:-<host>}:${PORT} and complete the first-run admin setup.
EOF
