#!/usr/bin/env bash
# ticket-bridge-status.sh
# Show the status of the last bridge run.
#
# Usage: ticket bridge-status [--format=json]
#   --format=json  Output raw JSON from status file (plus computed unresolved_alerts_count)
#
# Status file: <repo_root>/.tickets-tracker/.bridge-status.json
# Format:
#   { "last_run_timestamp": int, "success": bool, "error": str|null, "unresolved_conflicts": int }
#
# Note: .bridge-status.json was historically written by the edge-triggered bridge
# scripts. After the level-triggered reconciler cutover (epic 3a03), no producer
# writes this file — the reconciler emits health signals as
# `bridge_state/health/*.json` artifacts and the heartbeat canary
# (reconcile-bridge-canary.yml) covers liveness alerting. This script will
# therefore exit 1 ("file missing") on post-cutover repos; that exit code is
# correct for operator runbooks that rely on it ("no bridge has run yet").
# Operators monitoring reconciler health post-cutover should use the canary
# alert tag (`heartbeat-alert`) and inspect bridge_state/health/ on the tickets
# branch rather than this script. Retained as-is for backwards compatibility.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Allow tests to inject a custom tracker directory via TICKETS_TRACKER_DIR env var.
# When GIT_DIR is set (e.g., in tests), derive REPO_ROOT from its parent to avoid
# requiring an actual git repository at that path.
if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
    TRACKER_DIR="$TICKETS_TRACKER_DIR"
elif [ -n "${GIT_DIR:-}" ]; then
    REPO_ROOT="$(dirname "$GIT_DIR")"
    TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
else
    REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"
    TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
fi

STATUS_FILE="$TRACKER_DIR/.bridge-status.json"

# ── Usage ─────────────────────────────────────────────────────────────────────
_usage() {
    echo "Usage: ticket bridge-status [--format=json]" >&2
    exit 1
}

# ── Parse arguments ──────────────────────────────────────────────────────────
format="default"

for arg in "$@"; do
    case "$arg" in
        --format=json)
            format="json"
            ;;
        --format=*)
            echo "Error: unsupported format '${arg#--format=}'. Supported: json" >&2
            exit 1
            ;;
        -*)
            echo "Error: unknown option '$arg'" >&2
            _usage
            ;;
        *)
            echo "Error: unexpected argument '$arg'" >&2
            _usage
            ;;
    esac
done

# ── Check status file exists ──────────────────────────────────────────────────
if [ ! -f "$STATUS_FILE" ]; then
    echo "No bridge status file found. Has the bridge run yet?" >&2
    exit 1
fi

# ── Compute unresolved BRIDGE_ALERT count ────────────────────────────────────
# Scan all ticket directories for BRIDGE_ALERT events and count unresolved ones.
_count_unresolved_alerts() {
    python3 - "$TRACKER_DIR" <<'EOF'
import json
import sys
from pathlib import Path

tracker_dir = Path(sys.argv[1])
unresolved = 0

for ticket_dir in tracker_dir.iterdir():
    if not ticket_dir.is_dir() or ticket_dir.name.startswith("."):
        continue
    # Collect all BRIDGE_ALERT events in timestamp order
    alert_events = sorted(ticket_dir.glob("*-BRIDGE_ALERT.json"))
    alerts: dict[str, dict] = {}  # uuid -> alert state

    for event_path in alert_events:
        try:
            event = json.loads(event_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        event_uuid = event.get("uuid", "")
        data = event.get("data", {})

        if data.get("resolved"):
            # Resolution: mark matching alert resolved
            target_uuid = data.get("resolves_uuid") or data.get("alert_uuid")
            if target_uuid and target_uuid in alerts:
                alerts[target_uuid]["resolved"] = True
        else:
            alerts[event_uuid] = {"resolved": False}

    unresolved += sum(1 for a in alerts.values() if not a.get("resolved", False))

print(unresolved)
EOF
}

UNRESOLVED_ALERTS=$(_count_unresolved_alerts)

# ── Output ────────────────────────────────────────────────────────────────────
if [ "$format" = "json" ]; then
    # JSON output: raw status file contents + computed unresolved_alerts_count
    python3 - "$STATUS_FILE" "$UNRESOLVED_ALERTS" <<'EOF'
import json
import sys

status_path = sys.argv[1]
unresolved_alerts = int(sys.argv[2])

data = json.loads(open(status_path, encoding="utf-8").read())
data["unresolved_alerts_count"] = unresolved_alerts

print(json.dumps(data, ensure_ascii=False))
EOF
else
    # Human-readable output
    python3 - "$STATUS_FILE" "$UNRESOLVED_ALERTS" <<'EOF'
import json
import sys

status_path = sys.argv[1]
unresolved_alerts = int(sys.argv[2])

data = json.loads(open(status_path, encoding="utf-8").read())

last_run = data.get("last_run_timestamp", "unknown")
success = data.get("success", False)
error = data.get("error")
unresolved_conflicts = data.get("unresolved_conflicts", 0)

status_str = "success" if success else "failure"

print(f"Last run time:          {last_run}")
print(f"Status:                 {status_str}")

if error:
    print(f"Error:                  {error}")

print(f"Unresolved conflicts:   {unresolved_conflicts}")
print(f"Unresolved BRIDGE_ALERTs: {unresolved_alerts}")
EOF
fi
