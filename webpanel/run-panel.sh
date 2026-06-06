#!/usr/bin/env bash
# ============================================================================
# Entrypoint for the lxc-ansible web control panel.
# Creates a dedicated venv (separate from the Ansible .venv so the cron run is
# never affected), installs the web requirements, and launches Uvicorn.
#
# IMPORTANT: a single Uvicorn worker is intentional — the in-memory job
# registry is authoritative and must not be split across workers.
# ============================================================================
set -euo pipefail

PANEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${PANEL_DIR}/.venv-web"
REQ="${PANEL_DIR}/requirements-web.txt"
REQ_HASH_FILE="${VENV}/.requirements.sha256"

# Binds to all interfaces by default. The panel runs as root, so restrict
# access at the network/firewall layer or front it with an authenticated proxy.
HOST="${PANEL_HOST:-0.0.0.0}"
PORT="${PANEL_PORT:-8910}"

cd "${PANEL_DIR}"

# --- persistent venv, recreated only when requirements-web.txt changes ---
NEW_HASH="$(sha256sum "${REQ}" | awk '{print $1}')"
if [[ ! -d "${VENV}" ]] || [[ "$(cat "${REQ_HASH_FILE}" 2>/dev/null)" != "${NEW_HASH}" ]]; then
    echo "[webpanel] (re)creating venv..."
    rm -rf "${VENV}"
    python3 -m venv "${VENV}"
    "${VENV}/bin/pip" install --upgrade pip >/dev/null
    "${VENV}/bin/pip" install -r "${REQ}"
    echo "${NEW_HASH}" > "${REQ_HASH_FILE}"
fi

# shellcheck disable=SC1091
source "${VENV}/bin/activate"

echo "[webpanel] starting on http://${HOST}:${PORT}"
exec uvicorn app.main:app --host "${HOST}" --port "${PORT}" --workers 1 "$@"
