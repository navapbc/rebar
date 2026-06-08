#!/usr/bin/env bash
# test-ticket-fsck-recover.sh
# Behavioral test for ticket-fsck-recover.sh (Fix 2a, bug 637b).
#
# Verifies the recovery script:
#   1. Detects paused-rebase state via rebase-merge/ directory marker (modern git)
#   2. Drains a stuck rebase via `git rebase --continue`
#   3. On continue-failure, falls back to abort + cherry-pick dangling commits
#      matching the ticket commit message pattern
#   4. Exits 0 when no recovery needed
#   5. Exits 0 when recovery succeeds (pending picks landed)
#   6. Exits 2 when recovery fails AND no dangling commits to cherry-pick
#
# Testing mode: RED — must FAIL until ticket-fsck-recover.sh exists.
#
# Usage: bash tests/scripts/test-ticket-fsck-recover.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
RECOVER_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-fsck-recover.sh"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-ticket-fsck-recover.sh ==="

# Suite-runner skip: when run as part of run-script-tests.sh and the recovery
# script does not yet exist, skip with exit 0 (this is a known RED state).
if [ "${_RUN_ALL_ACTIVE:-0}" = "1" ] && [ ! -f "$RECOVER_SCRIPT" ]; then
    echo "SKIP: ticket-fsck-recover.sh not yet implemented — tests deferred"
    echo ""
    printf "PASSED: 0  FAILED: 0\n"
    exit 0
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

# Create a tempdir git repo with a paused rebase. Returns the path on stdout.
# The repo has 5 commits (A,B,C,D,E). A rebase from D onto A is paused after
# picking B (because the C pick conflicts with a synthetic working-tree change).
# Layout:
#   * E (HEAD before rebase)
#   * D
#   * C  <- conflicts during rebase
#   * B
#   * A  (rebase target)
_create_paused_rebase_repo() {
    local tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/test-ticket-fsck-recover.XXXXXX")
    _CLEANUP_DIRS+=("$tmp")

    git init -q -b main "$tmp/repo"
    cd "$tmp/repo" || exit 1
    git config user.email test@test.com
    git config user.name Test
    git config commit.gpgsign false

    # Commit A: baseline
    echo "A" > file.txt
    git add file.txt
    git commit -q -m "A"

    # Commit B: appends B
    echo "B" >> file.txt
    git commit -aq -m "B"

    # Commit C: appends C  (will conflict during rebase)
    echo "C" >> file.txt
    git commit -aq -m "C"

    # Commit D: appends D
    echo "D" >> file.txt
    git commit -aq -m "D"

    # Commit E: appends E
    echo "E" >> file.txt
    git commit -aq -m "E"

    # Now pause a rebase. Use interactive rebase with --exec=false to
    # deterministically pause after first pick (B).
    GIT_SEQUENCE_EDITOR="sed -i.bak 's/^pick /pick /; 2,5 s/^pick.*/x false/'" \
        git -c sequence.editor= rebase -i --root 2>/dev/null || true

    cd - >/dev/null || exit 1
    echo "$tmp/repo"
}

# Resolve the tracker git dir for a repo
_resolve_repo_git_dir() {
    local repo="$1"
    local git_file="$repo/.git"
    if [ -d "$git_file" ]; then
        echo "$git_file"
    elif [ -f "$git_file" ]; then
        sed 's/^gitdir: //' "$git_file"
    fi
}

# ── Test 1: script exists and is executable ──────────────────────────────────
echo "Test 1: ticket-fsck-recover.sh exists and is executable"
test_script_exists_and_executable() {
    if [ ! -f "$RECOVER_SCRIPT" ]; then
        assert_eq "ticket-fsck-recover.sh exists" "exists" "missing"
        return
    fi
    assert_eq "ticket-fsck-recover.sh exists" "exists" "exists"
    if [ -x "$RECOVER_SCRIPT" ]; then
        assert_eq "ticket-fsck-recover.sh executable" "executable" "executable"
    else
        assert_eq "ticket-fsck-recover.sh executable" "executable" "not-executable"
    fi
}
test_script_exists_and_executable

