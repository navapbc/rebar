#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-init.sh
# Tests for src/rebar/_engine/ticket init subcommand
#
# These tests are RED — they test functionality that does not yet exist.
# All test functions must return non-zero until `ticket init` is implemented.
#
# Usage: bash tests/scripts/suites/test-ticket-init.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-init.sh ==="

# ── Helper: create a fresh temp git repo ─────────────────────────────────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_test_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Test 1: test_ticket_init_creates_orphan_branch_and_worktree ──────────────
echo "Test 1: ticket init creates .tickets-tracker/ worktree on orphan branch"
test_ticket_init_creates_orphan_branch_and_worktree() {
    local repo
    repo=$(_make_test_repo)

    # Run from inside the repo; suppress incidental command noise only
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Assert: .tickets-tracker/ directory exists
    if [ -d "$repo/.tickets-tracker" ]; then
        assert_eq "orphan branch and worktree: .tickets-tracker/ exists" "exists" "exists"
    else
        assert_eq "orphan branch and worktree: .tickets-tracker/ exists" "exists" "missing"
    fi

    # Assert: .tickets-tracker/ is a git worktree (has .git file, not .git dir)
    if [ -f "$repo/.tickets-tracker/.git" ]; then
        assert_eq "orphan branch and worktree: .tickets-tracker/.git is a file" "file" "file"
    else
        assert_eq "orphan branch and worktree: .tickets-tracker/.git is a file" "file" "missing-or-dir"
    fi

    # Assert: the tickets orphan branch exists
    if git -C "$repo/.tickets-tracker" rev-parse --verify tickets &>/dev/null; then
        assert_eq "orphan branch and worktree: branch 'tickets' exists" "exists" "exists"
    else
        assert_eq "orphan branch and worktree: branch 'tickets' exists" "exists" "missing"
    fi
}
test_ticket_init_creates_orphan_branch_and_worktree

