#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-sync-detached-head-local-ahead.sh
#
# WS3 RED tests for the force-reset data-loss bug.
#
# Root cause: _ensure_initialized() (src/rebar/_engine/rebar) decides whether to
# `git reset --hard origin/tickets` by inspecting the BRANCH ref
# (`git log --oneline origin/tickets..tickets`). The tracker worktree can be in a
# detached-HEAD-local-ahead state — a local commit advances HEAD but not
# refs/heads/tickets (e.g. after a paused/aborted rebase, or on older git where
# the worktree is detached). In that state `origin/tickets..tickets` is EMPTY
# while `origin/tickets..HEAD` shows the unpushed commit, so the guard fires and
# `git reset --hard origin/tickets` DESTROYS the local-only commit.
#
# The existing test-ticket-sync-preserves-local.sh only covers the ATTACHED case
# (branch ref tracks HEAD), so it never caught this.
#
# Test 1 (detached-HEAD-local-ahead): the unpushed local commit MUST survive sync.
# Test 2 (reset-during-paused-rebase): when the tracker is mid-rebase, the sync
#         MUST NOT `git reset --hard` through the recovery state (would strand the
#         rebase's pending picks as dangling commits — silent data loss).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

# These tests specifically validate sync behavior — override the test-wide skip.
unset _TICKET_TEST_NO_SYNC

echo "=== test-ticket-sync-detached-head-local-ahead.sh ==="

PASSED=0
FAILED=0

# ── Helper: resolve the realpath'd tracker dir (matches _ensure_initialized) ──
_tracker_dir_for() {
    python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" \
        "$1/.tickets-tracker" 2>/dev/null || echo "$1/.tickets-tracker"
}

# ── Helper: the sync marker path _ensure_initialized computes for a tracker ──
_sync_marker_for() {
    local tracker_dir="$1"
    echo "/tmp/.ticket-sync-$(python3 -c \
        "import hashlib,sys; print(hashlib.md5(sys.argv[1].encode()).hexdigest()[:12])" \
        "$tracker_dir" 2>/dev/null || echo fallback)"
}

# ── Helper: repo + bare remote with the tickets branch already on origin ─────
_make_repo_with_remote() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    git init -q --bare "$tmp/remote.git"
    clone_test_repo "$tmp/local"
    git -C "$tmp/local" remote add origin "$tmp/remote.git"
    git -C "$tmp/local" push -u origin main --quiet 2>/dev/null || true
    # Bootstrap tickets without triggering sync (no marker games during setup).
    (cd "$tmp/local" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" init >/dev/null 2>&1) || true
    local tracker_dir
    tracker_dir=$(_tracker_dir_for "$tmp/local")
    if [ -d "$tracker_dir" ] && git -C "$tracker_dir" rev-parse --verify tickets &>/dev/null; then
        git -C "$tracker_dir" push origin HEAD:tickets --quiet 2>/dev/null || true
        git -C "$tracker_dir" fetch origin tickets --quiet 2>/dev/null || true
    fi
    echo "$tmp"
}

