#!/usr/bin/env bash
# tests/scripts/test-ticket-autoinit.sh
# RED tests for auto-init guard in src/rebar/_engine/ticket dispatcher.
#
# Tests that `ticket create` and `ticket show` silently auto-initialize
# the ticket system when .tickets-tracker/ does not exist, and that
# explicit `ticket init` still prints the success message.
#
# All 3 tests MUST FAIL until the auto-init guard is implemented in ticket.
#
# Usage: bash tests/scripts/test-ticket-autoinit.sh
# Returns: exit non-zero (RED) until the auto-init guard is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-autoinit.sh ==="

# ── Helper: create a fresh temp git repo WITHOUT ticket system initialized ─────
# This is intentionally NOT calling `ticket init` — the auto-init guard should do it.
_make_uninit_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_test_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Test 1: ticket create in fresh repo auto-inits silently and succeeds ──────
echo "Test 1: ticket create in fresh repo (no prior init) succeeds and creates .tickets-tracker/"
test_ticket_autoinit_on_create() {
    local repo
    repo=$(_make_uninit_repo)

    # Pre-condition: .tickets-tracker/ must NOT exist before the command
    if [ -d "$repo/.tickets-tracker" ]; then
        assert_eq "pre-condition: .tickets-tracker/ absent before create" "absent" "present"
        return
    fi

    # Run ticket create WITHOUT prior init — auto-init guard should handle it
    local stdout_out stderr_out exit_code=0
    stdout_out=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Auto-init test" 2>/tmp/autoinit_stderr_$$) || exit_code=$?
    stderr_out=$(cat /tmp/autoinit_stderr_$$ 2>/dev/null || true)
    rm -f /tmp/autoinit_stderr_$$
    local ticket_id
    ticket_id=$(echo "$stdout_out" | tail -1)

    # Assert: exits 0
    assert_eq "ticket create in fresh repo exits 0" "0" "$exit_code"

    # Assert: .tickets-tracker/ was created by auto-init
    if [ -d "$repo/.tickets-tracker" ]; then
        assert_eq ".tickets-tracker/ created by auto-init" "exists" "exists"
    else
        assert_eq ".tickets-tracker/ created by auto-init" "exists" "missing"
    fi

    # Assert: stdout contains a ticket ID (non-empty, matches pattern)
    if [ -n "$ticket_id" ] && [[ "$ticket_id" =~ ^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$ ]]; then
        assert_eq "ticket ID output to stdout" "match" "match"
    else
        assert_eq "ticket ID output to stdout" "match" "no-match: ${ticket_id:-<empty>}"
    fi

    # Assert: init output is NOT visible on stdout (suppressed)
    if [[ "$stdout_out" == *"Ticket system initialized"* ]]; then
        assert_eq "init message suppressed from stdout" "suppressed" "visible"
    else
        assert_eq "init message suppressed from stdout" "suppressed" "suppressed"
    fi
}
test_ticket_autoinit_on_create

# ── Test 2: ticket show in fresh repo auto-inits gracefully (no crash) ────────
echo "Test 2: ticket show in fresh repo (no prior init) exits gracefully without 'tickets-tracker not found' error"
test_ticket_autoinit_on_show() {
    local repo
    repo=$(_make_uninit_repo)

    # Pre-condition: .tickets-tracker/ must NOT exist before the command
    if [ -d "$repo/.tickets-tracker" ]; then
        assert_eq "pre-condition: .tickets-tracker/ absent before show" "absent" "present"
        return
    fi

    # Run ticket show WITHOUT prior init — auto-init guard should handle it.
    # We expect non-zero exit (no ticket to show), but NOT a crash with
    # "tickets-tracker not found" or similar filesystem-error messages.
    local stderr_out exit_code=0
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" show "nonexistent-id" 2>&1 >/dev/null) || exit_code=$?

    # Assert: .tickets-tracker/ was created by auto-init (init ran)
    if [ -d "$repo/.tickets-tracker" ]; then
        assert_eq ".tickets-tracker/ created by auto-init before show" "exists" "exists"
    else
        assert_eq ".tickets-tracker/ created by auto-init before show" "exists" "missing"
    fi

    # Assert: error message does NOT contain "tickets-tracker not found" (uninitialized crash)
    if echo "$stderr_out" | grep -iq "tickets-tracker not found"; then
        assert_eq "no uninitialized crash message" "no-crash" "crashed: $stderr_out"
    else
        assert_eq "no uninitialized crash message" "no-crash" "no-crash"
    fi

    # Assert: exits non-zero (no ticket to show, but gracefully)
    assert_eq "ticket show nonexistent-id exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"
}
test_ticket_autoinit_on_show

# ── Test 3: explicit ticket init still prints the success message ──────────────
echo "Test 3: explicit 'ticket init' prints 'Ticket system initialized.' to stderr"
test_ticket_explicit_init_still_prints_message() {
    local repo
    repo=$(_make_uninit_repo)

    # Run explicit ticket init (not auto-init) — should print the success message.
    # Informational messages go to stderr (stdout stays clean for machine-readable output).
    local stderr_out exit_code=0
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" init 2>&1 >/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "explicit ticket init exits 0" "0" "$exit_code"

    # Assert: stderr contains the success message
    if [[ "$stderr_out" == *"Ticket system initialized."* ]]; then
        assert_eq "explicit init prints 'Ticket system initialized.'" "printed" "printed"
    else
        assert_eq "explicit init prints 'Ticket system initialized.'" "printed" "not-printed: ${stderr_out:-<empty>}"
    fi
}
test_ticket_explicit_init_still_prints_message

print_summary
