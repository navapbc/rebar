#!/usr/bin/env bash
# tests/acceptance/test-ticket-delete-e2e.sh
# E2E acceptance tests for the full ticket delete lifecycle.
#
# GREEN tests — these must PASS because ticket delete is already implemented.
# Covers: --user-approved guard, children-block deletion, list visibility,
# --include-archived visibility, ready_to_work unblocking after delete,
# transitioning a deleted ticket exits non-zero, archived==True in compiled
# state, parent epic closure with mixed closed+deleted children, and bridge
# routing verification (delete call, not status-transition).
#
# Usage: bash tests/acceptance/test-ticket-delete-e2e.sh

# NOTE: -e is intentionally omitted — assertion helpers and early-return guards
# use non-zero returns by design. -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
HASH_SCRIPT="$REPO_ROOT/src/rebar/_engine/compute-verdict-hash.sh"

# ── Helper: close a story/epic through the verdict-hash gate ──────────────────
# Story/epic closure requires a verified completion verdict hash (commit a6e8925206).
# Compute the real HMAC the same way ticket-transition.sh does (same PROJECT_ROOT
# and cwd → same HEAD SHA and .closure-key), then pass it as --verdict-hash so the
# closure exits 0 under the gate.
_close_with_verdict() {
    local repo="$1"
    local ticket_id="$2"
    local from_status="${3:-open}"
    local hash
    hash=$(cd "$repo" && PROJECT_ROOT="$repo" bash "$HASH_SCRIPT" "$ticket_id" "PASS" 2>/dev/null)
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" "$from_status" closed --verdict-hash="$hash" >/dev/null 2>&1)
}

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-delete-e2e.sh ==="

# ── Helper: create a fresh ticket-enabled repo ────────────────────────────────
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

# ── Helper: extract a JSON field from `ticket deps` output ───────────────────
_deps_field() {
    local repo="$1"
    local ticket_id="$2"
    local field="$3"
    local output
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$ticket_id" 2>/dev/null) || output=""
    python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    val = d.get(sys.argv[2])
    print(str(val).lower() if isinstance(val, bool) else (str(val) if val is not None else ''))
except Exception:
    print('')
" "$output" "$field" 2>/dev/null || true
}

# ── Helper: extract a field from `ticket show` output ────────────────────────
_show_field() {
    local repo="$1"
    local ticket_id="$2"
    local field="$3"
    local show_output
    show_output=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || show_output=""
    python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    val = d.get(sys.argv[2])
    print(str(val).lower() if isinstance(val, bool) else (str(val) if val is not None else ''))
except Exception:
    print('')
" "$show_output" "$field" 2>/dev/null || true
}

# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Full lifecycle — epic with two child stories, delete one after closing
# the other; verifies status:deleted, archived:true, blocker removal, list
# visibility, transition rejection, and parent epic closure.
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test E2E-1: full lifecycle (epic+2 children, close one, delete other → parent closes)"
test_full_delete_lifecycle() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create epic E and two child stories S1, S2
    local epic_id s1_id s2_id
    epic_id=$(_create_ticket "$repo" epic "Parent epic")

    if [ -z "$epic_id" ]; then
        assert_eq "epic created" "non-empty" "empty"
        assert_pass_if_clean "test_full_delete_lifecycle"
        return
    fi

    s1_id=$(_create_ticket "$repo" story "Child story one" "--parent $epic_id")
    s2_id=$(_create_ticket "$repo" story "Child story two" "--parent $epic_id")

    if [ -z "$s1_id" ] || [ -z "$s2_id" ]; then
        assert_eq "both child stories created" "non-empty" "empty"
        assert_pass_if_clean "test_full_delete_lifecycle"
        return
    fi

    # Transition S1 to closed (so we have one closed + one open child).
    # Stories require a verified completion verdict hash to close (commit a6e8925206).
    local close_exit=0
    _close_with_verdict "$repo" "$s1_id" open || close_exit=$?
    assert_eq "transition S1 to closed exits 0" "0" "$close_exit"

    # Delete S2 with --user-approved
    local delete_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" delete "$s2_id" --user-approved >/dev/null 2>&1) || delete_exit=$?
    assert_eq "delete S2 exits 0" "0" "$delete_exit"

    # ticket show S2 should return status:deleted
    local status_val
    status_val=$(_show_field "$repo" "$s2_id" "status")
    assert_eq "ticket show returns status:deleted" "deleted" "$status_val"

    # ticket show S2 should return archived:true
    local archived_val
    archived_val=$(_show_field "$repo" "$s2_id" "archived")
    assert_eq "ticket show returns archived:true" "true" "$archived_val"

    # ticket list should NOT include S2 (deleted tickets excluded by default)
    local list_after
    list_after=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || list_after=""
    local still_present=0
    [[ "$list_after" == *"$s2_id"* ]] && still_present=1
    assert_eq "deleted ticket absent from ticket list" "0" "$still_present"

    # Transitioning a deleted ticket should fail (deleted is terminal)
    local transition_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$s2_id" deleted closed >/dev/null 2>&1) || transition_exit=$?
    assert_ne "transition deleted→closed exits non-zero" "0" "$transition_exit"

    # Parent epic should be closeable with one closed + one deleted child.
    # Epics require a verified completion verdict hash to close (commit a6e8925206).
    local epic_close_exit=0
    _close_with_verdict "$repo" "$epic_id" open || epic_close_exit=$?
    assert_eq "parent epic closes with mixed closed+deleted children" "0" "$epic_close_exit"

    assert_pass_if_clean "test_full_delete_lifecycle"
}
test_full_delete_lifecycle

# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Deleting a blocker sets ready_to_work=true on the dependent ticket
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test E2E-2: deleting a blocker unblocks dependent (ready_to_work=true)"
test_delete_clears_blocker() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local tkt_a tkt_d
    tkt_a=$(_create_ticket "$repo" task "Blocking task A")
    tkt_d=$(_create_ticket "$repo" task "Dependent task D")

    if [ -z "$tkt_a" ] || [ -z "$tkt_d" ]; then
        assert_eq "both tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_delete_clears_blocker"
        return
    fi

    # Link A blocks D
    local link_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$tkt_a" "$tkt_d" blocks >/dev/null 2>&1) || link_exit=$?
    assert_eq "link A blocks D exits 0" "0" "$link_exit"

    # D should NOT be ready_to_work before delete
    local rtw_before
    rtw_before=$(_deps_field "$repo" "$tkt_d" "ready_to_work")
    assert_eq "D not ready_to_work before delete" "false" "$rtw_before"

    # Delete A
    local delete_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" delete "$tkt_a" --user-approved >/dev/null 2>&1) || delete_exit=$?
    assert_eq "delete A exits 0" "0" "$delete_exit"

    # D should now be ready_to_work=true
    local rtw_after
    rtw_after=$(_deps_field "$repo" "$tkt_d" "ready_to_work")
    assert_eq "D ready_to_work=true after blocker deleted" "true" "$rtw_after"

    assert_pass_if_clean "test_delete_clears_blocker"
}
test_delete_clears_blocker

# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Children block deletion
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test E2E-3: children block deletion"
test_children_block_deletion() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local parent_id child_id
    parent_id=$(_create_ticket "$repo" epic "Epic with children")

    if [ -z "$parent_id" ]; then
        assert_eq "parent epic created" "non-empty" "empty"
        assert_pass_if_clean "test_children_block_deletion"
        return
    fi

    child_id=$(_create_ticket "$repo" story "Child story" "--parent $parent_id")

    if [ -z "$child_id" ]; then
        assert_eq "child story created" "non-empty" "empty"
        assert_pass_if_clean "test_children_block_deletion"
        return
    fi

    # Attempt to delete the parent — should be blocked
    local exit_code=0
    local combined_output
    combined_output=$(cd "$repo" && bash "$TICKET_SCRIPT" delete "$parent_id" --user-approved 2>&1) || exit_code=$?

    assert_ne "delete with children exits non-zero" "0" "$exit_code"
    assert_contains "output contains child ticket ID" "$child_id" "$combined_output"

    assert_pass_if_clean "test_children_block_deletion"
}
test_children_block_deletion

# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Delete without --user-approved is rejected
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test E2E-4: delete without --user-approved exits non-zero with usage hint"
test_delete_requires_user_approved() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Guard flag test")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for guard flag test" "non-empty" "empty"
        assert_pass_if_clean "test_delete_requires_user_approved"
        return
    fi

    local exit_code=0
    local combined_output
    combined_output=$(cd "$repo" && bash "$TICKET_SCRIPT" delete "$ticket_id" 2>&1) || exit_code=$?

    assert_ne "delete without --user-approved exits non-zero" "0" "$exit_code"
    assert_contains "output mentions --user-approved" "--user-approved" "$combined_output"

    assert_pass_if_clean "test_delete_requires_user_approved"
}
test_delete_requires_user_approved

# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Deleted ticket absent from list; present with --include-archived
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test E2E-5: deleted ticket absent from list, present with --include-archived"
test_delete_list_visibility() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Ticket for visibility test")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for visibility test" "non-empty" "empty"
        assert_pass_if_clean "test_delete_list_visibility"
        return
    fi

    # Confirm it's visible before deletion
    local list_before
    list_before=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || list_before=""
    assert_contains "ticket visible before delete" "$ticket_id" "$list_before"

    # Delete the ticket
    local delete_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" delete "$ticket_id" --user-approved >/dev/null 2>&1) || delete_exit=$?
    assert_eq "delete exits 0" "0" "$delete_exit"

    # Should NOT appear in default list
    local list_after
    list_after=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || list_after=""
    local present_default=0
    [[ "$list_after" == *"$ticket_id"* ]] && present_default=1
    assert_eq "deleted ticket absent from default list" "0" "$present_default"

    # SHOULD appear in --include-archived list
    local list_archived
    list_archived=$(cd "$repo" && bash "$TICKET_SCRIPT" list --include-archived 2>/dev/null) || list_archived=""
    assert_contains "deleted ticket present in --include-archived list" "$ticket_id" "$list_archived"

    assert_pass_if_clean "test_delete_list_visibility"
}
test_delete_list_visibility

# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Reconciler outbound differ routes a deleted ticket to the "Done"
# transition (no separate delete_issue call).
#
# History: the edge-triggered bridge (bridge/_outbound_handlers.handle_status_event,
# which called delete_issue for status==deleted) was removed in commit a3a3928f52
# when epic 3a03 cut over to the level-triggered reconciler. The reconciler has no
# delete_issue route — instead, dso_reconciler.outbound_differ maps a deleted local
# status to the Jira "Done" status (deleted -> Done) and excludes deleted tickets
# from outbound mutations by default. This test asserts that CURRENT behavior:
#   1. _map_local_to_jira_fields maps status "deleted" -> "Done".
#   2. compute_outbound_mutations excludes deleted tickets by default (no mutation).
#   3. When deleted is NOT excluded, the produced mutation is an "update" carrying
#      status "Done" — confirming there is NO delete-action / delete_issue route.
# Uses unittest.mock for the binding store; no real ACLI calls.
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test E2E-6: reconciler outbound differ maps deleted -> Done (no delete_issue route)"
test_reconciler_routes_deleted_to_done() {
    _snapshot_fail

    # Write the test script to a temp file (avoids heredoc-in-subshell issues)
    local py_script exit_code py_output
    py_script=$(mktemp "${TMPDIR:-/tmp}/reconciler-routing-test-XXXXXX".py)
    cat > "$py_script" << 'PYEOF'
import sys, os

repo_root = sys.argv[1]
sys.path.insert(0, os.path.join(repo_root, 'src', 'rebar', '_engine'))

from dso_reconciler.outbound_differ import (
    _map_local_to_jira_fields,
    compute_outbound_mutations,
)


class _FakeBindingStore:
    """Minimal BindingStoreProtocol stub: maps local_id -> jira_key."""

    def __init__(self, mapping):
        self._mapping = mapping

    def get_jira_key(self, local_id):
        return self._mapping.get(local_id)

    def is_bound(self, local_id):
        return local_id in self._mapping


ticket = {
    'ticket_id': 'test-del-0001',
    'title': 'test ticket',
    'description': '',
    'status': 'deleted',
    'ticket_type': 'task',
}

# (1) Field mapping: deleted local status -> Jira "Done".
mapped = _map_local_to_jira_fields(ticket)
assert mapped.get('status') == 'Done', \
    f'expected deleted -> Done, got {mapped.get("status")!r}'

binding_store = _FakeBindingStore({'test-del-0001': 'TEST-42'})
jira_snapshot = {'TEST-42': {'fields': {}}}

# (2) Default behavior: deleted tickets are excluded from outbound mutations.
default_mutations = compute_outbound_mutations(
    [ticket], jira_snapshot, binding_store,
)
assert default_mutations == [], \
    f'deleted ticket should be excluded by default, got {default_mutations!r}'

# (3) When deleted is NOT excluded, the reconciler emits an UPDATE carrying the
# Done status — never a delete action. This is the positive proof that the new
# reconciler routes deletion via the Done transition rather than a delete_issue call.
included_mutations = compute_outbound_mutations(
    [ticket], jira_snapshot, binding_store, excluded_statuses=set(),
)
assert len(included_mutations) == 1, \
    f'expected exactly one mutation, got {included_mutations!r}'
mutation = included_mutations[0]
assert mutation.action == 'update', \
    f'expected update action (Done transition), got {mutation.action!r}'
assert mutation.action != 'delete', \
    'reconciler must not emit a delete action for a deleted ticket'
assert mutation.fields.get('status') == 'Done', \
    f'expected status Done in update, got {mutation.fields.get("status")!r}'

print('RECONCILER_ROUTING_OK')
PYEOF

    exit_code=0
    py_output=$(python3 "$py_script" "$REPO_ROOT" 2>&1) || exit_code=$?
    rm -f "$py_script"

    assert_eq "reconciler routing test exits 0" "0" "$exit_code"
    assert_contains "reconciler routes deleted to Done transition" "RECONCILER_ROUTING_OK" "$py_output"

    assert_pass_if_clean "test_reconciler_routes_deleted_to_done"
}
test_reconciler_routes_deleted_to_done

print_summary
