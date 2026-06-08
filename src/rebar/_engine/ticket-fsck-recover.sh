#!/usr/bin/env bash
# ticket-fsck-recover.sh
# Destructive recovery for ticket-tracker stale-rebase state (bug 637b-63fe-9d44-4aab).
#
# Detects paused rebase state in the ticket-tracker git worktree using the
# correct modern-git markers (rebase-merge/ directory and rebase-apply/
# directory, in addition to the legacy REBASE_HEAD file), then attempts to
# drain the rebase via `git rebase --continue` with a timeout, falling back to
# `git rebase --abort` and cherry-picking dangling commits that match the
# ticket commit message pattern.
#
# IMPORTANT — this script IS destructive. It modifies git state in the tracker
# directory. The companion script ticket-fsck.sh remains strictly
# non-destructive. Use this script ONLY when ticket-tracker corruption from
# the stale-rebase bug has been confirmed (e.g., `dso ticket show <id>` returns
# "no events" for tickets that were recently created or modified).
#
# Usage:
#   ticket-fsck-recover.sh [--tracker-dir <path>] [--detect-only]
#                          [--recover-dangling] [--timeout <seconds>]
#                          [--help]
#
# Flags:
#   --tracker-dir <path>   Path to the tracker worktree (default: $REPO_ROOT/.tickets-tracker)
#   --detect-only          Report stale rebase state and exit; do not attempt recovery
#   --recover-dangling     Skip the --continue step; only run the dangling-commit
#                          cherry-pick recovery (use this when --continue has already
#                          failed and you want to re-attempt the cherry-pick phase)
#   --timeout <seconds>    Timeout for `git rebase --continue` (default: 30)
#   --help                 Print usage and exit 0
#
# Exit codes:
#   0  no recovery needed OR recovery succeeded
#   1  recovery attempted but failed (rebase still in progress AND no dangling
#      commits recovered)
#   2  fatal error (no tracker dir, invalid args)
#   3  stale rebase detected and --detect-only was passed

set -euo pipefail

# ── Parse args ───────────────────────────────────────────────────────────────
TRACKER_DIR=""
DETECT_ONLY=0
RECOVER_DANGLING=0
CONTINUE_TIMEOUT=30

_usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --tracker-dir)
            TRACKER_DIR="$2"; shift 2 ;;
        --tracker-dir=*)
            TRACKER_DIR="${1#--tracker-dir=}"; shift ;;
        --detect-only)
            DETECT_ONLY=1; shift ;;
        --recover-dangling)
            RECOVER_DANGLING=1; shift ;;
        --timeout)
            CONTINUE_TIMEOUT="$2"; shift 2 ;;
        --timeout=*)
            CONTINUE_TIMEOUT="${1#--timeout=}"; shift ;;
        --help|-h)
            _usage; exit 0 ;;
        *)
            echo "Error: unknown argument '$1'" >&2
            _usage >&2
            exit 2 ;;
    esac
done

# Default tracker dir: $REPO_ROOT/.tickets-tracker
if [ -z "$TRACKER_DIR" ]; then
    REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -z "$REPO_ROOT" ]; then
        echo "Error: not in a git repo and no --tracker-dir specified" >&2
        exit 2
    fi
    TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
fi

if [ ! -d "$TRACKER_DIR" ]; then
    echo "Error: tracker dir '$TRACKER_DIR' does not exist or is not a directory" >&2
    exit 2
fi

