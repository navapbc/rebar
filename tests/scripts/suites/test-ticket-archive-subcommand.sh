#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-archive-subcommand.sh
# RED integration tests for 'ticket archive <id>' subcommand.
#
# Exercises:
#   1. test_archive_open_ticket         — open ticket is archived and excluded from default list
#   2. test_archive_rejects_in_progress — in_progress ticket exits 1 with error message
#   3. test_archive_idempotent          — archiving an already-archived ticket exits 0
#   4. test_archive_show_reflects_archived — ticket show after archive has "archived": true
#
# RED STATE: Tests fail before ticket_archive() + dispatcher case are implemented.
# GREEN STATE: All tests pass after implementation.
#
# Usage: bash tests/scripts/suites/test-ticket-archive-subcommand.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e intentionally omitted — test assertions return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DISPATCHER="$REPO_ROOT/src/rebar/_engine/ticket"

source "$SCRIPT_DIR/../lib/assert.sh"
source "$SCRIPT_DIR/../lib/git-fixtures.sh"

echo "=== test-ticket-archive-subcommand.sh ==="

# ── Fixture helper ────────────────────────────────────────────────────────────
# Creates a full ticket-ready repo (with ticket system initialized).
_make_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# Creates a ticket and returns its ID (last line of output).
_create_ticket() {
    local repo="$1"
    local ticket_type="${2:-task}"
    local title="${3:-Test ticket}"
    local out
    out=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$DISPATCHER" create "$ticket_type" "$title" 2>/dev/null) || true
    echo "$out" | tail -1
}

# Transitions a ticket to in_progress status.
_set_in_progress() {
    local repo="$1"
    local ticket_id="$2"
    (cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$DISPATCHER" transition "$ticket_id" open in_progress 2>/dev/null) || true
}

# ── Cleanup ───────────────────────────────────────────────────────────────────
declare -a _CLEANUP_DIRS=()
trap 'rm -rf "${_CLEANUP_DIRS[@]:-}"' EXIT

# ── Test 1: archive an open ticket ───────────────────────────────────────────
echo "Test 1: 'ticket archive <open-id>' archives the ticket"
test_archive_open_ticket() {
    local repo ticket_id list_output list_archived_output exit_code

    repo=$(_make_repo)
    ticket_id=$(_create_ticket "$repo" task "Open ticket to archive")

    if [ -z "$ticket_id" ]; then
        assert_ne "ticket created (non-empty id)" "" "$ticket_id"
        return
    fi

    exit_code=0
    (cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$DISPATCHER" archive "$ticket_id" 2>/dev/null) || exit_code=$?
    assert_eq "archive of open ticket exits 0" "0" "$exit_code"

    list_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$DISPATCHER" list 2>/dev/null) || true
    local in_list="no"
    echo "$list_output" | grep -q "\"$ticket_id\"" && in_list="yes"
    assert_eq "archived ticket absent from default list" "no" "$in_list"

    list_archived_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$DISPATCHER" list --include-archived 2>/dev/null) || true
    local in_archived="no"
    echo "$list_archived_output" | grep -q "\"$ticket_id\"" && in_archived="yes"
    assert_eq "archived ticket visible with --include-archived" "yes" "$in_archived"
}
test_archive_open_ticket

# ── Test 2: reject non-open statuses ─────────────────────────────────────────
echo "Test 2: 'ticket archive <in_progress-id>' exits non-zero with status message"
test_archive_rejects_in_progress() {
    local repo ticket_id exit_code err_output

    repo=$(_make_repo)
    ticket_id=$(_create_ticket "$repo" task "In-progress ticket")

    if [ -z "$ticket_id" ]; then
        assert_ne "ticket created (non-empty id)" "" "$ticket_id"
        return
    fi

    _set_in_progress "$repo" "$ticket_id"

    exit_code=0
    err_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$DISPATCHER" archive "$ticket_id" 2>&1) || exit_code=$?

    assert_ne "archive rejects in_progress ticket (non-zero exit)" "0" "$exit_code"
    local has_msg="no"
    echo "$err_output" | grep -qi "in_progress\|not open\|only.*open\|status" && has_msg="yes"
    assert_eq "archive rejection includes status context in message" "yes" "$has_msg"
}
test_archive_rejects_in_progress

# ── Test 3: idempotent — second archive exits 0 silently ─────────────────────
echo "Test 3: archiving an already-archived ticket is idempotent (exits 0)"
test_archive_idempotent() {
    local repo ticket_id exit_code1 exit_code2

    repo=$(_make_repo)
    ticket_id=$(_create_ticket "$repo" task "Ticket for idempotent test")

    if [ -z "$ticket_id" ]; then
        assert_ne "ticket created (non-empty id)" "" "$ticket_id"
        return
    fi

    exit_code1=0
    (cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$DISPATCHER" archive "$ticket_id" 2>/dev/null) || exit_code1=$?
    assert_eq "first archive exits 0" "0" "$exit_code1"

    exit_code2=0
    (cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$DISPATCHER" archive "$ticket_id" 2>/dev/null) || exit_code2=$?
    assert_eq "second archive exits 0 (idempotent)" "0" "$exit_code2"
}
test_archive_idempotent

# ── Test 4: ticket show reflects archived: true ───────────────────────────────
echo "Test 4: 'ticket show <id>' after archive reports archived=true"
test_archive_show_reflects_archived() {
    local repo ticket_id show_output archived_val exit_code

    repo=$(_make_repo)
    ticket_id=$(_create_ticket "$repo" task "Ticket to check show after archive")

    if [ -z "$ticket_id" ]; then
        assert_ne "ticket created (non-empty id)" "" "$ticket_id"
        return
    fi

    exit_code=0
    (cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$DISPATCHER" archive "$ticket_id" 2>/dev/null) || exit_code=$?
    assert_eq "archive exits 0 before show check" "0" "$exit_code"

    if [ "$exit_code" -ne 0 ]; then
        return
    fi

    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$DISPATCHER" show "$ticket_id" 2>/dev/null) || true
    archived_val=$(echo "$show_output" | python3 -c "import json,sys; d=json.load(sys.stdin); print(str(d.get('archived','MISSING')).lower())" 2>/dev/null) || archived_val="error"
    assert_eq "ticket show reports archived=true after archive" "true" "$archived_val"
}
test_archive_show_reflects_archived

print_summary
