#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-delete.sh
# RED behavioral tests for the `ticket delete` subcommand.
#
# All test functions MUST FAIL until `ticket delete` is implemented.
# Covers: --user-approved guard flag, child-ticket blocking, list visibility,
# ready_to_work unblocking after delete, and UNLINK event emission.
#
# Usage: bash tests/scripts/suites/test-ticket-delete.sh
# Returns: exit non-zero (RED) until ticket delete is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-delete.sh ==="

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

# ── Helper: run ticket deps and extract a single JSON field ──────────────────
# Usage: _deps_field <repo> <ticket_id> <field>
# Returns the field value as a string (booleans become "true"/"false").
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

# ── DD1: delete without --user-approved flag exits non-zero with usage hint ──
echo "Test DD1: delete without --user-approved exits non-zero with usage hint"
test_delete_requires_user_approved_flag() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Guard flag test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for guard flag test" "non-empty" "empty"
        assert_pass_if_clean "test_delete_requires_user_approved_flag"
        return
    fi

    # Run `ticket delete <id>` WITHOUT --user-approved
    local exit_code=0
    local combined_output
    combined_output=$(cd "$repo" && bash "$TICKET_SCRIPT" delete "$ticket_id" 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_ne "delete without --user-approved exits non-zero" "0" "$exit_code"

    # Assert: output contains a hint about --user-approved
    assert_contains "output mentions --user-approved" "--user-approved" "$combined_output"

    assert_pass_if_clean "test_delete_requires_user_approved_flag"
}
test_delete_requires_user_approved_flag

# ── DD2: delete blocked when ticket has non-deleted children ─────────────────
echo "Test DD2: delete blocked when ticket has non-deleted children"
test_delete_blocked_by_non_deleted_children() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create parent ticket
    local parent_id
    parent_id=$(_create_ticket "$repo" epic "Parent with children")

    if [ -z "$parent_id" ]; then
        assert_eq "parent ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_delete_blocked_by_non_deleted_children"
        return
    fi

    # Create a child ticket under the parent
    local child_id
    child_id=$(_create_ticket "$repo" task "Child ticket" "--parent $parent_id")

    if [ -z "$child_id" ]; then
        assert_eq "child ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_delete_blocked_by_non_deleted_children"
        return
    fi

    # Attempt to delete the parent; should be blocked because child exists
    local exit_code=0
    local combined_output
    combined_output=$(cd "$repo" && bash "$TICKET_SCRIPT" delete "$parent_id" --user-approved 2>&1) || exit_code=$?

    # Assert: exits non-zero (blocked by existing child)
    assert_ne "delete with children exits non-zero" "0" "$exit_code"

    # Assert: output mentions the child ticket ID
    assert_contains "output contains child ticket ID" "$child_id" "$combined_output"

    assert_pass_if_clean "test_delete_blocked_by_non_deleted_children"
}
test_delete_blocked_by_non_deleted_children

# ── DD3: delete removes ticket from `ticket list` output ─────────────────────
echo "Test DD3: delete removes ticket from ticket list output"
test_delete_removes_from_ticket_list() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create a ticket with no children
    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Ticket to delete")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for deletion test" "non-empty" "empty"
        assert_pass_if_clean "test_delete_removes_from_ticket_list"
        return
    fi

    # Verify it appears in list before deletion
    local list_before
    list_before=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || list_before=""
    assert_contains "ticket visible in list before delete" "$ticket_id" "$list_before"

    # Run `ticket delete <id> --user-approved`
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" delete "$ticket_id" --user-approved >/dev/null 2>&1) || exit_code=$?

    # Assert: delete exits 0
    assert_eq "delete exits 0" "0" "$exit_code"

    # Assert: ticket no longer appears in `ticket list`
    local list_after
    list_after=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || list_after=""
    local still_present=0
    [[ "$list_after" == *"$ticket_id"* ]] && still_present=1
    assert_eq "deleted ticket absent from ticket list" "0" "$still_present"

    assert_pass_if_clean "test_delete_removes_from_ticket_list"
}
test_delete_removes_from_ticket_list

