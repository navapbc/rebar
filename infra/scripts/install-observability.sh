#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install-observability.sh — install the health/disk probe as a systemd timer (S2).
# Runs observability.sh every 5 minutes. Idempotent (overwrites the unit files).
# The disk-usage CloudWatch ALARM on the published metric is created by infra
# (aws CLI in S2 / formalized in S7 monitoring.tf) — this script only installs the
# producer (the metric/health probe).
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -m 0755 "${SCRIPT_DIR}/observability.sh" /usr/local/bin/rebar-observability.sh

cat >/etc/systemd/system/rebar-observability.service <<'UNIT'
[Unit]
Description=rebar health + disk observability probe
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/rebar-observability.sh
UNIT

cat >/etc/systemd/system/rebar-observability.timer <<'UNIT'
[Unit]
Description=Run rebar observability probe every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now rebar-observability.timer
echo "install-observability: timer enabled (every 5 min)." >&2
