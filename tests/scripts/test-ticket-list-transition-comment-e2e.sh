#!/usr/bin/env bash
# tests/scripts/test-ticket-list-transition-comment-e2e.sh
# End-to-end integration test for the ticket list + transition + comment workflow.
#
# Exercises: create → list → comment → transition → list → concurrency rejection
#            → transition → list → comment → show (with comments)
# Ghost prevention: directories without CREATE events cause errors on
#   transition and comment operations, and appear as error-status in list.
#
# Usage: bash tests/scripts/test-ticket-list-transition-comment-e2e.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e is intentionally omitted — test functions return non-zero by design.
# -e would abort the runner on expected assertion mismatches.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-list-transition-comment-e2e.sh ==="

# ── Helper: create a fresh temp git repo with ticket system initialized ───────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: get field from JSON string ────────────────────────────────────────
_json_field() {
    local json="$1"
    local field="$2"
    python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    v = data.get(sys.argv[2])
    print('' if v is None else v)
except Exception:
    print('')
" "$json" "$field" 2>/dev/null || true
}

# ── Helper: get ticket status from JSON array by id ───────────────────────────
_list_status_for() {
    local list_json="$1"
    local ticket_id="$2"
    python3 -c "
import json, sys
try:
    items = json.loads(sys.argv[1])
    for item in items:
        if item.get('ticket_id') == sys.argv[2]:
            print(item.get('status', ''))
            sys.exit(0)
    print('NOT_FOUND')
except Exception as e:
    print('ERROR:' + str(e))
" "$list_json" "$ticket_id" 2>/dev/null || true
}

# ── Helper: get ticket error from JSON array by id ────────────────────────────
_list_error_for() {
    local list_json="$1"
    local ticket_id="$2"
    python3 -c "
import json, sys
try:
    items = json.loads(sys.argv[1])
    for item in items:
        if item.get('ticket_id') == sys.argv[2]:
            print(item.get('error', 'NO_ERROR_FIELD'))
            sys.exit(0)
    print('NOT_FOUND')
except Exception as e:
    print('ERROR:' + str(e))
" "$list_json" "$ticket_id" 2>/dev/null || true
}

# ── Helper: count comment bodies in show output ───────────────────────────────
_comment_count() {
    local show_json="$1"
    python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    print(len(data.get('comments', [])))
except Exception:
    print(0)
" "$show_json" 2>/dev/null || true
}

# ── Helper: get Nth comment body (0-indexed) ──────────────────────────────────
_comment_body() {
    local show_json="$1"
    local idx="$2"
    python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    comments = data.get('comments', [])
    idx = int(sys.argv[2])
    if idx < len(comments):
        print(comments[idx].get('body', ''))
    else:
        print('INDEX_OUT_OF_RANGE')
except Exception as e:
    print('ERROR:' + str(e))
" "$show_json" "$idx" 2>/dev/null || true
}

