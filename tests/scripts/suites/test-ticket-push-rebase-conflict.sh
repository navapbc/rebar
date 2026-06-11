#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-push-rebase-conflict.sh
# Tests for bugs 89dc-0913 and eb1d-0e5b:
# _push_tickets_branch gives up on rebase conflict instead of falling back to git merge.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_LIB="$REPO_ROOT/src/rebar/_engine/ticket-lib.sh"
source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"
echo "=== test-ticket-push-rebase-conflict.sh ==="
echo "Test 1: push fallback uses merge when rebase fails on diverged tickets branch"
test_merge_fallback_on_rebase_conflict() {
    local tmp; tmp=$(mktemp -d); _CLEANUP_DIRS+=("$tmp")
    git init -q --bare "$tmp/remote.git"
    clone_test_repo "$tmp/repo-a"
    git -C "$tmp/repo-a" remote add origin "$tmp/remote.git"
    git -C "$tmp/repo-a" push -q -u origin main 2>/dev/null
    git -C "$tmp/repo-a" checkout --orphan tickets-init 2>/dev/null
    git -C "$tmp/repo-a" rm -rf . --quiet 2>/dev/null || true
    git -C "$tmp/repo-a" checkout -b tickets 2>/dev/null || git -C "$tmp/repo-a" checkout tickets 2>/dev/null
    mkdir -p "$tmp/repo-a/ticket-aaa1"
    echo '{"event_type":"SNAPSHOT","uuid":"aaa1"}' > "$tmp/repo-a/ticket-aaa1/snapshot.json"
    git -C "$tmp/repo-a" add ticket-aaa1/snapshot.json
    git -C "$tmp/repo-a" -c user.email="test@test.com" -c user.name="Test" \
        commit -q -m "ticket: SNAPSHOT aaa1" 2>/dev/null
    git -C "$tmp/repo-a" push -q origin tickets 2>/dev/null
    clone_test_repo "$tmp/repo-b"
    git -C "$tmp/repo-b" remote add origin "$tmp/remote.git"
    git -C "$tmp/repo-b" push -q -u origin main 2>/dev/null
    git -C "$tmp/repo-b" fetch origin tickets --quiet 2>/dev/null
    git -C "$tmp/repo-b" checkout -b tickets origin/tickets 2>/dev/null
    mkdir -p "$tmp/repo-b/ticket-bbb2"
    echo '{"event_type":"CREATE","uuid":"bbb2"}' > "$tmp/repo-b/ticket-bbb2/create.json"
    git -C "$tmp/repo-b" add ticket-bbb2/create.json
    git -C "$tmp/repo-b" -c user.email="test@test.com" -c user.name="Test" \
        commit -q -m "ticket: CREATE bbb2" 2>/dev/null
    mkdir -p "$tmp/repo-a/ticket-aaa1"
    echo '{"event_type":"TRANSITION","uuid":"aaa3"}' > "$tmp/repo-a/ticket-aaa1/transition.json"
    git -C "$tmp/repo-a" add ticket-aaa1/transition.json
    git -C "$tmp/repo-a" -c user.email="test@test.com" -c user.name="Test" \
        commit -q -m "ticket: TRANSITION aaa1" 2>/dev/null
    git -C "$tmp/repo-a" push -q origin tickets 2>/dev/null
    local result; result=$(source "$TICKET_LIB" 2>/dev/null; _push_tickets_branch "$tmp/repo-b" 2>&1; echo "EXIT:$?") || true
    local exit_code; exit_code=$(echo "$result" | grep "^EXIT:" | cut -d: -f2)
    assert_eq "push_returns_zero_on_diverged_branch" "0" "$exit_code"
    git -C "$tmp/repo-a" fetch origin tickets --quiet 2>/dev/null || true
    local remote_has_b=0
    git -C "$tmp/repo-a" show origin/tickets:ticket-bbb2/create.json >/dev/null 2>&1 && remote_has_b=1 || true
    assert_eq "bbb2_event_pushed_to_remote_after_recovery" "1" "$remote_has_b"
}
test_merge_fallback_on_rebase_conflict
echo "Test 2: ticket-lib.sh _push_tickets_branch contains merge fallback"
test_merge_fallback_exists_in_lib() {
    # grep the file directly to avoid echo|grep-q SIGPIPE false-negative under set -uo pipefail
    if grep -qE 'git.*merge.*origin/tickets|merge.*fallback' "$TICKET_LIB"; then
        (( ++PASS )); echo "PASS: merge fallback present in _push_tickets_branch"
    else
        (( ++FAIL )); echo "FAIL: merge fallback NOT found in _push_tickets_branch" >&2
    fi
}
test_merge_fallback_exists_in_lib
echo "Test 3: merge is the primary reconciliation path (Fix 3 of bug 637b superseded rebase-first)"
# Historical context: this test previously asserted that 'rebase --abort' was
# followed by merge fallback (covering bugs 89dc-0913 + eb1d-0e5b — "gives up on
# rebase conflict instead of falling back to git merge"). Bug 637b-63fe-9d44-4aab
# Fix 3 eliminated the rebase path entirely from _push_tickets_branch — merge
# is now the primary reconciliation, not a fallback after rebase failure. The
# original invariant ("merge runs on diverged branches") still holds, validated
# by Test 1 (behavioral) and Test 2 (presence). This test now asserts the
# stronger post-Fix-3 invariant: the primary path does NOT call git rebase on
# origin/tickets at all.
test_no_rebase_on_primary_reconciliation_path() {
    local fn_body; fn_body=$(awk '/_push_tickets_branch\(\)/{found=1} found{print} found && /^}$/{found=0}' "$TICKET_LIB")
    # shellcheck disable=SC2016
    if echo "$fn_body" | grep -qE '^\s*git[[:space:]]+-C[[:space:]]+"\$base_path"[[:space:]]+rebase[[:space:]]+origin/tickets'; then
        (( ++FAIL )); echo "FAIL: _push_tickets_branch still uses 'git rebase origin/tickets' on primary path (Fix 3 of bug 637b regressed)" >&2
    else
        (( ++PASS )); echo "PASS: _push_tickets_branch uses merge (not rebase) on primary path"
    fi
}
test_no_rebase_on_primary_reconciliation_path
echo ""
print_summary
