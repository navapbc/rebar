#!/usr/bin/env bash
# tests/scripts/test-bridge-observability-e2e.sh
# E2E integration test: bridge observability and recovery full flow.
#
# Story: dso-m91n — E2E test: bridge observability and recovery full flow
# Epic:  w21-qjcy  — As a developer, I can see bridge problems and recover from bad bridge actions
#
# This test covers the end-to-end developer workflow:
#   1. Initialize a ticket tracker
#   2. Create a test ticket
#   3. Inject a BRIDGE_ALERT event into the ticket dir
#   4. Run 'ticket show' — assert: JSON output contains bridge_alerts with 1 unresolved entry
#   5. Run 'ticket list' — assert: output includes bridge_alerts entry for the ticket
#   6. Write .bridge-status.json with success=false, error='test_error'
#   7. Run 'ticket bridge-status' — assert: output mentions failure and test_error
#   8. Run 'ticket bridge-fsck' — assert: exit 0 when no mapping issues
#   9. Inject a STATUS event + a REVERT event targeting the STATUS event
#  10. Run 'ticket show' — assert: reverts list has 1 entry
#  11. Run 'ticket revert <id> <bad_status_uuid>' — assert: new REVERT event file created
#
# Usage: bash tests/scripts/test-bridge-observability-e2e.sh
# Returns: exit 0 if all assertions pass, exit 1 if any fail.

# NOTE: -e is intentionally omitted — assert helpers return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_REVERT_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-revert.sh"
TICKET_BRIDGE_STATUS_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-bridge-status.sh"
TICKET_BRIDGE_FSCK_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-bridge-fsck.py"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-bridge-observability-e2e.sh ==="

# ── Cleanup registry ───────────────────────────────────────────────────────────
_CLEANUP_DIRS=()
_cleanup() {
    for d in "${_CLEANUP_DIRS[@]:-}"; do
        rm -rf "$d"
    done
}
trap _cleanup EXIT

# ── Helper: create a fresh temp git repo with ticket system initialized ────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: write a BRIDGE_ALERT event file into a ticket directory ────────────
# Usage: _write_bridge_alert_event <ticket_dir> <env_id> <alert_uuid>
_write_bridge_alert_event() {
    local ticket_dir="$1"
    local env_id="$2"
    local alert_uuid="$3"
    local ts
    ts=$(python3 -c "import time; print(int(time.time()))")
    python3 -c "
import json, sys
ticket_dir, env_id, alert_uuid, ts = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
event = {
    'timestamp': ts,
    'uuid': alert_uuid,
    'event_type': 'BRIDGE_ALERT',
    'env_id': env_id,
    'author': 'test-bridge',
    'data': {
        'alert_type': 'conflict_detected',
        'detail': 'Test bridge conflict for E2E',
        'resolved': False,
    },
}
filename = f'{ts}-{alert_uuid}-BRIDGE_ALERT.json'
filepath = f'{ticket_dir}/{filename}'
with open(filepath, 'w', encoding='utf-8') as f:
    json.dump(event, f)
print(filepath)
" "$ticket_dir" "$env_id" "$alert_uuid" "$ts"
}

# ── Helper: write a STATUS event file into a ticket directory ─────────────────
# Usage: _write_status_event <ticket_dir> <env_id> <status_uuid> <new_status>
_write_status_event() {
    local ticket_dir="$1"
    local env_id="$2"
    local status_uuid="$3"
    local new_status="$4"
    local ts
    ts=$(python3 -c "import time; print(int(time.time()) + 1)")
    python3 -c "
import json, sys
ticket_dir, env_id, status_uuid, new_status, ts = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
event = {
    'timestamp': ts,
    'uuid': status_uuid,
    'event_type': 'STATUS',
    'env_id': env_id,
    'author': 'test-bridge',
    'data': {
        'status': new_status,
        'current_status': None,
    },
}
filename = f'{ts}-{status_uuid}-STATUS.json'
filepath = f'{ticket_dir}/{filename}'
with open(filepath, 'w', encoding='utf-8') as f:
    json.dump(event, f)
print(filepath)
" "$ticket_dir" "$env_id" "$status_uuid" "$new_status" "$ts"
}

# ── Helper: write .bridge-status.json to the tracker dir ──────────────────────
# Usage: _write_bridge_status <tracker_dir> <success:true|false> <error_msg>
_write_bridge_status() {
    local tracker_dir="$1"
    local success="$2"
    local error_msg="$3"
    local ts
    ts=$(python3 -c "import time; print(int(time.time()))")
    python3 -c "
import json, sys
tracker_dir, success_str, error_msg, ts = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
success = success_str == 'true'
status_file = f'{tracker_dir}/.bridge-status.json'
data = {
    'last_run_timestamp': ts,
    'success': success,
    'error': error_msg if not success else None,
    'unresolved_conflicts': 1 if not success else 0,
}
with open(status_file, 'w', encoding='utf-8') as f:
    json.dump(data, f)
" "$tracker_dir" "$success" "$error_msg" "$ts"
}

