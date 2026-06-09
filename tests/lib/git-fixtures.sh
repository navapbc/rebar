#!/usr/bin/env bash
# tests/lib/git-fixtures.sh
# Shared git repo template for test files.
#
# Creates a template git repo once per process, then cp -r's it for each test
# that needs a fresh repo. ~10x faster than git init + add + commit per test.
#
# Usage:
#   source "$PLUGIN_ROOT/tests/lib/git-fixtures.sh"
#   clone_test_repo "$dest_path"
#
# Provides:
#   clone_test_repo <dest>  — fast-copy a pre-built template repo to <dest>
#
# The template contains:
#   - git init with branch "main"
#   - user.email "test@test.com", user.name "Test"
#   - A README.md with content "initial"
#   - One commit: "init"
#
# Template lifecycle:
#   - Created lazily on first clone_test_repo call
#   - Stored in _GIT_FIXTURE_TEMPLATE_DIR (exported so callers can inspect)
#   - Callers are responsible for their own dest cleanup

# Skip ticket dispatcher's remote sync in test repos (no remote exists).
# The env var is only checked by src/rebar/_engine/ticket _ensure_initialized;
# harmless for non-ticket tests.
export _TICKET_TEST_NO_SYNC=1

# Unset PROJECT_ROOT exported by the rebar CLI (bb42-1291).
# Tests that source this library create temp-repo fixtures and expect ticket
# scripts/libraries to resolve REPO_ROOT from CWD (the fixture path). With
# PROJECT_ROOT leaked from the shim, ticket-lib.sh / ticket-create.sh / etc.
# resolve to the host repo instead, writing events to the wrong tracker and
# causing cross-tracker state pollution and false-positive test failures.
# Applies to every test that sources this file — no per-test opt-in needed.
unset PROJECT_ROOT 2>/dev/null || true

# Temp dir cleanup on exit (guarded for sourced usage — avoid clobbering caller state)
if [[ -z "${_CLEANUP_DIRS+set}" ]]; then
    _CLEANUP_DIRS=()
    # Tolerate per-dir rm failures (bug 3867-b4d4). The last `rm -rf` exit code
    # would otherwise become the function's return value and leak into the
    # script's exit code via the EXIT trap. macOS $TMPDIR contains restricted
    # Apple system dirs (com.apple.*) and tests may set 000 permissions on
    # fixture subdirs during assertions — both produce spurious nonzero exits
    # even when every test assertion passed.
    _cleanup() { for d in "${_CLEANUP_DIRS[@]}"; do rm -rf "$d" 2>/dev/null || true; done; return 0; }
    trap _cleanup EXIT
fi

# Global: path to the cached template repo (empty = not yet created)
# Unconditional reset — prevents inherited env from batch runner restarts (e26c-fce4)
_GIT_FIXTURE_TEMPLATE_DIR=""

_ensure_git_fixture_template() {
    if [ -n "$_GIT_FIXTURE_TEMPLATE_DIR" ] && [ -d "$_GIT_FIXTURE_TEMPLATE_DIR/.git" ]; then
        return
    fi
    _GIT_FIXTURE_TEMPLATE_DIR=$(mktemp -d)
    _CLEANUP_DIRS+=("$_GIT_FIXTURE_TEMPLATE_DIR")
    git init -q -b main "$_GIT_FIXTURE_TEMPLATE_DIR"
    git -C "$_GIT_FIXTURE_TEMPLATE_DIR" config user.email "test@test.com"
    git -C "$_GIT_FIXTURE_TEMPLATE_DIR" config user.name "Test"
    git -C "$_GIT_FIXTURE_TEMPLATE_DIR" config commit.gpgsign false
    git -C "$_GIT_FIXTURE_TEMPLATE_DIR" config tag.gpgsign false
    git -C "$_GIT_FIXTURE_TEMPLATE_DIR" config gpg.format openpgp
    echo "initial" > "$_GIT_FIXTURE_TEMPLATE_DIR/README.md"
    git -C "$_GIT_FIXTURE_TEMPLATE_DIR" add -A
    git -C "$_GIT_FIXTURE_TEMPLATE_DIR" commit -q -m "init"
}

# clone_test_repo <dest>
# Fast-copies the template repo to <dest>. <dest> must not already exist.
clone_test_repo() {
    local dest="$1"
    _ensure_git_fixture_template
    cp -r "$_GIT_FIXTURE_TEMPLATE_DIR" "$dest"
}

# ── Ticket-ready template ────────────────────────────────────────────────────
# A second template that includes a pre-initialized ticket system (orphan
# `tickets` branch + `.tickets-tracker` worktree). Avoids running `ticket init`
# per test (~0.21s each × N tests). On clone, the worktree's absolute-path
# cross-references are rewritten to point at the new destination.

# Resolve REPO_ROOT for ticket init (tests may source this before cd'ing).
# GIT_DISCOVERY_ACROSS_FILESYSTEM=1 handles Docker volume mount boundaries.
# GITHUB_WORKSPACE fallback handles alpine CI tarball checkouts (no .git present).
_GIT_FIXTURE_REPO_ROOT="${_GIT_FIXTURE_REPO_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
_GIT_FIXTURE_REPO_ROOT="${_GIT_FIXTURE_REPO_ROOT:-${GITHUB_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}}"

