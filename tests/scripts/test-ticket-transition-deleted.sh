#!/usr/bin/env bash
# tests/scripts/test-ticket-transition-deleted.sh
# RED tests for transition rejection of 'deleted' target status and
# CLOSED_STATUSES coverage in ticket-transition.sh, ticket-unblock.py,
# and the next-batch port (rebar._engine_support.next_batch).
#
# All test functions MUST FAIL until the features they test are implemented:
#   - ticket-transition.sh: reject 'deleted' as a target_status with instructional error
#   - ticket-transition.sh: CLOSED_STATUSES inline Python includes 'deleted'
#   - ticket-unblock.py: _CLOSED_STATUSES set includes 'deleted'
#   - next_batch.py: _CLOSED_STATUSES in batch logic includes 'deleted'
#
# Usage: bash tests/scripts/test-ticket-transition-deleted.sh
# Returns: exit non-zero (RED) until all features are implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_TRANSITION_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-transition.sh"
TICKET_UNBLOCK_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-unblock.py"
# next-batch is now the Python port reached via the dispatcher (Tier C retired the
# bash ticket-next-batch.sh); the CLOSED_STATUSES static check below points at it.
TICKET_NEXT_BATCH_IMPL="$REPO_ROOT/src/rebar/_engine_support/next_batch.py"

HASH_SCRIPT="$REPO_ROOT/src/rebar/_engine/compute-verdict-hash.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-transition-deleted.sh ==="

_verdict_hash() {
    local repo="$1" ticket_id="$2"
    (cd "$repo" && PROJECT_ROOT="$repo" bash "$HASH_SCRIPT" "$ticket_id" PASS 2>/dev/null)
}

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
    local extra_args="${4:-}"
    local out
    # shellcheck disable=SC2086
    out=$(cd "$repo" && bash "$TICKET_SCRIPT" create "$ticket_type" "$title" $extra_args 2>/dev/null) || true
    echo "$out" | tail -1
}

# ── Helper: write a .tombstone.json into a ticket directory ───────────────────
# Simulates the effect of `ticket delete <id>` before that command is implemented.
_write_tombstone() {
    local tracker_dir="$1"
    local ticket_id="$2"
    local status="${3:-deleted}"
    local tombstone_path="$tracker_dir/$ticket_id/.tombstone.json"
    python3 -c "
import json, sys
tombstone = {'status': sys.argv[1], 'reason': 'test tombstone'}
with open(sys.argv[2], 'w') as f:
    json.dump(tombstone, f)
" "$status" "$tombstone_path"
    git -C "$tracker_dir" add "$ticket_id/.tombstone.json" 2>/dev/null
    git -C "$tracker_dir" commit -q --no-verify -m "test: tombstone $ticket_id" 2>/dev/null || true
}

# ── Test 1: transition to 'deleted' target is rejected with non-zero exit ──────
echo "Test 1: transition to 'deleted' target is rejected (non-zero exit)"
test_transition_to_deleted_is_rejected() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Ticket to delete")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_transition_to_deleted_is_rejected"
        return
    fi

    # Run: ticket transition <id> open deleted
    local output exit_code
    exit_code=0
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open deleted 2>&1) || exit_code=$?

    # Assert: exit code is non-zero
    assert_ne "transition to deleted exits non-zero" "0" "$exit_code"

    # Assert: output contains actionable rejection message
    assert_contains "output mentions 'deleted is not a valid transition target'" \
        "deleted is not a valid transition target" "$output"

    # Assert: output guides user to the correct command
    assert_contains "output mentions 'ticket delete'" \
        "ticket delete" "$output"

    assert_pass_if_clean "test_transition_to_deleted_is_rejected"
}
test_transition_to_deleted_is_rejected

