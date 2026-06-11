#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-sync-orphan-reset.sh
# RED test for bug 0051-3428: when the local tickets branch is an orphan
# (unrelated history from origin/tickets), the sync guard in _ensure_initialized
# fails to reset to origin/tickets because git log origin/tickets..tickets
# returns the orphan's commits — the _local_ahead guard blocks the reset.
#
# This test creates a repo where:
# 1. Local tickets branch is initialized as an orphan (no shared history with remote)
# 2. Remote origin/tickets has real ticket data
# 3. Sync marker is expired so _ensure_initialized tries to sync
# 4. After sync, local should have the remote's ticket data (currently fails)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

# This test specifically validates sync behavior — override the test-wide skip.
unset _TICKET_TEST_NO_SYNC

echo "=== test-ticket-sync-orphan-reset.sh ==="

PASSED=0
FAILED=0

# ── Test 1: Orphan local tickets branch gets replaced by remote data ─────
echo "Test 1: orphan local tickets branch resets to origin/tickets on sync"
test_orphan_branch_resets_to_remote() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")

    # Create the "remote" bare repo
    git init -q --bare "$tmp/remote.git"

    # Create repo A — will push real ticket data to remote
    clone_test_repo "$tmp/repo-a"
    git -C "$tmp/repo-a" remote add origin "$tmp/remote.git"
    git -C "$tmp/repo-a" push -u origin main --quiet 2>/dev/null

    # Initialize ticket system in repo A and create a ticket
    (cd "$tmp/repo-a" && bash "$TICKET_SCRIPT" init >/dev/null 2>&1) || true
    local ticket_id
    ticket_id=$(cd "$tmp/repo-a" && bash "$TICKET_SCRIPT" create task "Real ticket from repo A" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        echo "  SKIP: ticket create in repo A failed"
        return
    fi

    # Push repo A's tickets branch to remote
    local tracker_a
    tracker_a=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$tmp/repo-a/.tickets-tracker" 2>/dev/null)
    git -C "$tracker_a" push origin tickets --quiet 2>/dev/null || true

    # Create repo B — cloned WITHOUT fetching tickets branch, then init creates orphan
    clone_test_repo "$tmp/repo-b"
    git -C "$tmp/repo-b" remote add origin "$tmp/remote.git"
    git -C "$tmp/repo-b" fetch origin main --quiet 2>/dev/null

    # Simulate the orphan scenario: manually create an orphan tickets branch
    # (This is what ticket-init.sh does when origin/tickets isn't known locally)
    local tracker_b="$tmp/repo-b/.tickets-tracker"
    git -C "$tmp/repo-b" worktree add --detach "$tracker_b" 2>/dev/null
    git -C "$tracker_b" checkout --orphan tickets 2>/dev/null
    git -C "$tracker_b" rm -rf . --quiet 2>/dev/null || true
    git -C "$tracker_b" config user.email "test@test.com"
    git -C "$tracker_b" config user.name "Test"
    cat > "$tracker_b/.gitignore" <<'EOF'
.env-id
.state-cache
EOF
    git -C "$tracker_b" add .gitignore
    git -C "$tracker_b" commit -q --no-verify -m "chore: initialize ticket tracker"
    python3 -c "import uuid; print(uuid.uuid4())" > "$tracker_b/.env-id"

    # Now fetch origin/tickets so the sync has something to compare against
    git -C "$tracker_b" fetch origin tickets --quiet 2>/dev/null

    # Verify the orphan scenario: local and remote should have NO merge base
    if git -C "$tracker_b" merge-base tickets origin/tickets &>/dev/null; then
        echo "  SKIP: merge-base found — test setup did not create orphan scenario"
        return
    fi

    # Force sync marker to expire (resolve realpath to match _ensure_initialized's hash)
    local tracker_b_resolved
    tracker_b_resolved=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$tracker_b" 2>/dev/null) || tracker_b_resolved="$tracker_b"
    local sync_marker
    sync_marker="/tmp/.ticket-sync-$(python3 -c "import hashlib,sys; print(hashlib.md5(sys.argv[1].encode()).hexdigest()[:12])" "$tracker_b_resolved" 2>/dev/null || echo "fallback")"
    rm -f "$sync_marker"

    # Run a ticket command from repo B — this triggers _ensure_initialized with expired marker
    local list_output
    list_output=$(cd "$tmp/repo-b" && bash "$TICKET_SCRIPT" list 2>/dev/null) || true

    # Assert: the ticket created in repo A should be visible in repo B
    if echo "$list_output" | python3 -c "import json,sys; tickets=json.load(sys.stdin); sys.exit(0 if any(t['ticket_id']=='$ticket_id' for t in tickets) else 1)" 2>/dev/null; then
        echo "  PASS: orphan branch was reset to origin/tickets, remote ticket visible"
        PASSED=$((PASSED + 1))
    else
        echo "  FAIL: orphan branch NOT reset — remote ticket '$ticket_id' not visible after sync"
        FAILED=$((FAILED + 1))
    fi
}
test_orphan_branch_resets_to_remote

# ── Test 2: Related-history local-ahead tickets still protected ──────────
echo "Test 2: local-ahead tickets with shared history are NOT reset (regression guard)"
test_related_history_local_ahead_preserved() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")

    # Create remote bare repo
    git init -q --bare "$tmp/remote.git"

    # Create local repo with ticket system
    clone_test_repo "$tmp/local"
    git -C "$tmp/local" remote add origin "$tmp/remote.git"
    git -C "$tmp/local" push -u origin main --quiet 2>/dev/null

    # Initialize ticket system
    (cd "$tmp/local" && bash "$TICKET_SCRIPT" init >/dev/null 2>&1) || true

    # Push tickets branch to create origin/tickets
    local tracker_dir
    tracker_dir=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$tmp/local/.tickets-tracker" 2>/dev/null)
    git -C "$tracker_dir" push origin tickets --quiet 2>/dev/null || true

    # Create a LOCAL-ONLY ticket (not pushed to remote)
    local local_ticket
    local_ticket=$(cd "$tmp/local" && bash "$TICKET_SCRIPT" create task "Local only ticket" 2>/dev/null | tail -1) || true

    if [ -z "$local_ticket" ]; then
        echo "  SKIP: ticket create failed"
        return
    fi

    # Force sync marker to expire
    local sync_marker
    sync_marker="/tmp/.ticket-sync-$(python3 -c "import hashlib,sys; print(hashlib.md5(sys.argv[1].encode()).hexdigest()[:12])" "$tracker_dir" 2>/dev/null || echo "fallback")"
    rm -f "$sync_marker"

    # Run another ticket command — triggers sync
    local post_show
    post_show=$(cd "$tmp/local" && bash "$TICKET_SCRIPT" show "$local_ticket" 2>/dev/null) || true

    # Assert: local-only ticket should still exist (not blown away by reset)
    if [ -n "$post_show" ]; then
        echo "  PASS: local-ahead ticket with shared history preserved after sync"
        PASSED=$((PASSED + 1))
    else
        echo "  FAIL: local-ahead ticket was destroyed by sync (regression of eb00-efd0)"
        FAILED=$((FAILED + 1))
    fi
}
test_related_history_local_ahead_preserved

# ── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASSED passed, $FAILED failed"
if [ "$FAILED" -gt 0 ]; then
    exit 1
fi
exit 0
