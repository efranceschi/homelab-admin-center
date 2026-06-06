#!/usr/bin/env bash
# ============================================================================
# Install (or update) the `hac` systemd service so HomeLab Admin Center starts
# on boot and can be managed with systemctl. Run as root.
#
#   sudo ./install-service.sh          # install + enable + start
#   sudo ./install-service.sh --no-start
# ============================================================================
set -euo pipefail

PANEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="${PANEL_DIR}/systemd/hac.service"
UNIT_DST="/etc/systemd/system/hac.service"

if [[ $EUID -ne 0 ]]; then
    echo "This installer must run as root." >&2
    exit 1
fi

chmod +x "${PANEL_DIR}/run-panel.sh"

echo "[hac] installing unit -> ${UNIT_DST}"
install -m 0644 "${UNIT_SRC}" "${UNIT_DST}"

systemctl daemon-reload
systemctl enable hac.service

if [[ "${1:-}" != "--no-start" ]]; then
    echo "[hac] starting service (first start builds the venv; needs network)..."
    systemctl restart hac.service
    sleep 2
    systemctl --no-pager --full status hac.service || true
fi

cat <<'EOF'

[hac] Done. Useful commands:
  systemctl status hac        # service state
  systemctl restart hac       # restart
  systemctl stop hac          # stop
  journalctl -u hac -f        # live logs

Open http://<host>:8910 and complete the first-run admin setup.
EOF