# ── Test 2: exact error message wording for 'deleted' rejection ───────────────
echo "Test 2: transition to 'deleted' produces exact error message with 'use ticket delete <id>'"
test_transition_to_deleted_error_matches_delete_command_wording() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Another ticket for delete test")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_transition_to_deleted_error_matches_delete_command_wording"
        return
    fi

    # Run: ticket transition <id> open deleted
    local output exit_code
    exit_code=0
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open deleted 2>&1) || exit_code=$?

    # Assert: exit code is non-zero
    assert_ne "transition to deleted exits non-zero" "0" "$exit_code"

    # Assert: exact error message text
    local expected_msg="deleted is not a valid transition target -- use ticket delete $ticket_id to delete a ticket"
    assert_contains "exact error message matches expected wording" \
        "$expected_msg" "$output"

    assert_pass_if_clean "test_transition_to_deleted_error_matches_delete_command_wording"
}
test_transition_to_deleted_error_matches_delete_command_wording

# ── Test 3: CLOSED_STATUSES in ticket-transition.sh open-children check ───────
# A parent with one 'deleted' child and one 'closed' child should be closeable.
# Currently fails RED because the inline CLOSED_STATUSES set does not include 'deleted'.
echo "Test 3: parent with deleted child can be closed (CLOSED_STATUSES includes 'deleted' in transition.sh)"
test_closed_statuses_includes_deleted_in_transition_sh() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create parent epic
    local parent_id
    parent_id=$(_create_ticket "$repo" epic "Parent epic")
    if [ -z "$parent_id" ]; then
        assert_eq "parent ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_closed_statuses_includes_deleted_in_transition_sh"
        return
    fi

    # Create a child task under the parent (will be tombstoned as 'deleted')
    local child_deleted_id
    child_deleted_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Child to delete" --parent "$parent_id" 2>/dev/null | tail -1) || true
    if [ -z "$child_deleted_id" ]; then
        assert_eq "deleted child ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_closed_statuses_includes_deleted_in_transition_sh"
        return
    fi

    # Create a child task under the parent (will be closed normally)
    local child_closed_id
    child_closed_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Child to close" --parent "$parent_id" 2>/dev/null | tail -1) || true
    if [ -z "$child_closed_id" ]; then
        assert_eq "closed child ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_closed_statuses_includes_deleted_in_transition_sh"
        return
    fi

    # Tombstone the first child with status=deleted
    _write_tombstone "$tracker_dir" "$child_deleted_id" "deleted"

    # Properly close the second child via transition
    local close_exit=0
    cd "$repo" && bash "$TICKET_SCRIPT" transition "$child_closed_id" open closed 2>/dev/null || close_exit=$?
    if [ "$close_exit" -ne 0 ]; then
        assert_eq "second child closed successfully" "0" "$close_exit"
        assert_pass_if_clean "test_closed_statuses_includes_deleted_in_transition_sh"
        return
    fi

    # Now try to close the parent — should succeed because both children are terminal
    # (one deleted, one closed). Fails RED if CLOSED_STATUSES doesn't include 'deleted'.
    local parent_close_output parent_close_exit
    parent_close_exit=0
    parent_close_output=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$parent_id" open closed --verdict-hash="$(_verdict_hash "$repo" "$parent_id")" 2>&1) || parent_close_exit=$?

    # Assert: parent transition exits 0 (both children are in terminal state)
    assert_eq "parent can be closed when all children are in terminal state (deleted or closed)" \
        "0" "$parent_close_exit"

    assert_pass_if_clean "test_closed_statuses_includes_deleted_in_transition_sh"
}
test_closed_statuses_includes_deleted_in_transition_sh

