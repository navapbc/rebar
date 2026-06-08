#!/usr/bin/env bash
# test-push-tickets-dirty-wd-recovery.sh
#
# RED test for bug 12a6-d063-875e-4ed0: _push_tickets_branch fails to recover
# when the tracker working tree has uncommitted modifications to a tracked
# file that origin's HEAD would overwrite.
#
# Repro scenario (matches the live failure observed on a host tracker
# worktree during PR-4 audit, 2026-05-28):
#   - origin/tickets has commits A, B (B is ahead of local)
#   - local tickets has commit C on top of A (diverged from origin)
#   - local working tree has uncommitted modifications to a file that exists
#     on origin's HEAD (e.g., .bridge_state/bindings.json)
#
# Expected behavior AFTER fix:
#   - _push_tickets_branch stashes the dirty paths, fetches origin, merges,
#     pushes the local commit, then pops the stash to restore the dirty
#     modifications.
#   - Exit 0. origin/tickets contains commit C. Local WD modification is
#     preserved.
#
# Buggy behavior BEFORE fix (ticket-lib.sh:733-738):
#   - git merge origin/tickets refuses with "Your local changes would be
#     overwritten by merge" (NOT a content conflict).
#   - Code redirects merge stderr to /dev/null and classifies any non-zero
#     exit as "merge conflict, attempt $_attempt", calls git merge --abort,
#     then `return 0` — exits the while loop on iteration 1.
#   - The _max_retries=3 cap is dead code on this path.
#   - origin/tickets never receives commit C.
#
# Usage: bash tests/scripts/test-push-tickets-dirty-wd-recovery.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_LIB="$REPO_ROOT/src/rebar/_engine/ticket-lib.sh"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-push-tickets-dirty-wd-recovery.sh ==="

if [ ! -f "$TICKET_LIB" ]; then
    echo "FAIL: ticket-lib.sh not present at $TICKET_LIB"
    exit 1
fi

_CLEANUP_DIRS=()
_cleanup() {
    for _d in "${_CLEANUP_DIRS[@]+"${_CLEANUP_DIRS[@]}"}"; do
        rm -rf "$_d" 2>/dev/null || true
    done
}
trap _cleanup EXIT

# ── Helpers ───────────────────────────────────────────────────────────────────

# Build a fixture: bare remote with a tracked file that local will modify
# uncommitted. Pre-loads commit B on remote so local push fails non-FF.
_make_dirty_wd_fixture() {
    local tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/test-push-dirty-wd.XXXXXX")
    _CLEANUP_DIRS+=("$tmp")

    git init -q --bare -b tickets "$tmp/bare.git"

    # Local tracker — common ancestor A includes the file local will dirty.
    git init -q -b tickets "$tmp/tracker"
    (
        cd "$tmp/tracker" || exit 1
        git config user.email test@test.com
        git config user.name Test
        git config commit.gpgsign false
        git config gc.auto 0
        # Commit A: includes a "reconciler-owned" file the local will later modify
        mkdir -p reconciler-state
        echo '{"version": 1}' > reconciler-state/bindings.json
        echo "A" > a.txt
        git add a.txt reconciler-state/bindings.json
        git commit -q -m "A (common ancestor; includes reconciler-state)"
        git remote add origin "$tmp/bare.git"
        git push -q origin tickets
    )

    # Bare gets B (remote ahead). Remote modifies reconciler-state so the
    # local-dirty WD's modification on the same path triggers the "would be
    # overwritten by merge" refusal — the exact failure class seen in the
    # live bug. Remote also commits a ticket event so there's clear content
    # to verify lands on origin after the recovery.
    local _bare_writer="$tmp/bare-writer"
    git clone -q -b tickets "$tmp/bare.git" "$_bare_writer"
    (
        cd "$_bare_writer" || exit 1
        git config user.email test@test.com
        git config user.name Test
        git config commit.gpgsign false
        mkdir -p ticket-events
        echo '{"event": "CREATE", "id": "remote-only"}' > ticket-events/remote-only.json
        echo '{"version": 2, "remote_addition": true}' > reconciler-state/bindings.json
        git add ticket-events/remote-only.json reconciler-state/bindings.json
        git commit -q -m "B (remote ahead; ticket event + reconciler-state modification)"
        git push -q origin tickets
    )

    # Back to tracker: add diverging commit C (push will fail non-FF) AND
    # leave bindings.json dirty in the WD (uncommitted) — simulating the
    # Jira reconciler writing in between ticket-CLI operations.
    (
        cd "$tmp/tracker" || exit 1
        # Commit C: a normal ticket event (doesn't touch reconciler-state)
        mkdir -p ticket-events
        echo '{"event": "CREATE", "id": "local-only"}' > ticket-events/local-only.json
        git add ticket-events/local-only.json
        git commit -q -m "C (local-only ticket event)"
        # Now dirty bindings.json in the WD — uncommitted modification that
        # the remote's B touches, so a fetch+merge would refuse to overwrite.
        echo '{"version": 1, "local_uncommitted_change": true}' > reconciler-state/bindings.json
    )

    echo "$tmp"
}

