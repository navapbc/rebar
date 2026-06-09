#!/usr/bin/env bash
# ticket-sync.sh
#
# Shared, self-contained reconvergence helper for the periodic background sync of
# the local `tickets` branch with `origin/tickets`. Sourced by the `rebar`
# dispatcher's _ensure_initialized and by ticket-lifecycle.sh so there is ONE
# implementation of the cross-environment sync policy (no drift in concurrency
# logic).
#
# Concurrency Doctrine (REMEDIATION_PROPOSAL.md §0, I1-I9):
#   * Reconvergence is git MERGE-as-union, never rebase (bug 637b): event files
#     are append-only and UUID-named (I1/I2), so a merge unions both clones'
#     events with no real conflict, including across compaction *.retired/SNAPSHOT
#     boundaries (I9). Rebase would strand picks as dangling commits.
#   * Local-ahead is detected by HEAD, NOT the refs/heads/tickets branch ref. The
#     tracker worktree can be in a detached-HEAD-local-ahead state (e.g. after an
#     interrupted rebase, or on older git) where a local commit advances HEAD but
#     not the branch ref. The previous guard read `origin/tickets..tickets`
#     (branch ref) — empty in that state — and force-reset away the local commit
#     (the WS3 data-loss bug). We use `origin/tickets..HEAD`.
#   * The reset/merge runs UNDER the per-clone write lock (.ticket-write.lock),
#     the same lock the append+commit write path uses, so it cannot race a
#     concurrent local appender's `git add`/`commit` (index.lock contention).
#   * Never reset/merge through an in-progress rebase/merge recovery state
#     (bug 637b) — that would strand pending picks / abandon the merge.
#   * A merge conflict NEVER force-resets and NEVER hard-fails a read: it aborts
#     the merge, keeps local state intact, and surfaces an fsck hint. Only the
#     genuinely-unrelated-history (stale auto-init orphan, no merge-base) case
#     force-resets to adopt the remote — an atomic reset, never a merge (unrelated
#     merges conflict on committed scaffolding and would wedge reads). Discarded
#     orphan commits remain in the reflog (gc.auto=0 on the tracker).
#
# Usage: _reconverge_tickets <tracker_dir>
#   Best-effort: all failure paths return 0 so the caller's read/command proceeds.

