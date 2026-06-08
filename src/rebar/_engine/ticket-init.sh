#!/usr/bin/env bash
# ticket-init.sh
# Initialize the event-sourced ticket system:
#   - Creates an orphan 'tickets' branch (or fetches existing one)
#   - Mounts it as a worktree at .tickets-tracker/
#   - Commits .gitignore (excluding .env-id and .state-cache)
#   - Generates a UUID4 env-id
#   - Sets gc.auto=0 on the tickets worktree
#   - Adds .tickets-tracker to .git/info/exclude
set -euo pipefail

# ── Parse flags ───────────────────────────────────────────────────────────────
# Unset git hook env vars before any git commands so REPO_ROOT resolves from CWD.
# When run as a subprocess from a pre-commit hook, GIT_DIR is inherited and would
# cause git rev-parse --show-toplevel (and all subsequent git -C commands) to
# operate on the hook's repo instead of the intended target repo.
# PROJECT_ROOT is also unset: it is exported by the rebar CLI to the host project
# root, but ticket-init.sh must always initialize the repo at CWD (the target repo
# the CLI was invoked from), not the shim's project root.
unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR PROJECT_ROOT 2>/dev/null || true

_silent=false
for _arg in "$@"; do
    if [[ "$_arg" == "--silent" ]]; then
        _silent=true
    fi
done

REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel)}"
TRACKER_DIR="$REPO_ROOT/.tickets-tracker"

# ── Ensure .env-id exists ────────────────────────────────────────────────────
# .env-id is gitignored and must be generated locally on each environment.
# Called before every early exit to guarantee the postcondition that .env-id
# exists whenever the tracker directory is a valid worktree.
_ensure_env_id() {
    local _tracker="$1"
    # Resolve symlinks so we write to the real directory
    local _real_tracker
    _real_tracker=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$_tracker" 2>/dev/null) || _real_tracker="$_tracker"
    if [ -d "$_real_tracker" ] && [ ! -f "$_real_tracker/.env-id" ]; then
        python3 -c "import uuid; print(uuid.uuid4())" > "$_real_tracker/.env-id"
    fi
}

# ── Idempotency guard ────────────────────────────────────────────────────────
# If .tickets-tracker/ already exists and is a valid worktree, exit 0.
if [ -d "$TRACKER_DIR" ] && [ -f "$TRACKER_DIR/.git" ]; then
    # Verify it's actually a valid worktree
    if git -C "$TRACKER_DIR" rev-parse --is-inside-work-tree &>/dev/null; then
        # Detect and recover stale rebase/merge state on the tickets branch.
        # Modern git (>= 2.6) uses rebase-merge/ directory for interactive and
        # merge-backend rebases, rebase-apply/ for am-style rebases, and
        # MERGE_HEAD file during a paused merge. The legacy REBASE_HEAD file
        # at gitdir root is NOT created for the merge-backend rebases that the
        # _push_tickets_branch flow uses, so checking only REBASE_HEAD misses
        # the actual failure mode and leaves picks stranded as dangling
        # commits (bug 637b-63fe-9d44-4aab).
        #
        # Recovery strategy: first attempt `git rebase --continue` (with a
        # short timeout) to drain pending picks. Only abort if --continue
        # fails — abort discards the pending picks and forces re-recovery
        # via cherry-pick of dangling commits (see ticket-fsck-recover.sh).
        _tickets_git_dir=$(git -C "$TRACKER_DIR" rev-parse --git-dir 2>/dev/null)
        _stale_rebase_kind=""
        if [ -n "$_tickets_git_dir" ]; then
            if [ -d "$_tickets_git_dir/rebase-merge" ]; then
                _stale_rebase_kind="rebase-merge"
            elif [ -d "$_tickets_git_dir/rebase-apply" ]; then
                _stale_rebase_kind="rebase-apply"
            elif [ -f "$_tickets_git_dir/REBASE_HEAD" ]; then
                _stale_rebase_kind="REBASE_HEAD"
            elif [ -f "$_tickets_git_dir/MERGE_HEAD" ]; then
                _stale_rebase_kind="MERGE_HEAD"
            fi
        fi
        if [ -n "$_stale_rebase_kind" ]; then
            if [[ "$_silent" == false ]]; then
                echo "WARNING: Stale ${_stale_rebase_kind} state on tickets branch; attempting recovery" >&2
            fi
            case "$_stale_rebase_kind" in
                rebase-merge|rebase-apply|REBASE_HEAD)
                    # Try --continue first with a 10s timeout; abort on timeout/failure
                    _continue_rc=0
                    if command -v timeout >/dev/null 2>&1; then
                        timeout 10 git -C "$TRACKER_DIR" -c rebase.autostash=true rebase --continue >/dev/null 2>&1 || _continue_rc=$?
                    else
                        git -C "$TRACKER_DIR" -c rebase.autostash=true rebase --continue >/dev/null 2>&1 || _continue_rc=$?
                    fi
                    if [ "$_continue_rc" -ne 0 ]; then
                        if [[ "$_silent" == false ]]; then
                            echo "WARNING: rebase --continue failed (exit=$_continue_rc); aborting rebase. Run 'bash ${SCRIPT_DIR:-.}/ticket-fsck-recover.sh' to cherry-pick stranded commits." >&2
                        fi
                        git -C "$TRACKER_DIR" rebase --abort 2>/dev/null || true
                    fi
                    ;;
                MERGE_HEAD)
                    if [[ "$_silent" == false ]]; then
                        echo "WARNING: Aborting stale merge on tickets branch" >&2
                    fi
                    git -C "$TRACKER_DIR" merge --abort 2>/dev/null || true
                    ;;
            esac
        fi
        _ensure_env_id "$TRACKER_DIR"
        if [[ "$_silent" == false ]]; then
            echo "Ticket system already initialized." >&2
        fi
        exit 0
    fi