# ── DD4: delete blocker unblocks the dependent ticket (ready_to_work=true) ───
echo "Test DD4: deleting a blocker sets ready_to_work=true on the dependent ticket"
test_delete_clears_blocker_for_ready_to_work() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create ticket A (the dependent) and ticket B (the blocker)
    local tkt_a tkt_b
    tkt_a=$(_create_ticket "$repo" task "Dependent ticket A")
    tkt_b=$(_create_ticket "$repo" task "Blocking ticket B")

    if [ -z "$tkt_a" ] || [ -z "$tkt_b" ]; then
        assert_eq "both tickets A and B created" "non-empty" "empty"
        assert_pass_if_clean "test_delete_clears_blocker_for_ready_to_work"
        return
    fi

    # Link: B blocks A  (A depends_on B; B must close/be-deleted for A to be ready)
    local link_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$tkt_b" "$tkt_a" blocks >/dev/null 2>/dev/null) || link_exit=$?
    assert_eq "link B blocks A exits 0" "0" "$link_exit"

    # Verify A is NOT ready_to_work before delete
    local rtw_before
    rtw_before=$(_deps_field "$repo" "$tkt_a" "ready_to_work")
    assert_eq "A not ready_to_work before delete (B is open blocker)" "false" "$rtw_before"

    # Delete ticket B (the blocker)
    local delete_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" delete "$tkt_b" --user-approved >/dev/null 2>&1) || delete_exit=$?
    assert_eq "delete blocker B exits 0" "0" "$delete_exit"

    # Assert: A is now ready_to_work=true after B is deleted
    local rtw_after
    rtw_after=$(_deps_field "$repo" "$tkt_a" "ready_to_work")
    assert_eq "A ready_to_work=true after blocker deleted" "true" "$rtw_after"

    assert_pass_if_clean "test_delete_clears_blocker_for_ready_to_work"
}
test_delete_clears_blocker_for_ready_to_work

# ── DD5: delete writes UNLINK events canceling active LINKs ──────────────────
echo "Test DD5: delete writes UNLINK events for all active links to deleted ticket"
test_delete_writes_unlink_events_for_active_links() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create tickets A and B; link A relates_to B
    local tkt_a tkt_b
    tkt_a=$(_create_ticket "$repo" task "Ticket A with link")
    tkt_b=$(_create_ticket "$repo" task "Ticket B to be deleted")

    if [ -z "$tkt_a" ] || [ -z "$tkt_b" ]; then
        assert_eq "both tickets A and B created for unlink test" "non-empty" "empty"
        assert_pass_if_clean "test_delete_writes_unlink_events_for_active_links"
        return
    fi

    # Create a link between A and B
    local link_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$tkt_a" "$tkt_b" relates_to >/dev/null 2>/dev/null) || link_exit=$?
    assert_eq "link A relates_to B exits 0" "0" "$link_exit"

    # Delete ticket B
    local delete_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" delete "$tkt_b" --user-approved >/dev/null 2>&1) || delete_exit=$?
    assert_eq "delete ticket B exits 0" "0" "$delete_exit"

    # Assert: ticket A's directory contains an UNLINK event canceling the link to B
    local tracker_dir="$repo/.tickets-tracker"
    local unlink_count
    unlink_count=$(find "$tracker_dir/$tkt_a" -maxdepth 1 -name '*-UNLINK.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_ne "UNLINK event written to ticket A after B deleted" "0" "$unlink_count"

    assert_pass_if_clean "test_delete_writes_unlink_events_for_active_links"
}
test_delete_writes_unlink_events_for_active_links

# ── DD4b: ticket show returns status:deleted after delete ────────────────────
echo "Test DD4b: ticket show returns status:deleted for a deleted ticket"
test_delete_ticket_show_returns_status_deleted() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Ticket to verify show status")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for show status test" "non-empty" "empty"
        assert_pass_if_clean "test_delete_ticket_show_returns_status_deleted"
        return
    fi

    # Delete the ticket
    local delete_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" delete "$ticket_id" --user-approved >/dev/null 2>&1) || delete_exit=$?
    assert_eq "delete exits 0" "0" "$delete_exit"

    # Assert: ticket show returns status:deleted
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

    assert_eq "ticket show returns status:deleted" "deleted" "$status_val"

    assert_pass_if_clean "test_delete_ticket_show_returns_status_deleted"
}
test_delete_ticket_show_returns_status_deleted

print_summary
