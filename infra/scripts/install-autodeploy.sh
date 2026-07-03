#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install-autodeploy.sh — install the continuous auto-deploy units (epic 88ab / 8903).
#
# Mirrors install-observability.sh, with two deliberate differences for the STAGED
# ROLLOUT of a mechanism that guards a LIVE, FAIL-CLOSED gate:
#   1. self-bootstraps autodeploy's own git mirror clone at $MIRROR_DIR (so a manual
#      dry-run has something to diff against), and
#   2. installs the timer DISABLED — the operator enables it (`systemctl enable --now
#      rebar-autodeploy.timer`) only AFTER a manual dry-run (`systemctl start
#      rebar-autodeploy.service`) is confirmed healthy.
#
# Idempotent (overwrites unit files; clone is created only if absent). Run as root on the box.
# The .service ExecStart runs the script in place from $DEPLOY_REPO/infra/scripts/autodeploy.sh,
# so the deploy logic updates itself on the next rsync like any other source.
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_SRC="$(cd "${SCRIPT_DIR}/../systemd" && pwd)"

MIRROR_DIR="${MIRROR_DIR:-/var/lib/rebar/mirror}"
MIRROR_URL="${MIRROR_URL:-https://github.com/navapbc/rebar.git}"

# 1. self-bootstrap the mirror clone (regular clone so origin/main resolves; HTTPS only).
case "$MIRROR_URL" in https://*) : ;; *) echo "install-autodeploy: MIRROR_URL must be https:// (got $MIRROR_URL)" >&2; exit 1 ;; esac
if [ ! -d "$MIRROR_DIR/.git" ]; then
  echo "install-autodeploy: bootstrapping mirror clone $MIRROR_DIR from $MIRROR_URL" >&2
  mkdir -p "$(dirname "$MIRROR_DIR")"
  git clone -q "$MIRROR_URL" "$MIRROR_DIR"
fi

# 2. install the unit files (from the repo's infra/systemd, kept in sync with source).
install -m 0644 "${SYSTEMD_SRC}/rebar-autodeploy.service" /etc/systemd/system/rebar-autodeploy.service
install -m 0644 "${SYSTEMD_SRC}/rebar-autodeploy.timer"   /etc/systemd/system/rebar-autodeploy.timer

systemctl daemon-reload

# 3. STAGED ROLLOUT: units installed, timer left DISABLED. The operator does the dry-run
#    then enables. (Do NOT `enable --now` here.)
echo "install-autodeploy: units installed; timer is DISABLED (staged rollout)." >&2
echo "  dry-run:  systemctl start rebar-autodeploy.service && journalctl -u rebar-autodeploy.service -n 50" >&2
echo "  enable:   systemctl enable --now rebar-autodeploy.timer" >&2
echo "  back-out: systemctl disable --now rebar-autodeploy.timer" >&2