# ── Test 1: detached-HEAD local-ahead commit survives sync ───────────────────
echo "Test 1: detached-HEAD local-ahead commit survives _ensure_initialized sync"
test_detached_head_local_ahead_survives() {
    local tmp repo tracker_dir
    tmp=$(_make_repo_with_remote)
    repo="$tmp/local"
    tracker_dir=$(_tracker_dir_for "$repo")

    if ! git -C "$tracker_dir" rev-parse --verify origin/tickets &>/dev/null; then
        echo "  SKIP: origin/tickets not established (infrastructure issue)"
        return
    fi

    # Force the detached-HEAD-local-ahead state: detach, then commit so HEAD
    # advances but refs/heads/tickets stays pinned at origin/tickets.
    git -C "$tracker_dir" checkout -q --detach
    echo "detached-local-ahead marker" > "$tracker_dir/.detach-local-ahead-marker"
    git -C "$tracker_dir" add .detach-local-ahead-marker
    git -C "$tracker_dir" commit -q --no-verify -m "DETACHED local-ahead commit (unpushed)"
    local local_commit
    local_commit=$(git -C "$tracker_dir" rev-parse HEAD)

    # Sanity: the buggy branch-ref guard reads empty; the HEAD guard sees the commit.
    local branch_ahead head_ahead
    branch_ahead=$(git -C "$tracker_dir" rev-list origin/tickets..tickets --count 2>/dev/null || echo "?")
    head_ahead=$(git -C "$tracker_dir" rev-list origin/tickets..HEAD --count 2>/dev/null || echo "?")
    if [ "$branch_ahead" != "0" ] || [ "$head_ahead" != "1" ]; then
        echo "  SKIP: did not reach detached-ahead state (branch_ahead=$branch_ahead head_ahead=$head_ahead)"
        return
    fi

    # Expire the sync marker so _ensure_initialized actually syncs.
    rm -f "$(_sync_marker_for "$tracker_dir")"

    # Trigger _ensure_initialized via a normal ticket command.
    (cd "$repo" && bash "$TICKET_SCRIPT" list >/dev/null 2>&1) || true

    # Assert: the local-only commit is still reachable from HEAD.
    if git -C "$tracker_dir" merge-base --is-ancestor "$local_commit" HEAD 2>/dev/null \
        && [ -f "$tracker_dir/.detach-local-ahead-marker" ]; then
        echo "  PASS: detached-HEAD local-ahead commit survived sync"
        PASSED=$((PASSED + 1))
    else
        echo "  FAIL: local-ahead commit $local_commit destroyed by sync force-reset (HEAD=$(git -C "$tracker_dir" rev-parse HEAD))"
        FAILED=$((FAILED + 1))
    fi
}
test_detached_head_local_ahead_survives

# ── Test 2: sync must not reset --hard through an in-progress merge/rebase ────
# Isolates the rebase/merge-state guard from the HEAD-ahead guard: here HEAD is
# already AT origin/tickets (origin/tickets..HEAD is empty), so the HEAD guard
# alone would happily reset. Only a rebase/merge-in-progress guard prevents the
# sync from `git reset --hard`-ing through the recovery state (which clears
# MERGE_HEAD and silently abandons the in-progress merge — the bug-637b class).
echo "Test 2: sync refuses to force-reset while a merge is in progress (MERGE_HEAD present)"
test_sync_refuses_reset_during_merge_state() {
    local tmp repo tracker_dir
    tmp=$(_make_repo_with_remote)
    repo="$tmp/local"
    tracker_dir=$(_tracker_dir_for "$repo")

    if ! git -C "$tracker_dir" rev-parse --verify origin/tickets &>/dev/null; then
        echo "  SKIP: origin/tickets not established (infrastructure issue)"
        return
    fi

    # Ensure HEAD == origin/tickets so the HEAD-ahead guard would NOT protect us
    # (origin/tickets..HEAD is empty) — the only thing that should block the
    # reset is the recovery-state guard.
    git -C "$tracker_dir" reset --hard origin/tickets --quiet 2>/dev/null || true
    local head_ahead
    head_ahead=$(git -C "$tracker_dir" rev-list origin/tickets..HEAD --count 2>/dev/null || echo "?")
    if [ "$head_ahead" != "0" ]; then
        echo "  SKIP: HEAD not aligned with origin/tickets (head_ahead=$head_ahead)"
        return
    fi

    # Simulate an interrupted merge: write MERGE_HEAD (exactly what `git merge`
    # leaves on a conflict pause). _check_no_rebase_in_progress detects this.
    local merge_head_path
    merge_head_path=$(git -C "$tracker_dir" rev-parse --git-path MERGE_HEAD 2>/dev/null)
    git -C "$tracker_dir" rev-parse HEAD > "$merge_head_path"
    if [ ! -f "$merge_head_path" ]; then
        echo "  SKIP: could not write MERGE_HEAD marker"
        return
    fi

    rm -f "$(_sync_marker_for "$tracker_dir")"
    (cd "$repo" && bash "$TICKET_SCRIPT" list >/dev/null 2>&1) || true

    # Assert: MERGE_HEAD is still present — sync did NOT reset through the merge.
    if [ -f "$merge_head_path" ]; then
        echo "  PASS: sync preserved the in-progress merge state (no force-reset)"
        PASSED=$((PASSED + 1))
    else
        echo "  FAIL: sync force-reset through the in-progress merge (MERGE_HEAD cleared)"
        FAILED=$((FAILED + 1))
    fi
}
test_sync_refuses_reset_during_merge_state

echo ""
printf "PASSED: %d  FAILED: %d\n" "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
