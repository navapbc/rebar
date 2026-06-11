#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-sync-preserves-local.sh
# RED test for bug eb00-efd0: _ensure_initialized() in ticket CLI runs
# git reset --hard origin/tickets which destroys local-only ticket commits
# when the 5-minute sync marker expires.
#
# This test creates a repo with a remote, creates a local-only ticket,
# then forces the sync marker to expire and runs another ticket command.
# The local ticket must survive the sync.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

# This test specifically validates sync behavior — override the test-wide skip.
unset _TICKET_TEST_NO_SYNC

echo "=== test-ticket-sync-preserves-local.sh ==="

PASSED=0
FAILED=0

# ── Helper: create a repo with a remote that has a tickets branch ─────────
_make_repo_with_remote() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")

    # Create the "remote" bare repo
    git init -q --bare "$tmp/remote.git"

    # Create the "local" repo pointing at the remote
    clone_test_repo "$tmp/local"
    git -C "$tmp/local" remote add origin "$tmp/remote.git"
    git -C "$tmp/local" push -u origin main --quiet 2>/dev/null

    # Initialize ticket system in local repo
    (cd "$tmp/local" && bash "$TICKET_SCRIPT" init >/dev/null 2>&1) || true

    # Push the tickets branch to the remote so origin/tickets exists
    local tracker_dir
    tracker_dir=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$tmp/local/.tickets-tracker" 2>/dev/null) || tracker_dir="$tmp/local/.tickets-tracker"
    if [ -d "$tracker_dir" ] && git -C "$tracker_dir" rev-parse --verify tickets &>/dev/null; then
        git -C "$tracker_dir" push origin tickets --quiet 2>/dev/null || true
    fi

    echo "$tmp"
}

# ── Test 1: Local-only ticket survives sync marker expiration ──────────────
echo "Test 1: ticket created locally survives when sync marker expires and another command runs"
test_local_ticket_survives_sync() {
    local tmp
    tmp=$(_make_repo_with_remote)
    local repo="$tmp/local"

    # Create a ticket (local-only, not pushed to remote)
    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Local-only ticket" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        echo "  SKIP: ticket create failed (test infrastructure issue)"
        return
    fi

    # Verify ticket is readable before sync
    local pre_show
    pre_show=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true
    if [ -z "$pre_show" ]; then
        echo "  SKIP: ticket show failed before sync (test infrastructure issue)"
        return
    fi

    # Force the sync marker to expire by deleting it
    local tracker_dir
    tracker_dir=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$repo/.tickets-tracker" 2>/dev/null) || tracker_dir="$repo/.tickets-tracker"
    local sync_marker
    sync_marker="/tmp/.ticket-sync-$(python3 -c "import hashlib,sys; print(hashlib.md5(sys.argv[1].encode()).hexdigest()[:12])" "$tracker_dir" 2>/dev/null || echo "fallback")"
    rm -f "$sync_marker"

    # Run another ticket command — this will trigger _ensure_initialized with expired marker
    local post_show exit_code=0
    post_show=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || exit_code=$?

    # Assert: ticket is still readable after sync
    if [ "$exit_code" -eq 0 ] && [ -n "$post_show" ]; then
        echo "  PASS: ticket survived sync marker expiration"
        PASSED=$((PASSED + 1))
    else
        echo "  FAIL: ticket lost after sync marker expiration (exit=$exit_code, output='${post_show:-<empty>}')"
        FAILED=$((FAILED + 1))
    fi
}
test_local_ticket_survives_sync

# ── Test 2: Local ticket survives when a different ticket is compacted ─────
echo "Test 2: ticket A survives when ticket B is compacted and sync marker expires"
test_local_ticket_survives_compact_and_sync() {
    local tmp
    tmp=$(_make_repo_with_remote)
    local repo="$tmp/local"

    # Create two tickets
    local ticket_a ticket_b
    ticket_a=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Ticket A" 2>/dev/null | tail -1) || true
    ticket_b=$(cd "$repo" && bash "$TICKET_SCRIPT" create bug "Ticket B" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_a" ] || [ -z "$ticket_b" ]; then
        echo "  SKIP: ticket create failed"
        return
    fi

    # Close ticket B (triggers compact-on-close)
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_b" open closed --reason="Fixed: test" 2>/dev/null) || true

    # Force sync marker to expire
    local tracker_dir
    tracker_dir=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$repo/.tickets-tracker" 2>/dev/null) || tracker_dir="$repo/.tickets-tracker"
    local sync_marker
    sync_marker="/tmp/.ticket-sync-$(python3 -c "import hashlib,sys; print(hashlib.md5(sys.argv[1].encode()).hexdigest()[:12])" "$tracker_dir" 2>/dev/null || echo "fallback")"
    rm -f "$sync_marker"

    # Run ticket show on ticket A — triggers sync with expired marker
    local post_show exit_code=0
    post_show=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_a" 2>/dev/null) || exit_code=$?

    # Assert: ticket A is still readable
    if [ "$exit_code" -eq 0 ] && [ -n "$post_show" ]; then
        echo "  PASS: ticket A survived compact of B + sync expiration"
        PASSED=$((PASSED + 1))
    else
        echo "  FAIL: ticket A lost after compact of B + sync (exit=$exit_code)"
        FAILED=$((FAILED + 1))
    fi
}
test_local_ticket_survives_compact_and_sync

echo ""
printf "PASSED: %d  FAILED: %d\n" "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
