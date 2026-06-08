#!/usr/bin/env bash
# tests/scripts/test-ticket-reducer-batch.sh
# RED tests for `python3 ticket-reducer.py --batch <tracker_dir>` — batch reduce mode.
#
# All test functions MUST FAIL until --batch mode is implemented in ticket-reducer.py.
# Covers: batch JSON array output, empty tracker, ghost ticket error status,
# equivalence with individual reduces.
#
# Usage: bash tests/scripts/test-ticket-reducer-batch.sh
# Returns: exit non-zero (RED) until --batch mode is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
REDUCER_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-reducer.py"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-reducer-batch.sh ==="

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
    echo "$out" | tail -1
}

# ── Test 1: batch reduce with 3 tickets → JSON array with 3 states ────────────
echo "Test 1: batch reduce with 3 tickets returns JSON array with all ticket states"
test_batch_reduce_returns_all_tickets() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1 id2 id3
    id1=$(_create_ticket "$repo" task "Batch ticket one")
    id2=$(_create_ticket "$repo" epic "Batch ticket two")
    id3=$(_create_ticket "$repo" story "Batch ticket three")

    if [ -z "$id1" ] || [ -z "$id2" ] || [ -z "$id3" ]; then
        assert_eq "all 3 tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_batch_reduce_returns_all_tickets"
        return
    fi

    local batch_output
    local exit_code=0
    batch_output=$(python3 "$REDUCER_SCRIPT" --batch "$tracker_dir" 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "batch reduce exits 0" "0" "$exit_code"

    # Assert: output is a JSON array containing all 3 ticket IDs with required fields
    local check_result
    check_result=$(python3 - "$batch_output" "$id1" "$id2" "$id3" <<'PYEOF'
import json, sys

try:
    tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

id1 = sys.argv[2]
id2 = sys.argv[3]
id3 = sys.argv[4]

errors = []

if not isinstance(tickets, list):
    print(f"NOT_ARRAY: got {type(tickets).__name__}")
    sys.exit(2)

if len(tickets) != 3:
    errors.append(f"expected 3 tickets, got {len(tickets)}")

ticket_ids = [t.get("ticket_id") for t in tickets if isinstance(t, dict)]

for tid in [id1, id2, id3]:
    if tid not in ticket_ids:
        errors.append(f"ticket_id {tid!r} not found in batch output")

# Check required fields on each ticket
required_fields = ["ticket_id", "ticket_type", "title", "status"]
for t in tickets:
    if not isinstance(t, dict):
        errors.append(f"non-dict element in array: {type(t).__name__}")
        continue
    for field in required_fields:
        if field not in t:
            errors.append(f"ticket {t.get('ticket_id', '?')}: missing field {field!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(3)
else:
    print("OK")
PYEOF
) || true

    if [ "$check_result" = "OK" ]; then
        assert_eq "batch contains all 3 tickets with required fields" "OK" "OK"
    else
        assert_eq "batch contains all 3 tickets with required fields" "OK" "$check_result"
    fi

    assert_pass_if_clean "test_batch_reduce_returns_all_tickets"
}
test_batch_reduce_returns_all_tickets

# ── Test 2: batch reduce on empty tracker → outputs '[]' ──────────────────────
echo "Test 2: batch reduce on empty tracker returns empty JSON array"
test_batch_reduce_empty_tracker() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local batch_output
    local exit_code=0
    batch_output=$(python3 "$REDUCER_SCRIPT" --batch "$tracker_dir" 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "empty batch reduce exits 0" "0" "$exit_code"

    # Assert: output is the empty JSON array
    local normalized
    normalized=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]))" "$batch_output" 2>/dev/null) || true
    assert_eq "empty tracker returns []" "[]" "$normalized"

    assert_pass_if_clean "test_batch_reduce_empty_tracker"
}
test_batch_reduce_empty_tracker