# ── Test 1: test_list_transition_comment_full_lifecycle ──────────────────────
echo "Test 1: full lifecycle — create → list → comment → transition → list → close → comment → show"
test_list_transition_comment_full_lifecycle() {
    _snapshot_fail
    local repo
    repo=$(_make_test_repo)

    # Initialize ticket system
    local init_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || init_exit=$?
    assert_eq "lifecycle: init exits 0" "0" "$init_exit"
    if [ "$init_exit" -ne 0 ]; then return; fi

    # Step 1: Create two tickets
    local id1 id2
    id1=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "First task" 2>/dev/null | tail -1) || true
    id2=$(cd "$repo" && bash "$TICKET_SCRIPT" create bug "Second bug" 2>/dev/null | tail -1) || true

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "lifecycle: both ticket IDs are non-empty" "non-empty" "empty: '${id1:-}' '${id2:-}'"
        return
    else
        assert_eq "lifecycle: both ticket IDs are non-empty" "non-empty" "non-empty"
    fi

    # Step 2: ticket list → both appear with status='open'
    local list_out
    list_out=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || true

    local s1_initial s2_initial
    s1_initial=$(_list_status_for "$list_out" "$id1")
    s2_initial=$(_list_status_for "$list_out" "$id2")
    assert_eq "lifecycle: list shows id1 with status=open" "open" "$s1_initial"
    assert_eq "lifecycle: list shows id2 with status=open" "open" "$s2_initial"

    # Step 3: ticket comment <id1> 'starting work'
    local comment1_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" comment "$id1" "starting work" 2>/dev/null) || comment1_exit=$?
    assert_eq "lifecycle: comment 'starting work' exits 0" "0" "$comment1_exit"

    # Step 4: ticket transition <id1> open in_progress
    local transition1_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$id1" open in_progress 2>/dev/null) || transition1_exit=$?
    assert_eq "lifecycle: transition open→in_progress exits 0" "0" "$transition1_exit"

    # Step 5: ticket list → id1 has status='in_progress', id2 still 'open'
    list_out=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || true

    local s1_after_transition s2_unchanged
    s1_after_transition=$(_list_status_for "$list_out" "$id1")
    s2_unchanged=$(_list_status_for "$list_out" "$id2")
    assert_eq "lifecycle: id1 status=in_progress after transition" "in_progress" "$s1_after_transition"
    assert_eq "lifecycle: id2 status=open (unchanged)" "open" "$s2_unchanged"

    # Step 6: transition with wrong current_status → exits non-zero
    # id1 is now in_progress; supplying 'open' as current_status should fail
    local bad_transition_exit=0
    local bad_transition_err
    bad_transition_err=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$id1" open closed 2>&1) || bad_transition_exit=$?
    assert_ne "lifecycle: wrong current_status → non-zero exit" "0" "$bad_transition_exit"
    # Verify error message mentions the actual status 'in_progress'
    assert_contains "lifecycle: error message mentions actual status 'in_progress'" "in_progress" "$bad_transition_err"

    # Step 7: ticket transition <id1> in_progress closed
    local transition2_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$id1" in_progress closed 2>/dev/null) || transition2_exit=$?
    assert_eq "lifecycle: transition in_progress→closed exits 0" "0" "$transition2_exit"

    # Step 8: ticket list → id1 has status='closed'
    list_out=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || true
    local s1_closed
    s1_closed=$(_list_status_for "$list_out" "$id1")
    assert_eq "lifecycle: id1 status=closed" "closed" "$s1_closed"

    # Step 9: ticket comment <id1> 'done'
    local comment2_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" comment "$id1" "done" 2>/dev/null) || comment2_exit=$?
    assert_eq "lifecycle: comment 'done' exits 0" "0" "$comment2_exit"

    # Step 10: ticket show <id1> → comments contains both comments in order
    local show_out
    show_out=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$id1" 2>/dev/null) || true

    local comment_count
    comment_count=$(_comment_count "$show_out")
    assert_eq "lifecycle: show has 2 comments" "2" "$comment_count"

    local body0 body1
    body0=$(_comment_body "$show_out" 0)
    body1=$(_comment_body "$show_out" 1)
    assert_eq "lifecycle: comment[0] is 'starting work'" "starting work" "$body0"
    assert_eq "lifecycle: comment[1] is 'done'" "done" "$body1"

    assert_pass_if_clean "test_list_transition_comment_full_lifecycle"
}
test_list_transition_comment_full_lifecycle

# ── Test 2: test_ghost_prevention_e2e ────────────────────────────────────────
echo "Test 2: ghost prevention — ticket dir without CREATE → errors on transition/comment, error status in list"
test_ghost_prevention_e2e() {
    _snapshot_fail
    local repo
    repo=$(_make_test_repo)

    local init_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || init_exit=$?
    assert_eq "ghost: init exits 0" "0" "$init_exit"
    if [ "$init_exit" -ne 0 ]; then return; fi

    # Step 11: manually create a directory with no event files (true ghost)
    local ghost_id="ghost-xyz"
    mkdir -p "$repo/.tickets-tracker/$ghost_id"

    # Step 12: ticket list → ghost-xyz appears with error status (not crash)
    local list_exit=0
    local list_out
    list_out=$(cd "$repo" && bash "$TICKET_SCRIPT" list --status=error 2>/dev/null) || list_exit=$?
    # list should still exit 0 even with a ghost ticket
    assert_eq "ghost: list exits 0 despite ghost ticket" "0" "$list_exit"

    # Ghost with no event files: reducer returns None → list falls back to error state
    # The fallback path in ticket-list.sh produces: {"ticket_id": "ghost-xyz", "status": "error", ...}
    # Note: error-state tickets are filtered from default list; use --status=error to see them.
    local ghost_status
    ghost_status=$(_list_status_for "$list_out" "$ghost_id")
    assert_eq "ghost: ghost-xyz appears with status=error in list" "error" "$ghost_status"

    # Verify the error field value in the error-state dict (contract: three keys {status, error, ticket_id})
    local ghost_error
    ghost_error=$(_list_error_for "$list_out" "$ghost_id")
    assert_eq "ghost: error field is 'reducer_failed' (no event files → reducer returns None)" "reducer_failed" "$ghost_error"

    # Step 13: ticket transition ghost-xyz → exits non-zero
    local ghost_transition_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ghost_id" open in_progress 2>/dev/null) || ghost_transition_exit=$?
    assert_ne "ghost: transition on ghost → non-zero exit" "0" "$ghost_transition_exit"

    # Step 14: ticket comment ghost-xyz → exits non-zero
    local ghost_comment_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" comment "$ghost_id" "test" 2>/dev/null) || ghost_comment_exit=$?
    assert_ne "ghost: comment on ghost → non-zero exit" "0" "$ghost_comment_exit"

    assert_pass_if_clean "test_ghost_prevention_e2e"
}
test_ghost_prevention_e2e

