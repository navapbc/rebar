#!/usr/bin/env bash
# tests/scratch/test-ticket-init-idempotent.sh
#
# Behavioral test: ticket-init.sh's .scratch/ exclusion handling is idempotent
# against a pre-upgrade tickets-tracker fixture.
#
# Testing Mode: GREEN (ticket-init.sh is already implemented per task ed47)
#
# Scenario:
#   We manually fabricate a "pre-upgrade" tickets-tracker fixture — a git repo
#   with a mounted tickets worktree and .git/info/exclude entries that predate
#   the .scratch/ exclusion change. The fixture has:
#     - A main repo with two pre-existing entries in .git/info/exclude
#       (e.g., .env and .DS_Store) but NO .scratch/ line
#     - A tickets-branch worktree with .gitignore containing .env-id and
#       .state-cache but NO .scratch/ line
#     - The tickets worktree's own .git/info/exclude with one pre-existing
#       entry (e.g., .env-id) but NO .scratch/ line
#
# Assertions after first re-run:
#   1. .scratch/ is added to main repo .git/info/exclude
#   2. .scratch/ is added to tickets-branch .gitignore (committed on branch)
#   3. .scratch/ is added to tickets-tracker worktree's .git/info/exclude
#   4. Pre-existing entries in all 3 locations are preserved
#
# Assertions after second re-run (idempotency):
#   5. Exactly ONE .scratch/ entry in main repo .git/info/exclude
#   6. Exactly ONE .scratch/ entry in tickets-branch .gitignore
#   7. Exactly ONE .scratch/ entry in tickets-tracker worktree's .git/info/exclude
#
# Usage: bash tests/scratch/test-ticket-init-idempotent.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_INIT="$REPO_ROOT/src/rebar/_engine/ticket-init.sh"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-ticket-init-idempotent.sh: .scratch/ exclusion idempotency against pre-upgrade fixture ==="

# ── Preflight ────────────────────────────────────────────────────────────────
if [ ! -f "$TICKET_INIT" ]; then
    echo "FATAL: ticket-init.sh not found at $TICKET_INIT" >&2
    exit 1
fi

# ── Cleanup tracking ──────────────────────────────────────────────────────────
_CLEANUP_DIRS=()
_cleanup() {
    for d in "${_CLEANUP_DIRS[@]:-}"; do
        [ -n "$d" ] && rm -rf "$d"
    done
}
trap _cleanup EXIT