# ── Test 3: ghost ticket (no CREATE event, corrupt JSON) → status: error ──────
echo "Test 3: ghost ticket dir (no CREATE event, corrupt JSON) appears with status: error"
test_batch_reduce_handles_ghost_tickets() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create one normal ticket
    local normal_id
    normal_id=$(_create_ticket "$repo" task "Normal ticket")

    # Manually create a ghost ticket dir with corrupt JSON (no CREATE event)
    local ghost_id="ghost-batch1"
    mkdir -p "$tracker_dir/$ghost_id"
    printf 'not-valid-json' > "$tracker_dir/$ghost_id/0000000001-aaaa-COMMENT.json"
    git -C "$tracker_dir" add "$ghost_id/0000000001-aaaa-COMMENT.json" 2>/dev/null
    git -C "$tracker_dir" commit -q -m "test: add ghost ticket dir" 2>/dev/null || true

    local batch_output
    local exit_code=0
    batch_output=$(python3 "$REDUCER_SCRIPT" --batch "$tracker_dir" 2>/dev/null) || exit_code=$?

    # Assert: exits 0 (ghost tickets should not crash batch mode)
    assert_eq "batch exits 0 with ghost ticket" "0" "$exit_code"

    # Assert: ghost ticket appears in output with status='error'
    local ghost_check
    ghost_check=$(python3 - "$batch_output" "$ghost_id" <<'PYEOF'
import json, sys

try:
    tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

ghost_id = sys.argv[2]

if not isinstance(tickets, list):
    print(f"NOT_ARRAY: {type(tickets).__name__}")
    sys.exit(2)

ghost = next((t for t in tickets if isinstance(t, dict) and t.get("ticket_id") == ghost_id), None)
if ghost is None:
    print(f"GHOST_NOT_IN_BATCH:{ghost_id}")
    sys.exit(3)

status = ghost.get("status", "")
if status != "error":
    print(f"GHOST_STATUS_WRONG:expected 'error', got {status!r}")
    sys.exit(4)

error_field = ghost.get("error", "")
if not error_field:
    print("GHOST_MISSING_ERROR_FIELD")
    sys.exit(5)

print("OK")
PYEOF
) || true

    if [ "$ghost_check" = "OK" ]; then
        assert_eq "ghost ticket in batch with error status" "OK" "OK"
    else
        assert_eq "ghost ticket in batch with error status" "OK" "$ghost_check"
    fi

    assert_pass_if_clean "test_batch_reduce_handles_ghost_tickets"
}
test_batch_reduce_handles_ghost_tickets

# ── Test 4: batch output equivalent to individual reduces ─────────────────────
echo "Test 4: batch reduce output is equivalent to individual reduce calls"
test_batch_reduce_output_equivalent_to_individual() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1 id2
    id1=$(_create_ticket "$repo" task "Equiv ticket one")
    id2=$(_create_ticket "$repo" epic "Equiv ticket two")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "both tickets created for equivalence test" "non-empty" "empty"
        assert_pass_if_clean "test_batch_reduce_output_equivalent_to_individual"
        return
    fi

    # Run batch reduce
    local batch_output
    local batch_exit=0
    batch_output=$(python3 "$REDUCER_SCRIPT" --batch "$tracker_dir" 2>/dev/null) || batch_exit=$?

    # Assert: batch exits 0 (gate — if this fails, remaining assertions are moot)
    assert_eq "batch reduce exits 0 for equivalence" "0" "$batch_exit"

    if [ "$batch_exit" -ne 0 ]; then
        assert_eq "batch output equivalent to individual reduces" "OK" "BATCH_FAILED:exit=$batch_exit"
        assert_pass_if_clean "test_batch_reduce_output_equivalent_to_individual"
        return
    fi

    # Run individual reduces
    local ind1_output ind2_output
    local ind1_exit=0 ind2_exit=0
    ind1_output=$(python3 "$REDUCER_SCRIPT" "$tracker_dir/$id1" 2>/dev/null) || ind1_exit=$?
    ind2_output=$(python3 "$REDUCER_SCRIPT" "$tracker_dir/$id2" 2>/dev/null) || ind2_exit=$?

    # Assert: individual reduces exit 0
    assert_eq "individual reduce 1 exits 0" "0" "$ind1_exit"
    assert_eq "individual reduce 2 exits 0" "0" "$ind2_exit"

    # Assert: batch output contains equivalent data to individual reduces
    local equiv_check
    equiv_check=$(python3 - "$batch_output" "$ind1_output" "$ind2_output" "$id1" "$id2" <<'PYEOF'
import json, sys

try:
    batch_tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"BATCH_PARSE_ERROR:{e}")
    sys.exit(1)

try:
    ind1 = json.loads(sys.argv[2])
    ind2 = json.loads(sys.argv[3])
except Exception as e:
    print(f"INDIVIDUAL_PARSE_ERROR:{e}")
    sys.exit(2)

id1 = sys.argv[4]
id2 = sys.argv[5]

errors = []

if not isinstance(batch_tickets, list):
    print(f"BATCH_NOT_ARRAY:{type(batch_tickets).__name__}")
    sys.exit(3)

# Find each ticket in batch output and compare to individual output
batch_by_id = {t.get("ticket_id"): t for t in batch_tickets if isinstance(t, dict)}

for tid, ind_state in [(id1, ind1), (id2, ind2)]:
    if tid not in batch_by_id:
        errors.append(f"ticket {tid!r} not in batch output")
        continue
    batch_state = batch_by_id[tid]
    # Compare key fields (skip comments timestamps which may differ slightly)
    for field in ["ticket_id", "ticket_type", "title", "status", "author", "parent_id", "priority"]:
        bval = batch_state.get(field)
        ival = ind_state.get(field)
        if bval != ival:
            errors.append(f"ticket {tid} field {field!r}: batch={bval!r} vs individual={ival!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(4)

print("OK")
PYEOF
) || true

    if [ "$equiv_check" = "OK" ]; then
        assert_eq "batch output equivalent to individual reduces" "OK" "OK"
    else
        assert_eq "batch output equivalent to individual reduces" "OK" "$equiv_check"
    fi

    assert_pass_if_clean "test_batch_reduce_output_equivalent_to_individual"
}
test_batch_reduce_output_equivalent_to_individual

print_summary
