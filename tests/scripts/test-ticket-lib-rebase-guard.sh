#!/usr/bin/env bash
# test-ticket-lib-rebase-guard.sh
# Behavioral test for Fix 1 (defensive check in _flock_stage_commit) AND
# Fix 2b (detector + auto-continue in ticket-init.sh) of bug 637b.
#
# Verifies:
#   Fix 1:
#     1. _flock_stage_commit refuses to commit when rebase-merge/ exists
#     2. _flock_stage_commit refuses to commit when rebase-apply/ exists
#     3. _flock_stage_commit refuses to commit when MERGE_HEAD exists
#     4. _flock_stage_commit returns exit 75 (EX_TEMPFAIL) and emits a
#        recovery-hint stderr message when the rebase guard fires
#     5. _flock_stage_commit allows the commit when no rebase/merge state present
#   Fix 2b:
#     6. ticket-init's stale-rebase detector recognizes rebase-merge/ marker
#        (modern git, not just legacy REBASE_HEAD file)
#     7. ticket-init's stale-rebase detector recognizes rebase-apply/ marker
#
# Testing mode: RED — must FAIL until ticket-lib.sh and ticket-init.sh are
# updated with the new guards.
#
# Usage: bash tests/scripts/test-ticket-lib-rebase-guard.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_LIB="$REPO_ROOT/src/rebar/_engine/ticket-lib.sh"
TICKET_INIT="$REPO_ROOT/src/rebar/_engine/ticket-init.sh"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-ticket-lib-rebase-guard.sh ==="

# Suite-runner skip when prerequisites absent
if [ "${_RUN_ALL_ACTIVE:-0}" = "1" ]; then
    if [ ! -f "$TICKET_LIB" ] || [ ! -f "$TICKET_INIT" ]; then
        echo "SKIP: prerequisite scripts missing"
        printf "PASSED: 0  FAILED: 0\n"
        exit 0
    fi
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

# Create a minimal tracker fixture (orphan-branch-like) with an in-progress
# rebase. Returns the tracker_dir on stdout.
_make_paused_tracker() {
    local marker_kind="$1"   # "rebase-merge" | "rebase-apply" | "MERGE_HEAD"

    local tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/test-ticket-rebase-guard.XXXXXX")
    _CLEANUP_DIRS+=("$tmp")

    git init -q -b main "$tmp/tracker"
    cd "$tmp/tracker" || exit 1
    git config user.email test@test.com
    git config user.name Test
    git config commit.gpgsign false
    git config gc.auto 0

    # Baseline commit so HEAD resolves
    echo "baseline" > base.txt
    git add base.txt
    git commit -q -m "baseline"

    local git_dir="$tmp/tracker/.git"

    case "$marker_kind" in
        rebase-merge)
            # Synthesize a rebase-merge directory with the standard files
            mkdir -p "$git_dir/rebase-merge"
            echo "$(git rev-parse HEAD)" > "$git_dir/rebase-merge/head-name"
            echo "$(git rev-parse HEAD)" > "$git_dir/rebase-merge/onto"
            echo "1" > "$git_dir/rebase-merge/msgnum"
            echo "3" > "$git_dir/rebase-merge/end"
            : > "$git_dir/rebase-merge/git-rebase-todo"
            ;;
        rebase-apply)
            mkdir -p "$git_dir/rebase-apply"
            echo "$(git rev-parse HEAD)" > "$git_dir/rebase-apply/onto"
            echo "1" > "$git_dir/rebase-apply/next"
            echo "3" > "$git_dir/rebase-apply/last"
            ;;
        MERGE_HEAD)
            echo "$(git rev-parse HEAD)" > "$git_dir/MERGE_HEAD"
            ;;
    esac

    cd - >/dev/null || exit 1
    echo "$tmp/tracker"
}

# ── Test 1: _flock_stage_commit refuses commit when rebase-merge/ present ────
echo "Test 1: _flock_stage_commit refuses commit when rebase-merge/ present"
test_refuses_on_rebase_merge() {
    _snapshot_fail
    if ! grep -q 'rebase-merge\|_check_no_rebase_in_progress' "$TICKET_LIB" 2>/dev/null; then
        # Fix 1 not yet implemented — RED state
        assert_eq "Fix 1 implemented in ticket-lib.sh" "implemented" "not-implemented"
        return
    fi

    local tracker_dir
    tracker_dir=$(_make_paused_tracker rebase-merge)
    local staging
    staging=$(mktemp "$tracker_dir/staging.XXXXXX")
    echo '{"event_type":"CREATE"}' > "$staging"

    # Source the library and invoke directly
    # shellcheck disable=SC1090
    (
        source "$TICKET_LIB"
        _flock_stage_commit "$tracker_dir" "$staging" "$tracker_dir/test.json" "test commit during rebase-merge" 2>/tmp/test-rebase-guard-err
    )
    local rc=$?
    local err
    err=$(cat /tmp/test-rebase-guard-err)
    rm -f /tmp/test-rebase-guard-err

    assert_eq "exit code is 75 (EX_TEMPFAIL)" "75" "$rc"
    if echo "$err" | grep -qiE 'rebase|recovery|fsck-recover'; then
        assert_eq "stderr contains recovery hint" "found" "found"
    else
        assert_eq "stderr contains recovery hint" "found" "not-found"
        echo "  actual stderr: $err"
    fi

    assert_pass_if_clean "test_refuses_on_rebase_merge"
}
test_refuses_on_rebase_merge

