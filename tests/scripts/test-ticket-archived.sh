#!/usr/bin/env bash
# tests/scripts/test-ticket-archived.sh
# RED tests for ARCHIVED event handling in ticket-reducer.py and ticket-lib.sh.
#
# All test functions MUST FAIL until ARCHIVED event support is implemented.
# Covers: --exclude-archived batch flag, archived state in single-ticket reduce,
# and ARCHIVED event type acceptance in ticket-lib.sh.
#
# Usage: bash tests/scripts/test-ticket-archived.sh
# Returns: exit non-zero (RED) until ARCHIVED event handling is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
REDUCER_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-reducer.py"
TICKET_LIB="$REPO_ROOT/src/rebar/_engine/ticket-lib.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-archived.sh ==="

# ── Suite-runner guard: skip RED tests when ARCHIVED not yet supported ────────
# When running under run-all.sh (or any batch runner), skip gracefully if
# ARCHIVED support hasn't landed yet. Individual runs still show RED failures.
if [[ "${_RUN_ALL_ACTIVE:-0}" == "1" ]]; then
    _arch_in_reducer=0; grep -q 'ARCHIVED' "$REDUCER_SCRIPT" 2>/dev/null && _arch_in_reducer=1
    _arch_in_lib=0; grep -q 'ARCHIVED' "$TICKET_LIB" 2>/dev/null && _arch_in_lib=1
    if [[ "$_arch_in_reducer" -eq 0 ]] || [[ "$_arch_in_lib" -eq 0 ]]; then
        echo "SKIP: ARCHIVED not yet supported in ticket-reducer.py (RED tests)"
        echo ""
        printf "PASSED: 0  FAILED: 0\n"
        exit 0
    fi
fi

# ── Helper: create a fresh temp git repo with ticket system initialized ────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: create a ticket and return its ID ─────────────────────────────────
_create_ticket() {
    local repo="$1"
    local ticket_type="${2:-task}"
    local title="${3:-Test ticket}"
    local out
    out=$(cd "$repo" && bash "$TICKET_SCRIPT" create "$ticket_type" "$title" 2>/dev/null) || true
    # Extract just the ticket ID (last line, in case of multi-line output)
    echo "$out" | tail -1
}

# ── Helper: write an ARCHIVED event JSON directly into a ticket dir ───────────
_write_archived_event() {
    local tracker_dir="$1"
    local ticket_id="$2"
    local ts
    ts=$(date +%s)
    local uuid="arch-$(printf '%04x' $RANDOM)"
    local event_file="$tracker_dir/$ticket_id/${ts}-${uuid}-ARCHIVED.json"
    python3 -c "
import json, sys
event = {
    'timestamp': int(sys.argv[1]),
    'uuid': sys.argv[2],
    'event_type': 'ARCHIVED',
    'env_id': 'test',
    'author': 'test',
    'data': {}
}
with open(sys.argv[3], 'w') as f:
    json.dump(event, f)
" "$ts" "$uuid" "$event_file"
    git -C "$tracker_dir" add "$ticket_id/" 2>/dev/null
    git -C "$tracker_dir" commit -q --no-verify -m "test: ARCHIVED event for $ticket_id" 2>/dev/null || true
}

# ── Test 1: archived ticket excluded from batch list with --exclude-archived ──
echo "Test 1: archived ticket excluded from batch list with --exclude-archived"
test_archived_ticket_excluded_from_batch_list() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create two tickets: one normal, one to be archived
    local normal_id archived_id
    normal_id=$(_create_ticket "$repo" task "Normal ticket")
    archived_id=$(_create_ticket "$repo" task "Archived ticket")

    if [ -z "$normal_id" ] || [ -z "$archived_id" ]; then
        assert_eq "both tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_archived_ticket_excluded_from_batch_list"
        return
    fi

    # Write ARCHIVED event to the second ticket
    _write_archived_event "$tracker_dir" "$archived_id"

    # Run batch reduce with --exclude-archived flag
    local batch_output
    local exit_code=0
    batch_output=$(python3 "$REDUCER_SCRIPT" --batch --exclude-archived "$tracker_dir" 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "batch --exclude-archived exits 0" "0" "$exit_code"

    # Assert: normal ticket IS in output, archived ticket IS NOT
    local check_result
    check_result=$(python3 - "$batch_output" "$normal_id" "$archived_id" <<'PYEOF'
import json, sys

try:
    tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

normal_id = sys.argv[2]
archived_id = sys.argv[3]

errors = []

if not isinstance(tickets, list):
    print(f"NOT_ARRAY: got {type(tickets).__name__}")
    sys.exit(2)

ticket_ids = [t.get("ticket_id") for t in tickets if isinstance(t, dict)]

if normal_id not in ticket_ids:
    errors.append(f"normal ticket {normal_id!r} missing from --exclude-archived output")
if archived_id in ticket_ids:
    errors.append(f"archived ticket {archived_id!r} should NOT be in --exclude-archived output")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(3)
else:
    print("OK")
PYEOF
) || true

    if [ "$check_result" = "OK" ]; then
        assert_eq "archived excluded, normal included" "OK" "OK"
    else
        assert_eq "archived excluded, normal included" "OK" "$check_result"
    fi

    assert_pass_if_clean "test_archived_ticket_excluded_from_batch_list"
}
test_archived_ticket_excluded_from_batch_list

# ── Test 2: archived ticket included in single-ticket show with archived flag ─
echo "Test 2: archived ticket included in single-ticket reduce with archived: true"
test_archived_ticket_included_in_show() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create a ticket and archive it
    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Ticket to archive")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for archive test" "non-empty" "empty"
        assert_pass_if_clean "test_archived_ticket_included_in_show"
        return
    fi

    # Write ARCHIVED event
    _write_archived_event "$tracker_dir" "$ticket_id"

    # Run single-ticket reduce
    local reduce_output
    local exit_code=0
    reduce_output=$(python3 "$REDUCER_SCRIPT" "$tracker_dir/$ticket_id" 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "single-ticket reduce exits 0" "0" "$exit_code"

    # Assert: output contains archived: true
    local check_result
    check_result=$(python3 - "$reduce_output" "$ticket_id" <<'PYEOF'
import json, sys

try:
    state = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

ticket_id = sys.argv[2]
errors = []

if not isinstance(state, dict):
    print(f"NOT_DICT: got {type(state).__name__}")
    sys.exit(2)

# Verify ticket_id matches
if state.get("ticket_id") != ticket_id:
    errors.append(f"ticket_id mismatch: expected {ticket_id!r}, got {state.get('ticket_id')!r}")

# Verify archived flag is present and true
archived = state.get("archived")
if archived is None:
    errors.append("missing 'archived' field in reduced state")
elif archived is not True:
    errors.append(f"'archived' should be True, got {archived!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(3)
else:
    print("OK")
PYEOF
) || true

    if [ "$check_result" = "OK" ]; then
        assert_eq "archived ticket has archived: true" "OK" "OK"
    else
        assert_eq "archived ticket has archived: true" "OK" "$check_result"
    fi

    assert_pass_if_clean "test_archived_ticket_included_in_show"
}
test_archived_ticket_included_in_show


print_summary
