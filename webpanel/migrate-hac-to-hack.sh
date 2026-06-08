#!/usr/bin/env bash
# ============================================================================
# One-shot migration for the HAC -> H.A.C.K. rename. Moves an existing `hac`
# deployment to the new `hack` identifiers: install tree, data dirs, system
# account, and systemd/sudoers/tmpfiles units. Run as root.
#
#   sudo /opt/hac/webpanel/migrate-hac-to-hack.sh
#
# Idempotent: each step is skipped when already in the target state, so a
# re-run after a partial migration converges. After the tree is moved the
# script re-execs install-service.sh from the NEW location to install the
# `hack` unit and start the service.
#
# IMPORTANT: run this from OUTSIDE /opt/hac (e.g. from /root or /tmp) — the
# directory is moved out from under any shell cwd'd into it.
# ============================================================================
set -euo pipefail

OLD_DIR="/opt/hac"
NEW_DIR="/opt/hack"

say() { echo -e "\033[1;36m[migrate]\033[0m $*"; }
die() { echo -e "\033[1;31m[migrate] $*\033[0m" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "This migration must run as root."

# --- 1. stop the old service (ignore if it was never installed) -------------
if systemctl list-unit-files hac.service >/dev/null 2>&1 \
   && [[ "$(systemctl is-active hac.service 2>/dev/null || true)" == "active" ]]; then
    say "stopping hac.service"
    systemctl stop hac.service || true
fi

# --- 2. move the install tree (the cutover point) ---------------------------
if [[ -d "${OLD_DIR}" && ! -e "${NEW_DIR}" ]]; then
    say "moving ${OLD_DIR} -> ${NEW_DIR}"
    mv "${OLD_DIR}" "${NEW_DIR}"
elif [[ -d "${NEW_DIR}" ]]; then
    say "${NEW_DIR} already present — skipping tree move"
else
    die "neither ${OLD_DIR} nor ${NEW_DIR} exists; nothing to migrate"
fi

# --- 3. migrate data directories (skip when target already exists) ----------
for pair in "/etc/hac:/etc/hack" "/var/lib/hac:/var/lib/hack" "/var/log/hac:/var/log/hack"; do
    src="${pair%%:*}"; dst="${pair##*:}"
    if [[ -d "${src}" && ! -e "${dst}" ]]; then
        say "moving ${src} -> ${dst}"
        mv "${src}" "${dst}"
    fi
done

# --- 4. rename the system group and user ------------------------------------
if getent group hac >/dev/null && ! getent group hack >/dev/null; then
    say "renaming group hac -> hack"
    groupmod -n hack hac
fi
if getent passwd hac >/dev/null && ! getent passwd hack >/dev/null; then
    say "renaming user hac -> hack (home -> ${NEW_DIR})"
    usermod -l hack -d "${NEW_DIR}" hac
fi

# --- 5. remove stale `hac` units / lock so install-service.sh starts clean ---
say "removing legacy hac units"
[[ "$(systemctl is-enabled hac.service 2>/dev/null || true)" == "enabled" ]] \
    && systemctl disable hac.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/hac.service \
      /etc/sudoers.d/hac \
      /etc/tmpfiles.d/hac.conf \
      /run/hac.lock
systemctl daemon-reload

# --- 6. install the new `hack` unit + sudoers + tmpfiles, start the service --
say "installing the hack service from ${NEW_DIR}"
"${NEW_DIR}/webpanel/install-service.sh"

# --- 7. report --------------------------------------------------------------
say "migration complete. Verify with:"
echo "  systemctl is-active hack"
echo "  curl -s http://127.0.0.1:8910/openapi.json | head -c 80; echo"