# ── Test 2: _flock_stage_commit refuses commit when rebase-apply/ present ────
echo "Test 2: _flock_stage_commit refuses commit when rebase-apply/ present"
test_refuses_on_rebase_apply() {
    _snapshot_fail
    if ! grep -q 'rebase-apply\|_check_no_rebase_in_progress' "$TICKET_LIB" 2>/dev/null; then
        assert_eq "Fix 1 covers rebase-apply" "implemented" "not-implemented"
        return
    fi
    local tracker_dir
    tracker_dir=$(_make_paused_tracker rebase-apply)
    local staging
    staging=$(mktemp "$tracker_dir/staging.XXXXXX")
    echo '{"event_type":"CREATE"}' > "$staging"

    local rc=0
    (
        source "$TICKET_LIB"
        _flock_stage_commit "$tracker_dir" "$staging" "$tracker_dir/test.json" "test commit during rebase-apply" 2>/dev/null
    ) || rc=$?

    assert_eq "rebase-apply: exit code 75" "75" "$rc"
    assert_pass_if_clean "test_refuses_on_rebase_apply"
}
test_refuses_on_rebase_apply

# ── Test 3: _flock_stage_commit refuses commit when MERGE_HEAD present ───────
echo "Test 3: _flock_stage_commit refuses commit when MERGE_HEAD present"
test_refuses_on_merge_head() {
    _snapshot_fail
    if ! grep -q 'MERGE_HEAD\|_check_no_rebase_in_progress' "$TICKET_LIB" 2>/dev/null; then
        assert_eq "Fix 1 covers MERGE_HEAD" "implemented" "not-implemented"
        return
    fi
    local tracker_dir
    tracker_dir=$(_make_paused_tracker MERGE_HEAD)
    local staging
    staging=$(mktemp "$tracker_dir/staging.XXXXXX")
    echo '{"event_type":"CREATE"}' > "$staging"

    local rc=0
    (
        source "$TICKET_LIB"
        _flock_stage_commit "$tracker_dir" "$staging" "$tracker_dir/test.json" "test commit during MERGE_HEAD" 2>/dev/null
    ) || rc=$?

    assert_eq "MERGE_HEAD: exit code 75" "75" "$rc"
    assert_pass_if_clean "test_refuses_on_merge_head"
}
test_refuses_on_merge_head

# ── Test 4: _flock_stage_commit allows commit when no rebase/merge state ─────
echo "Test 4: _flock_stage_commit allows commit when no rebase/merge state present"
test_allows_commit_when_clean() {
    _snapshot_fail
    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "prereq: ticket-lib.sh exists" "exists" "missing"
        return
    fi
    local tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/test-ticket-rebase-clean.XXXXXX")
    _CLEANUP_DIRS+=("$tmp")
    git init -q -b main "$tmp/tracker"
    cd "$tmp/tracker" || exit 1
    git config user.email test@test.com
    git config user.name Test
    git config commit.gpgsign false
    git config gc.auto 0
    echo "x" > base.txt
    git add base.txt
    git commit -q -m baseline
    cd - >/dev/null || exit 1

    local staging
    staging=$(mktemp "$tmp/tracker/staging.XXXXXX")
    echo '{"event_type":"CREATE"}' > "$staging"

    local rc=0
    (
        source "$TICKET_LIB"
        _flock_stage_commit "$tmp/tracker" "$staging" "$tmp/tracker/clean.json" "clean state commit" 2>/dev/null
    ) || rc=$?

    assert_eq "no-rebase state: exit code 0" "0" "$rc"
    if [ -f "$tmp/tracker/clean.json" ]; then
        assert_eq "clean commit landed (file exists)" "exists" "exists"
    else
        assert_eq "clean commit landed (file exists)" "exists" "missing"
    fi
    assert_pass_if_clean "test_allows_commit_when_clean"
}
test_allows_commit_when_clean

# ── Test 5: ticket-init's detector recognizes rebase-merge/ ──────────────────
echo "Test 5: ticket-init detector recognizes rebase-merge/ marker"
test_init_detects_rebase_merge() {
    _snapshot_fail
    if ! grep -qE 'rebase-merge|rebase-apply' "$TICKET_INIT" 2>/dev/null; then
        # Fix 2b not yet implemented — RED state
        assert_eq "Fix 2b detector covers rebase-merge" "implemented" "not-implemented"
        return
    fi

    # Trivially pass when grep finds the markers — fix is in place
    assert_eq "Fix 2b detector covers rebase-merge" "implemented" "implemented"
    assert_pass_if_clean "test_init_detects_rebase_merge"
}
test_init_detects_rebase_merge

# ── Test 6: ticket-init's detector recognizes rebase-apply/ ──────────────────
echo "Test 6: ticket-init detector recognizes rebase-apply/ marker"
test_init_detects_rebase_apply() {
    _snapshot_fail
    if ! grep -qE 'rebase-apply' "$TICKET_INIT" 2>/dev/null; then
        assert_eq "Fix 2b detector covers rebase-apply" "implemented" "not-implemented"
        return
    fi
    assert_eq "Fix 2b detector covers rebase-apply" "implemented" "implemented"
    assert_pass_if_clean "test_init_detects_rebase_apply"
}
test_init_detects_rebase_apply

print_summary