# ── Test 2: test_ticket_init_creates_env_id ───────────────────────────────────
echo "Test 2: ticket init creates .env-id with UUID4 content"
test_ticket_init_creates_env_id() {
    local repo
    repo=$(_make_test_repo)

    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Assert: .env-id file exists
    if [ -f "$repo/.tickets-tracker/.env-id" ]; then
        assert_eq "env-id: .env-id exists" "exists" "exists"
    else
        assert_eq "env-id: .env-id exists" "exists" "missing"
        return
    fi

    # Assert: content matches UUID4 pattern (8-4-4-4-12 hex, version 4, variant bits 8/9/a/b)
    local env_id
    env_id=$(cat "$repo/.tickets-tracker/.env-id")
    if [[ "$env_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]; then
        assert_eq "env-id: content is valid UUID4" "valid" "valid"
    else
        assert_eq "env-id: content is valid UUID4" "valid" "invalid: $env_id"
    fi
}
test_ticket_init_creates_env_id

# ── Test 3: test_ticket_init_is_idempotent ────────────────────────────────────
echo "Test 3: ticket init is idempotent (second run exits 0)"
test_ticket_init_is_idempotent() {
    local repo
    repo=$(_make_test_repo)

    # First run
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Second run — must not fail
    local exit2=0
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || exit2=$?

    assert_eq "idempotent: second run exits 0" "0" "$exit2"
}
test_ticket_init_is_idempotent

# ── Test 4: test_ticket_init_adds_to_gitignore ───────────────────────────────
echo "Test 4: ticket init commits .gitignore on tickets branch excluding .env-id and .state-cache"
test_ticket_init_adds_to_gitignore() {
    local repo
    repo=$(_make_test_repo)

    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Assert: .gitignore exists as a committed file on the tickets branch
    if git -C "$repo/.tickets-tracker" show tickets:.gitignore &>/dev/null; then
        assert_eq "gitignore: committed on tickets branch" "committed" "committed"
    else
        assert_eq "gitignore: committed on tickets branch" "committed" "missing"
        return
    fi

    local gitignore_content
    gitignore_content=$(git -C "$repo/.tickets-tracker" show tickets:.gitignore 2>/dev/null)

    # Assert: .env-id is excluded
    if [[ "$gitignore_content" == *".env-id"* ]]; then
        assert_eq "gitignore: excludes .env-id" "excluded" "excluded"
    else
        assert_eq "gitignore: excludes .env-id" "excluded" "missing"
    fi

    # Assert: .state-cache is excluded
    if [[ "$gitignore_content" == *".state-cache"* ]]; then
        assert_eq "gitignore: excludes .state-cache" "excluded" "excluded"
    else
        assert_eq "gitignore: excludes .state-cache" "excluded" "missing"
    fi
}
test_ticket_init_adds_to_gitignore

# ── Test 4b: test_ticket_init_seeds_precommit_stub ────────────────────────────
# Bug 27d8-b230: the tickets orphan branch needs a no-op .pre-commit-config.yaml
# so the pre-commit framework (when installed as a pre-push hook in the host
# repo) accepts pushes from .tickets-tracker without PRE_COMMIT_ALLOW_NO_CONFIG=1
# on every caller.
echo "Test 4b: ticket init commits no-op .pre-commit-config.yaml on tickets branch"
test_ticket_init_seeds_precommit_stub() {
    local repo
    repo=$(_make_test_repo)

    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Assert: .pre-commit-config.yaml exists as a committed file on the tickets branch
    if git -C "$repo/.tickets-tracker" show tickets:.pre-commit-config.yaml &>/dev/null; then
        assert_eq "precommit-stub: committed on tickets branch" "committed" "committed"
    else
        assert_eq "precommit-stub: committed on tickets branch" "committed" "missing"
        return
    fi

    # Assert: content declares no-op repos list (the framework requires `repos:` key)
    local stub_content
    stub_content=$(git -C "$repo/.tickets-tracker" show tickets:.pre-commit-config.yaml 2>/dev/null)
    if [[ "$stub_content" == *"repos: []"* ]]; then
        assert_eq "precommit-stub: declares 'repos: []' no-op" "ok" "ok"
    else
        assert_eq "precommit-stub: declares 'repos: []' no-op" "ok" "missing"
    fi
}
test_ticket_init_seeds_precommit_stub


# ── Test 5: test_ticket_init_adds_to_git_info_exclude ────────────────────────
echo "Test 5: ticket init adds .tickets-tracker to .git/info/exclude"
test_ticket_init_adds_to_git_info_exclude() {
    local repo
    repo=$(_make_test_repo)

    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Assert: .git/info/exclude exists
    if [ -f "$repo/.git/info/exclude" ]; then
        assert_eq "git-info-exclude: file exists" "exists" "exists"
    else
        assert_eq "git-info-exclude: file exists" "exists" "missing"
        return
    fi

    # Assert: .tickets-tracker is listed
    if grep -q '\.tickets-tracker' "$repo/.git/info/exclude"; then
        assert_eq "git-info-exclude: contains .tickets-tracker" "present" "present"
    else
        assert_eq "git-info-exclude: contains .tickets-tracker" "present" "missing"
    fi
}
test_ticket_init_adds_to_git_info_exclude

# ── Test 6: test_ticket_init_remounts_existing_branch ─────────────────────────
echo "Test 6: ticket init remounts existing tickets branch from remote"
test_ticket_init_remounts_existing_branch() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")

    # Step 1: Create a bare remote with an existing tickets branch
    git init -q --bare "$tmp/remote.git"

    # Step 2: Create an origin repo, init tickets, and push to the bare remote
    git init -q -b main "$tmp/origin"
    git -C "$tmp/origin" config user.email "test@test.com"
    git -C "$tmp/origin" config user.name "Test"
    echo "initial" > "$tmp/origin/README.md"
    git -C "$tmp/origin" add -A
    git -C "$tmp/origin" commit -q -m "init"
    git -C "$tmp/origin" remote add origin "$tmp/remote.git"
    git -C "$tmp/origin" push -q origin main 2>/dev/null

    # Create orphan tickets branch on origin and push it
    (cd "$tmp/origin" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true
    git -C "$tmp/origin" push -q origin tickets 2>/dev/null

    # Step 3: Clone from the bare remote (simulates fresh clone)
    git clone -q "$tmp/remote.git" "$tmp/clone" 2>/dev/null

    # Step 4: Run ticket init in the clone — should mount existing branch
    (cd "$tmp/clone" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Assert: .tickets-tracker/ exists in the clone
    if [ -d "$tmp/clone/.tickets-tracker" ]; then
        assert_eq "remount: .tickets-tracker/ exists" "exists" "exists"
    else
        assert_eq "remount: .tickets-tracker/ exists" "exists" "missing"
        return
    fi

    # Assert: tickets branch exists and has the .gitignore from origin
    if git -C "$tmp/clone/.tickets-tracker" show tickets:.gitignore &>/dev/null; then
        assert_eq "remount: .gitignore from remote branch" "committed" "committed"
    else
        assert_eq "remount: .gitignore from remote branch" "committed" "missing"
    fi

}
test_ticket_init_remounts_existing_branch

# ── Test 7: test_ticket_init_sets_gc_auto_zero ────────────────────────────────
echo "Test 7: ticket init sets gc.auto=0 on the tickets worktree"
test_ticket_init_sets_gc_auto_zero() {
    local repo
    repo=$(_make_test_repo)

    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Assert: gc.auto is set to 0
    local gc_auto
    gc_auto="$(git -C "$repo/.tickets-tracker" config gc.auto 2>/dev/null || echo "unset")"
    assert_eq "gc.auto=0: gc.auto is set to 0" "0" "$gc_auto"
}
test_ticket_init_sets_gc_auto_zero

# ── Test 8: test_ticket_init_creates_symlink_in_worktree ──────────────────────
echo "Test 8: ticket init creates .tickets-tracker as a symlink in a git worktree"
test_ticket_init_creates_symlink_in_worktree() {
    local tmp main_repo worktree_dir
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")

    # Set up a main repo with tickets initialized
    main_repo="$tmp/main"
    clone_test_repo "$main_repo"
    (cd "$main_repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Create a real git worktree (not a clone — a worktree; .git is a file)
    worktree_dir="$tmp/worktree"
    git -C "$main_repo" worktree add "$worktree_dir" -b wt-branch 2>/dev/null

    # Run ticket init from inside the git worktree
    (cd "$worktree_dir" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Assert: .tickets-tracker in the worktree is a symlink (not a real dir or worktree)
    if [ -L "$worktree_dir/.tickets-tracker" ]; then
        assert_eq "symlink in worktree: .tickets-tracker is a symlink" "symlink" "symlink"
    else
        assert_eq "symlink in worktree: .tickets-tracker is a symlink" "symlink" "not-a-symlink"
    fi
}
test_ticket_init_creates_symlink_in_worktree

# ── Test 9: test_ticket_init_symlink_points_to_real_dir ───────────────────────
echo "Test 9: symlink target resolves to a valid directory"
test_ticket_init_symlink_points_to_real_dir() {
    local tmp main_repo worktree_dir symlink_target
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")

    # Set up a main repo with tickets initialized
    main_repo="$tmp/main"
    clone_test_repo "$main_repo"
    (cd "$main_repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Create a real git worktree
    worktree_dir="$tmp/worktree"
    git -C "$main_repo" worktree add "$worktree_dir" -b wt-branch2 2>/dev/null

    # Run ticket init from inside the git worktree
    (cd "$worktree_dir" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Assert: symlink target resolves to a valid directory
    if [ -L "$worktree_dir/.tickets-tracker" ]; then
        symlink_target=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$worktree_dir/.tickets-tracker" 2>/dev/null || true)
        if [ -d "$symlink_target" ]; then
            assert_eq "symlink target: resolves to valid directory" "valid-dir" "valid-dir"
        else
            assert_eq "symlink target: resolves to valid directory" "valid-dir" "not-a-dir: $symlink_target"
        fi
    else
        assert_eq "symlink target: .tickets-tracker must be a symlink first" "symlink" "not-a-symlink"
    fi
}
test_ticket_init_symlink_points_to_real_dir

# ── Test 10: test_ticket_init_idempotent_when_symlink_exists ──────────────────
echo "Test 10: ticket init is idempotent when .tickets-tracker is already a symlink"
test_ticket_init_idempotent_when_symlink_exists() {
    local tmp main_repo worktree_dir exit2
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")

    # Set up a main repo with tickets initialized
    main_repo="$tmp/main"
    clone_test_repo "$main_repo"
    (cd "$main_repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Create a real git worktree
    worktree_dir="$tmp/worktree"
    git -C "$main_repo" worktree add "$worktree_dir" -b wt-branch3 2>/dev/null

    # First init — creates the symlink
    (cd "$worktree_dir" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Second init — must exit 0 with symlink still in place
    exit2=0
    (cd "$worktree_dir" && bash "$TICKET_SCRIPT" init 2>/dev/null) || exit2=$?

    assert_eq "idempotent symlink: second init exits 0" "0" "$exit2"
}
test_ticket_init_idempotent_when_symlink_exists

# ── Test 11: test_ticket_init_handles_real_dir_before_symlink ────────────────
echo "Test 11: ticket init replaces real .tickets-tracker/ dir in worktree with symlink"
test_ticket_init_handles_real_dir_before_symlink() {
    local tmp main_repo worktree_dir exit_code
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")

    # Set up a main repo with tickets initialized
    main_repo="$tmp/main"
    clone_test_repo "$main_repo"
    (cd "$main_repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Create a real git worktree
    worktree_dir="$tmp/worktree"
    git -C "$main_repo" worktree add "$worktree_dir" -b wt-branch4 2>/dev/null

    # Simulate a prior auto-init that created a real directory (not a symlink)
    mkdir -p "$worktree_dir/.tickets-tracker"

    # Run ticket init — should replace real dir with symlink, exit 0
    exit_code=0
    (cd "$worktree_dir" && bash "$TICKET_SCRIPT" init 2>/dev/null) || exit_code=$?

    assert_eq "real-dir-replaced: init exits 0" "0" "$exit_code"

    # Assert: .tickets-tracker is now a symlink
    if [ -L "$worktree_dir/.tickets-tracker" ]; then
        assert_eq "real-dir-replaced: .tickets-tracker is a symlink after init" "symlink" "symlink"
    else
        assert_eq "real-dir-replaced: .tickets-tracker is a symlink after init" "symlink" "not-a-symlink"
    fi
}
test_ticket_init_handles_real_dir_before_symlink

# ── Test 12: test_auto_detect_main_worktree_via_git_list ─────────────────────
echo "Test 12: git worktree list --porcelain is parsed to find the main repo for symlink target"
test_auto_detect_main_worktree_via_git_list() {
    local tmp main_repo worktree_dir symlink_target
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")

    # Set up a main repo with tickets initialized
    main_repo="$tmp/main"
    clone_test_repo "$main_repo"
    (cd "$main_repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Create a real git worktree
    worktree_dir="$tmp/worktree"
    git -C "$main_repo" worktree add "$worktree_dir" -b wt-branch5 2>/dev/null

    # Run ticket init from inside the git worktree
    (cd "$worktree_dir" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Assert: .tickets-tracker in the worktree is a symlink pointing to main repo's .tickets-tracker
    if [ -L "$worktree_dir/.tickets-tracker" ]; then
        symlink_target=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$worktree_dir/.tickets-tracker" 2>/dev/null || true)
        local expected_target
        expected_target=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$main_repo/.tickets-tracker" 2>/dev/null || true)
        if [ "$symlink_target" = "$expected_target" ]; then
            assert_eq "symlink-target: points to main repo .tickets-tracker" "correct" "correct"
        else
            assert_eq "symlink-target: points to main repo .tickets-tracker" "$expected_target" "$symlink_target"
        fi
    else
        assert_eq "symlink-target: .tickets-tracker must be a symlink first" "symlink" "not-a-symlink"
    fi
}
test_auto_detect_main_worktree_via_git_list

# ── Test 13: test_ticket_init_generates_env_id_when_symlink_exists_but_env_id_missing
echo "Test 13: ticket init generates .env-id when symlink is correct but .env-id is missing"
test_ticket_init_generates_env_id_when_symlink_exists_but_env_id_missing() {
    local tmp main_repo worktree_dir
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")

    # Set up a main repo with tickets initialized
    main_repo="$tmp/main"
    clone_test_repo "$main_repo"
    (cd "$main_repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Create a real git worktree
    worktree_dir="$tmp/worktree"
    git -C "$main_repo" worktree add "$worktree_dir" -b wt-branch-envid 2>/dev/null

    # First init — creates symlink and .env-id
    (cd "$worktree_dir" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Delete .env-id from the tracker (simulates fresh clone or cleanup)
    local real_tracker
    real_tracker=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$worktree_dir/.tickets-tracker" 2>/dev/null)
    rm -f "$real_tracker/.env-id"

    # Verify .env-id is actually missing
    if [ -f "$real_tracker/.env-id" ]; then
        assert_eq "pre-condition: .env-id deleted" "missing" "still-exists"
        return
    fi

    # Second init — should regenerate .env-id even though symlink is correct
    local exit_code=0
    (cd "$worktree_dir" && bash "$TICKET_SCRIPT" init 2>/dev/null) || exit_code=$?

    assert_eq "env-id regeneration: init exits 0" "0" "$exit_code"

    # Assert: .env-id now exists
    if [ -f "$real_tracker/.env-id" ]; then
        assert_eq "env-id regeneration: .env-id exists after re-init" "exists" "exists"
    else
        assert_eq "env-id regeneration: .env-id exists after re-init" "exists" "missing"
        return
    fi

    # Assert: content is valid UUID4
    local env_id
    env_id=$(cat "$real_tracker/.env-id")
    if [[ "$env_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]; then
        assert_eq "env-id regeneration: content is valid UUID4" "valid" "valid"
    else
        assert_eq "env-id regeneration: content is valid UUID4" "valid" "invalid: $env_id"
    fi
}
test_ticket_init_generates_env_id_when_symlink_exists_but_env_id_missing

# ── Test 14: test_ticket_init_generates_env_id_on_main_repo_idempotent_path
echo "Test 14: ticket init generates .env-id on main repo idempotent re-run"
test_ticket_init_generates_env_id_on_main_repo_idempotent_path() {
    local repo
    repo=$(_make_test_repo)

    # First init
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # Delete .env-id
    rm -f "$repo/.tickets-tracker/.env-id"

    # Second init — should regenerate .env-id
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || exit_code=$?

    assert_eq "main-repo env-id regen: init exits 0" "0" "$exit_code"

    if [ -f "$repo/.tickets-tracker/.env-id" ]; then
        assert_eq "main-repo env-id regen: .env-id exists" "exists" "exists"
    else
        assert_eq "main-repo env-id regen: .env-id exists" "exists" "missing"
    fi
}
test_ticket_init_generates_env_id_on_main_repo_idempotent_path

# ── Test 15: test_ticket_init_emits_error_and_exits_when_worktree_add_fails ──
# Given: tickets branch exists locally AND is already checked out in another worktree
# When:  ticket init runs (git worktree add will fail with "already checked out")
# Then:  stderr contains an ERROR message with the git error text, and init exits non-zero
#        so callers get a clear root-cause message instead of a cryptic downstream error
echo "Test 15: ticket init emits ERROR to stderr and exits non-zero when git worktree add fails"
test_ticket_init_emits_error_and_exits_when_worktree_add_fails() {
    local repo
    repo=$(_make_test_repo)

    # Create the tickets orphan branch in the repo
    git -C "$repo" checkout --orphan tickets 2>/dev/null
    git -C "$repo" rm -rf . --quiet 2>/dev/null || true
    git -C "$repo" commit --allow-empty -q --no-verify -m "init tickets" 2>/dev/null
    git -C "$repo" checkout main 2>/dev/null || git -C "$repo" checkout - 2>/dev/null

    # Check out tickets branch in a second worktree (forces worktree add to fail)
    local second_wt
    second_wt=$(mktemp -d)
    _CLEANUP_DIRS+=("$second_wt")
    git -C "$repo" worktree add "$second_wt" tickets 2>/dev/null

    # Now try to init — should exit non-zero with an actionable ERROR on stderr
    local stderr_out=""
    local exit_code=0
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" init 2>&1 >/dev/null) || exit_code=$?

    assert_eq "worktree-add-fail: init exits non-zero" "non-zero" "$([[ $exit_code -ne 0 ]] && echo non-zero || echo zero)"

    if echo "$stderr_out" | grep -qi "error\|ERROR"; then
        assert_eq "worktree-add-fail: stderr ERROR emitted" "yes" "yes"
    else
        assert_eq "worktree-add-fail: stderr ERROR emitted" "yes" "no (stderr was: $stderr_out)"
    fi
}
test_ticket_init_emits_error_and_exits_when_worktree_add_fails

# ── Test 16: test_ticket_init_succeeds_on_blank_repo_zero_commits ─────────────
# Given: freshly git-initialized repo with zero commits (no HEAD)
# When:  ticket init runs
# Then:  .tickets-tracker/ is created and init exits 0 (git worktree add --orphan
#        handles the no-HEAD case; git < 2.40 gets a clear error message instead)
echo "Test 16: ticket init succeeds on blank repo (zero commits, no HEAD)"
test_ticket_init_succeeds_on_blank_repo_zero_commits() {
    local tmp repo
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    repo="$tmp/blank"
    git init "$repo" --quiet
    git -C "$repo" config user.email "test@test.com"
    git -C "$repo" config user.name "Test"

    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || exit_code=$?

    assert_eq "blank-repo init: exits 0" "0" "$exit_code"

    if [ -d "$repo/.tickets-tracker" ]; then
        assert_eq "blank-repo init: .tickets-tracker/ exists" "exists" "exists"
    else
        assert_eq "blank-repo init: .tickets-tracker/ exists" "exists" "missing"
    fi
}
test_ticket_init_succeeds_on_blank_repo_zero_commits

print_summary