# _ticket_sync_gitdir <tracker_dir> — echo the resolved git dir for the tracker
# worktree (.git is a file pointing at the real gitdir for linked worktrees).
_ticket_sync_gitdir() {
    local tracker_dir="$1"
    local gitfile="$tracker_dir/.git"
    local gitdir=""
    if [ -f "$gitfile" ]; then
        gitdir=$(sed 's/^gitdir: //' "$gitfile" 2>/dev/null)
        case "$gitdir" in /*) ;; *) gitdir="$tracker_dir/$gitdir" ;; esac
    elif [ -d "$gitfile" ]; then
        gitdir="$gitfile"
    fi
    printf '%s' "$gitdir"
}

# _ticket_sync_in_recovery_state <tracker_dir> — return 0 if the tracker is mid
# rebase/merge (rebase-merge/, rebase-apply/, REBASE_HEAD, or MERGE_HEAD present).
_ticket_sync_in_recovery_state() {
    local tracker_dir="$1"
    local gitdir
    gitdir=$(_ticket_sync_gitdir "$tracker_dir")
    [ -z "$gitdir" ] && return 1
    if [ -d "$gitdir/rebase-merge" ] || [ -d "$gitdir/rebase-apply" ] \
       || [ -f "$gitdir/REBASE_HEAD" ] || [ -f "$gitdir/MERGE_HEAD" ]; then
        return 0
    fi
    return 1
}

# _do_reconverge_tickets <tracker_dir> — the locked mutation critical section.
# Assumes the write lock is already held AND `git fetch` already ran (so origin/
# tickets is current). Does NO network I/O — only local ref reads and the
# reset/merge that mutate HEAD/index/worktree, which is why it must hold the lock.
_do_reconverge_tickets() {
    local tracker_dir="$1"

    # Recovery-state guard (637b), re-checked UNDER the lock: never reset/merge
    # through an interrupted rebase/merge — it would strand pending picks or
    # abandon the merge (e.g. `git reset --hard` silently clears MERGE_HEAD).
    if _ticket_sync_in_recovery_state "$tracker_dir"; then
        echo "Warning: tickets sync skipped — tracker in rebase/merge recovery state (run: rebar fsck-recover)" >&2
        return 0
    fi

    git -C "$tracker_dir" rev-parse --verify origin/tickets >/dev/null 2>&1 || return 0

    # Unrelated histories (no common ancestor): stale auto-init orphan that never
    # knew origin/tickets — adopt the remote via atomic reset (never a merge:
    # unrelated merges conflict on committed scaffolding and would wedge reads).
    if ! git -C "$tracker_dir" merge-base HEAD origin/tickets >/dev/null 2>&1; then
        # Surface any local-only commits being set aside (recoverable via reflog;
        # the tracker runs gc.auto=0) so the discard is never silent.
        local orphan_ahead
        orphan_ahead=$(git -C "$tracker_dir" rev-list --count HEAD --not origin/tickets 2>/dev/null || echo 0)
        if [ "${orphan_ahead:-0}" -gt 0 ]; then
            echo "Warning: tickets sync — local 'tickets' history is unrelated to origin/tickets; adopting origin and setting aside ${orphan_ahead} local-only commit(s) (recoverable: git -C \"$tracker_dir\" reflog)" >&2
        fi
        git -C "$tracker_dir" reset --hard origin/tickets --quiet 2>/dev/null || true
        return 0
    fi

    # Related histories. Local-ahead is measured by HEAD (see header).
    local local_ahead
    local_ahead=$(git -C "$tracker_dir" rev-list origin/tickets..HEAD 2>/dev/null) || true

    if [ -z "$local_ahead" ]; then
        # No local-only commits → fast-forward adoption of origin. Atomic, safe.
        git -C "$tracker_dir" reset --hard origin/tickets --quiet 2>/dev/null || true
        return 0
    fi

    # Local has commits origin does not. If origin is already an ancestor of HEAD
    # there is nothing to merge (local strictly ahead — it will push later).
    if git -C "$tracker_dir" merge-base --is-ancestor origin/tickets HEAD 2>/dev/null; then
        return 0
    fi

    # Diverged: reconverge by MERGE-as-union. On conflict, do NOT reset (would
    # drop local) and do NOT hard-fail the read — abort, keep local, hint fsck.
    if ! git -C "$tracker_dir" merge origin/tickets --no-edit \
        -m "Merge origin/tickets (auto-reconcile during sync)" >/dev/null 2>&1; then
        git -C "$tracker_dir" merge --abort 2>/dev/null || true
        echo "Warning: tickets sync could not auto-merge origin/tickets — local state kept; run: rebar fsck-recover" >&2
    fi
    return 0
}

# _reconverge_tickets <tracker_dir> — acquire the per-clone write lock, then run
# the reconvergence critical section. Best-effort (returns 0 on any failure,
# including lock contention — another holder is likely writing/syncing already).
_reconverge_tickets() {
    local tracker_dir="$1"
    [ -d "$tracker_dir" ] || return 0
    # Canonicalize so the lock path (and the mkdir fallback's lock dir) is byte-
    # identical to the write path's (_flock_stage_commit does `cd && pwd -P`),
    # preserving mutual exclusion even for symlinked/relative callers.
    tracker_dir=$(cd "$tracker_dir" 2>/dev/null && pwd -P) || return 0

    # Cheap pre-lock early-out: skip a tracker mid rebase/merge recovery.
    if _ticket_sync_in_recovery_state "$tracker_dir"; then
        echo "Warning: tickets sync skipped — tracker in rebase/merge recovery state (run: rebar fsck-recover)" >&2
        return 0
    fi

    # Fetch OUTSIDE the lock. `git fetch` only updates remote-tracking refs
    # (refs/remotes/origin/*), never HEAD/index/the worktree, so it cannot race a
    # concurrent local committer — and a slow/hung fetch must NOT hold the write
    # lock and block every local writer. Only the reset/merge below is locked.
    git -C "$tracker_dir" fetch origin tickets --quiet 2>/dev/null || return 0
    git -C "$tracker_dir" rev-parse --verify origin/tickets >/dev/null 2>&1 || return 0

    local lock_file="$tracker_dir/.ticket-write.lock"
    : >> "$lock_file" 2>/dev/null || true

    local lock_timeout="${TICKET_SYNC_LOCK_TIMEOUT:-15}"

    # Prefer util-linux flock(1) (matches the write path). BusyBox flock is
    # unreliable for the fd form, so fall through to the mkdir fallback.
    local flock_bin=""
    if command -v flock >/dev/null 2>&1 && flock --version 2>&1 | grep -qi 'util-linux'; then
        flock_bin="$(command -v flock)"
    fi
    if [ -z "$flock_bin" ]; then
        local ul_flock
        ul_flock=$(find /opt/homebrew/Cellar/util-linux -name flock -path "*/bin/flock" 2>/dev/null | sort -V | tail -1)
        if [ -n "$ul_flock" ] && [ -x "$ul_flock" ]; then
            flock_bin="$ul_flock"
        fi
    fi

    if [ -n "$flock_bin" ]; then
        # FD 201 (the write path uses 200; distinct number avoids confusion).
        (
            "$flock_bin" -x -w "$lock_timeout" 201 || exit 0
            _do_reconverge_tickets "$tracker_dir"
        ) 201>"$lock_file"
    else
        # mkdir-based atomic lock — contends with the write path's same lock dir.
        local lock_dir="${lock_file}.d"
        local deadline
        deadline=$(( $(date +%s) + lock_timeout ))
        while [ "$(date +%s)" -lt "$deadline" ]; do
            if mkdir "$lock_dir" 2>/dev/null; then
                _do_reconverge_tickets "$tracker_dir"
                rmdir "$lock_dir" 2>/dev/null || true
                return 0
            fi
            sleep 0.1
        done
    fi
    return 0
}