# ── Test 4: CLOSED_STATUSES in ticket-unblock.py includes 'deleted' ──────────
# Ticket A is blocked by ticket B (tombstoned as deleted). We close ticket C.
# ticket-unblock.py should recognize A as unblocked because B is deleted (terminal).
# Currently fails RED because _CLOSED_STATUSES = {"closed"} only. When B's dir
# exists with a .tombstone.json, reduce_ticket still returns status='open' (it ignores
# the tombstone file). So _is_closed('open') = False → B is not considered closed →
# A is not reported as newly unblocked when C is closed.
echo "Test 4: ticket-unblock.py recognizes 'deleted' tombstone status as terminal (CLOSED_STATUSES)"
test_closed_statuses_includes_deleted_in_ticket_unblock() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create three tickets: A (blocked), B (blocker, will be tombstoned), C (closing trigger)
    local ticket_a ticket_b ticket_c
    ticket_a=$(_create_ticket "$repo" task "Ticket A - blocked by deleted B")
    ticket_b=$(_create_ticket "$repo" task "Ticket B - blocker to be deleted")
    ticket_c=$(_create_ticket "$repo" task "Ticket C - the one we close to trigger unblock check")

    if [ -z "$ticket_a" ] || [ -z "$ticket_b" ] || [ -z "$ticket_c" ]; then
        assert_eq "all three tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_closed_statuses_includes_deleted_in_ticket_unblock"
        return
    fi

    # Write a LINK event directly into ticket B's directory to record "B blocks A"
    # (We write the event manually to avoid link-redirect behavior in ticket CLI)
    local ts link_uuid link_event_file
    ts=$(python3 -c "import time; print(int(time.time_ns()))")
    link_uuid=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
    link_event_file="$tracker_dir/$ticket_b/${ts}-${link_uuid}-LINK.json"
    python3 -c "
import json, sys
event = {
    'timestamp': int(sys.argv[1]),
    'uuid': sys.argv[2],
    'event_type': 'LINK',
    'env_id': 'test',
    'author': 'test',
    'data': {
        'relation': 'blocks',
        'target_id': sys.argv[3],
        'link_uuid': sys.argv[2],
    }
}
with open(sys.argv[4], 'w') as f:
    json.dump(event, f)
" "$ts" "$link_uuid" "$ticket_a" "$link_event_file"
    git -C "$tracker_dir" add "$ticket_b/" 2>/dev/null
    git -C "$tracker_dir" commit -q -m "test: LINK $ticket_b blocks $ticket_a" 2>/dev/null || true

    # Tombstone ticket B with status=deleted
    _write_tombstone "$tracker_dir" "$ticket_b" "deleted"

    # Simulate ticket delete B: call unblock with B as the newly-closed ticket.
    # Production flow: ticket-lib-api.sh ticket_delete tombstones B, then calls
    # detect_newly_unblocked([B], ...). B is in newly_closed_set; its full reducer
    # state (including LINK deps) must be loaded so blocked_by[A]={B} is built.
    local unblock_output unblock_exit
    unblock_exit=0
    unblock_output=$(python3 "$TICKET_UNBLOCK_SCRIPT" "$tracker_dir" "$ticket_b" 2>&1) || unblock_exit=$?

    # Assert: exits 0
    assert_eq "ticket-unblock.py exits 0 when run for ticket B" "0" "$unblock_exit"

    # Assert: ticket A appears in the UNBLOCKED output
    # A is blocked by B; B is tombstoned as deleted and included in the closed batch.
    # The reducer must load B's full state (for deps/blocked_by map) and override status.
    assert_contains "ticket A appears as UNBLOCKED after B is tombstoned-deleted" \
        "UNBLOCKED $ticket_a" "$unblock_output"

    assert_pass_if_clean "test_closed_statuses_includes_deleted_in_ticket_unblock"
}
test_closed_statuses_includes_deleted_in_ticket_unblock