# ── Test 1: push with dirty WD recovers via stash + merge + push + pop ────────
echo "Test 1: dirty WD does not block tickets push"
test_dirty_wd_does_not_block_push() {
    _snapshot_fail
    local tmp tracker bare
    tmp=$(_make_dirty_wd_fixture)
    tracker="$tmp/tracker"
    bare="$tmp/bare.git"

    # Snapshot the local commit SHA that MUST end up on origin.
    local _local_c
    _local_c=$(git -C "$tracker" rev-parse HEAD)

    # Snapshot the dirty WD content that MUST be preserved.
    local _local_dirty_content
    _local_dirty_content=$(cat "$tracker/reconciler-state/bindings.json")

    # Source ticket-lib.sh and invoke _push_tickets_branch.
    # shellcheck disable=SC1090
    source "$TICKET_LIB"
    local _push_out
    _push_out=$(_push_tickets_branch "$tracker" 2>&1)
    local _push_exit=$?

    # Assertion 1: function exits 0 (best-effort contract).
    assert_eq "test_dirty_wd_does_not_block_push: function exits 0" \
        "0" "$_push_exit"

    # Assertion 2: origin/tickets contains the local commit C (the whole point).
    # Before the fix, push silently fails so C never lands on origin.
    local _origin_has_c
    if git -C "$bare" log --format=%H tickets 2>/dev/null | grep -qF "$_local_c"; then
        _origin_has_c="yes"
    else
        _origin_has_c="no"
    fi
    assert_eq "test_dirty_wd_does_not_block_push: origin/tickets contains local commit C" \
        "yes" "$_origin_has_c"

    # Assertion 3: the local-uncommitted modification remains operator-
    # recoverable. The function stashes the dirty WD, merges, then pops the
    # stash. When remote and local both touched the same reconciler-owned
    # path, stash pop produces standard git conflict markers (operator can
    # resolve with `git checkout --theirs` or by re-running the reconciler).
    # When remote did not touch the path, the pop reapplies cleanly. Either
    # outcome is acceptable — the local uncommitted change is not lost.
    local _post_content
    _post_content=$(cat "$tracker/reconciler-state/bindings.json")
    local _recoverable="no"
    # Clean reapply: post-content equals pre-pop dirty content.
    if [ "$_post_content" = "$_local_dirty_content" ]; then
        _recoverable="yes"
    fi
    # OR conflict-mark reapply: post-content contains the local marker AND
    # the git conflict-marker boundaries — operator can resolve.
    if echo "$_post_content" | grep -qF "local_uncommitted_change" \
        && echo "$_post_content" | grep -qE '^(<<<<<<<|=======|>>>>>>>)'; then
        _recoverable="yes"
    fi
    assert_eq "test_dirty_wd_does_not_block_push: local dirty change is operator-recoverable" \
        "yes" "$_recoverable"

    # Assertion 4: NO "merge conflict, attempt 1" misleading warning emitted.
    # The actual error class is "would be overwritten", not a content conflict.
    local _emits_misleading="no"
    if echo "$_push_out" | grep -qF "merge conflict, attempt 1"; then
        _emits_misleading="yes"
    fi
    assert_eq "test_dirty_wd_does_not_block_push: no misleading 'merge conflict' warning" \
        "no" "$_emits_misleading"

    assert_pass_if_clean "test_dirty_wd_does_not_block_push"
}
test_dirty_wd_does_not_block_push

# ── Test 2: retry loop actually retries (counter advances past 1) ─────────────
# Independent regression assertion: even when the recovery path can't avoid
# all failures, the retry loop must advance past attempt 1 on a recoverable
# class of failure. Today the loop is dead code — every failure returns 0
# from the body, so attempt 2/3 never run.
#
# Fixture: a tracker where the merge cannot reconcile (intentionally
# conflicting content on both sides of the merged file). The fix should
# emit "attempt 1", "attempt 2", "attempt 3" warnings, then give up.
echo "Test 2: retry loop advances past attempt 1 on unrecoverable failure"
test_retry_loop_advances_past_attempt_1() {
    _snapshot_fail
    local tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/test-push-retry-advance.XXXXXX")
    _CLEANUP_DIRS+=("$tmp")

    git init -q --bare -b tickets "$tmp/bare.git"
    git init -q -b tickets "$tmp/tracker"
    (
        cd "$tmp/tracker" || exit 1
        git config user.email test@test.com
        git config user.name Test
        git config commit.gpgsign false
        git config gc.auto 0
        echo "common" > shared.txt
        git add shared.txt
        git commit -q -m "A"
        git remote add origin "$tmp/bare.git"
        git push -q origin tickets
    )

    # Bare modifies shared.txt → "remote_value"
    git clone -q -b tickets "$tmp/bare.git" "$tmp/bare-writer"
    (
        cd "$tmp/bare-writer" || exit 1
        git config user.email test@test.com
        git config user.name Test
        git config commit.gpgsign false
        echo "remote_value" > shared.txt
        git add shared.txt
        git commit -q -m "B (remote modifies shared)"
        git push -q origin tickets
    )

    # Tracker modifies shared.txt → "local_value" (conflicts with remote on merge).
    (
        cd "$tmp/tracker" || exit 1
        echo "local_value" > shared.txt
        git add shared.txt
        git commit -q -m "C (local conflicts)"
    )

    # shellcheck disable=SC1090
    source "$TICKET_LIB"
    local _push_out
    _push_out=$(_push_tickets_branch "$tmp/tracker" 2>&1)

    # Assertion: the warning text shows attempt 2 (and/or attempt 3) was
    # actually reached. Before the fix, only "attempt 1" is ever emitted.
    local _saw_attempt_gt_1="no"
    if echo "$_push_out" | grep -qE "(attempt 2|attempt 3|after [0-9]+ retries)"; then
        _saw_attempt_gt_1="yes"
    fi
    assert_eq "test_retry_loop_advances_past_attempt_1: emits attempt 2/3 or 'after N retries'" \
        "yes" "$_saw_attempt_gt_1"

    assert_pass_if_clean "test_retry_loop_advances_past_attempt_1"
}
test_retry_loop_advances_past_attempt_1

print_summary
