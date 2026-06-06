#!/usr/bin/env bash
# ============================================================================
# HomeLab Admin Center (HAC) — one-line web installer.
#
#   curl -fsSL https://raw.githubusercontent.com/efranceschi/homelab-admin-center/main/install.sh | sudo bash
#
# Clones (or updates) the project, seeds example config, and installs + starts
# the `hac` systemd service. Run as root on the Proxmox node.
#
# Environment overrides:
#   HAC_INSTALL_DIR  target directory   (default: /opt/lxc-ansible)
#   HAC_BRANCH       git branch         (default: main)
#   HAC_REPO         git repository URL (default: the public repo)
#   HAC_NO_START=1   install but do not start the service
# ============================================================================
set -euo pipefail

REPO_URL="${HAC_REPO:-https://github.com/efranceschi/homelab-admin-center.git}"
INSTALL_DIR="${HAC_INSTALL_DIR:-/opt/lxc-ansible}"
BRANCH="${HAC_BRANCH:-main}"

say() { echo -e "\033[1;36m[HAC]\033[0m $*"; }
die() { echo -e "\033[1;31m[HAC] $*\033[0m" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Please run as root (pipe to 'sudo bash')."

say "Installing HomeLab Admin Center -> ${INSTALL_DIR} (branch ${BRANCH})"

# --- prerequisites ----------------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
    say "Installing prerequisites (git, python3-venv)..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y --no-install-recommends \
        git python3 python3-venv ca-certificates curl >/dev/null
else
    command -v git >/dev/null || die "git is required but not found."
    command -v python3 >/dev/null || die "python3 is required but not found."
fi

# --- clone or update --------------------------------------------------------
if [ -d "${INSTALL_DIR}/.git" ]; then
    say "Existing checkout found; updating..."
    git -C "${INSTALL_DIR}" fetch --all --quiet
    git -C "${INSTALL_DIR}" checkout "${BRANCH}" --quiet
    git -C "${INSTALL_DIR}" pull --ff-only --quiet
elif [ -e "${INSTALL_DIR}" ] && [ -n "$(ls -A "${INSTALL_DIR}" 2>/dev/null)" ]; then
    die "${INSTALL_DIR} exists and is not a git checkout. Move it or set HAC_INSTALL_DIR."
else
    say "Cloning ${REPO_URL}..."
    git clone --branch "${BRANCH}" "${REPO_URL}" "${INSTALL_DIR}" --quiet
fi

# --- seed environment-specific config from examples (never overwrite) -------
GV="${INSTALL_DIR}/inventory/group_vars/all"
[ -f "${GV}/main.yml" ]   || { cp "${GV}/main.example.yml" "${GV}/main.yml"; say "Seeded inventory/group_vars/all/main.yml"; }
[ -f "${INSTALL_DIR}/inventory/proxmox.yml" ] || cp "${INSTALL_DIR}/inventory/proxmox.example.yml" "${INSTALL_DIR}/inventory/proxmox.yml"

# --- install + start the service --------------------------------------------
chmod +x "${INSTALL_DIR}/webpanel/install-service.sh" "${INSTALL_DIR}/webpanel/run-panel.sh"
if [ "${HAC_NO_START:-0}" = "1" ]; then
    "${INSTALL_DIR}/webpanel/install-service.sh" --no-start
else
    "${INSTALL_DIR}/webpanel/install-service.sh"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
say "Installation complete."
say "Open  http://${IP:-<host>}:8910  and create the first admin account."
say "Manage with:  systemctl status hac   |   journalctl -u hac -f"