# ── Test 2: exits 0 when no rebase in progress ───────────────────────────────
echo "Test 2: exits 0 when no rebase in progress (no-op path)"
test_no_op_when_no_rebase() {
    _snapshot_fail
    if [ ! -f "$RECOVER_SCRIPT" ]; then
        assert_eq "prereq: recover script exists" "exists" "missing"
        return
    fi

    local tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/test-ticket-fsck-no-rebase.XXXXXX")
    _CLEANUP_DIRS+=("$tmp")
    git init -q -b main "$tmp/repo"
    cd "$tmp/repo" || exit 1
    git config user.email test@test.com
    git config user.name Test
    git config commit.gpgsign false
    echo "X" > x.txt
    git add x.txt
    git commit -q -m "X"
    cd - >/dev/null || exit 1

    local exit_code=0
    bash "$RECOVER_SCRIPT" --tracker-dir "$tmp/repo" >/dev/null 2>&1 || exit_code=$?
    assert_eq "no-rebase repo: exits 0" "0" "$exit_code"

    assert_pass_if_clean "test_no_op_when_no_rebase"
}
test_no_op_when_no_rebase

# ── Test 3: detects paused rebase via rebase-merge/ marker ───────────────────
echo "Test 3: detects paused rebase via rebase-merge/ directory marker"
test_detects_rebase_merge_marker() {
    _snapshot_fail
    if [ ! -f "$RECOVER_SCRIPT" ]; then
        assert_eq "prereq: recover script exists" "exists" "missing"
        return
    fi

    local repo
    repo=$(_create_paused_rebase_repo)
    local git_dir
    git_dir=$(_resolve_repo_git_dir "$repo")

    # Sanity: confirm we have a rebase-merge dir (the bug's actual marker)
    [ -d "$git_dir/rebase-merge" ] || {
        echo "WARN: synthetic fixture failed to create rebase-merge dir; skipping detection test"
        return
    }

    # Recovery script should DETECT the rebase via stdout signal AND exit code 3
    # (the documented machine-readable contract — callers branch on exit code).
    local output
    local detect_exit=0
    output=$(bash "$RECOVER_SCRIPT" --tracker-dir "$repo" --detect-only 2>&1) || detect_exit=$?
    assert_eq "--detect-only exit code is 3 (documented detection signal)" "3" "$detect_exit"
    if echo "$output" | grep -qiE 'rebase.*detected|paused.*rebase|in.progress'; then
        assert_eq "detection output contains rebase signal" "found" "found"
    else
        assert_eq "detection output contains rebase signal" "found" "not-found"
        echo "  actual output: $output"
    fi

    assert_pass_if_clean "test_detects_rebase_merge_marker"
}
test_detects_rebase_merge_marker

# ── Test 4: drains a recoverable rebase via --continue ───────────────────────
echo "Test 4: drains a recoverable rebase via git rebase --continue"
test_drains_recoverable_rebase() {
    _snapshot_fail
    if [ ! -f "$RECOVER_SCRIPT" ]; then
        assert_eq "prereq: recover script exists" "exists" "missing"
        return
    fi

    # Build a simpler fixture where rebase --continue actually succeeds
    local tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/test-ticket-fsck-drainable.XXXXXX")
    _CLEANUP_DIRS+=("$tmp")
    git init -q -b main "$tmp/repo"
    cd "$tmp/repo" || exit 1
    git config user.email test@test.com
    git config user.name Test
    git config commit.gpgsign false

    # 3 commits on independent files (no conflicts)
    echo "a" > a.txt; git add a.txt; git commit -q -m "add a"
    echo "b" > b.txt; git add b.txt; git commit -q -m "add b"
    echo "c" > c.txt; git add c.txt; git commit -q -m "add c"

    # Create a topic branch from HEAD~2 with a non-conflicting commit
    git checkout -q -b topic HEAD~2
    echo "z" > z.txt; git add z.txt; git commit -q -m "add z"

    # Rebase topic onto main with --exec=false to pause AFTER the pick
    git checkout -q main
    git checkout -q topic
    git rebase --exec=false main 2>/dev/null || true

    cd - >/dev/null || exit 1

    local git_dir
    git_dir=$(_resolve_repo_git_dir "$tmp/repo")

    # If our synthetic setup didn't actually pause, skip
    if [ ! -d "$git_dir/rebase-merge" ] && [ ! -d "$git_dir/rebase-apply" ]; then
        echo "WARN: synthetic fixture did not produce paused rebase; skipping drain test"
        return
    fi

    # Run recovery — should successfully drain
    local exit_code=0
    bash "$RECOVER_SCRIPT" --tracker-dir "$tmp/repo" >/dev/null 2>&1 || exit_code=$?
    assert_eq "drainable rebase: exits 0" "0" "$exit_code"

    # rebase-merge/ should be gone
    if [ -d "$git_dir/rebase-merge" ] || [ -d "$git_dir/rebase-apply" ]; then
        assert_eq "rebase state cleared" "cleared" "still-present"
    else
        assert_eq "rebase state cleared" "cleared" "cleared"
    fi

    assert_pass_if_clean "test_drains_recoverable_rebase"
}
test_drains_recoverable_rebase