fi

# ── Git worktree symlink setup ────────────────────────────────────────────────
# When running inside a git worktree (not the main repo), .git is a file.
# In this case, .tickets-tracker should be a symlink to the main repo's copy.
if [ -f "$REPO_ROOT/.git" ]; then
    # Parse git worktree list --porcelain to find the main worktree path.
    # The first 'worktree' entry (without 'bare') is the main worktree.
    _main_worktree=""
    _first_worktree=""
    while IFS= read -r _line; do
        if [[ "$_line" == worktree\ * ]]; then
            _wt_path="${_line#worktree }"
            if [ -z "$_first_worktree" ]; then
                _first_worktree="$_wt_path"
            fi
        fi
    done < <(git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null)
    _main_worktree="$_first_worktree"

    if [ -z "$_main_worktree" ]; then
        echo "Error: could not detect main worktree path via git worktree list" >&2
        exit 1
    fi

    _main_tracker="$_main_worktree/.tickets-tracker"

    # Check if main worktree has initialized .tickets-tracker
    if [ ! -d "$_main_tracker" ]; then
        echo "Error: Run ticket init from the main repo first, then re-run from the worktree." >&2
        exit 1
    fi

    # Idempotency: symlink already exists and points to the correct target
    if [ -L "$TRACKER_DIR" ]; then
        _current_target="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$TRACKER_DIR" 2>/dev/null || true)"
        _expected_target="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$_main_tracker" 2>/dev/null || true)"
        if [ "$_current_target" = "$_expected_target" ]; then
            _ensure_env_id "$TRACKER_DIR"
            if [[ "$_silent" == false ]]; then
                echo "Ticket system already initialized." >&2
            fi
            exit 0
        fi
        # Symlink points to wrong target — remove and re-create
        rm -f "$TRACKER_DIR"
    fi

    # If a real (non-symlink) directory exists here, remove it if it's not a real git worktree.
    # A real git worktree has a .git file inside it; a plain directory does not.
    if [ -d "$TRACKER_DIR" ] && [ ! -L "$TRACKER_DIR" ]; then
        if [ -f "$TRACKER_DIR/.git" ]; then
            # It has a .git file — it's a real git worktree; do not remove automatically
            echo "Error: .tickets-tracker/ is a real git worktree in this worktree checkout. Remove it manually first." >&2
            exit 1
        else
            # Empty/plain directory (transient state) — safe to remove
            rm -rf "$TRACKER_DIR"
        fi
    fi

    # Create the symlink
    ln -s "$_main_tracker" "$TRACKER_DIR"

    # Add .tickets-tracker to this worktree's .git/info/exclude
    _wt_git_file="$REPO_ROOT/.git"
    _wt_git_dir="$(sed -n 's/^gitdir: //p' "$_wt_git_file")"
    # If _wt_git_dir is relative, resolve it relative to REPO_ROOT
    if [[ "$_wt_git_dir" != /* ]]; then
        _wt_git_dir="$REPO_ROOT/$_wt_git_dir"
    fi
    _wt_exclude_file="$_wt_git_dir/info/exclude"
    mkdir -p "$(dirname "$_wt_exclude_file")"
    if [ ! -f "$_wt_exclude_file" ]; then
        echo ".tickets-tracker" > "$_wt_exclude_file"
    elif ! grep -q '\.tickets-tracker' "$_wt_exclude_file"; then
        echo ".tickets-tracker" >> "$_wt_exclude_file"
    fi

    _ensure_env_id "$TRACKER_DIR"
    if [[ "$_silent" == false ]]; then
        echo "Ticket system initialized (symlink to main repo)." >&2
    fi
    exit 0
fi

# ── Clean up partial-stale worktree directory ─────────────────────────────────
# If .tickets-tracker/ exists but is not a valid worktree (e.g., partial crash),
# prune stale worktree entries and remove the directory so we can re-create it.
if [ -d "$TRACKER_DIR" ] && ! git -C "$TRACKER_DIR" rev-parse --is-inside-work-tree &>/dev/null 2>&1; then
    git -C "$REPO_ROOT" worktree prune 2>/dev/null || true
    rm -rf "$TRACKER_DIR"
fi

# ── Add .tickets-tracker and .scratch/ to .git/info/exclude ──────────────────
_git_dir="$REPO_ROOT/.git"
# In a worktree, .git is a file pointing to the real git dir
if [ -f "$_git_dir" ]; then
    _git_dir="$(sed -n 's/^gitdir: //p' "$_git_dir")"
fi
_exclude_file="$_git_dir/info/exclude"
mkdir -p "$(dirname "$_exclude_file")"
if [ ! -f "$_exclude_file" ]; then
    echo ".tickets-tracker" > "$_exclude_file"
elif ! grep -q '\.tickets-tracker' "$_exclude_file"; then
    echo ".tickets-tracker" >> "$_exclude_file"
fi
# Add .scratch/ exclusion to the main repo exclude (idempotent)
if ! grep -qFx '.scratch/' "$_exclude_file" 2>/dev/null; then
    echo ".scratch/" >> "$_exclude_file"
fi

# ── Acquire exclusive lock (30s timeout) ──────────────────────────────────────
_lock_base="$REPO_ROOT/.git"
# Resolve real git dir if in a worktree
if [ -f "$_lock_base" ]; then
    _lock_base="$(sed -n 's/^gitdir: //p' "$_lock_base")"
    # Navigate up from worktree gitdir to the common git dir
    _lock_base="$(cd "$_lock_base" && cd "$(git rev-parse --git-common-dir)" && pwd)"
fi
_lock_dir="$_lock_base/ticket-init.lock"

# Portable mkdir-based lock (atomic on all platforms, works on macOS + Linux)
_lock_acquired=false
_lock_deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$_lock_deadline" ]; do
    if mkdir "$_lock_dir" 2>/dev/null; then
        _lock_acquired=true
        # Remove lock on exit (normal or error)
        # shellcheck disable=SC2154
        trap 'code=$?; rmdir "$_lock_dir" 2>/dev/null; exit $code' EXIT INT TERM
        break
    fi
    sleep 1
done
if [ "$_lock_acquired" = false ]; then
    echo "Error: could not acquire ticket-init lock within 30s" >&2
    exit 1
fi

# ── Create or mount the tickets branch ────────────────────────────────────────
_branch_exists_local=false
_branch_exists_remote=false

if git -C "$REPO_ROOT" rev-parse --verify tickets &>/dev/null; then
    _branch_exists_local=true
fi

if git -C "$REPO_ROOT" rev-parse --verify origin/tickets &>/dev/null; then
    _branch_exists_remote=true
fi

if [ "$_branch_exists_local" = true ]; then
    # Branch exists locally — just mount the worktree
    _wt_err_tmp=$(mktemp /tmp/dso-init-wt.XXXXXX)
    git -C "$REPO_ROOT" worktree add "$TRACKER_DIR" tickets >/dev/null 2>"$_wt_err_tmp" || {
        echo "ERROR: git worktree add (local branch) failed: $(cat "$_wt_err_tmp")" >&2
        rm -f "$_wt_err_tmp"; exit 1
    }
    rm -f "$_wt_err_tmp"
elif [ "$_branch_exists_remote" = true ]; then
    # Branch exists on remote — fetch and mount
    git -C "$REPO_ROOT" fetch origin tickets 2>/dev/null
    _wt_err_tmp=$(mktemp /tmp/dso-init-wt.XXXXXX)
    git -C "$REPO_ROOT" worktree add "$TRACKER_DIR" tickets >/dev/null 2>"$_wt_err_tmp" || {
        echo "ERROR: git worktree add (remote branch) failed: $(cat "$_wt_err_tmp")" >&2
        rm -f "$_wt_err_tmp"; exit 1
    }
    rm -f "$_wt_err_tmp"
else
    # No branch anywhere — create orphan branch.
    # Use git worktree add --orphan when available (git >= 2.40): works even
    # on blank repos with no HEAD. Fall back to --detach + checkout --orphan
    # for older git (requires HEAD; fails on zero-commit repos).
    _wt_err_tmp=$(mktemp /tmp/dso-init-wt.XXXXXX)
    _git_minor=$(git --version 2>/dev/null | grep -o '[0-9]\+\.[0-9]\+' | head -1 | cut -d. -f2 || echo "0")
    _git_major=$(git --version 2>/dev/null | grep -o '[0-9]\+\.[0-9]\+' | head -1 | cut -d. -f1 || echo "2")
    if (( _git_major > 2 || (_git_major == 2 && _git_minor >= 40) )); then
        git -C "$REPO_ROOT" worktree add --orphan -b tickets "$TRACKER_DIR" >/dev/null 2>"$_wt_err_tmp" || {
            echo "ERROR: git worktree add --orphan failed: $(cat "$_wt_err_tmp")" >&2
            rm -f "$_wt_err_tmp"; exit 1
        }
    else
        git -C "$REPO_ROOT" worktree add --detach "$TRACKER_DIR" >/dev/null 2>"$_wt_err_tmp" || {
            echo "ERROR: git worktree add (detach) failed: $(cat "$_wt_err_tmp")" >&2
            echo "NOTE: git < 2.40 detected. On a blank repo (zero commits), create an initial commit first: git commit --allow-empty -m 'init'" >&2
            rm -f "$_wt_err_tmp"; exit 1
        }
        git -C "$TRACKER_DIR" checkout --orphan tickets 2>/dev/null
        git -C "$TRACKER_DIR" rm -rf . --quiet 2>/dev/null || true
    fi
    rm -f "$_wt_err_tmp"

    # Set user config with fallback to defaults
    _user_email="$(git -C "$REPO_ROOT" config user.email 2>/dev/null || echo "ticket-system@localhost")"
    _user_name="$(git -C "$REPO_ROOT" config user.name 2>/dev/null || echo "Ticket System")"
    git -C "$TRACKER_DIR" config user.email "$_user_email"
    git -C "$TRACKER_DIR" config user.name "$_user_name"
    git -C "$TRACKER_DIR" config commit.gpgsign false
    git -C "$TRACKER_DIR" config tag.gpgsign false

    git -C "$TRACKER_DIR" commit --allow-empty -q --no-verify -m "chore: initialize ticket tracker"
fi

# ── Ensure user config is set (for remount case) ─────────────────────────────
if ! git -C "$TRACKER_DIR" config user.email &>/dev/null; then
    _user_email="$(git -C "$REPO_ROOT" config user.email 2>/dev/null || echo "ticket-system@localhost")"
    _user_name="$(git -C "$REPO_ROOT" config user.name 2>/dev/null || echo "Ticket System")"
    git -C "$TRACKER_DIR" config user.email "$_user_email"
    git -C "$TRACKER_DIR" config user.name "$_user_name"
fi

# ── Commit .gitignore on the tickets branch ───────────────────────────────────
# Only if .gitignore doesn't already exist on the branch
if ! git -C "$TRACKER_DIR" show tickets:.gitignore &>/dev/null 2>&1; then
    cat > "$TRACKER_DIR/.gitignore" <<'GITIGNORE'
.env-id
.closure-key
.state-cache
.scratch/
GITIGNORE
    git -C "$TRACKER_DIR" add .gitignore
    git -C "$TRACKER_DIR" commit -q --no-verify -m "chore: add .gitignore for env-id, state-cache, and scratch"
else
    # Pre-upgrade path: .gitignore exists but may be missing entries.
    _gitignore_content=$(git -C "$TRACKER_DIR" show tickets:.gitignore 2>/dev/null || echo "")
    _gitignore_updated=false
    if ! echo "$_gitignore_content" | grep -qFx '.scratch/' 2>/dev/null; then
        git -C "$TRACKER_DIR" show tickets:.gitignore > "$TRACKER_DIR/.gitignore"
        echo ".scratch/" >> "$TRACKER_DIR/.gitignore"
        _gitignore_updated=true
    fi
    if ! echo "$_gitignore_content" | grep -qFx '.closure-key' 2>/dev/null; then
        if [ "$_gitignore_updated" = false ]; then
            git -C "$TRACKER_DIR" show tickets:.gitignore > "$TRACKER_DIR/.gitignore"
        fi
        echo ".closure-key" >> "$TRACKER_DIR/.gitignore"
        _gitignore_updated=true
    fi
    if [ "$_gitignore_updated" = true ]; then
        git -C "$TRACKER_DIR" add .gitignore
        git -C "$TRACKER_DIR" commit -q --no-verify -m "chore: update .gitignore (closure-key, scratch)"
    fi
fi

# ── Add .scratch/ to tickets-tracker worktree's .git/info/exclude ────────────
# The tickets-tracker is a linked worktree: its .git is a file containing
# "gitdir: <path>" pointing to .git/worktrees/<name>/. We write to that
# worktree-specific exclude file so .scratch/ is isolated at the worktree
# level and git check-ignore confirms it.
#
# Note: `git rev-parse --git-path info/exclude` returns the shared
# .git/info/exclude for linked worktrees, not the worktree-specific path.
# We resolve the worktree-specific path via the .git file instead, using
# the same technique git rev-parse --git-path info/exclude would use
# internally if it returned the worktree gitdir path.
_tracker_git_file="$TRACKER_DIR/.git"
_tracker_git_dir=""
if [ -f "$_tracker_git_file" ]; then
    # Read the worktree-specific git dir from the .git pointer file
    _tracker_git_dir=$(sed -n 's/^gitdir: //p' "$_tracker_git_file")
    # Resolve relative path (git may emit a relative path)
    if [ -n "$_tracker_git_dir" ] && [[ "$_tracker_git_dir" != /* ]]; then
        _tracker_git_dir="$TRACKER_DIR/$_tracker_git_dir"
    fi
fi
if [ -z "$_tracker_git_dir" ]; then
    # Fallback: non-worktree case — use git rev-parse --git-path info/exclude
    _tracker_git_dir=$(git -C "$TRACKER_DIR" rev-parse --git-path info/exclude 2>/dev/null | xargs dirname 2>/dev/null || echo "$TRACKER_DIR/.git/info")
    _tracker_git_dir=$(dirname "$_tracker_git_dir")
fi
_tracker_exclude_file="$_tracker_git_dir/info/exclude"
mkdir -p "$(dirname "$_tracker_exclude_file")"
if ! grep -qFx '.scratch/' "$_tracker_exclude_file" 2>/dev/null; then
    echo ".scratch/" >> "$_tracker_exclude_file"
fi

# ── Commit no-op .pre-commit-config.yaml on the tickets branch ───────────────
# The pre-commit framework, when installed as a pre-push hook in the host
# repo's .git/hooks/pre-push, runs on every push from this linked worktree.
# Without a .pre-commit-config.yaml the framework exits non-zero and rejects
# the push. A stub `repos: []` config is a valid no-op: pre-commit accepts
# it and runs no hooks, so pushes succeed without requiring callers to set
# PRE_COMMIT_ALLOW_NO_CONFIG=1. Bug 27d8-b230.
if ! git -C "$TRACKER_DIR" show tickets:.pre-commit-config.yaml &>/dev/null 2>&1; then
    cat > "$TRACKER_DIR/.pre-commit-config.yaml" <<'PRECOMMIT'
# No-op pre-commit config for the tickets orphan branch.
# The tickets branch carries event-sourced ticket data only — no source
# code to lint — so no hooks are needed. This empty config exists solely
# so the pre-commit framework (when installed as a pre-push hook in the
# host repo) accepts pushes from the .tickets-tracker linked worktree
# without requiring PRE_COMMIT_ALLOW_NO_CONFIG=1 on every caller.
repos: []
PRECOMMIT
    git -C "$TRACKER_DIR" add .pre-commit-config.yaml
    git -C "$TRACKER_DIR" commit -q --no-verify -m "chore: add no-op .pre-commit-config.yaml (bug 27d8-b230)"
fi

# ── Generate env-id ───────────────────────────────────────────────────────────
if [ ! -f "$TRACKER_DIR/.env-id" ]; then
    python3 -c "import uuid; print(uuid.uuid4())" > "$TRACKER_DIR/.env-id"
fi

# ── Generate closure-key (verdict hash gate) ─────────────────────────────────
# Used by compute-verdict-hash.sh and ticket-transition.sh to produce/verify
# HMAC-based verdict hashes for story/epic closure. Gitignored and local.
if [ ! -f "$TRACKER_DIR/.closure-key" ]; then
    python3 -c "import uuid; print(uuid.uuid4())" > "$TRACKER_DIR/.closure-key"
fi

# ── Set gc.auto=0 on the tickets worktree ─────────────────────────────────────
git -C "$TRACKER_DIR" config gc.auto 0

if [[ "$_silent" == false ]]; then
    echo "Ticket system initialized." >&2
fi
