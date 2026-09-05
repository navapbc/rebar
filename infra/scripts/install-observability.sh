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

# Where the units are written. Overridable ONLY so a test can render them and assert the
# relationship between the service's start timeout and the timer's period; production is
# unchanged.
# mechanism-ok: env_var UNIT_DIR — 1205-63b2-2c01-4e7f: render-target seam so the start-timeout
# invariant is testable without root.
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"

cat >"${UNIT_DIR}/rebar-observability.service" <<'UNIT'
[Unit]
Description=rebar health + disk observability probe
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/rebar-observability.sh
# A truncated run publishes its own death certificate (bug 9313-1fac-9f32-4b07). systemd runs
# ExecStopPost after the main process has gone INCLUDING when TimeoutStartSec SIGTERM-ed it, and
# exports $SERVICE_RESULT to it, so this is the one hook that observes the kill the probe cannot
# observe itself. Without it "the probe was killed before this section" and "this section had
# nothing to publish" are the same gap on the metric side, and with
# treat_missing_data = "breaching" they page identically — which is how four of six alarms came
# to be firing on healthy values on 2026-09-05.
ExecStopPost=/usr/local/bin/rebar-observability.sh --report-exit
# The stop path gets an explicit ceiling too (bug 9313-1fac-9f32-4b07). systemd's 90s default
# applied, so stop was never unbounded — but 90s is three times the START budget's tail reserve
# for a hook whose whole job is one `put-metric-data`, and "a default happens to bound it" is the
# reasoning that produced 495s of ceilings inside a 240s timeout in the first place. The hook now
# reads a cached region rather than IMDS and its guard runs before any prologue, so 15s is
# generous; the point is that the number is stated and summed rather than inherited.
TimeoutStopSec=15
# A `Type=oneshot` with no TimeoutStartSec gets TimeoutStartUSec=INFINITY — systemd's
# DefaultTimeoutStartSec does not apply to it. That is not merely "a slow run blocks the next
# one": OnUnitActiveSec below is measured from the last COMPLETED activation, so a run that
# never finishes does not delay the timer, it DELETES its next elapse
# (NextElapseUSecMonotonic=infinity, confirmed on systemd 255). One overrun and this probe is
# silent until the host reboots, which is how the 2026-09-04 Gerrit outage lasted 41 minutes
# (bug 1205-63b2-2c01-4e7f).
#
# 240s is strictly below the 300s period, so a hung run is killed BEFORE the next elapse would
# have been and can never overlap it. The activation then always completes, so the timer re-arms
# on its own — the latch is removed at its root rather than papered over with a second trigger.
TimeoutStartSec=240
# This probe reads journald and walks docker's storage; it is the heavier I/O consumer on a box
# whose job is serving Gerrit, and it was competing at full priority. Same pairing as
# rebar-autodeploy.service.
Nice=10
IOSchedulingClass=idle
UNIT

cat >"${UNIT_DIR}/rebar-observability.timer" <<'UNIT'
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