# ── Test 3: test_multiple_tickets_in_list ────────────────────────────────────
echo "Test 3: multiple tickets — list contains all created ticket IDs"
test_multiple_tickets_in_list() {
    _snapshot_fail
    local repo
    repo=$(_make_test_repo)

    local init_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || init_exit=$?
    assert_eq "multi-list: init exits 0" "0" "$init_exit"
    if [ "$init_exit" -ne 0 ]; then return; fi

    # Create three tickets of different types
    local id1 id2 id3
    id1=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Alpha" 2>/dev/null | tail -1) || true
    id2=$(cd "$repo" && bash "$TICKET_SCRIPT" create bug "Beta" 2>/dev/null | tail -1) || true
    id3=$(cd "$repo" && bash "$TICKET_SCRIPT" create story "Gamma" 2>/dev/null | tail -1) || true

    if [ -z "$id1" ] || [ -z "$id2" ] || [ -z "$id3" ]; then
        assert_eq "multi-list: all 3 IDs non-empty" "non-empty" "some-empty: '${id1:-}' '${id2:-}' '${id3:-}'"
        return
    else
        assert_eq "multi-list: all 3 IDs non-empty" "non-empty" "non-empty"
    fi

    # Run ticket list and verify all three appear
    local list_out
    list_out=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || true

    local count
    count=$(python3 -c "
import json, sys
try:
    items = json.loads(sys.argv[1])
    print(len(items))
except Exception:
    print(0)
" "$list_out" 2>/dev/null) || true
    assert_eq "multi-list: list contains 3 tickets" "3" "$count"

    local s1 s2 s3
    s1=$(_list_status_for "$list_out" "$id1")
    s2=$(_list_status_for "$list_out" "$id2")
    s3=$(_list_status_for "$list_out" "$id3")
    assert_eq "multi-list: id1 status=open" "open" "$s1"
    assert_eq "multi-list: id2 status=open" "open" "$s2"
    assert_eq "multi-list: id3 status=open" "open" "$s3"

    assert_pass_if_clean "test_multiple_tickets_in_list"
}
test_multiple_tickets_in_list

# ── Test 4: test_fsck_needed_corrupt_create ──────────────────────────────────
echo "Test 4: fsck_needed — corrupt CREATE (valid JSON, missing ticket_type) → status=fsck_needed, transition/comment non-zero"
test_fsck_needed_corrupt_create() {
    _snapshot_fail
    local repo
    repo=$(_make_test_repo)

    local init_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || init_exit=$?
    assert_eq "fsck: init exits 0" "0" "$init_exit"
    if [ "$init_exit" -ne 0 ]; then return; fi

    # Create a ticket directory with a parseable CREATE event missing ticket_type
    local corrupt_id="corrupt-abc"
    mkdir -p "$repo/.tickets-tracker/$corrupt_id"
    python3 -c "
import json
event = {'event_type': 'CREATE', 'data': {'title': 'Missing type field'}}
print(json.dumps(event))
" > "$repo/.tickets-tracker/$corrupt_id/001_CREATE.json"

    # Step 1: ticket list → corrupt-abc appears with status='fsck_needed', exits 0
    local list_exit=0
    local list_out
    list_out=$(cd "$repo" && bash "$TICKET_SCRIPT" list --status=fsck_needed 2>/dev/null) || list_exit=$?
    assert_eq "fsck: list exits 0 despite corrupt CREATE" "0" "$list_exit"

    local corrupt_status
    corrupt_status=$(_list_status_for "$list_out" "$corrupt_id")
    assert_eq "fsck: corrupt-abc appears with status=fsck_needed in list" "fsck_needed" "$corrupt_status"

    # Verify error field is 'corrupt_create_event'
    local corrupt_error
    corrupt_error=$(_list_error_for "$list_out" "$corrupt_id")
    assert_eq "fsck: error field is 'corrupt_create_event'" "corrupt_create_event" "$corrupt_error"

    # Step 2: ticket transition corrupt-abc → exits non-zero
    local fsck_transition_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$corrupt_id" open in_progress 2>/dev/null) || fsck_transition_exit=$?
    assert_ne "fsck: transition on fsck_needed ticket → non-zero exit" "0" "$fsck_transition_exit"

    # Step 3: ticket comment corrupt-abc → exits non-zero
    local fsck_comment_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" comment "$corrupt_id" "test" 2>/dev/null) || fsck_comment_exit=$?
    assert_ne "fsck: comment on fsck_needed ticket → non-zero exit" "0" "$fsck_comment_exit"

    assert_pass_if_clean "test_fsck_needed_corrupt_create"
}
test_fsck_needed_corrupt_create

print_summary
