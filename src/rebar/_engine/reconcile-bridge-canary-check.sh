#!/usr/bin/env bash
# reconcile-bridge-canary-check.sh
# Staleness-detection core extracted from reconcile-bridge-canary.yml.
#
# Usage:
#   reconcile-bridge-canary-check.sh \
#     --alert-window-hours <N> \
#     --last-success-epoch <unix_epoch|never|api-error> \
#     [--now-epoch <unix_epoch>]
#
# Outputs (to stdout, one key=value per line):
#   stale=true|false
#   last_run_ago=<human string>
#   status_msg=<human string>
#
# Exit codes:
#   0  — check completed (stale or not); inspect stale= line for result
#   1  — invalid arguments (e.g., non-positive-integer alert_window_hours)
#
# This script intentionally has NO dependency on the gh CLI or GITHUB_OUTPUT
# so it can be exercised deterministically in unit tests.

set -euo pipefail

# ── Argument parsing ──────────────────────────────────────────────────────────
ALERT_WINDOW_HOURS=""
LAST_SUCCESS_EPOCH=""    # unix epoch int, "never", or "api-error"
NOW_EPOCH=""             # optional override for testing; default: date -u +%s

while [[ $# -gt 0 ]]; do
  case "$1" in
    --alert-window-hours)
      ALERT_WINDOW_HOURS="$2"; shift 2 ;;
    --last-success-epoch)
      LAST_SUCCESS_EPOCH="$2"; shift 2 ;;
    --now-epoch)
      NOW_EPOCH="$2"; shift 2 ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      exit 1 ;;
  esac
done

if [[ -z "$ALERT_WINDOW_HOURS" ]]; then
  echo "ERROR: --alert-window-hours is required" >&2
  exit 1
fi

if [[ -z "$LAST_SUCCESS_EPOCH" ]]; then
  echo "ERROR: --last-success-epoch is required" >&2
  exit 1
fi

# ── Validate ALERT_WINDOW_HOURS is a positive integer (mirrors workflow guard) ─
if ! [[ "$ALERT_WINDOW_HOURS" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: alert_window_hours must be a positive integer, got: '${ALERT_WINDOW_HOURS}'" >&2
  exit 1
fi

# ── Determine now ─────────────────────────────────────────────────────────────
if [[ -z "$NOW_EPOCH" ]]; then
  NOW_EPOCH=$(date -u +%s)
fi

window_secs=$(( ALERT_WINDOW_HOURS * 3600 ))
now_epoch="$NOW_EPOCH"
cutoff_epoch=$(( now_epoch - window_secs ))

# ── Staleness logic (mirrors the workflow step exactly) ───────────────────────
if [[ "$LAST_SUCCESS_EPOCH" == "api-error" ]]; then
  # Transient GitHub API error — do not alert (mirror: "treat as transient")
  echo "stale=false"
  echo "last_run_ago=unknown"
  echo "status_msg=GitHub Actions API error — heartbeat indeterminate, treating as transient."

elif [[ "$LAST_SUCCESS_EPOCH" == "never" ]]; then
  # No successful runs ever found
  echo "stale=true"
  echo "last_run_ago=never"
  echo "status_msg=No successful reconcile-bridge.yml runs found."

else
  # Numeric epoch provided: compare against cutoff
  run_epoch="$LAST_SUCCESS_EPOCH"
  age_secs=$(( now_epoch - run_epoch ))
  age_hours=$(( age_secs / 3600 ))
  age_mins=$(( (age_secs % 3600) / 60 ))

  if (( run_epoch < cutoff_epoch )); then
    echo "stale=true"
    echo "last_run_ago=${age_hours}h ${age_mins}m ago"
    echo "status_msg=Last successful run was ${age_hours}h ${age_mins}m ago (threshold: ${ALERT_WINDOW_HOURS}h)."
  else
    echo "stale=false"
    echo "last_run_ago=${age_hours}h ${age_mins}m ago"
    echo "status_msg=Reconciler is healthy — last successful run was ${age_hours}h ${age_mins}m ago."
  fi
fi