# ══════════════════════════════════════════════════════════════════════════════
# Main E2E scenario: bridge observability and recovery
# ══════════════════════════════════════════════════════════════════════════════

echo ""
echo "scenario_bridge_observability_and_recovery_e2e"
echo "  steps: bridge_alerts emission, bridge-status (last_run), bridge-fsck (no issues), REVERT flow"
_snapshot_fail

REPO=$(_make_test_repo)
TRACKER_DIR="$REPO/.tickets-tracker"

# ── Step 1+2: Verify init succeeded and create a test ticket ──────────────────
assert_eq \
    "step1: .tickets-tracker/ exists after init" \
    "1" \
    "$(test -d "$TRACKER_DIR" && echo 1 || echo 0)"

ticket_id=""
ticket_id=$(cd "$REPO" && bash "$TICKET_SCRIPT" create task "Bridge observability test ticket" 2>/dev/null | tail -1) || true

assert_ne \
    "step2: create returned a non-empty ticket ID" \
    "" \
    "$ticket_id"

if [ -z "$ticket_id" ]; then
    echo "BAIL: ticket creation failed — cannot proceed with E2E test" >&2
    print_summary
fi

TICKET_DIR="$TRACKER_DIR/$ticket_id"

assert_eq \
    "step2: ticket directory exists" \
    "1" \
    "$(test -d "$TICKET_DIR" && echo 1 || echo 0)"

# ── Step 3: Inject a BRIDGE_ALERT event into the ticket dir ──────────────────
ENV_ID=$(cat "$TRACKER_DIR/.env-id" 2>/dev/null | tr -d '[:space:]') || ENV_ID="test-env"
ALERT_UUID=$(python3 -c "import uuid; print(str(uuid.uuid4()))")

alert_event_file=$(_write_bridge_alert_event "$TICKET_DIR" "$ENV_ID" "$ALERT_UUID")

assert_eq \
    "step3: BRIDGE_ALERT event file created" \
    "1" \
    "$(test -f "$alert_event_file" && echo 1 || echo 0)"

