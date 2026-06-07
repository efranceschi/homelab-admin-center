#!/usr/bin/env bash
# ============================================================================
# Entrypoint único do lxc-ansible — disparado diariamente pelo cron.d.
# Prepara um venv (recriado só quando requirements.txt muda), instala as
# collections localmente e executa o playbook mestre sobre os LXC rodando.
# ============================================================================
set -euo pipefail

PROJECT_DIR="/opt/hac"
VENV="${PROJECT_DIR}/.venv"
LOG_DIR="/var/log/hac"
LOCK="/run/hac.lock"
REQ_HASH_FILE="${VENV}/.requirements.sha256"

cd "${PROJECT_DIR}"
mkdir -p "${LOG_DIR}"

# --- evita execuções sobrepostas (cron + execução manual) ---
exec 9>"${LOCK}"
if ! flock -n 9; then
    echo "[lxc-ansible] já existe uma execução em andamento; saindo." >&2
    exit 0
fi

# --- venv persistente, recriado só se requirements.txt mudar ---
NEW_HASH="$(sha256sum requirements.txt | awk '{print $1}')"
if [[ ! -d "${VENV}" ]] || [[ ! -f "${REQ_HASH_FILE}" ]] || [[ "$(cat "${REQ_HASH_FILE}" 2>/dev/null)" != "${NEW_HASH}" ]]; then
    echo "[lxc-ansible] (re)criando venv..."
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

# --- execução do playbook ---
TS="$(date +%F-%H%M%S)"
LOG="${LOG_DIR}/run-${TS}.log"
echo "[lxc-ansible] iniciando playbook em ${TS} (log: ${LOG})"

set +e
ANSIBLE_CONFIG="${PROJECT_DIR}/ansible.cfg" \
    ansible-playbook playbooks/site.yml "$@" 2>&1 | tee "${LOG}"
rc=${PIPESTATUS[0]}
set -e

# --- rotação simples: mantém os 30 logs mais recentes ---
ls -1t "${LOG_DIR}"/run-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f

echo "[lxc-ansible] finalizado com rc=${rc}"
exit "${rc}"