# ── Helper: resolve the tickets-tracker worktree-specific git dir ─────────────
# Returns the path to the gitdir (e.g. .git/worktrees/<name>) for the
# tickets-tracker linked worktree.
_resolve_tracker_git_dir() {
    local tracker_dir="$1"
    local git_file="$tracker_dir/.git"
    local git_dir=""
    if [ -f "$git_file" ]; then
        git_dir="$(sed -n 's/^gitdir: //p' "$git_file")"
        if [ -n "$git_dir" ] && [[ "$git_dir" != /* ]]; then
            git_dir="$tracker_dir/$git_dir"
        fi
    fi
    if [ -z "$git_dir" ]; then
        # Fallback: non-worktree case
        git_dir="$tracker_dir/.git"
    fi
    echo "$git_dir"
}

# ── Helper: fabricate a pre-upgrade tickets-tracker fixture ──────────────────
#
# Produces a main git repo at $tmpdir that simulates the "pre-upgrade" state:
# the tickets branch exists with an old-style .gitignore (no .scratch/ entry),
# but the worktree is NOT yet mounted. This mirrors an environment where the
# old ticket-init.sh was used on another machine and the tickets branch was
# already pushed, but the current machine has never run ticket-init.sh — or
# the worktree was pruned and needs to be remounted. When the new ticket-init.sh
# runs on this fixture, it goes through the full init path (branch already
# exists locally → mount worktree → run .scratch/ exclusion code).
#
# Fixture state:
#   - Tickets branch committed with .env-id and .state-cache in .gitignore
#     but NO .scratch/ line (pre-upgrade)
#   - Worktree directory does NOT exist (not yet mounted)
#   - Main repo .git/info/exclude has .tickets-tracker and .env
#     but NO .scratch/ entry (pre-upgrade)
#
# After the fixture is built, ticket-init.sh will:
#   1. Not hit the early-exit guard (no .tickets-tracker/ dir)
#   2. Detect tickets branch exists locally
#   3. Mount the worktree
#   4. Run the pre-upgrade .gitignore upgrade path (append .scratch/)
#   5. Set up .git/info/exclude entries including .scratch/
#
_make_preupgrade_fixture() {
    local tmpdir
    tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/test-ticket-init-idempotent.XXXXXX")
    _CLEANUP_DIRS+=("$tmpdir")

    # ── Step 1: Create the main repo ─────────────────────────────────────────
    git -C "$tmpdir" init -q
    git -C "$tmpdir" config user.email "test@test.invalid"
    git -C "$tmpdir" config user.name "Test"
    git -C "$tmpdir" config commit.gpgsign false
    git -C "$tmpdir" config tag.gpgsign false
    # Initial commit so worktree add works on git < 2.40
    git -C "$tmpdir" commit --allow-empty -q --no-verify -m "init"

    # ── Step 2: Create the tickets orphan branch (no worktree) ───────────────
    # Create an orphan branch directly (not as a worktree) so that the main
    # repo has a "tickets" branch but no .tickets-tracker/ worktree directory.
    # This simulates a pre-upgrade state where the branch was created on
    # another machine and the worktree has not been mounted here yet.
    _git_minor=$(git --version 2>/dev/null | grep -o '[0-9]\+\.[0-9]\+' | head -1 | cut -d. -f2 || echo "0")
    _git_major=$(git --version 2>/dev/null | grep -o '[0-9]\+\.[0-9]\+' | head -1 | cut -d. -f1 || echo "2")

    # Use a temp worktree to bootstrap the orphan branch, then remove the
    # worktree (leaving only the branch). This is the cleanest cross-version
    # approach to create a committed orphan branch without a worktree.
    local bootstrap_dir
    bootstrap_dir=$(mktemp -d "${TMPDIR:-/tmp}/test-ticket-init-bootstrap.XXXXXX")
    _CLEANUP_DIRS+=("$bootstrap_dir")

    if (( _git_major > 2 || (_git_major == 2 && _git_minor >= 40) )); then
        git -C "$tmpdir" worktree add --orphan -b tickets "$bootstrap_dir" -q 2>/dev/null
    else
        git -C "$tmpdir" worktree add --detach "$bootstrap_dir" -q 2>/dev/null
        git -C "$bootstrap_dir" checkout --orphan tickets 2>/dev/null
        git -C "$bootstrap_dir" rm -rf . --quiet 2>/dev/null || true
    fi

    git -C "$bootstrap_dir" config user.email "test@test.invalid"
    git -C "$bootstrap_dir" config user.name "Test"
    git -C "$bootstrap_dir" config commit.gpgsign false
    git -C "$bootstrap_dir" config tag.gpgsign false

    # ── Step 3: Commit pre-upgrade .gitignore (NO .scratch/) ─────────────────
    cat > "$bootstrap_dir/.gitignore" <<'GITIGNORE'
.env-id
.state-cache
GITIGNORE
    git -C "$bootstrap_dir" add .gitignore
    git -C "$bootstrap_dir" commit -q --no-verify -m "chore: pre-upgrade .gitignore (no .scratch/)"

    # Optionally also commit a no-op pre-commit config (matches what old init produced)
    cat > "$bootstrap_dir/.pre-commit-config.yaml" <<'PRECOMMIT'
repos: []
PRECOMMIT
    git -C "$bootstrap_dir" add .pre-commit-config.yaml
    git -C "$bootstrap_dir" commit -q --no-verify -m "chore: add no-op .pre-commit-config.yaml"

    # ── Step 4: Remove the bootstrap worktree, leaving only the branch ────────
    # After this, $tmpdir has a "tickets" branch but NO .tickets-tracker/ dir.
    git -C "$tmpdir" worktree remove "$bootstrap_dir" 2>/dev/null || {
        # Fallback: prune stale entry if remove fails
        rm -rf "$bootstrap_dir"
        git -C "$tmpdir" worktree prune 2>/dev/null || true
    }

    # ── Step 5: Set up main repo .git/info/exclude with pre-existing entries ──
    # NO .scratch/ entry — this is the pre-upgrade state.
    local main_exclude="$tmpdir/.git/info/exclude"
    mkdir -p "$(dirname "$main_exclude")"
    # Write known pre-existing lines (not .scratch/)
    printf '.tickets-tracker\n.env\n' > "$main_exclude"

    # Verify the fixture is correct: tickets branch exists, no worktree
    if ! git -C "$tmpdir" rev-parse --verify tickets &>/dev/null; then
        echo "FIXTURE ERROR: tickets branch not created in $tmpdir" >&2
        return 1
    fi
    if [ -d "$tmpdir/.tickets-tracker" ]; then
        echo "FIXTURE ERROR: .tickets-tracker/ should not exist yet in $tmpdir" >&2
        return 1
    fi

    echo "$tmpdir"
}

# ══════════════════════════════════════════════════════════════════════════════
# Test suite
# ══════════════════════════════════════════════════════════════════════════════

# ── Build the fixture; pre-run state variables ────────────────────────────────
_FIXTURE_REPO=""
_FIXTURE_REPO=$(_make_preupgrade_fixture)

_TRACKER_DIR="$_FIXTURE_REPO/.tickets-tracker"
_MAIN_EXCLUDE="$_FIXTURE_REPO/.git/info/exclude"

# ── Capture baseline of pre-existing main-exclude entries ─────────────────────
# The tracker worktree does NOT yet exist at this point — _TRACKER_EXCLUDE
# is resolved AFTER the first run once the worktree has been mounted.
_main_exclude_baseline=$(cat "$_MAIN_EXCLUDE" 2>/dev/null || echo "")

# ══════════════════════════════════════════════════════════════════════════════
# FIRST RUN: ticket-init.sh against the pre-upgrade fixture
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "── First run: ticket-init.sh against pre-upgrade fixture ──"

_run1_exit=0
( cd "$_FIXTURE_REPO" && PROJECT_ROOT="$_FIXTURE_REPO" bash "$TICKET_INIT" --silent ) \
    >/dev/null 2>&1 || _run1_exit=$?

assert_eq "first ticket-init.sh run exits 0" "0" "$_run1_exit"

# Resolve tracker gitdir AFTER first run (worktree is now mounted)
_TRACKER_GIT_DIR=$(_resolve_tracker_git_dir "$_TRACKER_DIR")
_TRACKER_EXCLUDE="$_TRACKER_GIT_DIR/info/exclude"

# ── Test 1: .scratch/ added to main repo .git/info/exclude ───────────────────
echo ""
echo "── Test 1: .scratch/ added to main repo .git/info/exclude after first run ──"

test_scratch_in_main_exclude_after_first_run() {
    local found_scratch=0
    if grep -qFx '.scratch/' "$_MAIN_EXCLUDE" 2>/dev/null; then
        found_scratch=1
    fi
    assert_eq ".scratch/ present in main repo .git/info/exclude" "1" "$found_scratch"
}
test_scratch_in_main_exclude_after_first_run

# ── Test 2: .scratch/ added to tickets-branch .gitignore (committed) ─────────
echo ""
echo "── Test 2: .scratch/ added to tickets-branch .gitignore (committed) after first run ──"

test_scratch_in_tickets_gitignore_after_first_run() {
    local gitignore_content
    gitignore_content=$(git -C "$_TRACKER_DIR" show tickets:.gitignore 2>/dev/null || echo "")
    local found_scratch=0
    if echo "$gitignore_content" | grep -qFx '.scratch/' 2>/dev/null; then
        found_scratch=1
    fi
    assert_eq ".scratch/ present in committed tickets-branch .gitignore" "1" "$found_scratch"
}
test_scratch_in_tickets_gitignore_after_first_run

# ── Test 3: .scratch/ added to tickets-tracker worktree's .git/info/exclude ──
echo ""
echo "── Test 3: .scratch/ added to tickets-tracker worktree .git/info/exclude after first run ──"

test_scratch_in_tracker_exclude_after_first_run() {
    local found_scratch=0
    if grep -qFx '.scratch/' "$_TRACKER_EXCLUDE" 2>/dev/null; then
        found_scratch=1
    fi
    assert_eq ".scratch/ present in tracker worktree .git/info/exclude" "1" "$found_scratch"
}
test_scratch_in_tracker_exclude_after_first_run

# ── Test 4: Pre-existing entries in main exclude are preserved ────────────────
echo ""
echo "── Test 4: Pre-existing entries preserved in main repo .git/info/exclude ──"

test_preexisting_main_entries_preserved() {
    # The fixture had ".tickets-tracker" and ".env" before ticket-init ran.
    local found_tickets_tracker=0
    local found_env=0
    if grep -qFx '.tickets-tracker' "$_MAIN_EXCLUDE" 2>/dev/null; then
        found_tickets_tracker=1
    fi
    if grep -qFx '.env' "$_MAIN_EXCLUDE" 2>/dev/null; then
        found_env=1
    fi
    assert_eq "pre-existing .tickets-tracker entry preserved in main exclude" "1" "$found_tickets_tracker"
    assert_eq "pre-existing .env entry preserved in main exclude" "1" "$found_env"
}
test_preexisting_main_entries_preserved

# ── Test 5: Pre-existing tickets .gitignore entries are preserved ─────────────
echo ""
echo "── Test 5: Pre-existing tickets .gitignore entries (.env-id, .state-cache) preserved ──"

test_preexisting_gitignore_entries_preserved() {
    local gitignore_content
    gitignore_content=$(git -C "$_TRACKER_DIR" show tickets:.gitignore 2>/dev/null || echo "")
    local found_envid=0
    local found_statecache=0
    if echo "$gitignore_content" | grep -qFx '.env-id' 2>/dev/null; then
        found_envid=1
    fi
    if echo "$gitignore_content" | grep -qFx '.state-cache' 2>/dev/null; then
        found_statecache=1
    fi
    assert_eq "pre-existing .env-id entry preserved in tickets .gitignore" "1" "$found_envid"
    assert_eq "pre-existing .state-cache entry preserved in tickets .gitignore" "1" "$found_statecache"
}
test_preexisting_gitignore_entries_preserved

# ══════════════════════════════════════════════════════════════════════════════
# SECOND RUN: ticket-init.sh again (idempotency check)
# The worktree now EXISTS (early-exit path fires). The .scratch/ exclusions
# must already be in place from the first run — second run must not duplicate.
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "── Second run: ticket-init.sh again (idempotency) ──"

_run2_exit=0
( cd "$_FIXTURE_REPO" && PROJECT_ROOT="$_FIXTURE_REPO" bash "$TICKET_INIT" --silent ) \
    >/dev/null 2>&1 || _run2_exit=$?

assert_eq "second ticket-init.sh run exits 0" "0" "$_run2_exit"

# ── Test 6: Exactly one .scratch/ entry in main repo .git/info/exclude ────────
echo ""
echo "── Test 6: Exactly ONE .scratch/ entry in main repo .git/info/exclude (no duplicates) ──"

test_no_duplicate_scratch_in_main_exclude() {
    local scratch_count
    scratch_count=$(grep -cFx '.scratch/' "$_MAIN_EXCLUDE" 2>/dev/null || echo "0")
    assert_eq "exactly one .scratch/ entry in main repo .git/info/exclude" "1" "$scratch_count"
}
test_no_duplicate_scratch_in_main_exclude

# ── Test 7: Exactly one .scratch/ entry in tickets-branch .gitignore ──────────
echo ""
echo "── Test 7: Exactly ONE .scratch/ entry in tickets-branch .gitignore (no duplicates) ──"

test_no_duplicate_scratch_in_gitignore() {
    local gitignore_content
    gitignore_content=$(git -C "$_TRACKER_DIR" show tickets:.gitignore 2>/dev/null || echo "")
    local scratch_count
    scratch_count=$(echo "$gitignore_content" | grep -cFx '.scratch/' 2>/dev/null || echo "0")
    assert_eq "exactly one .scratch/ entry in tickets-branch .gitignore" "1" "$scratch_count"
}
test_no_duplicate_scratch_in_gitignore

# ── Test 8: Exactly one .scratch/ entry in tracker worktree .git/info/exclude ─
echo ""
echo "── Test 8: Exactly ONE .scratch/ entry in tracker .git/info/exclude (no duplicates) ──"

test_no_duplicate_scratch_in_tracker_exclude() {
    local scratch_count
    scratch_count=$(grep -cFx '.scratch/' "$_TRACKER_EXCLUDE" 2>/dev/null || echo "0")
    assert_eq "exactly one .scratch/ entry in tracker worktree .git/info/exclude" "1" "$scratch_count"
}
test_no_duplicate_scratch_in_tracker_exclude

# ── Test 9: Pre-existing entries still intact after second run ────────────────
echo ""
echo "── Test 9: Pre-existing entries still intact in all locations after second run ──"

test_preexisting_entries_intact_after_second_run() {
    # Main exclude: .tickets-tracker and .env must still be present
    local found_tickets_tracker=0
    local found_env=0
    if grep -qFx '.tickets-tracker' "$_MAIN_EXCLUDE" 2>/dev/null; then
        found_tickets_tracker=1
    fi
    if grep -qFx '.env' "$_MAIN_EXCLUDE" 2>/dev/null; then
        found_env=1
    fi
    assert_eq ".tickets-tracker preserved after second run" "1" "$found_tickets_tracker"
    assert_eq ".env preserved after second run" "1" "$found_env"

    # Tickets .gitignore: .env-id and .state-cache must still be present
    local gitignore_content
    gitignore_content=$(git -C "$_TRACKER_DIR" show tickets:.gitignore 2>/dev/null || echo "")
    local found_envid=0
    local found_statecache=0
    if echo "$gitignore_content" | grep -qFx '.env-id' 2>/dev/null; then
        found_envid=1
    fi
    if echo "$gitignore_content" | grep -qFx '.state-cache' 2>/dev/null; then
        found_statecache=1
    fi
    assert_eq ".env-id preserved in gitignore after second run" "1" "$found_envid"
    assert_eq ".state-cache preserved in gitignore after second run" "1" "$found_statecache"
}
test_preexisting_entries_intact_after_second_run

# ══════════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════════
echo ""
print_summary
