#!/usr/bin/env bash
# tests/scripts/test-ticket-lifecycle-sync-preserves-local.sh
# RED tests for bug 46ee-7d1c: ticket-lifecycle.sh and sprint-list-epics.sh
# execute `git reset --hard origin/tickets` which destroys local-only ticket
# commits that have not yet been pushed to the remote.
#
# These tests must FAIL with the current code (RED) because neither script has
# the local-ahead guard that the ticket CLI dispatcher was given in ae1002e9.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
LIFECYCLE_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-lifecycle.sh"
SPRINT_LIST_SCRIPT="$REPO_ROOT/src/rebar/_engine/sprint-list-epics.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-lifecycle-sync-preserves-local.sh ==="

PASSED=0
FAILED=0

# ── Helper: create an isolated repo with a functioning remote ────────────────
# Returns the tmp root in stdout; sets up:
#   $tmp/remote.git  — bare remote
#   $tmp/local       — working clone with ticket system initialized and
#                      the tickets branch already pushed to origin
_make_repo_with_remote() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")

    # Bare remote
    git init -q --bare "$tmp/remote.git"

    # Local working repo (clone from template for speed)
    clone_test_repo "$tmp/local"
    git -C "$tmp/local" remote add origin "$tmp/remote.git"
    git -C "$tmp/local" push -u origin main --quiet 2>/dev/null || true

    # Bootstrap ticket system
    (cd "$tmp/local" && bash "$TICKET_SCRIPT" init >/dev/null 2>&1) || true

    # Push the tickets branch so origin/tickets exists
    local tracker_dir
    tracker_dir=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" \
        "$tmp/local/.tickets-tracker" 2>/dev/null) || tracker_dir="$tmp/local/.tickets-tracker"

    if [ -d "$tracker_dir" ] && git -C "$tracker_dir" rev-parse --verify tickets &>/dev/null; then
        git -C "$tracker_dir" push origin tickets --quiet 2>/dev/null || true
    fi

    echo "$tmp"
}

# ── Helper: resolve tracker dir for a local repo path ───────────────────────
_tracker_dir_for() {
    local repo="$1"
    python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" \
        "$repo/.tickets-tracker" 2>/dev/null || echo "$repo/.tickets-tracker"
}

# ── Test 1: ticket-lifecycle.sh does not destroy a local-only ticket ─────────
echo "Test 1: local-only ticket survives ticket-lifecycle.sh sync"
test_lifecycle_preserves_local_ticket() {
    local tmp repo tracker_dir ticket_id pre_show post_show exit_code

    tmp=$(_make_repo_with_remote)
    repo="$tmp/local"
    tracker_dir=$(_tracker_dir_for "$repo")

    # Create a ticket locally — do NOT push the tickets branch afterwards
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Lifecycle-local-only ticket" 2>/dev/null) || true

    if [ -z "$ticket_id" ]; then
        echo "  SKIP: ticket create failed (infrastructure issue)"
        return
    fi

    # Confirm the ticket is readable before the lifecycle run
    pre_show=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true
    if [ -z "$pre_show" ]; then
        echo "  SKIP: ticket show failed before lifecycle run (infrastructure issue)"
        return
    fi

    # Verify there IS a local commit ahead of origin so the test scenario is real
    local local_ahead
    local_ahead=$(git -C "$tracker_dir" rev-list origin/tickets..HEAD --count 2>/dev/null || echo "0")
    if [ "$local_ahead" -eq 0 ]; then
        echo "  SKIP: no local-ahead commits found — fixture did not produce the expected state"
        return
    fi

    # Run ticket-lifecycle.sh against the tracker directly
    exit_code=0
    bash "$LIFECYCLE_SCRIPT" --base-path="$tracker_dir" >/dev/null 2>&1 || exit_code=$?
    # lifecycle exits non-zero on push failure (no real remote push needed for test);
    # we only care about whether the local ticket data survived — not the push result.

    # Assert: ticket is still readable after lifecycle ran
    post_show=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    if [ -n "$post_show" ]; then
        echo "  PASS: ticket survived ticket-lifecycle.sh sync"
        PASSED=$((PASSED + 1))
    else
        echo "  FAIL: ticket was destroyed by ticket-lifecycle.sh git reset --hard origin/tickets"
        FAILED=$((FAILED + 1))
    fi
}
test_lifecycle_preserves_local_ticket

# ── Test 2: sprint-list-epics.sh does not destroy a local-only ticket ────────
echo "Test 2: local-only ticket survives sprint-list-epics.sh sync"
test_sprint_list_epics_preserves_local_ticket() {
    local tmp repo tracker_dir ticket_id pre_show post_show exit_code

    tmp=$(_make_repo_with_remote)
    repo="$tmp/local"
    tracker_dir=$(_tracker_dir_for "$repo")

    # Create a ticket locally — do NOT push the tickets branch
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create epic "Sprint-local-only epic" 2>/dev/null) || true

    if [ -z "$ticket_id" ]; then
        echo "  SKIP: ticket create failed (infrastructure issue)"
        return
    fi

    # Confirm the ticket is readable before the sprint-list-epics run
    pre_show=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true
    if [ -z "$pre_show" ]; then
        echo "  SKIP: ticket show failed before sprint-list-epics run (infrastructure issue)"
        return
    fi

    # Verify the local-ahead condition exists so the scenario is meaningful
    local local_ahead
    local_ahead=$(git -C "$tracker_dir" rev-list origin/tickets..HEAD --count 2>/dev/null || echo "0")
    if [ "$local_ahead" -eq 0 ]; then
        echo "  SKIP: no local-ahead commits — fixture did not produce expected state"
        return
    fi

    # Delete the sync marker so sprint-list-epics.sh performs its sync unconditionally
    local resolved_tracker
    resolved_tracker=$(cd "$tracker_dir" && pwd -P 2>/dev/null || echo "$tracker_dir")
    local sync_hash
    sync_hash=$(echo -n "$resolved_tracker" | md5sum 2>/dev/null | cut -d' ' -f1 \
        || md5 -q -s "$resolved_tracker" 2>/dev/null || echo "fallback")
    rm -f "/tmp/.ticket-sync-${sync_hash}"

    # Run sprint-list-epics.sh against the isolated repo
    exit_code=0
    TICKETS_TRACKER_DIR="$tracker_dir" PROJECT_ROOT="$repo" \
        bash "$SPRINT_LIST_SCRIPT" >/dev/null 2>&1 || exit_code=$?
    # exit code 1 = "no open epics" is acceptable for the test; we only care about data survival

    # Assert: ticket is still readable after sprint-list-epics ran
    post_show=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    if [ -n "$post_show" ]; then
        echo "  PASS: ticket survived sprint-list-epics.sh sync"
        PASSED=$((PASSED + 1))
    else
        echo "  FAIL: ticket was destroyed by sprint-list-epics.sh git reset --hard origin/tickets"
        FAILED=$((FAILED + 1))
    fi
}
test_sprint_list_epics_preserves_local_ticket

echo ""
printf "PASSED: %d  FAILED: %d\n" "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