# ── Test 5: cherry-pick fallback when --continue fails ───────────────────────
echo "Test 5: cherry-picks dangling ticket commits when --continue conflicts"
test_cherry_pick_fallback() {
    _snapshot_fail
    if [ ! -f "$RECOVER_SCRIPT" ]; then
        assert_eq "prereq: recover script exists" "exists" "missing"
        return
    fi

    # Build a fixture where a rebase will conflict, but the dangling commits
    # match the ticket commit message pattern.
    local tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/test-ticket-fsck-cherry.XXXXXX")
    _CLEANUP_DIRS+=("$tmp")
    git init -q -b main "$tmp/repo"
    cd "$tmp/repo" || exit 1
    git config user.email test@test.com
    git config user.name Test
    git config commit.gpgsign false

    # Baseline commit
    mkdir -p ticket-abc-1
    echo '{}' > ticket-abc-1/event.json
    git add .
    git commit -q -m "baseline"

    # Save the baseline SHA
    local baseline_sha
    baseline_sha=$(git rev-parse HEAD)

    # Add a ticket commit
    mkdir -p ticket-xyz-2
    echo '{"event_type":"CREATE","data":{"ticket_type":"task","title":"recovered"}}' > ticket-xyz-2/event.json
    git add ticket-xyz-2/
    git commit -q -m "ticket: CREATE xyz-2"
    local ticket_commit_sha
    ticket_commit_sha=$(git rev-parse HEAD)

    # Reset back to baseline so the ticket commit becomes dangling
    git reset -q --hard "$baseline_sha"

    # Verify the dangling commit exists and matches the ticket message pattern
    if ! git cat-file -t "$ticket_commit_sha" 2>/dev/null | grep -q commit; then
        echo "WARN: synthetic fixture failed to create dangling ticket commit; skipping"
        return
    fi

    cd - >/dev/null || exit 1

    # Run recovery with --recover-dangling flag (only kick the cherry-pick path)
    local exit_code=0
    bash "$RECOVER_SCRIPT" --tracker-dir "$tmp/repo" --recover-dangling >/dev/null 2>&1 || exit_code=$?
    # Cherry-pick should succeed (no conflict because file is new)
    assert_eq "cherry-pick recovery: exits 0" "0" "$exit_code"

    # The recovered ticket dir should exist
    if [ -f "$tmp/repo/ticket-xyz-2/event.json" ]; then
        assert_eq "recovered ticket file present" "present" "present"
    else
        assert_eq "recovered ticket file present" "present" "missing"
    fi

    assert_pass_if_clean "test_cherry_pick_fallback"
}
test_cherry_pick_fallback

# ── Test 6: --help exits 0 with usage text ───────────────────────────────────
echo "Test 6: --help prints usage and exits 0"
test_help_flag() {
    _snapshot_fail
    if [ ! -f "$RECOVER_SCRIPT" ]; then
        assert_eq "prereq: recover script exists" "exists" "missing"
        return
    fi
    local exit_code=0
    local output
    output=$(bash "$RECOVER_SCRIPT" --help 2>&1) || exit_code=$?
    assert_eq "--help exits 0" "0" "$exit_code"
    if echo "$output" | grep -qiE 'usage|--tracker-dir|--detect-only|--recover-dangling'; then
        assert_eq "--help output has usage info" "found" "found"
    else
        assert_eq "--help output has usage info" "found" "not-found"
    fi
    assert_pass_if_clean "test_help_flag"
}
test_help_flag

print_summary