# ── Test 5: next-batch (Python port) treats a tombstoned-deleted blocker as closed ─
# Epic has child task A which depends_on task B (tombstoned as deleted via .tombstone.json).
# next-batch should include A (B is deleted/terminal so A is unblocked).
# Fails RED because:
#   1. ticket list returns B with status='open' (reducer ignores .tombstone.json)
#   2. CLOSED_STATUSES = {"closed","done","completed"} — 'deleted' not included
#   3. ticket_status_map[B] = 'open' → A is treated as blocked → excluded from batch
echo "Test 5: ticket-next-batch includes task when its depends_on blocker is tombstoned-deleted"
test_closed_statuses_includes_deleted_in_next_batch() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create an epic (container only — won't be filtered by parent-redirect since
    # we write the LINK event for A directly without going through the ticket CLI)
    local epic_id
    epic_id=$(_create_ticket "$repo" epic "Test epic for next-batch deleted blocker")
    if [ -z "$epic_id" ]; then
        assert_eq "epic created" "non-empty" "empty"
        assert_pass_if_clean "test_closed_statuses_includes_deleted_in_next_batch"
        return
    fi

    # Create task A as a child of the epic
    local task_a
    task_a=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Task A - depends on deleted B" --parent "$epic_id" 2>/dev/null | tail -1) || true
    if [ -z "$task_a" ]; then
        assert_eq "task A created under epic" "non-empty" "empty"
        assert_pass_if_clean "test_closed_statuses_includes_deleted_in_next_batch"
        return
    fi

    # Create task B (the blocker) — also as a child of the epic so the link
    # isn't redirected to the epic level (both A and B are same-level siblings)
    local task_b
    task_b=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Task B - to be tombstoned deleted" --parent "$epic_id" 2>/dev/null | tail -1) || true
    if [ -z "$task_b" ]; then
        assert_eq "task B created under epic" "non-empty" "empty"
        assert_pass_if_clean "test_closed_statuses_includes_deleted_in_next_batch"
        return
    fi

    # Write LINK event directly into A's directory to record "A depends_on B"
    # (Avoids link-redirect which promotes links from children to parent epic)
    local ts link_uuid link_event_file
    ts=$(python3 -c "import time; print(int(time.time_ns()))")
    link_uuid=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
    link_event_file="$tracker_dir/$task_a/${ts}-${link_uuid}-LINK.json"
    python3 -c "
import json, sys
event = {
    'timestamp': int(sys.argv[1]),
    'uuid': sys.argv[2],
    'event_type': 'LINK',
    'env_id': 'test',
    'author': 'test',
    'data': {
        'relation': 'depends_on',
        'target_id': sys.argv[3],
        'link_uuid': sys.argv[2],
    }
}
with open(sys.argv[4], 'w') as f:
    json.dump(event, f)
" "$ts" "$link_uuid" "$task_b" "$link_event_file"
    git -C "$tracker_dir" add "$task_a/" 2>/dev/null
    git -C "$tracker_dir" commit -q -m "test: LINK $task_a depends_on $task_b" 2>/dev/null || true

    # Tombstone task B with status=deleted
    _write_tombstone "$tracker_dir" "$task_b" "deleted"

    # Run ticket-next-batch for the epic
    local batch_output batch_exit
    batch_exit=0
    batch_output=$(cd "$repo" && \
        TICKETS_TRACKER_DIR="$tracker_dir" \
        bash "$TICKET_SCRIPT" next-batch "$epic_id" 2>/dev/null) || batch_exit=$?

    # Assert: next-batch exits 0
    assert_eq "ticket-next-batch exits 0" "0" "$batch_exit"

    # Assert: task A appears as a TASK in the batch output (B deleted → A not blocked)
    # Fails RED: ticket list returns B with status='open' (tombstone ignored by reducer).
    # CLOSED_STATUSES doesn't include 'deleted' → A is treated as blocked → excluded.
    assert_contains "TASK line for A appears in next batch (B tombstoned-deleted)" \
        "TASK: $task_a" "$batch_output"

    assert_pass_if_clean "test_closed_statuses_includes_deleted_in_next_batch"
}
test_closed_statuses_includes_deleted_in_next_batch