# ── Resolve git dir for the tracker worktree ─────────────────────────────────
_resolve_tracker_git_dir() {
    local tracker_git="$TRACKER_DIR/.git"
    if [ -f "$tracker_git" ]; then
        local gitdir
        gitdir=$(sed 's/^gitdir: //' "$tracker_git")
        if [[ "$gitdir" != /* ]]; then
            gitdir="$TRACKER_DIR/$gitdir"
        fi
        echo "$gitdir"
    elif [ -d "$tracker_git" ]; then
        echo "$tracker_git"
    else
        echo ""
    fi
}

TRACKER_GIT_DIR="$(_resolve_tracker_git_dir)"
if [ -z "$TRACKER_GIT_DIR" ]; then
    echo "Error: could not resolve git directory for tracker '$TRACKER_DIR'" >&2
    exit 2
fi

# ── Detect stale rebase state ────────────────────────────────────────────────
# Modern git (>= 2.6) uses rebase-merge/ for interactive/merge rebases and
# rebase-apply/ for am-style rebases. The legacy REBASE_HEAD file at gitdir
# root is created for some operations but NOT for the merge backend used by
# `git rebase -i` and `git rebase --merge`. Check all three markers.
_detect_stale_rebase() {
    local marker_kind=""
    if [ -d "$TRACKER_GIT_DIR/rebase-merge" ]; then
        marker_kind="rebase-merge"
    elif [ -d "$TRACKER_GIT_DIR/rebase-apply" ]; then
        marker_kind="rebase-apply"
    elif [ -f "$TRACKER_GIT_DIR/REBASE_HEAD" ]; then
        marker_kind="REBASE_HEAD"
    elif [ -f "$TRACKER_GIT_DIR/MERGE_HEAD" ]; then
        marker_kind="MERGE_HEAD"
    fi
    echo "$marker_kind"
}

REBASE_KIND="$(_detect_stale_rebase)"

# ── --detect-only path ───────────────────────────────────────────────────────
if [ "$DETECT_ONLY" = "1" ]; then
    if [ -z "$REBASE_KIND" ]; then
        echo "No stale rebase or merge state detected in tracker '$TRACKER_DIR'"
        exit 0
    fi
    echo "Stale rebase detected: marker_kind=$REBASE_KIND tracker='$TRACKER_DIR' gitdir='$TRACKER_GIT_DIR'"
    # Surface counts when rebase-merge
    if [ "$REBASE_KIND" = "rebase-merge" ]; then
        if [ -f "$TRACKER_GIT_DIR/rebase-merge/msgnum" ] && [ -f "$TRACKER_GIT_DIR/rebase-merge/end" ]; then
            _msgnum=$(cat "$TRACKER_GIT_DIR/rebase-merge/msgnum" 2>/dev/null || echo "?")
            _end=$(cat "$TRACKER_GIT_DIR/rebase-merge/end" 2>/dev/null || echo "?")
            echo "  Progress: $_msgnum / $_end picks completed"
        fi
    fi
    exit 3
fi

# ── No-op path: nothing to recover ───────────────────────────────────────────
if [ -z "$REBASE_KIND" ] && [ "$RECOVER_DANGLING" = "0" ]; then
    echo "No stale rebase or merge state detected — nothing to recover"
    exit 0
fi

recovered_count=0

# ── Attempt rebase --continue (skip when --recover-dangling) ─────────────────
if [ -n "$REBASE_KIND" ] && [ "$RECOVER_DANGLING" = "0" ]; then
    case "$REBASE_KIND" in
        rebase-merge|rebase-apply|REBASE_HEAD)
            echo "Stale rebase detected ($REBASE_KIND); attempting 'git rebase --continue' with timeout=${CONTINUE_TIMEOUT}s"
            _continue_exit=0
            if command -v timeout >/dev/null 2>&1; then
                timeout "$CONTINUE_TIMEOUT" git -C "$TRACKER_DIR" -c rebase.autostash=true rebase --continue 2>&1 || _continue_exit=$?
            else
                git -C "$TRACKER_DIR" -c rebase.autostash=true rebase --continue 2>&1 || _continue_exit=$?
            fi
            if [ "$_continue_exit" -eq 0 ]; then
                # Continue succeeded — verify no rebase state remains
                _after_kind="$(_detect_stale_rebase)"
                if [ -z "$_after_kind" ]; then
                    echo "Recovery successful: rebase drained via --continue"
                    exit 0
                fi
                # Still in rebase (perhaps additional manual conflicts)
                echo "WARN: rebase --continue exited 0 but rebase state still present (marker=$_after_kind); falling back to abort + cherry-pick"
            else
                echo "WARN: rebase --continue failed (exit=$_continue_exit); falling back to abort + cherry-pick of dangling commits"
            fi
            ;;
        MERGE_HEAD)
            echo "Stale merge state detected; aborting merge"
            git -C "$TRACKER_DIR" merge --abort 2>/dev/null || true
            ;;
    esac
fi

# ── Abort + cherry-pick dangling ticket commits ──────────────────────────────
# Abort any remaining rebase/merge state so the tracker is clean for cherry-pick
_final_kind="$(_detect_stale_rebase)"
case "$_final_kind" in
    rebase-merge|rebase-apply|REBASE_HEAD)
        echo "Aborting rebase to enable cherry-pick recovery"
        git -C "$TRACKER_DIR" rebase --abort 2>/dev/null || true
        ;;
    MERGE_HEAD)
        echo "Aborting merge to enable cherry-pick recovery"
        git -C "$TRACKER_DIR" merge --abort 2>/dev/null || true
        ;;
esac

# Find dangling commits whose subject matches the ticket commit message pattern
# Pattern: "ticket: <EVENT> <id>" where EVENT in {CREATE,STATUS,COMMENT,LINK,UNLINK,EDIT,FILE_IMPACT,VERIFY_COMMANDS,ARCHIVED,SYNC,SNAPSHOT,REVERT,COMPACT,DELETE,TAG,UNTAG}
echo "Scanning for dangling ticket commits to cherry-pick"
_fsck_output=$(git -C "$TRACKER_DIR" fsck --no-reflogs 2>/dev/null || true)
_dangling_shas=()
while IFS= read -r line; do
    case "$line" in
        "dangling commit "*)
            _sha="${line#dangling commit }"
            _subject=$(git -C "$TRACKER_DIR" log -1 --format='%s' "$_sha" 2>/dev/null || true)
            if echo "$_subject" | grep -qE '^ticket: (CREATE|STATUS|COMMENT|LINK|UNLINK|EDIT|FILE_IMPACT|VERIFY_COMMANDS|ARCHIVED|SYNC|SNAPSHOT|REVERT|COMPACT|DELETE|TAG|UNTAG)'; then
                _dangling_shas+=("$_sha")
            fi
            ;;
    esac
done <<< "$_fsck_output"

if [ "${#_dangling_shas[@]}" -eq 0 ]; then
    echo "No dangling ticket commits found"
    if [ -n "$REBASE_KIND" ] && [ "$recovered_count" -eq 0 ]; then
        # We had a stale rebase but recovered nothing
        exit 1
    fi
    exit 0
fi

# Sort dangling commits by committer date so cherry-picks land in chronological order
_sorted_shas=()
while IFS= read -r line; do
    _sorted_shas+=("$line")
done < <(
    for _s in "${_dangling_shas[@]}"; do
        _cd=$(git -C "$TRACKER_DIR" log -1 --format='%ct %H' "$_s" 2>/dev/null || true)
        [ -n "$_cd" ] && echo "$_cd"
    done | sort -n | awk '{print $2}'
)

echo "Found ${#_sorted_shas[@]} dangling ticket commits — cherry-picking in chronological order"

# Cherry-pick each, skipping any that fail
for _sha in "${_sorted_shas[@]}"; do
    _cp_exit=0
    git -C "$TRACKER_DIR" cherry-pick --allow-empty --strategy=recursive -X theirs "$_sha" >/dev/null 2>&1 || _cp_exit=$?
    if [ "$_cp_exit" -eq 0 ]; then
        _short_sha=$(git -C "$TRACKER_DIR" rev-parse --short "$_sha")
        _picked_subject=$(git -C "$TRACKER_DIR" log -1 --format='%s' "$_sha")
        echo "  cherry-picked $_short_sha: $_picked_subject"
        recovered_count=$((recovered_count + 1))
    else
        # Skip on conflict — leave the cherry-pick aborted state
        git -C "$TRACKER_DIR" cherry-pick --abort >/dev/null 2>&1 || true
        _short_sha=$(git -C "$TRACKER_DIR" rev-parse --short "$_sha")
        echo "  skipped $_short_sha (cherry-pick conflict — manual recovery required)"
    fi
done

echo "Recovery complete: $recovered_count commits cherry-picked"
if [ "$recovered_count" -eq 0 ] && [ -n "$REBASE_KIND" ]; then
    exit 1
fi
exit 0