# ── Step 4: ticket show — assert bridge_alerts in JSON output with 1 unresolved
show_out=""
show_out=$(cd "$REPO" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

alert_count=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    alerts = data.get('bridge_alerts', [])
    print(len(alerts))
except Exception:
    print(0)
" "$show_out" 2>/dev/null) || alert_count=0

assert_eq \
    "step4: ticket show JSON contains 1 bridge_alert entry" \
    "1" \
    "$alert_count"

unresolved_count=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    alerts = data.get('bridge_alerts', [])
    print(sum(1 for a in alerts if not a.get('resolved', False)))
except Exception:
    print(0)
" "$show_out" 2>/dev/null) || unresolved_count=0

assert_eq \
    "step4: ticket show JSON shows 1 unresolved bridge_alert" \
    "1" \
    "$unresolved_count"

# Step 4 also verifies that 'ticket show' emits a WARNING to stderr when alerts exist
show_stderr=""
show_stderr=$(cd "$REPO" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>&1 >/dev/null) || true

assert_contains \
    "step4: ticket show warns about unresolved bridge alerts on stderr" \
    "bridge alert" \
    "$show_stderr"

# ── Step 5: ticket list — assert bridge_alerts entry appears in output ────────
list_out=""
list_out=$(cd "$REPO" && bash "$TICKET_SCRIPT" list 2>/dev/null) || true

list_bridge_alert_count=$(python3 -c "
import json, sys
try:
    tickets = json.loads(sys.argv[1])
    for t in tickets:
        if t.get('ticket_id') == sys.argv[2]:
            alerts = t.get('bridge_alerts', [])
            print(len(alerts))
            sys.exit(0)
    print(0)
except Exception:
    print(0)
" "$list_out" "$ticket_id" 2>/dev/null) || list_bridge_alert_count=0

assert_eq \
    "step5: ticket list includes bridge_alerts for the ticket" \
    "1" \
    "$list_bridge_alert_count"

# Step 5 also verifies aggregate stderr warning from 'ticket list'
list_stderr=""
list_stderr=$(cd "$REPO" && bash "$TICKET_SCRIPT" list 2>&1 >/dev/null) || true

assert_contains \
    "step5: ticket list warns about unresolved bridge alerts on stderr" \
    "bridge alert" \
    "$list_stderr"

# ── Step 6: Write .bridge-status.json with failure info ──────────────────────
_write_bridge_status "$TRACKER_DIR" "false" "test_error"

assert_eq \
    "step6: .bridge-status.json created" \
    "1" \
    "$(test -f "$TRACKER_DIR/.bridge-status.json" && echo 1 || echo 0)"

# ── Step 7: ticket bridge-status — assert failure and test_error in output ────
bridge_status_out=""
bridge_status_exit=0
bridge_status_out=$(cd "$REPO" && bash "$TICKET_SCRIPT" bridge-status 2>&1) || bridge_status_exit=$?

assert_eq \
    "step7: ticket bridge-status exits 0" \
    "0" \
    "$bridge_status_exit"

assert_contains \
    "step7: ticket bridge-status output mentions failure status" \
    "failure" \
    "$bridge_status_out"

assert_contains \
    "step7: ticket bridge-status output mentions test_error" \
    "test_error" \
    "$bridge_status_out"

assert_contains \
    "step7: ticket bridge-status output mentions last_run timestamp" \
    "Last run time" \
    "$bridge_status_out"

# ── Step 8: ticket bridge-fsck — no mapping issues → exit 0 ──────────────────
# The ticket has no SYNC events, so bridge-fsck should find no orphans/duplicates/stale.
bridge_fsck_out=""
bridge_fsck_exit=0
bridge_fsck_out=$(cd "$REPO" && bash "$TICKET_SCRIPT" bridge-fsck 2>&1) || bridge_fsck_exit=$?

assert_eq \
    "step8: ticket bridge-fsck exits 0 (no mapping issues)" \
    "0" \
    "$bridge_fsck_exit"

assert_contains \
    "step8: ticket bridge-fsck reports no issues" \
    "No issues found" \
    "$bridge_fsck_out"

# ── Step 9: Inject a STATUS event and a pre-built REVERT event ────────────────
STATUS_UUID=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
status_event_file=$(_write_status_event "$TICKET_DIR" "$ENV_ID" "$STATUS_UUID" "in_progress")

assert_eq \
    "step9: STATUS event file created" \
    "1" \
    "$(test -f "$status_event_file" && echo 1 || echo 0)"

# Write a REVERT event targeting the STATUS event directly (as if injected)
REVERT_UUID=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
revert_ts=$(python3 -c "import time; print(int(time.time()) + 2)")
python3 -c "
import json, sys
ticket_dir, env_id, revert_uuid, status_uuid, ts = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
event = {
    'timestamp': ts,
    'uuid': revert_uuid,
    'event_type': 'REVERT',
    'env_id': env_id,
    'author': 'test-bridge',
    'data': {
        'target_event_uuid': status_uuid,
        'target_event_type': 'STATUS',
        'reason': 'bad status transition in test',
    },
}
filename = f'{ts}-{revert_uuid}-REVERT.json'
filepath = f'{ticket_dir}/{filename}'
with open(filepath, 'w', encoding='utf-8') as f:
    json.dump(event, f)
" "$TICKET_DIR" "$ENV_ID" "$REVERT_UUID" "$STATUS_UUID" "$revert_ts"

# ── Step 10: ticket show — assert reverts list has 1 entry ────────────────────
show_with_revert=""
show_with_revert=$(cd "$REPO" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

revert_count=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    reverts = data.get('reverts', [])
    print(len(reverts))
except Exception:
    print(0)
" "$show_with_revert" 2>/dev/null) || revert_count=0

assert_eq \
    "step10: ticket show JSON contains 1 revert entry" \
    "1" \
    "$revert_count"

revert_target=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    reverts = data.get('reverts', [])
    if reverts:
        print(reverts[0].get('target_event_uuid', ''))
    else:
        print('')
except Exception:
    print('')
" "$show_with_revert" 2>/dev/null) || revert_target=""

assert_eq \
    "step10: revert entry targets the STATUS event UUID" \
    "$STATUS_UUID" \
    "$revert_target"

# ── Step 11: ticket revert <id> <bad_status_uuid> — assert new REVERT event ──
# Use a second STATUS event to revert via the CLI (the previous one is already
# reverted by the injected event; create a fresh STATUS event for the CLI call).
STATUS_UUID_2=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
status_event_file_2=$(_write_status_event "$TICKET_DIR" "$ENV_ID" "$STATUS_UUID_2" "closed")

revert_count_before=$(find "$TICKET_DIR" -maxdepth 1 -name '*-REVERT.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')

revert_cli_out=""
revert_cli_exit=0
revert_cli_out=$(cd "$REPO" && bash "$TICKET_SCRIPT" revert "$ticket_id" "$STATUS_UUID_2" --reason="bad close in E2E test" 2>&1) || revert_cli_exit=$?

assert_eq \
    "step11: ticket revert CLI exits 0" \
    "0" \
    "$revert_cli_exit"

revert_count_after=$(find "$TICKET_DIR" -maxdepth 1 -name '*-REVERT.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')

assert_eq \
    "step11: ticket revert creates a new REVERT event file (count increased by 1)" \
    "$((revert_count_before + 1))" \
    "$revert_count_after"

assert_contains \
    "step11: ticket revert CLI output confirms the revert" \
    "Reverted event" \
    "$revert_cli_out"

# ── Step 11b: verify final state via ticket show (2 reverts total) ────────────
final_show=""
final_show=$(cd "$REPO" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

final_revert_count=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    reverts = data.get('reverts', [])
    print(len(reverts))
except Exception:
    print(0)
" "$final_show" 2>/dev/null) || final_revert_count=0

assert_eq \
    "step11b: final ticket show shows 2 total revert entries" \
    "2" \
    "$final_revert_count"

assert_pass_if_clean "scenario_bridge_observability_and_recovery_e2e"

# ── Summary ────────────────────────────────────────────────────────────────────

print_summary
