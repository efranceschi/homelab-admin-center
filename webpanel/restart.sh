#!/usr/bin/env bash
# ============================================================================
# Restart the HAC web panel without root.
#
# The `hac` service runs uvicorn without --reload, so code changes only apply
# after a restart. `systemctl restart hac` needs privileges this shell may lack;
# instead this drives the panel's own self-restart endpoint
# (POST /settings/system/restart -> system.request_restart -> os._exit(0), then
# systemd Restart=always respawns it fresh). No sudo required.
#
# Simpler no-credentials alternative (immediate when idle, otherwise drains
# running jobs first): send SIGHUP to the main process —
#   kill -HUP "$(cat run_dirs/hac.pid)"
# See docs/sighup-restart.md. This script remains useful for the immediate,
# no-drain HTTP restart and for waiting until the panel is back up.
#
# Credentials (an admin account) resolve in this order:
#   1. HAC_USER / HAC_PASS environment variables.
#   2. ~/.netrc entry for the HAC_URL host (machine/login/password).
# Other config:
#   HAC_URL   panel base URL (default http://127.0.0.1:8910)
# ============================================================================
set -euo pipefail

BASE="${HAC_URL:-http://127.0.0.1:8910}"
BASE="${BASE%/}"  # strip trailing slash
NETRC="${HOME}/.netrc"

die() { echo "restart.sh: $*" >&2; exit 1; }
command -v curl >/dev/null || die "curl not found"

# --- resolve credentials ----------------------------------------------------
USER="${HAC_USER:-}"
PASS="${HAC_PASS:-}"
if [[ -z "${USER}" || -z "${PASS}" ]] && [[ -f "${NETRC}" ]]; then
    host="${BASE#*://}"; host="${host%%/*}"; host="${host%%:*}"
    # Read login/password for the matching `machine` block; tokens may span lines.
    creds="$(awk -v h="${host}" '
        { for (i=1;i<=NF;i++) tok[++n]=$i }
        END {
            for (i=1;i<=n;i++) {
                if (tok[i]=="machine") m=(tok[i+1]==h)
                else if (m && tok[i]=="login")    l=tok[i+1]
                else if (m && tok[i]=="password") p=tok[i+1]
            }
            if (l!="" && p!="") print l "\t" p
        }' "${NETRC}")"
    if [[ -n "${creds}" ]]; then
        USER="${USER:-${creds%%$'\t'*}}"
        PASS="${PASS:-${creds##*$'\t'}}"
    fi
fi
[[ -n "${USER}" && -n "${PASS}" ]] || \
    die "no credentials: set HAC_USER/HAC_PASS or add a ~/.netrc entry for the host"

JAR="$(mktemp)"
trap 'rm -f "${JAR}"' EXIT

# Scrape the per-session synchronizer CSRF token embedded in a rendered page.
scrape_csrf() {  # $1 = path
    curl -fsS -b "${JAR}" -c "${JAR}" "${BASE}$1" \
        | grep -oP 'name="csrf_token"\s+value="\K[^"]+' | head -n1
}

# --- 1. seed session + token ------------------------------------------------
token="$(scrape_csrf /login)" || die "cannot reach panel at ${BASE}"
[[ -n "${token}" ]] || die "no CSRF token at /login (is ${BASE} the panel?)"

# --- 2. authenticate --------------------------------------------------------
code="$(curl -fsS -o /dev/null -w '%{http_code}' \
    -b "${JAR}" -c "${JAR}" -X POST "${BASE}/login" \
    -H "x-csrf-token: ${token}" \
    --data-urlencode "username=${USER}" \
    --data-urlencode "password=${PASS}" || true)"
# A successful login redirects (303); a failed one re-renders /login (200).
[[ "${code}" == "303" ]] || die "login failed (HTTP ${code}); check credentials"

# --- 3. token bound to the authed session -----------------------------------
token="$(scrape_csrf /settings)" || die "cannot load /settings after login"
[[ -n "${token}" ]] || die "not authorized for /settings (admin account required)"

# --- 4. trigger the restart -------------------------------------------------
echo "Requesting panel restart at ${BASE} ..."
curl -fsS -o /dev/null -b "${JAR}" -X POST "${BASE}/settings/system/restart" \
    -H "x-csrf-token: ${token}" || die "restart request rejected"

# --- 5. wait for the panel to come back -------------------------------------
deadline=$(( SECONDS + 30 ))
while (( SECONDS < deadline )); do
    sleep 1
    if curl -fsS -o /dev/null "${BASE}/login" 2>/dev/null; then
        echo "Panel is back up."
        exit 0
    fi
done
die "panel did not respond within 30s after restart"