_GIT_FIXTURE_TICKET_TEMPLATE_DIR=""

_ensure_ticket_fixture_template() {
    if [ -n "$_GIT_FIXTURE_TICKET_TEMPLATE_DIR" ] && [ -d "$_GIT_FIXTURE_TICKET_TEMPLATE_DIR/repo/.git" ]; then
        return
    fi
    # Start from the base git template
    _ensure_git_fixture_template
    _GIT_FIXTURE_TICKET_TEMPLATE_DIR=$(mktemp -d)
    _CLEANUP_DIRS+=("$_GIT_FIXTURE_TICKET_TEMPLATE_DIR")
    cp -r "$_GIT_FIXTURE_TEMPLATE_DIR" "$_GIT_FIXTURE_TICKET_TEMPLATE_DIR/repo"

    # Run the real ticket init only — no pre-created tickets.
    # Eliminates the per-test `ticket init` overhead (~0.21s × N tests).
    # Template size is kept small to avoid cp -r overhead dominating savings.
    local _ticket_script="${REBAR_ENGINE:-${_GIT_FIXTURE_REPO_ROOT}/src/rebar/_engine}/ticket"
    if [ -f "$_ticket_script" ]; then
        _ticket_init_err=$(mktemp)
        if ! (cd "$_GIT_FIXTURE_TICKET_TEMPLATE_DIR/repo" && \
            _TICKET_TEST_NO_SYNC=1 bash "$_ticket_script" init >/dev/null 2>"$_ticket_init_err"); then
            echo "WARNING: ticket init failed in fixture setup:" >&2
            cat "$_ticket_init_err" >&2
        fi
        rm -f "$_ticket_init_err"
    fi
}

# clone_ticket_repo <dest>
# Fast-copies the ticket-ready template to <dest> and rewrites worktree
# absolute paths. <dest> must not already exist.
clone_ticket_repo() {
    local dest="$1"
    _ensure_ticket_fixture_template
    cp -r "$_GIT_FIXTURE_TICKET_TEMPLATE_DIR/repo" "$dest"

    # Rewrite worktree cross-references (absolute paths baked into template)
    # Detect actual worktree metadata dir name: git sanitizes leading '.' to '-'
    # but behaviour differs across git versions. Use glob so shellcheck is happy.
    local wt_name="-tickets-tracker"
    local _wt_dir
    for _wt_dir in "$dest/.git/worktrees"/*/; do
        [ -d "$_wt_dir" ] && wt_name=$(basename "$_wt_dir") && break
    done
    local wt_gitdir="$dest/.git/worktrees/$wt_name/gitdir"
    local tracker_gitfile="$dest/.tickets-tracker/.git"  # tickets-boundary-ok

    if [ -f "$wt_gitdir" ]; then
        echo "$dest/.tickets-tracker/.git" > "$wt_gitdir"  # tickets-boundary-ok
    fi
    if [ -f "$tracker_gitfile" ]; then
        echo "gitdir: $dest/.git/worktrees/$wt_name" > "$tracker_gitfile"
    fi

    # Verify the path rewrite succeeded. On some git versions (e.g. alpine/busybox
    # git 2.43.x) the sanitised worktree dir name or cross-reference format may
    # differ, leaving the worktree broken after a plain cp. When verification
    # fails, tear out the stale state and re-add the worktree from scratch so git
    # bakes in the correct absolute paths for this destination.
    if ! GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git -C "$dest/.tickets-tracker" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        # Preserve .env-id (gitignored — not part of branch content)
        local _env_id=""
        [ -f "$dest/.tickets-tracker/.env-id" ] && _env_id=$(cat "$dest/.tickets-tracker/.env-id")  # tickets-boundary-ok
        rm -rf "$dest/.tickets-tracker"
        rm -rf "$dest/.git/worktrees"
        GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git -C "$dest" worktree add "$dest/.tickets-tracker" tickets >/dev/null 2>&1 || true
        if [ -d "$dest/.tickets-tracker" ]; then
            [ -n "$_env_id" ] && echo "$_env_id" > "$dest/.tickets-tracker/.env-id"  # tickets-boundary-ok
            git -C "$dest/.tickets-tracker" config commit.gpgsign false 2>/dev/null || true
            git -C "$dest/.tickets-tracker" config tag.gpgsign false 2>/dev/null || true
        fi
    fi

    # Pre-set gc.auto=0 in the tickets worktree so write_commit_event skips
    # the redundant per-operation check (~10ms saved per ticket operation).
    if [ -d "$dest/.tickets-tracker" ]; then
        git -C "$dest/.tickets-tracker" config gc.auto 0
        # Signal to _flock_stage_commit that gc.auto is already 0 — avoids a
        # git subprocess on every write op (~10ms × N ops per test suite run).
        # NOTE: intentionally NOT exported — must not bleed into parallel test
        # processes or sibling tests that run ticket ops against a different repo
        # where gc.auto was not pre-set (bug 9d55-e6c3). Sourced libs (ticket-lib.sh)
        # inherit this variable from the current process env without export.
        # Subprocess ticket calls (bash scripts/ticket ...) do not inherit it and
        # will perform the gc.auto check on first write — the ~10ms cost is
        # acceptable in exchange for correct test isolation.
        _REBAR_GC_AUTO_ZERO=1
    fi
}