# ── Test 6: transition to 'deleted' from in_progress is also rejected ─────────
echo "Test 6: transition to 'deleted' from in_progress is also rejected"
test_transition_to_deleted_from_in_progress_rejected() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "In-progress ticket for delete test")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_transition_to_deleted_from_in_progress_rejected"
        return
    fi

    # Transition to in_progress first
    cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open in_progress 2>/dev/null || true

    # Now attempt to transition from in_progress to deleted — must also be rejected
    local output exit_code
    exit_code=0
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" in_progress deleted 2>&1) || exit_code=$?

    assert_ne "transition in_progress→deleted exits non-zero" "0" "$exit_code"
    assert_contains "output mentions 'deleted is not a valid transition target'" \
        "deleted is not a valid transition target" "$output"

    assert_pass_if_clean "test_transition_to_deleted_from_in_progress_rejected"
}
test_transition_to_deleted_from_in_progress_rejected

# ── Test 7: ticket status unchanged after rejected transition to deleted ────────
echo "Test 7: ticket status remains unchanged after rejected transition to deleted"
test_transition_to_deleted_leaves_status_unchanged() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Status stability test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_transition_to_deleted_leaves_status_unchanged"
        return
    fi

    # Attempt rejected transition
    cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open deleted 2>/dev/null || true

    # Assert: status is still 'open' (not 'deleted')
    local show_output status_val
    show_output=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || show_output=""
    status_val=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('status', ''))
except Exception:
    print('')
" "$show_output" 2>/dev/null) || status_val=""

    assert_eq "ticket status still open after rejected delete-transition" "open" "$status_val"

    assert_pass_if_clean "test_transition_to_deleted_leaves_status_unchanged"
}
test_transition_to_deleted_leaves_status_unchanged

# ── Test 8: parent with ONLY deleted children (no closed) can be closed ────────
echo "Test 8: parent with only deleted children (no closed) can be closed"
test_closed_statuses_parent_with_only_deleted_children_closeable() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local parent_id
    parent_id=$(_create_ticket "$repo" epic "Parent with only deleted children")
    if [ -z "$parent_id" ]; then
        assert_eq "parent created" "non-empty" "empty"
        assert_pass_if_clean "test_closed_statuses_parent_with_only_deleted_children_closeable"
        return
    fi

    local child1 child2
    child1=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Child 1 to delete" --parent "$parent_id" 2>/dev/null | tail -1) || true
    child2=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Child 2 to delete" --parent "$parent_id" 2>/dev/null | tail -1) || true

    if [ -z "$child1" ] || [ -z "$child2" ]; then
        assert_eq "both children created" "non-empty" "empty"
        assert_pass_if_clean "test_closed_statuses_parent_with_only_deleted_children_closeable"
        return
    fi

    # Tombstone both children as deleted
    _write_tombstone "$tracker_dir" "$child1" "deleted"
    _write_tombstone "$tracker_dir" "$child2" "deleted"

    # Assert: parent can be closed when all children are deleted
    local parent_exit=0
    cd "$repo" && bash "$TICKET_SCRIPT" transition "$parent_id" open closed --verdict-hash="$(_verdict_hash "$repo" "$parent_id")" 2>/dev/null || parent_exit=$?

    assert_eq "parent closeable with all-deleted children" "0" "$parent_exit"

    assert_pass_if_clean "test_closed_statuses_parent_with_only_deleted_children_closeable"
}
test_closed_statuses_parent_with_only_deleted_children_closeable

