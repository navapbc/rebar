#!/usr/bin/env bash
# Install the continuous auto-deploy units as root. The idempotent installer creates
# its HTTPS mirror when absent, overwrites unit files, and leaves the timer disabled
# until an operator completes the printed dry-run and enable steps.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_SRC="$(cd "${SCRIPT_DIR}/../systemd" && pwd)"

MIRROR_DIR="${MIRROR_DIR:-/var/lib/rebar/mirror}"
MIRROR_URL="${MIRROR_URL:-https://github.com/navapbc/rebar.git}"

# 1. Bootstrap the HTTPS mirror when absent.
case "$MIRROR_URL" in https://*) : ;; *) echo "install-autodeploy: MIRROR_URL must be https:// (got $MIRROR_URL)" >&2; exit 1 ;; esac
if [ ! -d "$MIRROR_DIR/.git" ]; then
  echo "install-autodeploy: bootstrapping mirror clone $MIRROR_DIR from $MIRROR_URL" >&2
  mkdir -p "$(dirname "$MIRROR_DIR")"
  git clone -q "$MIRROR_URL" "$MIRROR_DIR"
fi

# 2. Install the maintained systemd units.
install -m 0644 "${SYSTEMD_SRC}/rebar-autodeploy.service" /etc/systemd/system/rebar-autodeploy.service
install -m 0644 "${SYSTEMD_SRC}/rebar-autodeploy.timer"   /etc/systemd/system/rebar-autodeploy.timer

systemctl daemon-reload

# 3. Leave the timer disabled for the operator's staged rollout.
echo "install-autodeploy: units installed; timer is DISABLED (staged rollout)." >&2
echo "  dry-run:  systemctl start rebar-autodeploy.service && journalctl -u rebar-autodeploy.service -n 50" >&2
echo "  enable:   systemctl enable --now rebar-autodeploy.timer" >&2
echo "  back-out: systemctl disable --now rebar-autodeploy.timer" >&2
