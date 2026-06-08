#!/usr/bin/env bash
# tests/scripts/test-ticket-health-guards.sh
# RED tests for shared helper functions in src/rebar/_engine/ticket-lib.sh.
#
# Specifically tests:
#   - ticket_read_status()         — reads compiled ticket status from reducer
#
# Usage: bash tests/scripts/test-ticket-health-guards.sh

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_LIB="$REPO_ROOT/src/rebar/_engine/ticket-lib.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-health-guards.sh ==="

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

# ── Helper: create a ticket with a parent and return its ID ───────────────────
_create_child_ticket() {
    local repo="$1"
    local parent_id="$2"
    local title="${3:-Child ticket}"
    local out
    out=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "$title" --parent "$parent_id" 2>/dev/null) || true
    echo "$out" | tail -1
}

# ── Helper: transition a ticket ───────────────────────────────────────────────
_transition_ticket() {
    local repo="$1"
    local ticket_id="$2"
    local from="$3"
    local to="$4"
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" "$from" "$to" 2>/dev/null) || true
}

# ── Test 1: ticket_read_status returns correct compiled status ─────────────────
echo "Test 1: ticket_read_status() function exists and returns current status of a ticket"
test_ticket_read_status_returns_current_status() {
    _snapshot_fail

    # Source ticket-lib.sh to check if ticket_read_status is defined
    # RED: ticket_read_status does not exist in ticket-lib.sh yet
    local fn_exists=0
    (source "$TICKET_LIB" 2>/dev/null && declare -f ticket_read_status >/dev/null 2>&1) || fn_exists=$?

    if [ "$fn_exists" -ne 0 ]; then
        # Function does not exist — assert failure to mark RED
        assert_eq "ticket_read_status function exists in ticket-lib.sh" "exists" "missing"
        assert_pass_if_clean "test_ticket_read_status_returns_current_status"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create a ticket (status = open by default)
    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Status read test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for status read test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_read_status_returns_current_status"
        return
    fi

    # Call ticket_read_status with the tracker_dir and ticket_id
    # Expected: returns "open" since the ticket was just created
    local status_out
    local status_exit=0
    status_out=$(
        (cd "$repo" && source "$TICKET_LIB" && ticket_read_status "$tracker_dir" "$ticket_id")
    ) || status_exit=$?

    # Assert: function exits 0
    assert_eq "ticket_read_status: exits 0" "0" "$status_exit"

    # Assert: returns "open" for a freshly created ticket
    assert_eq "ticket_read_status: returns 'open' for new ticket" "open" "$status_out"

    # Now transition ticket to in_progress and re-check
    _transition_ticket "$repo" "$ticket_id" "open" "in_progress"

    local status_after
    local status_after_exit=0
    status_after=$(
        (cd "$repo" && source "$TICKET_LIB" && ticket_read_status "$tracker_dir" "$ticket_id")
    ) || status_after_exit=$?

    # Assert: returns updated status after transition
    assert_eq "ticket_read_status: returns 'in_progress' after transition" "in_progress" "$status_after"

    assert_pass_if_clean "test_ticket_read_status_returns_current_status"
}
test_ticket_read_status_returns_current_status

print_summary