# ── Test 9: deleted status appears in CLOSED_STATUSES in all 3 source files ────
echo "Test 9: 'deleted' string appears in CLOSED_STATUSES in all three source locations"
test_closed_statuses_deleted_present_in_all_three_files() {
    _snapshot_fail

    # Static check: grep for 'deleted' in each CLOSED_STATUSES definition
    local transition_sh_has unblock_py_has next_batch_has

    transition_sh_has=$(grep -c "'deleted'" "$REPO_ROOT/src/rebar/_engine/ticket-transition.sh" 2>/dev/null) || transition_sh_has=0
    unblock_py_has=$(grep -c '"deleted"' "$REPO_ROOT/src/rebar/_engine/ticket-unblock.py" 2>/dev/null) || unblock_py_has=0
    next_batch_has=$(grep -c '"deleted"' "$TICKET_NEXT_BATCH_IMPL" 2>/dev/null) || next_batch_has=0

    assert_ne "'deleted' present in ticket-transition.sh" "0" "$transition_sh_has"
    assert_ne "'deleted' present in ticket-unblock.py" "0" "$unblock_py_has"
    assert_ne "'deleted' present in next_batch.py (_CLOSED_STATUSES)" "0" "$next_batch_has"

    assert_pass_if_clean "test_closed_statuses_deleted_present_in_all_three_files"
}
test_closed_statuses_deleted_present_in_all_three_files

# ── Test 10: next-batch excludes task when its BLOCKS-relation blocker is deleted
echo "Test 10: next-batch treats deleted ticket as terminal for 'blocks' relation too"
test_closed_statuses_blocks_relation_with_deleted_blocker_resolved() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local epic_id
    epic_id=$(_create_ticket "$repo" epic "Test epic for blocks-relation delete")
    if [ -z "$epic_id" ]; then
        assert_eq "epic created" "non-empty" "empty"
        assert_pass_if_clean "test_closed_statuses_blocks_relation_with_deleted_blocker_resolved"
        return
    fi

    local task_a task_b
    task_a=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Task A blocked by B" --parent "$epic_id" 2>/dev/null | tail -1) || true
    task_b=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Task B blocks A (to be deleted)" --parent "$epic_id" 2>/dev/null | tail -1) || true

    if [ -z "$task_a" ] || [ -z "$task_b" ]; then
        assert_eq "both tasks created" "non-empty" "empty"
        assert_pass_if_clean "test_closed_statuses_blocks_relation_with_deleted_blocker_resolved"
        return
    fi

    # Write LINK directly: B blocks A
    local ts link_uuid link_event_file
    ts=$(python3 -c "import time; print(int(time.time_ns()))")
    link_uuid=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
    link_event_file="$tracker_dir/$task_b/${ts}-${link_uuid}-LINK.json"
    python3 -c "
import json, sys
event = {
    'timestamp': int(sys.argv[1]),
    'uuid': sys.argv[2],
    'event_type': 'LINK',
    'env_id': 'test',
    'author': 'test',
    'data': {'relation': 'blocks', 'target_id': sys.argv[3], 'link_uuid': sys.argv[2]},
}
with open(sys.argv[4], 'w') as f:
    json.dump(event, f)
" "$ts" "$link_uuid" "$task_a" "$link_event_file"
    git -C "$tracker_dir" add "$task_b/" 2>/dev/null
    git -C "$tracker_dir" commit -q -m "test: LINK $task_b blocks $task_a" 2>/dev/null || true

    # Tombstone task B
    _write_tombstone "$tracker_dir" "$task_b" "deleted"

    # next-batch should include task_a (blocker B is deleted/terminal)
    local batch_output batch_exit
    batch_exit=0
    batch_output=$(cd "$repo" && \
        TICKETS_TRACKER_DIR="$tracker_dir" \
        bash "$TICKET_SCRIPT" next-batch "$epic_id" 2>/dev/null) || batch_exit=$?

    assert_eq "next-batch exits 0" "0" "$batch_exit"
    assert_contains "task A in batch when blocks-relation blocker is deleted" \
        "TASK: $task_a" "$batch_output"

    assert_pass_if_clean "test_closed_statuses_blocks_relation_with_deleted_blocker_resolved"
}
test_closed_statuses_blocks_relation_with_deleted_blocker_resolved

print_summary
