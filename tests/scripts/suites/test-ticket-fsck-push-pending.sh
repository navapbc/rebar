#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-fsck-push-pending.sh
#
# WS3: push is best-effort, so a successful local commit with a failed/absent
# push silently diverges from origin. fsck must SURFACE that divergence
# ("local ahead of origin/tickets, push pending") instead of staying silent — so
# an operator/agent can tell a clone has un-pushed work.
#
# Test 1: when the local tickets branch is ahead of origin/tickets, fsck emits a
#         PUSH_PENDING notice naming the un-pushed commit count.
# Test 2: the notice is informational only — it does NOT turn a clean fsck into a
#         failure (exit stays 0 when there are no real integrity issues).
# Test 3: when local is in sync with origin, fsck emits NO PUSH_PENDING notice.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
FSCK_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-fsck.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-fsck-push-pending.sh ==="

PASSED=0
FAILED=0

_tracker_dir_for() {
    python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" \
        "$1/.tickets-tracker" 2>/dev/null || echo "$1/.tickets-tracker"
}

_make_repo_with_remote() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    git init -q --bare "$tmp/remote.git"
    clone_test_repo "$tmp/local"
    git -C "$tmp/local" remote add origin "$tmp/remote.git"
    git -C "$tmp/local" push -u origin main --quiet 2>/dev/null || true
    (cd "$tmp/local" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" init >/dev/null 2>&1) || true
    local tracker_dir
    tracker_dir=$(_tracker_dir_for "$tmp/local")
    if [ -d "$tracker_dir" ] && git -C "$tracker_dir" rev-parse --verify tickets &>/dev/null; then
        git -C "$tracker_dir" push origin HEAD:tickets --quiet 2>/dev/null || true
        git -C "$tracker_dir" fetch origin tickets --quiet 2>/dev/null || true
    fi
    echo "$tmp"
}

# ── Tests 1 & 2: local ahead of origin → PUSH_PENDING notice, exit still 0 ────
echo "Test 1/2: fsck surfaces PUSH_PENDING when local is ahead of origin (and stays exit 0)"
test_fsck_reports_push_pending() {
    local tmp repo tracker_dir
    tmp=$(_make_repo_with_remote)
    repo="$tmp/local"
    tracker_dir=$(_tracker_dir_for "$repo")

    if ! git -C "$tracker_dir" rev-parse --verify origin/tickets &>/dev/null; then
        echo "  SKIP: origin/tickets not established (infrastructure issue)"
        return
    fi

    # Create a local-only commit without pushing: drop origin, create, re-add.
    git -C "$tracker_dir" remote remove origin 2>/dev/null || true
    (cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "unpushed local ticket" >/dev/null 2>&1) || true
    git -C "$tracker_dir" remote add origin "$tmp/remote.git" 2>/dev/null || true
    git -C "$tracker_dir" fetch origin tickets --quiet 2>/dev/null || true

    local ahead
    ahead=$(git -C "$tracker_dir" rev-list origin/tickets..HEAD --count 2>/dev/null || echo 0)
    if [ "$ahead" -lt 1 ]; then
        echo "  SKIP: did not reach local-ahead state (ahead=$ahead)"
        return
    fi

    local out exit_code=0
    out=$(PROJECT_ROOT="$repo" _TICKET_TEST_NO_SYNC=1 bash "$FSCK_SCRIPT" 2>&1) || exit_code=$?

    if echo "$out" | grep -q "PUSH_PENDING"; then
        echo "  PASS (Test 1): fsck emitted PUSH_PENDING notice"
        PASSED=$((PASSED + 1))
    else
        echo "  FAIL (Test 1): fsck did not surface local-ahead/push-pending. Output:"
        echo "$out" | sed 's/^/    /'
        FAILED=$((FAILED + 1))
    fi

    if [ "$exit_code" -eq 0 ]; then
        echo "  PASS (Test 2): PUSH_PENDING is informational — fsck exit 0"
        PASSED=$((PASSED + 1))
    else
        echo "  FAIL (Test 2): fsck exited $exit_code (PUSH_PENDING should not be an integrity failure)"
        FAILED=$((FAILED + 1))
    fi
}
test_fsck_reports_push_pending

# ── Test 3: in-sync local → no PUSH_PENDING notice ───────────────────────────
echo "Test 3: fsck emits no PUSH_PENDING when local is in sync with origin"
test_fsck_quiet_when_in_sync() {
    local tmp repo tracker_dir
    tmp=$(_make_repo_with_remote)
    repo="$tmp/local"
    tracker_dir=$(_tracker_dir_for "$repo")

    git -C "$tracker_dir" fetch origin tickets --quiet 2>/dev/null || true
    local ahead
    ahead=$(git -C "$tracker_dir" rev-list origin/tickets..HEAD --count 2>/dev/null || echo 0)
    if [ "$ahead" -ne 0 ]; then
        echo "  SKIP: local unexpectedly ahead of origin (ahead=$ahead)"
        return
    fi

    local out
    out=$(PROJECT_ROOT="$repo" _TICKET_TEST_NO_SYNC=1 bash "$FSCK_SCRIPT" 2>&1) || true
    if echo "$out" | grep -q "PUSH_PENDING"; then
        echo "  FAIL (Test 3): fsck emitted PUSH_PENDING when in sync"
        FAILED=$((FAILED + 1))
    else
        echo "  PASS (Test 3): no PUSH_PENDING when in sync"
        PASSED=$((PASSED + 1))
    fi
}
test_fsck_quiet_when_in_sync

echo ""
printf "PASSED: %d  FAILED: %d\n" "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
