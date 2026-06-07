#!/usr/bin/env bash
# ============================================================================
# Single entrypoint for lxc-ansible — triggered daily by cron.d.
# Prepares a venv (recreated only when requirements.txt changes), installs the
# collections locally and runs the master playbook over the running LXCs.
# ============================================================================
set -euo pipefail

PROJECT_DIR="/opt/hac"
VENV="${PROJECT_DIR}/.venv"
LOG_DIR="/var/log/hac"
LOCK="/run/hac.lock"
REQ_HASH_FILE="${VENV}/.requirements.sha256"

cd "${PROJECT_DIR}"
mkdir -p "${LOG_DIR}"

# --- avoid overlapping runs (cron + manual run) ---
exec 9>"${LOCK}"
if ! flock -n 9; then
    echo "[lxc-ansible] a run is already in progress; exiting." >&2
    exit 0
fi

# --- persistent venv, recreated only if requirements.txt changes ---
NEW_HASH="$(sha256sum requirements.txt | awk '{print $1}')"
if [[ ! -d "${VENV}" ]] || [[ ! -f "${REQ_HASH_FILE}" ]] || [[ "$(cat "${REQ_HASH_FILE}" 2>/dev/null)" != "${NEW_HASH}" ]]; then
    echo "[lxc-ansible] (re)creating venv..."
    rm -rf "${VENV}"
    python3 -m venv "${VENV}"
    "${VENV}/bin/pip" install --upgrade pip >/dev/null
    "${VENV}/bin/pip" install -r requirements.txt
    echo "${NEW_HASH}" > "${REQ_HASH_FILE}"
fi

# shellcheck disable=SC1091
source "${VENV}/bin/activate"

# --- collections locais (idempotente) ---
ansible-galaxy collection install -r requirements.yml -p ./collections >/dev/null

# --- playbook run ---
TS="$(date +%F-%H%M%S)"
LOG="${LOG_DIR}/run-${TS}.log"
echo "[lxc-ansible] starting playbook at ${TS} (log: ${LOG})"

set +e
ANSIBLE_CONFIG="${PROJECT_DIR}/ansible.cfg" \
    ansible-playbook playbooks/site.yml "$@" 2>&1 | tee "${LOG}"
rc=${PIPESTATUS[0]}
set -e

# --- simple rotation: keep the 30 most recent logs ---
# Log names are program-controlled (run-<timestamp>.log), so ls -t is safe here.
# shellcheck disable=SC2012
ls -1t "${LOG_DIR}"/run-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f

echo "[lxc-ansible] finished with rc=${rc}"
exit "${rc}"
