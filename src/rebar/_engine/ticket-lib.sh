#!/usr/bin/env bash
# ticket-lib.sh
# Shared library for ticket event writing. Sourced (not executed) by ticket commands.
#
# Provides:
#   write_commit_event <ticket_id> <temp_event_json_path>
#     Atomically writes an event file and commits it to the tickets branch.
#
# Requirements:
#   - ticket init must have been run (.tickets-tracker/ worktree must exist)
#   - jq must be available (for JSON parsing and canonicalization)
#   - flock (util-linux) used for portable serialization (macOS + Linux)
#
# Stable internal API — used by write_commit_event and emit-review-event.sh
#
# _check_no_rebase_in_progress <tracker_dir>
# Returns 0 if the tracker is in a clean (non-rebase, non-merge) state.
# Returns 75 (EX_TEMPFAIL) if a rebase-merge/, rebase-apply/, REBASE_HEAD, or
# MERGE_HEAD marker is present, indicating the tracker is mid-recovery and
# committing on top of it would silently abandon pending picks (bug 637b).
#
# When the guard fires, emits a clear stderr message with the recovery hint
# pointing at ticket-fsck-recover.sh.
_check_no_rebase_in_progress() {
    local tracker_dir="$1"
    local tracker_git_file="$tracker_dir/.git"
    local tracker_git_dir=""
    if [ -f "$tracker_git_file" ]; then
        tracker_git_dir=$(sed 's/^gitdir: //' "$tracker_git_file")
        if [[ "$tracker_git_dir" != /* ]]; then
            tracker_git_dir="$tracker_dir/$tracker_git_dir"
        fi
    elif [ -d "$tracker_git_file" ]; then
        tracker_git_dir="$tracker_git_file"
    fi
    if [ -z "$tracker_git_dir" ]; then
        # Defensive: if we can't resolve the gitdir, don't block — let the
        # downstream git command fail with its own clearer error.
        return 0
    fi
    local rebase_kind=""
    if [ -d "$tracker_git_dir/rebase-merge" ]; then
        rebase_kind="rebase-merge"
    elif [ -d "$tracker_git_dir/rebase-apply" ]; then
        rebase_kind="rebase-apply"
    elif [ -f "$tracker_git_dir/REBASE_HEAD" ]; then
        rebase_kind="REBASE_HEAD"
    elif [ -f "$tracker_git_dir/MERGE_HEAD" ]; then
        rebase_kind="MERGE_HEAD"
    fi
    if [ -n "$rebase_kind" ]; then
        echo "Error: ticket write blocked — tracker is in $rebase_kind recovery state." >&2
        echo "  tracker: $tracker_dir" >&2
        echo "  Run: rebar fsck-recover --tracker-dir \"$tracker_dir\" (or ticket-fsck-recover.sh from the rebar engine dir)" >&2
        return 75
    fi
    return 0
}

# _flock_stage_commit <tracker_dir> <staging_temp> <final_path> <commit_msg>
# Args:
#   tracker_dir:   canonical path to .tickets-tracker/ (derives lock_file)
#   staging_temp:  absolute path to the staged temp file (same filesystem as tracker_dir)
#   final_path:    absolute destination path (atomic rename target)
#   commit_msg:    git commit message string
#
# Handles:
#   - Acquires flock on .tickets-tracker/.ticket-write.lock
#   - REBASE/MERGE GUARD: refuses commit when tracker is in rebase-merge/,
#     rebase-apply/, REBASE_HEAD, or MERGE_HEAD recovery state (bug 637b) —
#     commits during a paused rebase silently abandon pending picks, so we
#     fail loudly with exit 75 + recovery hint instead.
#   - atomic rename (staging_temp → final_path)
#   - git add (tracker_dir-relative path) + git commit
#   - gc.auto=0 guard
#   - Lock timeout (30s), max retries (2)
_flock_stage_commit() {
    # Resolve to canonical path so callers using a symlink and callers using
    # the real path always contend on the same lock file (cross-path serialization).
    local tracker_dir
    tracker_dir=$(cd "$1" && pwd -P)
    local staging_temp="$2"
    local final_path="$3"
    local commit_msg="$4"

    local lock_file="$tracker_dir/.ticket-write.lock"

    # ── Validate: tracker_dir exists ────────────────────────────────────────
    if [ ! -d "$tracker_dir" ]; then
        echo "Error: tracker directory does not exist: $tracker_dir" >&2
        return 1
    fi

    # ── Derive tracker_dir-relative path from final_path ────────────────────
    local relative_path="${final_path#"$tracker_dir/"}"

    # ── Ensure gc.auto=0 in tickets worktree (skip if already set) ───────────
    # _REBAR_GC_AUTO_ZERO=1: caller guarantees gc.auto is already 0 (set by ticket
    # init and clone_ticket_repo) — skips the git subprocess check (~10ms/op).
    if [ "${_REBAR_GC_AUTO_ZERO:-0}" != "1" ] && \
       [ "$(git -C "$tracker_dir" config --get gc.auto 2>/dev/null)" != "0" ]; then
        git -C "$tracker_dir" config gc.auto 0
    fi

    # ── Locate util-linux flock binary (not in PATH on macOS; BusyBox flock on
    # Alpine does not reliably support the FD-based form used below) ──
    # Only accept flock when it is util-linux flock; BusyBox flock (Alpine/embedded)
    # exits non-zero for `flock -x -w N FD` in the subshell-redirect context used
    # here.  If the binary in PATH is not util-linux, fall through to the mkdir
    # fallback unconditionally.
    local _flock_bin=""
    if command -v flock >/dev/null 2>&1; then
        if flock --version 2>&1 | grep -qi 'util-linux'; then
            _flock_bin="$(command -v flock)"
        fi
        # Non-util-linux flock (e.g. BusyBox): leave _flock_bin empty → mkdir fallback
    fi
    if [ -z "$_flock_bin" ]; then
        # Homebrew util-linux installs flock outside PATH on macOS
        local _ul_flock
        _ul_flock=$(find /opt/homebrew/Cellar/util-linux -name flock -path "*/bin/flock" 2>/dev/null | sort -V | tail -1)
        if [ -n "$_ul_flock" ] && [ -x "$_ul_flock" ]; then
            _flock_bin="$_ul_flock"
        fi
    fi

    # ── Acquire flock, then atomic rename + commit ──────────────────────────
    local max_retries=2
    local flock_timeout="${FLOCK_STAGE_COMMIT_TIMEOUT:-30}"
    local attempt=0
    local lock_acquired=false

    # Ensure the lock file exists before flock tries to open it
    : >> "$lock_file"

    while [ "$attempt" -lt "$max_retries" ]; do
        attempt=$((attempt + 1))

        local flock_exit=0

        if [ -n "$_flock_bin" ]; then
            # bash-native path: use flock(1) fd-based form.
            # FD 200 is opened on the lock file; flock -x -w acquires LOCK_EX,
            # waiting up to $flock_timeout seconds before returning exit 1.
            # The subshell inherits FD 200 and the lock is released on subshell exit.
            # shellcheck disable=SC2093
            (
                "$_flock_bin" -x -w "$flock_timeout" 200 || exit 1
                # REBASE/MERGE GUARD (bug 637b): refuse to commit if tracker is
                # in rebase-merge/, rebase-apply/, REBASE_HEAD, or MERGE_HEAD
                # recovery state. Committing during a paused rebase silently
                # abandons pending picks, so fail loudly instead.
                _check_no_rebase_in_progress "$tracker_dir" || exit 75
                # Atomic rename (same filesystem — mktemp was created inside tracker_dir)
                mv "$staging_temp" "$final_path" || exit 3
                # git add + commit while holding the lock; clean up final_path on failure
                git -C "$tracker_dir" add "$relative_path" 2>/dev/null \
                    && git -C "$tracker_dir" commit -q --no-verify -m "$commit_msg" 2>/dev/null \
                    || { rm -f "$final_path"; exit 2; }
            ) 200>"$lock_file" || flock_exit=$?
        else
            # Fallback when flock binary is not available (e.g. non-Homebrew macOS):
            # mkdir-based atomic lock — mkdir is atomic on POSIX filesystems.
            local _lock_dir="${lock_file}.d"
            local _deadline
            _deadline=$(( $(date +%s) + flock_timeout ))
            local _got_lock=false
            while [ "$(date +%s)" -lt "$_deadline" ]; do
                if mkdir "$_lock_dir" 2>/dev/null; then
                    _got_lock=true
                    break
                fi
                sleep 0.1
            done
            if [ "$_got_lock" = false ]; then
                flock_exit=1
            else
                (
                    # REBASE/MERGE GUARD (bug 637b): refuse to commit if
                    # tracker is in rebase/merge recovery state. See the
                    # flock-bin path above for rationale.
                    _check_no_rebase_in_progress "$tracker_dir" || exit 75
                    mv "$staging_temp" "$final_path" || exit 3
                    git -C "$tracker_dir" add "$relative_path" 2>/dev/null \
                        && git -C "$tracker_dir" commit -q --no-verify -m "$commit_msg" 2>/dev/null \
                        || { rm -f "$final_path"; exit 2; }
                ) || flock_exit=$?
                rmdir "$_lock_dir" 2>/dev/null || true
            fi
        fi

        if [ "$flock_exit" -eq 0 ]; then
            lock_acquired=true
            break
        elif [ "$flock_exit" -eq 2 ]; then
            echo "Error: git commit failed while holding lock" >&2
            return 1
        elif [ "$flock_exit" -eq 3 ]; then
            rm -f "$staging_temp"
            echo "Error: atomic rename failed" >&2
            return 1
        elif [ "$flock_exit" -eq 75 ]; then
            # Rebase/merge guard fired (bug 637b) — refuse the write rather
            # than silently abandon pending picks. Do not retry; the tracker
            # needs operational recovery before any new ticket write.
            rm -f "$staging_temp"
            return 75
        fi
        # flock_exit=1 means lock timeout — retry
    done

    if [ "$lock_acquired" = false ]; then
        local total_wait=$((flock_timeout * max_retries))
        echo "flock: could not acquire lock after ${total_wait}s" >&2
        rm -f "$staging_temp"
        return 1
    fi

    return 0
}

# _flock_write_json <lock_file> <staging_temp> <final_path>
# Cross-platform atomic JSON file write with exclusive lock.
# For use with non-git-tracked files (e.g., cycle-ledger.json in /tmp/).
# Unlike _flock_stage_commit, this function does NOT perform git operations.
#
# Works on macOS without Homebrew util-linux: falls through to Python
# fcntl.flock when util-linux flock binary is unavailable (mkdir-based locking
# does not contend with fcntl.flock, so Python is the macOS-safe fallback).
#
# Args:
#   lock_file:     path to the lock file (created if absent; must be in a writable dir)
#   staging_temp:  absolute path to the staged temp file (same filesystem as final_path)
#   final_path:    absolute destination path (atomic rename target)
#
# Env:
#   FLOCK_STAGE_COMMIT_TIMEOUT  — lock timeout per attempt in seconds (default: 30)
#
# Exit codes:
#   0  — success: staging_temp atomically renamed to final_path
#   1  — lock timeout after max retries, or missing staging_temp
#   3  — atomic rename failed (filesystem error)
_flock_write_json() {
    # Works on macOS without Homebrew util-linux via Python fcntl.flock fallback
    # (mkdir-based locking does not contend with fcntl.flock; Python is required).
    local lock_file="$1"
    local staging_temp="$2"
    local final_path="$3"

    # ── Validate: staging_temp must exist before we attempt anything ─────────
    if [ ! -f "$staging_temp" ]; then
        echo "Error: staging temp file does not exist: $staging_temp" >&2
        return 1
    fi

    local flock_timeout="${FLOCK_STAGE_COMMIT_TIMEOUT:-30}"
    local max_retries=2

    # ── Locate util-linux flock binary (not in PATH on macOS; BusyBox flock on
    # Alpine does not reliably support the FD-based form used below) ──
    # Only accept flock when it is util-linux flock; BusyBox flock (Alpine/embedded)
    # exits non-zero for `flock -x -w N FD` in the subshell-redirect context used
    # here.  If the binary in PATH is not util-linux, fall through to the
    # Python fcntl.flock fallback unconditionally.
    local _flock_bin=""
    if command -v flock >/dev/null 2>&1; then
        if flock --version 2>&1 | grep -qi 'util-linux'; then
            _flock_bin="$(command -v flock)"
        fi
        # Non-util-linux flock (e.g. BusyBox): leave _flock_bin empty → Python fallback
    fi
    if [ -z "$_flock_bin" ]; then
        # Homebrew util-linux installs flock outside PATH on macOS
        local _ul_flock
        _ul_flock=$(find /opt/homebrew/Cellar/util-linux -name flock -path "*/bin/flock" 2>/dev/null | sort -V | tail -1)
        if [ -n "$_ul_flock" ] && [ -x "$_ul_flock" ]; then
            _flock_bin="$_ul_flock"
        fi
    fi

    # Ensure the lock file exists before acquisition
    : >> "$lock_file"

    local attempt=0
    local lock_acquired=false

    while [ "$attempt" -lt "$max_retries" ]; do
        attempt=$((attempt + 1))

        local flock_exit=0

        if [ -n "$_flock_bin" ]; then
            # bash-native path: use flock(1) fd-based form.
            # FD 200 is opened on the lock file; flock -x -w acquires LOCK_EX,
            # waiting up to $flock_timeout seconds before returning exit 1.
            # shellcheck disable=SC2093
            (
                "$_flock_bin" -x -w "$flock_timeout" 200 || exit 1
                mv "$staging_temp" "$final_path" || exit 3
            ) 200>"$lock_file" || flock_exit=$?
        else
            # Python fcntl.flock fallback — works on macOS without Homebrew util-linux.
            # fcntl.flock is advisory and cross-process; it contends correctly with
            # other fcntl.flock callers (including the flock(1) binary on Linux).
            # mkdir-based locking does NOT contend with fcntl.flock, so Python is
            # the macOS-safe cross-platform fallback here.
            local _py_result
            _py_result=$(python3 - "$lock_file" "$staging_temp" "$final_path" "$flock_timeout" <<'PYEOF'
import fcntl, os, sys, time, signal

lock_path, staging, final, timeout_str = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
timeout = int(timeout_str)

fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
deadline = time.monotonic() + timeout
acquired = False

while time.monotonic() < deadline:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        break
    except (OSError, IOError):
        time.sleep(0.05)

if not acquired:
    os.close(fd)
    sys.exit(1)

try:
    os.rename(staging, final)
except OSError as e:
    os.close(fd)
    print(f"Error: atomic rename failed: {e}", file=sys.stderr)
    sys.exit(3)

fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
sys.exit(0)
PYEOF
            ) || flock_exit=$?
        fi

        if [ "$flock_exit" -eq 0 ]; then
            lock_acquired=true
            break
        elif [ "$flock_exit" -eq 3 ]; then
            rm -f "$staging_temp"
            echo "Error: atomic rename failed" >&2
            return 3
        fi
        # flock_exit=1 means lock timeout — retry
    done

    if [ "$lock_acquired" = false ]; then
        local total_wait=$((flock_timeout * max_retries))
        echo "flock: could not acquire lock after ${total_wait}s" >&2
        rm -f "$staging_temp"
        return 1
    fi

    return 0
}

# write_commit_event <ticket_id> <temp_event_json_path>
# Args:
#   ticket_id: the ticket directory name (e.g., w21-ablv)
#   temp_event_json_path: path to the fully-constructed JSON event file (temp file)
#
# Steps:
#   1. Validates .tickets-tracker/ exists and is a valid worktree
#   2. Reads event_type, timestamp, uuid from JSON via python3
#   3. Creates ticket dir: mkdir -p .tickets-tracker/<ticket_id>
#   4. Stages temp file in .tickets-tracker/ (same filesystem for atomic rename)
#   5. Delegates to _flock_stage_commit for flock + atomic rename + git commit
write_commit_event() {
    local ticket_id="$1"
    local temp_event_json_path="$2"

    local repo_root=""
    if [[ -z "${TICKETS_TRACKER_DIR:-}" ]]; then
        repo_root="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel)"
    fi
    local tracker_dir_raw="${TICKETS_TRACKER_DIR:-$repo_root/.tickets-tracker}"
    # Resolve to canonical path so that callers using a symlink and callers using
    # the real path always contend on the same lock file (cross-path serialization).
    # Use realpath (available on macOS and Linux) for symlink resolution.
    local tracker_dir
    if [ -d "$tracker_dir_raw" ] && command -v realpath >/dev/null 2>&1; then
        tracker_dir=$(realpath "$tracker_dir_raw")
    elif [ -d "$tracker_dir_raw" ]; then
        tracker_dir=$(cd "$tracker_dir_raw" && pwd -P)
    else
        tracker_dir="$tracker_dir_raw"
    fi
    local lock_file="$tracker_dir/.ticket-write.lock"

    # ── Validate: ticket system must be initialized ──────────────────────────
    if [ ! -d "$tracker_dir" ] || [ ! -f "$tracker_dir/.git" ]; then
        echo "Error: ticket system not initialized. Run 'ticket init' first." >&2
        return 1
    fi
    if ! GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git -C "$tracker_dir" rev-parse --is-inside-work-tree &>/dev/null; then
        echo "Error: .tickets-tracker is not a valid git worktree." >&2
        return 1
    fi

    # ── Validate: temp event JSON exists ─────────────────────────────────────
    if [ ! -f "$temp_event_json_path" ]; then
        echo "Error: event JSON file not found: $temp_event_json_path" >&2
        return 1
    fi

    # ── Extract event metadata via jq (bash-native, zero python3) ───────────
    local event_type timestamp uuid
    event_type=$(jq -r '.event_type // empty' "$temp_event_json_path" 2>/dev/null) || {
        echo "Error: failed to parse event JSON (event_type)" >&2
        return 1
    }
    timestamp=$(jq -r '.timestamp // empty' "$temp_event_json_path" 2>/dev/null) || {
        echo "Error: failed to parse event JSON (timestamp)" >&2
        return 1
    }
    uuid=$(jq -r '.uuid // empty' "$temp_event_json_path" 2>/dev/null) || {
        echo "Error: failed to parse event JSON (uuid)" >&2
        return 1
    }
    if [ -z "$event_type" ] || [ -z "$timestamp" ] || [ -z "$uuid" ]; then
        echo "Error: event JSON missing required fields (event_type, timestamp, uuid)" >&2
        return 1
    fi

    # ── Normalize event_type to uppercase and validate against allowed enum ──
    event_type=$(echo "$event_type" | tr '[:lower:]' '[:upper:]')
    case "$event_type" in
        CREATE|STATUS|COMMENT|LINK|UNLINK|SNAPSHOT|SYNC|REVERT|EDIT|ARCHIVED|FILE_IMPACT|VERIFY_COMMANDS) ;;
        *)
            echo "Error: invalid event_type '$event_type'. Must be one of: CREATE, STATUS, COMMENT, LINK, UNLINK, SNAPSHOT, SYNC, REVERT, EDIT, ARCHIVED, FILE_IMPACT, VERIFY_COMMANDS" >&2
            return 1
            ;;
    esac

    # ── Determine final filename ─────────────────────────────────────────────
    local final_filename="${timestamp}-${uuid}-${event_type}.json"
    local ticket_dir="$tracker_dir/$ticket_id"
    local final_path="$ticket_dir/$final_filename"

    # ── Create ticket directory ──────────────────────────────────────────────
    mkdir -p "$ticket_dir"

    # ── Stage temp file in .tickets-tracker/ (same filesystem for atomic rename)
    # mktemp in .tickets-tracker/ ensures same-filesystem atomic mv inside flock
    # to replace the python3 subprocess that epic 78fc-3858 targets for elimination.
    # jq -S -c '.' produces output byte-for-byte identical to Python's
    # json.dumps(ensure_ascii=False,separators=(',',':'),sort_keys=True) — verified
    # by test_write_commit_event_json_byte_exact in tests/scripts/test-ticket-write-commit-event.sh.
    # The project "no-jq" guideline targets avoiding jq as a JSON parsing utility in
    # hook scripts where python3 is the sanctioned alternative; this site uses jq as a
    # subprocess-count optimization replacing python3, not as an ad-hoc parser.
    # jq is a system dependency on macOS (via Homebrew) and all major Linux distributions.
    local staging_temp
    staging_temp=$(mktemp "$tracker_dir/.tmp-event-XXXXXX")
    jq -S -c '.' "$temp_event_json_path" > "$staging_temp" 2>/dev/null || {
        rm -f "$staging_temp"
        echo "Error: failed to write staging temp file" >&2
        return 1
    }
    # ── Delegate to _flock_stage_commit for flock + atomic rename + commit ──
    local commit_msg="ticket: ${event_type} ${ticket_id}"
    _flock_stage_commit "$tracker_dir" "$staging_temp" "$final_path" "$commit_msg" || return $?

    # Push to remote after successful commit (best-effort with retry)
    _push_tickets_branch "$tracker_dir"

    return 0
}

# _push_tickets_branch <base_path>
# Push the tickets branch to origin with retry logic for non-fast-forward.
# Best-effort: push failures are logged but do not fail the caller.
_push_tickets_branch() {
    local base_path="$1"
    local _remote
    _remote=$(git -C "$base_path" remote 2>/dev/null | head -1)
    if [ -z "$_remote" ]; then
        return 0  # No remote — nothing to push
    fi

    local _max_retries=3
    local _attempt=0
    while [ "$_attempt" -lt "$_max_retries" ]; do
        _attempt=$((_attempt + 1))
        local _push_exit=0
        local _push_stderr=""
        # Push HEAD:tickets (not bare "tickets") so the current detached-HEAD
        # commit is pushed regardless of refs/heads/tickets state. The
        # .tickets-tracker worktree normally operates in detached HEAD; commits
        # advance HEAD but not the local branch ref, so `push origin tickets`
        # silently pushes a stale ref and fails non-fast-forward. Bug 27d8-b230.
        _push_stderr=$(PRE_COMMIT_ALLOW_NO_CONFIG=1 git -C "$base_path" push origin HEAD:tickets 2>&1) || _push_exit=$?

        if [ "$_push_exit" -eq 0 ]; then
            return 0
        fi

        if echo "$_push_stderr" | grep -qiE 'non-fast-forward|rejected|fetch first'; then
            # MERGE-AS-DEFAULT (bug 637b Fix 3): reconcile a non-fast-forward
            # push by MERGING origin/tickets, not REBASING. Rebase exposes a
            # multi-step state machine (rebase-merge/ directory + sequencer)
            # which, if interrupted mid-pick by signal or by a concurrent
            # writer landing a commit on the in-rebase HEAD, strands the
            # pending picks as unreachable dangling commits — silent data loss.
            # Merge is atomic: one commit object, no rebase-merge state.
            # Ticket event files use UUID-named append-only filenames, so the
            # merge is correct even across compaction boundaries where rebase
            # would conflict on deleted files.
            git -C "$base_path" fetch origin tickets 2>/dev/null || true

            # Defense in depth: if a prior failure left the tracker in a
            # rebase/merge recovery state, refuse to merge (would compound
            # the failure). Surfaces the recovery requirement loudly.
            if ! _check_no_rebase_in_progress "$base_path" 2>/dev/null; then
                echo "Warning: cannot reconcile push — tracker is in rebase/merge recovery state. Run ticket-fsck-recover.sh." >&2
                return 0  # Best-effort: don't fail the caller
            fi

            local _merge_exit=0
            # Capture stderr so we can distinguish error classes — the prior
            # `2>/dev/null` swallowed the "would be overwritten by merge"
            # signal that flags a dirty-WD recovery class (bug 12a6).
            local _merge_stderr
            _merge_stderr=$(git -C "$base_path" merge origin/tickets --no-edit -m "Merge origin/tickets (auto-reconcile during push retry)" 2>&1) || _merge_exit=$?

            if [ "$_merge_exit" -eq 0 ]; then
                # Merge clean; loop continues to retry the push on next iter.
                continue
            fi

            # Classify the merge failure. "Would be overwritten by merge" is a
            # dirty-WD class — the working tree has uncommitted modifications
            # on tracked paths the merge would touch. The Jira reconciler
            # writes .bridge_state/* files the ticket CLI never touches; those
            # are a frequent source of this class. Recovery: stash → retry
            # merge → pop. Non-overlapping reconciler paths reapply cleanly
            # because ticket event files are UUID-named and append-only.
            if echo "$_merge_stderr" | grep -qiE 'would be overwritten by merge|local changes.*would be overwritten'; then
                # No merge state to abort here: when git refuses with "would
                # be overwritten" the merge never started — there is no
                # MERGE_HEAD and `git merge --abort` would exit non-zero
                # complaining about "fatal: There is no merge to abort"
                # (which is harmless; we suppress its stderr).
                local _stash_exit=0
                git -C "$base_path" stash push --quiet -m "_push_tickets_branch:auto-stash" 2>/dev/null || _stash_exit=$?
                if [ "$_stash_exit" -ne 0 ]; then
                    echo "Warning: tickets branch push failed: stash failed (attempt $_attempt)" >&2
                    continue
                fi
                local _merge2_exit=0
                git -C "$base_path" merge origin/tickets --no-edit -m "Merge origin/tickets (auto-reconcile, post-stash)" 2>/dev/null || _merge2_exit=$?
                # Pop unconditionally — non-overlapping paths reapply cleanly.
                git -C "$base_path" stash pop --quiet 2>/dev/null || true
                if [ "$_merge2_exit" -ne 0 ]; then
                    git -C "$base_path" merge --abort 2>/dev/null || true
                    echo "Warning: tickets branch merge failed after stash recovery (attempt $_attempt)" >&2
                fi
                continue  # Next iteration retries push (succeeds if merge2 was clean).
            fi

            # Real content conflict — retry won't help, but `continue` instead
            # of `return 0` so _max_retries is honored and the terminal warning
            # at the end of the loop fires (instead of dying silently on
            # iteration 1). Costs 2 extra merge attempts on truly unresolvable
            # conflicts but makes the retry semantics match the documented
            # bound. Pre-12a6 behavior bypassed _max_retries entirely.
            git -C "$base_path" merge --abort 2>/dev/null || true
            echo "Warning: tickets branch push failed (merge conflict, attempt $_attempt)" >&2
            continue
        else
            echo "Warning: tickets branch push failed (exit $_push_exit): $_push_stderr" >&2
            return 0  # Best-effort: don't fail the caller
        fi
    done

    echo "Warning: tickets branch push failed after $_max_retries retries" >&2
    return 0  # Best-effort
}

# ticket_read_status <tracker_dir> <ticket_id>
# Returns the current compiled status of a ticket (e.g., open, in_progress, closed, blocked).
# Computes REDUCER path internally from BASH_SOURCE — does NOT rely on caller-set globals.
#
# Args:
#   tracker_dir: path to .tickets-tracker worktree (passed by caller)
#   ticket_id:   ticket directory name (e.g., dso-abc1)
#
# Outputs the status string to stdout. Exits non-zero on error.
ticket_read_status() {
    local tracker_dir="$1"
    local ticket_id="$2"

    # Resolve REDUCER path relative to this script's location
    local lib_dir
    lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local reducer="$lib_dir/ticket-reducer.py"

    # Canonicalize ticket_id: accepts 16-hex, 8-hex short ID, jira_key,
    # alias, or unique prefix (>=4 chars). Failure falls through to the
    # raw dir-existence check below, preserving the historical error
    # message that callers (ticket-transition.sh, ticket-create.sh,
    # ticket-link.sh, ticket-lib-api.sh:ticket_create) parse from stderr.
    if declare -f resolve_ticket_id >/dev/null 2>&1; then
        local _resolved
        if _resolved="$(TICKETS_TRACKER_DIR="$tracker_dir" resolve_ticket_id "$ticket_id" 2>/dev/null)" \
            && [ -n "$_resolved" ]; then
            ticket_id="$_resolved"
        fi
    fi

    local ticket_dir="$tracker_dir/$ticket_id"

    if [ ! -d "$ticket_dir" ]; then
        echo "Error: ticket directory not found: $ticket_dir" >&2
        return 1
    fi

    local state_json
    state_json=$(python3 "$reducer" "$ticket_dir" 2>/dev/null) || {
        echo "Error: reducer failed for ticket $ticket_id" >&2
        return 1
    }

    python3 -c "
import json, sys
state = json.loads(sys.argv[1])
print(state.get('status', ''))
" "$state_json"
}

# _tag_add <ticket_id> <tag>
# Idempotently adds a tag to a ticket by writing an EDIT event.
# If the tag is already present, exits 0 without writing an event.
#
# Honors TICKET_CMD env var (for testability); otherwise uses ticket script
# relative to this file. Honors TICKETS_TRACKER_DIR env var for tracker path.
_tag_add() {
    local ticket_id="$1"
    local tag="$2"

    # Resolve ticket command
    local _lib_dir
    _lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local _ticket_cmd="${TICKET_CMD:-$_lib_dir/ticket}"

    # Resolve tracker dir
    local _repo_root=""
    if [[ -z "${TICKETS_TRACKER_DIR:-}" ]]; then
        _repo_root="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel)"
    fi
    local _tracker_dir="${TICKETS_TRACKER_DIR:-$_repo_root/.tickets-tracker}"

    # Read current tags — use in-process ticket_show when available to avoid
    # spawning a bash subprocess (~30ms overhead) for each tag operation.
    local _show_output
    if declare -f ticket_show >/dev/null 2>&1; then
        _show_output=$(TICKETS_TRACKER_DIR="$_tracker_dir" ticket_show "$ticket_id" 2>/dev/null) || true
    else
        _show_output=$(TICKETS_TRACKER_DIR="$_tracker_dir" bash "$_ticket_cmd" show "$ticket_id" 2>/dev/null) || true
    fi
    local _current_tags
    _current_tags=$(echo "$_show_output" \
        | python3 -c "import json,sys; tags=json.load(sys.stdin).get('tags',[]); print(','.join(tags) if tags else '')" 2>/dev/null || echo "")

    # Idempotency: skip if tag already present
    if echo ",$_current_tags," | grep -qF ",$tag,"; then
        return 0
    fi

    # Build new tags list
    local _new_tags_json
    _new_tags_json=$(python3 -c "
import json, sys
current = [t for t in sys.argv[1].split(',') if t] if sys.argv[1] else []
tag = sys.argv[2]
current.append(tag)
print(json.dumps(current))
" "$_current_tags" "$tag")

    # Read env-id and author
    local _env_id
    _env_id=$(cat "$_tracker_dir/.env-id" 2>/dev/null || echo "")
    local _author
    _author=$(git config user.name 2>/dev/null || echo "unknown")

    # Build EDIT event JSON
    local _temp_event
    _temp_event=$(mktemp "$_tracker_dir/.tmp-tag-add-XXXXXX")

    python3 -c "
import json, sys, time, uuid

env_id = sys.argv[1]
author = sys.argv[2]
tags = json.loads(sys.argv[3])
out_path = sys.argv[4]

event = {
    'timestamp': time.time_ns(),
    'uuid': str(uuid.uuid4()),
    'event_type': 'EDIT',
    'env_id': env_id,
    'author': author,
    'data': {
        'fields': {
            'tags': tags
        }
    }
}

with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(event, f, ensure_ascii=False)
" "$_env_id" "$_author" "$_new_tags_json" "$_temp_event" || {
        rm -f "$_temp_event"
        echo "Error: failed to build EDIT event JSON for _tag_add" >&2
        return 1
    }

    write_commit_event "$ticket_id" "$_temp_event" || {
        rm -f "$_temp_event"
        echo "Error: failed to write EDIT event for _tag_add" >&2
        return 1
    }

    rm -f "$_temp_event"
    return 0
}

# _tag_remove <ticket_id> <tag>
# Idempotently removes a tag from a ticket by writing an EDIT event.
# If the tag is absent, exits 0 without writing an event.
# When removing the last tag, writes data.fields.tags = [] (not null).
#
# Honors TICKET_CMD env var (for testability); otherwise uses ticket script
# relative to this file. Honors TICKETS_TRACKER_DIR env var for tracker path.
_tag_remove() {
    local ticket_id="$1"
    local tag="$2"

    # Resolve ticket command
    local _lib_dir
    _lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local _ticket_cmd="${TICKET_CMD:-$_lib_dir/ticket}"

    # Resolve tracker dir
    local _repo_root=""
    if [[ -z "${TICKETS_TRACKER_DIR:-}" ]]; then
        _repo_root="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel)"
    fi
    local _tracker_dir="${TICKETS_TRACKER_DIR:-$_repo_root/.tickets-tracker}"

    # Read current tags — use in-process ticket_show when available to avoid
    # spawning a bash subprocess (~30ms overhead) for each tag operation.
    local _show_output
    if declare -f ticket_show >/dev/null 2>&1; then
        _show_output=$(TICKETS_TRACKER_DIR="$_tracker_dir" ticket_show "$ticket_id" 2>/dev/null) || true
    else
        _show_output=$(TICKETS_TRACKER_DIR="$_tracker_dir" bash "$_ticket_cmd" show "$ticket_id" 2>/dev/null) || true
    fi
    local _current_tags
    _current_tags=$(echo "$_show_output" \
        | python3 -c "import json,sys; tags=json.load(sys.stdin).get('tags',[]); print(','.join(tags) if tags else '')" 2>/dev/null || echo "")

    # Idempotency: skip if tag is absent
    if ! echo ",$_current_tags," | grep -qF ",$tag,"; then
        return 0
    fi

    # Build new tags list (excluding removed tag)
    local _new_tags_json
    _new_tags_json=$(python3 -c "
import json, sys
current = [t for t in sys.argv[1].split(',') if t] if sys.argv[1] else []
tag = sys.argv[2]
remaining = [t for t in current if t != tag]
print(json.dumps(remaining))
" "$_current_tags" "$tag")

    # Read env-id and author
    local _env_id
    _env_id=$(cat "$_tracker_dir/.env-id" 2>/dev/null || echo "")
    local _author
    _author=$(git config user.name 2>/dev/null || echo "unknown")

    # Build EDIT event JSON
    local _temp_event
    _temp_event=$(mktemp "$_tracker_dir/.tmp-tag-remove-XXXXXX")

    python3 -c "
import json, sys, time, uuid

env_id = sys.argv[1]
author = sys.argv[2]
tags = json.loads(sys.argv[3])
out_path = sys.argv[4]

event = {
    'timestamp': time.time_ns(),
    'uuid': str(uuid.uuid4()),
    'event_type': 'EDIT',
    'env_id': env_id,
    'author': author,
    'data': {
        'fields': {
            'tags': tags
        }
    }
}

with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(event, f, ensure_ascii=False)
" "$_env_id" "$_author" "$_new_tags_json" "$_temp_event" || {
        rm -f "$_temp_event"
        echo "Error: failed to build EDIT event JSON for _tag_remove" >&2
        return 1
    }

    write_commit_event "$ticket_id" "$_temp_event" || {
        rm -f "$_temp_event"
        echo "Error: failed to write EDIT event for _tag_remove" >&2
        return 1
    }

    rm -f "$_temp_event"
    return 0
}

# _compact_preconditions <ticket_dir> <epic_id>
# Compacts all flat PRECONDITIONS event files in ticket_dir into a single
# PRECONDITIONS-SNAPSHOT.json, then retires the originals by renaming them
# to *.retired (preserving the audit trail).
#
# Args:
#   ticket_dir: absolute path to the ticket event directory
#   epic_id:    ticket ID (used for diagnostic log)
#
# Steps:
#   1. Enumerate *-PRECONDITIONS.json files (excluding *-PRECONDITIONS-SNAPSHOT.json and *.retired)
#   2. Build merged payload applying LWW across composite keys (gate_name, session_id, worktree_id)
#   3. Write merged payload to temp file, then atomic rename to final SNAPSHOT path
#   4. Rename each original to *.retired (audit trail preserved)
#   5. Clean up any .tmp files on failure
#
# Exit codes:
#   0 = success
#   1 = error (no events found or filesystem error)
_compact_preconditions() {
    local ticket_dir="$1"
    local epic_id="${2:-unknown}"

    if [ ! -d "$ticket_dir" ]; then
        echo "[compact_preconditions] ERROR: ticket dir not found: $ticket_dir" >&2
        return 1
    fi

    # Enumerate live PRECONDITIONS events (exclude snapshots and retired files)
    local event_files=()
    while IFS= read -r -d '' f; do
        event_files+=("$f")
    done < <(find "$ticket_dir" -maxdepth 1 \
        -name '*-PRECONDITIONS.json' \
        ! -name '*-PRECONDITIONS-SNAPSHOT.json' \
        ! -name '*.retired' \
        -print0 2>/dev/null | sort -z)

    if [ "${#event_files[@]}" -eq 0 ]; then
        echo "[compact_preconditions] INFO: no live PRECONDITIONS events for $epic_id — skipping" >&2
        return 1
    fi

    # Build merged payload via LWW across composite keys using Python
    local ts
    ts=$(python3 -c "import time; print(int(time.time_ns()))")
    local snap_uuid
    snap_uuid=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
    local tmp_path="$ticket_dir/${ts}-${snap_uuid}-PRECONDITIONS-SNAPSHOT.json.tmp"
    local final_path="$ticket_dir/${ts}-${snap_uuid}-PRECONDITIONS-SNAPSHOT.json"

    # Build file list as NUL-separated string for Python
    local file_list_str
    file_list_str=$(printf '%s\0' "${event_files[@]}" | python3 -c "
import sys
parts = sys.stdin.buffer.read().split(b'\x00')
print('\n'.join(p.decode('utf-8') for p in parts if p))
")

    local merge_exit=0
    python3 -c "
import json, sys, os, time, uuid as uuid_mod

event_files = [l for l in sys.argv[1].split('\n') if l.strip()]
ticket_dir = sys.argv[2]
ts = int(sys.argv[3])
snap_uuid = sys.argv[4]
tmp_path = sys.argv[5]
epic_id = sys.argv[6]

# LWW merge: composite key = (gate_name, session_id, worktree_id)
# Last-write-wins by timestamp within each composite key group.
# DUAL-FORMAT NOTE: _write_preconditions writes gate_name/session_id/worktree_id at
# the top level of the event. Older callers (integration tests, bench fixtures) write
# them nested inside the 'data' dict. Handle both by checking top-level first.
merged = {}
for fpath in event_files:
    try:
        with open(fpath, encoding='utf-8') as fh:
            ev = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        print(f'[compact_preconditions] WARN: skipping corrupt file {fpath}: {e}', file=sys.stderr)
        continue
    data = ev.get('data', {})
    gate_name = ev.get('gate_name') or data.get('gate_name', '')
    session_id = ev.get('session_id') or data.get('session_id', '')
    worktree_id = ev.get('worktree_id') or data.get('worktree_id', '')
    key = (gate_name, session_id, worktree_id)
    ev_ts = ev.get('timestamp', 0)
    if key not in merged or ev_ts > merged[key]['_ts']:
        merged[key] = dict(data)
        merged[key]['_ts'] = ev_ts
        merged[key]['_gate_name'] = gate_name

# Build final merged gate_verdicts, manifest_depth, and represented_gate_names
gate_verdicts = {}
manifest_depth = 0
represented_gate_names = []
for key, payload in merged.items():
    gv = payload.get('gate_verdicts', {})
    if isinstance(gv, dict):
        gate_verdicts.update(gv)
    gn = payload.get('_gate_name', key[0] if key else '')
    if gn and gn not in represented_gate_names:
        represented_gate_names.append(gn)
    d = payload.get('manifest_depth', 0)
    if isinstance(d, (int, float)) and d > manifest_depth:
        manifest_depth = d

snapshot = {
    'timestamp': ts,
    'uuid': snap_uuid,
    'event_type': 'PRECONDITIONS',
    'compacted': True,
    'env_id': 'compaction',
    'author': 'compact_preconditions',
    'data': {
        'schema_version': 1,
        'gate_name': 'compacted',
        'session_id': 'compacted',
        'worktree_id': 'compacted',
        'verdict': 'pass',
        'manifest_depth': manifest_depth,
        'gate_verdicts': gate_verdicts,
        'represented_gate_names': represented_gate_names,
        'source_count': len(event_files),
    }
}

with open(tmp_path, 'w', encoding='utf-8') as fh:
    json.dump(snapshot, fh)

print(f'[compact_preconditions] snapshot written: {tmp_path}', file=sys.stderr)
" "$file_list_str" "$ticket_dir" "$ts" "$snap_uuid" "$tmp_path" "$epic_id" || merge_exit=$?

    if [ "$merge_exit" -ne 0 ]; then
        rm -f "$tmp_path"
        echo "[compact_preconditions] ERROR: merge failed for $epic_id" >&2
        return 1
    fi

    # Atomic rename: tmp → final
    local rename_exit=0
    mv "$tmp_path" "$final_path" || rename_exit=$?
    if [ "$rename_exit" -ne 0 ]; then
        rm -f "$tmp_path"
        echo "[compact_preconditions] ERROR: atomic rename failed for $epic_id" >&2
        return 1
    fi

    echo "[compact_preconditions] snapshot written: $final_path" >&2

    # Retire original event files
    local f
    for f in "${event_files[@]}"; do
        mv "$f" "${f}.retired" 2>/dev/null || \
            echo "[compact_preconditions] WARN: could not retire $f" >&2
    done

    return 0
}

# _tag_add_checked <ticket_id> <tag>
# Adds a tag to a ticket. Tags are free-form in rebar — there is no special
# gating on any tag value. (The DSO Planning-Intelligence-Log gate on
# "brainstorm:complete" was removed when rebar was decoupled from the plugin.)
# Retained as a thin wrapper so existing call sites stay stable.
_tag_add_checked() {
    local ticket_id="$1"
    local tag="$2"
    _tag_add "$ticket_id" "$tag"
}

# _write_preconditions <ticket_id> <gate_name> <session_id> <worktree_id> <tier> <data_json>
# Writes an immutable PRECONDITIONS event JSON into .tickets-tracker/<ticket_id>/
# using _flock_stage_commit for atomic writes (same contract as write_commit_event).
#
# Args:
#   ticket_id:   ticket directory name (e.g., test-t1a2)
#   gate_name:   name of the gate being recorded (e.g., "story_gate")
#   session_id:  session identifier
#   worktree_id: worktree branch identifier
#   tier:        review tier (e.g., "light", "standard", "deep")
#   data_json:   JSON object with additional data (defaults to {})
_write_preconditions() {
    local ticket_id="$1"
    local gate_name="$2"
    local session_id="$3"
    local worktree_id="$4"
    local tier="$5"
    # NOTE: ${6:-{}} appends a literal '}' when $6 is set (bash parse ambiguity).
    # Use explicit if/else to safely default to '{}' only when $6 is absent.
    local data_json
    if [[ -n "${6:-}" ]]; then
        data_json="$6"
    else
        data_json="{}"
    fi

    local repo_root=""
    if [[ -z "${TICKETS_TRACKER_DIR:-}" ]]; then
        repo_root="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel)"
    fi
    local tracker_dir_raw="${TICKETS_TRACKER_DIR:-$repo_root/.tickets-tracker}"
    local tracker_dir
    tracker_dir=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$tracker_dir_raw")

    # ── Validate: ticket system must be initialized ──────────────────────────
    if [ ! -d "$tracker_dir" ] || [ ! -f "$tracker_dir/.git" ]; then
        echo "Error: ticket system not initialized. Run 'ticket init' first." >&2
        return 1
    fi

    # ── Generate timestamp and UUID ──────────────────────────────────────────
    local timestamp_ms file_uuid
    timestamp_ms=$(python3 -c "import time; print(int(time.time() * 1000))")
    file_uuid=$(python3 -c "import uuid; print(str(uuid.uuid4()))")

    # ── Determine ticket dir and final path ──────────────────────────────────
    local ticket_dir="$tracker_dir/$ticket_id"
    local final_filename="${timestamp_ms}-${file_uuid}-PRECONDITIONS.json"
    local final_path="$ticket_dir/$final_filename"

    # ── Create ticket directory ──────────────────────────────────────────────
    mkdir -p "$ticket_dir"

    # ── Stage temp in tracker_dir (same filesystem for atomic rename) ────────
    local staging_temp
    staging_temp=$(mktemp "$tracker_dir/.tmp-preconditions-stage-XXXXXX")
    trap 'rm -f "${staging_temp:-}"' EXIT

    python3 -c "
import json, sys, time

timestamp_ms = int(sys.argv[1])
gate_name    = sys.argv[2]
session_id   = sys.argv[3]
worktree_id  = sys.argv[4]
tier         = sys.argv[5]
data_json    = sys.argv[6]
staging_path = sys.argv[7]

try:
    data_obj = json.loads(data_json)
except json.JSONDecodeError:
    data_obj = {}

# Derive schema_version and manifest_depth from tier
# (per preconditions-schema-v2.md contract)
tier_to_schema = {
    'minimal':  (1, 'minimal'),
    'standard': (2, 'standard'),
    'deep':     (2, 'deep'),
}
sv, md = tier_to_schema.get(tier, (1, 'minimal'))

payload = {
    'event_type': 'PRECONDITIONS',
    'schema_version': sv,
    'manifest_depth': md,
    'gate_name': gate_name,
    'session_id': session_id,
    'worktree_id': worktree_id,
    'tier': tier,
    'timestamp': timestamp_ms,
    'gate_verdicts': [],
    'evidence_ref': {},
    'affects_fields': [],
    'data': data_obj,
}

with open(staging_path, 'w', encoding='utf-8') as f:
    json.dump(payload, f, ensure_ascii=False)
" "$timestamp_ms" "$gate_name" "$session_id" "$worktree_id" "$tier" "$data_json" "$staging_temp" || {
        echo "Error: failed to write preconditions payload" >&2
        return 1
    }

    # ── Acquire flock, atomic rename, and commit ─────────────────────────────
    local commit_msg="preconditions: RECORD ${ticket_id}"
    _flock_stage_commit "$tracker_dir" "$staging_temp" "$final_path" "$commit_msg" || return $?

    # Clear trap — file has been renamed
    trap - EXIT

    _push_tickets_branch "$tracker_dir"

    echo "Preconditions recorded: $final_filename"
}

# resolve_ticket_id <input>
# Resolves a ticket identifier (full ID, 8-hex short ID, alias, jira_key, or prefix)
# to the canonical ticket directory name. Prints the resolved ID to stdout on success.
#
# Resolution order:
#   1. Exact 16-hex passthrough: ^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$
#      Verifies the ticket directory exists; returns ID unchanged.
#   2. Exact 8-hex backward compat: ^[a-z0-9]{4}-[a-z0-9]{4}$
#      Scans for ticket dirs whose first 9 chars (xxxx-xxxx) match; returns if unique.
#   3. jira_key match: scans CREATE events for data.jira_key == input; returns if unique.
#   4. Alias match: scans CREATE events for data.alias == input; returns if unique.
#   5. Unique prefix (>= 4 chars): scans ticket dirs for IDs starting with input.
#   6. Ambiguous or not found: prints error to stderr, exits 1.
#
# Honors TICKETS_TRACKER_DIR env var for tracker path.
#
# Exit codes:
#   0 = success (resolved ID printed to stdout)
#   1 = not found or ambiguous (error on stderr)
resolve_ticket_id() {
    local input="$1"

    local _repo_root=""
    if [[ -z "${TICKETS_TRACKER_DIR:-}" ]]; then
        _repo_root="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel)"
    fi
    local _tracker_dir="${TICKETS_TRACKER_DIR:-$_repo_root/.tickets-tracker}"

    # ── Step 1: Exact 16-hex passthrough ─────────────────────────────────────
    if [[ "$input" =~ ^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$ ]]; then
        if [ -d "$_tracker_dir/$input" ]; then
            echo "$input"
            return 0
        fi
        echo "Error: ticket '$input' not found" >&2
        return 1
    fi

    # ── Step 2: Exact 8-hex backward compat ──────────────────────────────────
    if [[ "$input" =~ ^[a-z0-9]{4}-[a-z0-9]{4}$ ]]; then
        # Direct directory match (legacy or test-planted short ID)
        if [ -d "$_tracker_dir/$input" ]; then
            echo "$input"
            return 0
        fi
        # Bug 19a3-03ca: delegate the scan to ticket-alias-resolve.py
        # --mode=8hex. Before this consolidation, a bash while-read loop
        # called $(basename ...) per directory across ~20K dirs, costing
        # ~80s of fork/exec overhead at scale. The Python helper does
        # all directory iteration and substring comparison in one process.
        # Best-effort: bash fallback runs on either helper unavailable OR
        # helper exit !=0; in both cases a stderr warning is emitted on
        # failure so the cause is observable. Unified error semantics with
        # _ticketlib_resolve_short_id in ticket-lib-api.sh.
        local _8hex_matches=() _used_helper_8hex=0
        local _resolver_short
        _resolver_short="$(dirname "${BASH_SOURCE[0]}")/ticket-alias-resolve.py"
        if [ -f "$_resolver_short" ] && command -v python3 >/dev/null 2>&1; then
            local _short_out _short_rc=0
            _short_out=$(python3 "$_resolver_short" --mode=8hex "$input" "$_tracker_dir" 2>/dev/null) || _short_rc=$?
            if [ "$_short_rc" -eq 0 ]; then
                _used_helper_8hex=1
                if [ -n "$_short_out" ]; then
                    local _short_line
                    while IFS= read -r _short_line; do
                        [ -z "$_short_line" ] && continue
                        _8hex_matches+=("$_short_line")
                    done <<< "$_short_out"
                fi
            else
                echo "Warning: 8-hex resolver exited $_short_rc for input '$input' — falling back to bash scan" >&2
            fi
        fi
        if [ "$_used_helper_8hex" -eq 0 ]; then
            # Fallback bash scan — uses ${var##*/} param expansion (no fork).
            local _entry
            while IFS= read -r -d '' _entry; do
                local _base
                _base="${_entry##*/}"
                if [[ "${_base:0:9}" == "$input" ]]; then
                    _8hex_matches+=("$_base")
                fi
            done < <(find -L "$_tracker_dir" -mindepth 1 -maxdepth 1 -type d \
                ! -name '.*' -print0 2>/dev/null)
        fi
        if [ "${#_8hex_matches[@]}" -eq 1 ]; then
            echo "${_8hex_matches[0]}"
            return 0
        elif [ "${#_8hex_matches[@]}" -gt 1 ]; then
            echo "Error: Ambiguous 8-hex ID '$input' matches: ${_8hex_matches[*]}" >&2
            return 1
        fi
        echo "Error: ticket '$input' not found" >&2
        return 1
    fi

    # ── Steps 3 & 4: Alias and jira_key scan ─────────────────────────────────
    # Single Python helper iterates all ticket directories in one process.
    # The helper also computes alias-from-ticket_id when data.alias is missing
    # (legacy tickets created before the alias feature shipped), so backfilled
    # aliases are resolvable too. One subprocess vs O(N) per-file Python calls.
    local _alias_matches=()
    local _jira_matches=()
    local _resolver_script
    _resolver_script="$(dirname "${BASH_SOURCE[0]}")/ticket-alias-resolve.py"
    if [ ! -f "$_resolver_script" ]; then
        echo "Error: alias resolver missing at $_resolver_script" >&2
        return 1
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        echo "Error: python3 not found in PATH (required for alias resolver)" >&2
        return 1
    fi
    # Capture output + exit code separately. Piping to read via process
    # substitution discards the exit status; if the resolver crashes
    # mid-scan we'd silently get zero matches and the caller couldn't tell
    # 'no match found' from 'resolver exploded' (cycle-3 review).
    local _resolver_out _resolver_rc=0
    _resolver_out=$(python3 "$_resolver_script" "$input" "$_tracker_dir") || _resolver_rc=$?
    if [ "$_resolver_rc" -ne 0 ]; then
        echo "Error: alias resolver exited $_resolver_rc for input '$input'" >&2
        return 1
    fi
    local _scan_kind _scan_id
    if [ -n "$_resolver_out" ]; then
        while IFS=$'\t' read -r _scan_kind _scan_id; do
            [ -z "$_scan_kind" ] && continue
            case "$_scan_kind" in
                alias) _alias_matches+=("$_scan_id") ;;
                jira)  _jira_matches+=("$_scan_id") ;;
            esac
        done <<< "$_resolver_out"
    fi

    if [ "${#_jira_matches[@]}" -eq 1 ]; then
        echo "${_jira_matches[0]}"
        return 0
    elif [ "${#_jira_matches[@]}" -gt 1 ]; then
        echo "Error: Ambiguous jira_key '$input' matches multiple tickets: ${_jira_matches[*]}" >&2
        return 1
    fi

    if [ "${#_alias_matches[@]}" -eq 1 ]; then
        echo "${_alias_matches[0]}"
        return 0
    elif [ "${#_alias_matches[@]}" -gt 1 ]; then
        echo "Error: Ambiguous alias '$input' matches multiple tickets: ${_alias_matches[*]}" >&2
        return 1
    fi

    # ── Step 5: Unique prefix (>= 4 chars) ───────────────────────────────────
    if [ "${#input}" -ge 4 ]; then
        # Bug 19a3-03ca: delegate to ticket-alias-resolve.py --mode=prefix
        # (see Step 2 for performance rationale). Best-effort error semantics
        # — bash fallback runs on helper unavailable OR helper exit !=0,
        # with a stderr warning on failure (see Step 2 above).
        local _prefix_matches=() _used_helper_prefix=0
        local _resolver_pref
        _resolver_pref="$(dirname "${BASH_SOURCE[0]}")/ticket-alias-resolve.py"
        if [ -f "$_resolver_pref" ] && command -v python3 >/dev/null 2>&1; then
            local _pref_out _pref_rc=0
            _pref_out=$(python3 "$_resolver_pref" --mode=prefix "$input" "$_tracker_dir" 2>/dev/null) || _pref_rc=$?
            if [ "$_pref_rc" -eq 0 ]; then
                _used_helper_prefix=1
                if [ -n "$_pref_out" ]; then
                    local _pref_line
                    while IFS= read -r _pref_line; do
                        [ -z "$_pref_line" ] && continue
                        _prefix_matches+=("$_pref_line")
                    done <<< "$_pref_out"
                fi
            else
                echo "Warning: prefix resolver exited $_pref_rc for input '$input' — falling back to bash scan" >&2
            fi
        fi
        if [ "$_used_helper_prefix" -eq 0 ]; then
            local _entry2
            while IFS= read -r -d '' _entry2; do
                local _base2
                _base2="${_entry2##*/}"
                if [[ "$_base2" == "$input"* ]]; then
                    _prefix_matches+=("$_base2")
                fi
            done < <(find -L "$_tracker_dir" -mindepth 1 -maxdepth 1 -type d \
                ! -name '.*' -print0 2>/dev/null)
        fi
        if [ "${#_prefix_matches[@]}" -eq 1 ]; then
            echo "${_prefix_matches[0]}"
            return 0
        elif [ "${#_prefix_matches[@]}" -gt 1 ]; then
            echo "Error: Ambiguous prefix '$input' matches multiple tickets: ${_prefix_matches[*]}" >&2
            return 1
        fi
    fi

    # ── Step 6: Not found ─────────────────────────────────────────────────────
    echo "Error: ticket '$input' not found" >&2
    return 1
}

# _read_latest_preconditions <ticket_id_or_dir> [<gate_name> <session_id>]
# Reads PRECONDITIONS events. Supports two calling conventions:
#   1-arg  (ticket_dir):            full path to ticket event dir; returns latest event overall
#   3-arg  (ticket_id, gate, sess): derives dir from tracker; filters by composite key (LWW)
# Snapshot-aware: checks for *-PRECONDITIONS-SNAPSHOT.json first in both modes.
# Retry-once: sleeps 50ms and retries on transient ENOENT/OSError before giving up.
# Invariant: ticket_ids must never begin with '/' — the leading-slash heuristic dispatches
#   between 1-arg absolute-path mode and 3-arg ticket_id mode.
# Returns empty string and exits 0 when no matching events exist (3-arg mode only).
# Exits 1 when no events exist or on persistent error (1-arg mode).
_read_latest_preconditions() {
    local ticket_id="$1"
    local gate_name="${2:-}"
    local session_id="${3:-}"

    local ticket_dir
    if [[ "$ticket_id" == /* ]]; then
        # 1-arg form: full absolute path passed directly (used by compaction tests)
        ticket_dir="$ticket_id"
    else
        local repo_root=""
        if [[ -z "${TICKETS_TRACKER_DIR:-}" ]]; then
            repo_root="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel)"
        fi
        local tracker_dir_raw="${TICKETS_TRACKER_DIR:-$repo_root/.tickets-tracker}"
        local tracker_dir
        tracker_dir=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$tracker_dir_raw")
        ticket_dir="$tracker_dir/$ticket_id"
    fi

    # Retry-once on transient ENOENT (50ms sleep)
    local attempt=0
    while [ "$attempt" -lt 2 ]; do
        attempt=$((attempt + 1))

        if [ ! -d "$ticket_dir" ]; then
            if [ "$attempt" -lt 2 ]; then
                sleep 0.05  # retry-once: 50ms sleep on transient ENOENT
                continue
            fi
            # 1-arg callers (full path) expect exit 1 for nonexistent dir; 3-arg callers tolerate 0
            [[ "$ticket_id" == /* ]] && return 1 || return 0
        fi

        local _result
        local _exit=0
        _result=$(python3 -c "
import json, os, sys, tempfile

ticket_dir = sys.argv[1]
gate_name  = sys.argv[2]
session_id = sys.argv[3]
filter_by_key = bool(gate_name and session_id)
ticket_id  = os.path.basename(ticket_dir)

try:
    entries = os.listdir(ticket_dir)
except OSError:
    sys.exit(1 if not filter_by_key else 0)

# Check for snapshot first (snapshot-aware read)
snapshots = sorted(
    f for f in entries
    if f.endswith('-PRECONDITIONS-SNAPSHOT.json') and not f.endswith('.retired')
)
if snapshots:
    snap_path = os.path.join(ticket_dir, snapshots[-1])
    try:
        with open(snap_path, encoding='utf-8') as f:
            snap = json.load(f)
        snap_data = snap.get('data', snap)
        if not filter_by_key:
            # 1-arg mode: normalize to same contract as flat-event path and _api.py
            print(json.dumps({
                'status': 'present',
                'gate_verdicts': snap_data.get('gate_verdicts', {}),
                'manifest_depth': snap_data.get('manifest_depth', 0),
                'compacted': True,
            }))
            sys.exit(0)
        elif (snap_data.get('gate_name') == gate_name and
              snap_data.get('session_id') == session_id):
            # 3-arg mode: return raw snapshot data for the matching key
            print(json.dumps(snap_data))
            sys.exit(0)
    except (OSError, json.JSONDecodeError):
        pass  # fall through to flat events

# Collect all flat PRECONDITIONS files
candidates = []
for fname in entries:
    if not fname.endswith('-PRECONDITIONS.json'):
        continue
    if fname.endswith('-PRECONDITIONS-SNAPSHOT.json') or fname.endswith('.retired'):
        continue
    fpath = os.path.join(ticket_dir, fname)
    try:
        with open(fpath, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        continue
    if filter_by_key:
        if data.get('gate_name') == gate_name and data.get('session_id') == session_id:
            candidates.append((fname, fpath, data))
    else:
        candidates.append((fname, fpath, data))

if not candidates:
    # 1-arg mode (no filter): pre-manifest = no events → exit 1 (callers use || true)
    # 3-arg mode (with filter): no matching events is graceful → exit 0 (pre-manifest ticket)
    sys.exit(1 if not filter_by_key else 0)

# Lexicographic sort on filename (ISO8601 timestamp prefix = chronological order)
candidates.sort(key=lambda x: x[0])

if not filter_by_key:
    # 1-arg mode: LWW merge — collect all gate_verdicts and max manifest_depth
    merged_gv = {}
    manifest_depth = 0
    for fname, fpath, event in candidates:
        inner = event.get('data', event)
        merged_gv.update(inner.get('gate_verdicts', {}))
        # Also pick up individual gate verdict from gate_name field
        gn = inner.get('gate_name', '')
        if gn and 'verdict' in inner:
            merged_gv[gn] = inner['verdict']
        d = inner.get('manifest_depth', 0)
        if isinstance(d, int) and d > manifest_depth:
            manifest_depth = d
    print(json.dumps({'status': 'present', 'gate_verdicts': merged_gv, 'manifest_depth': manifest_depth}))
    sys.exit(0)

_, latest_path, latest_data = candidates[-1]

# Forward-compat: warn once per (ticket_id, schema_version) when schema_version is unknown (> 2)
schema_version = latest_data.get('schema_version', 1)
if isinstance(schema_version, int) and schema_version > 2:
    warn_dir = os.path.join(tempfile.gettempdir(), 'rebar-preconditions-warn')
    os.makedirs(warn_dir, exist_ok=True)
    warn_key = '{}_{}_v{}'.format(ticket_id, gate_name, schema_version)
    warn_file = os.path.join(warn_dir, warn_key)
    if not os.path.exists(warn_file):
        print(
            '[WARN] preconditions reader: unknown schema_version={} for ticket {} '
            '-- falling back to minimal-tier interpretation'.format(schema_version, ticket_id),
            file=sys.stderr
        )
        open(warn_file, 'w').close()

with open(latest_path, encoding='utf-8') as f:
    print(f.read(), end='')
" "$ticket_dir" "$gate_name" "$session_id") || _exit=$?

        if [ "$_exit" -ne 0 ]; then
            if [ "$attempt" -lt 2 ]; then
                sleep 0.05  # retry-once: 50ms sleep on transient read error
                continue
            fi
            [[ "$ticket_id" == /* ]] && return 1 || return 0
        fi

        echo "$_result"
        return 0
    done

    [[ "$ticket_id" == /* ]] && return 1 || return 0
}

# format_ticket_id <ticket_id> [mode]
# Formats a canonical ticket ID for display according to the configured display mode.
#
# Args:
#   ticket_id: the canonical 16-hex ticket directory name (e.g., abcd1234efgh5678)
#   mode:      optional — overrides ticket.display_mode config (auto|canonical|alias|short)
#
# Modes:
#   auto (default): cascade jira_key → alias → short → canonical; most human-friendly form
#   canonical:      returns the ID unchanged
#   alias:          reads data.alias from the ticket's CREATE event; falls back to canonical
#   short:          returns shortest unambiguous prefix >= 4 chars; falls back to canonical
#                   if no unique prefix exists at any length
#   <other>:        falls back to auto with a warning on stderr
#
# Config: reads ticket.display_mode from WORKFLOW_CONFIG_FILE.
# Honors TICKETS_TRACKER_DIR env var for tracker path.
format_ticket_id() {
    local ticket_id="$1"
    local mode="${2:-}"

    # ── Resolve display mode from config if not explicitly provided ──────────
    if [ -z "$mode" ]; then
        local _config_file
        if [ -n "${WORKFLOW_CONFIG_FILE:-}" ]; then
            _config_file="$WORKFLOW_CONFIG_FILE"
        else
            local _repo_root
            _repo_root="${REBAR_ROOT:-${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}}" || _repo_root=""
            if [ -n "${REBAR_CONFIG:-}" ] && [ -f "${REBAR_CONFIG}" ]; then
                _config_file="$REBAR_CONFIG"
            elif [ -n "$_repo_root" ] && [ -f "${_repo_root}/.rebar/config.conf" ]; then
                _config_file="${_repo_root}/.rebar/config.conf"
            else
                _config_file="${_repo_root}/.rebar.conf"
            fi
        fi
        mode=$(grep '^ticket\.display_mode=' "$_config_file" 2>/dev/null | cut -d= -f2- | head -1 || true)
        mode="${mode:-auto}"
    fi

    # ── Resolve tracker dir ───────────────────────────────────────────────────
    local _tracker_dir
    if [[ -n "${TICKETS_TRACKER_DIR:-}" ]]; then
        _tracker_dir="$TICKETS_TRACKER_DIR"
    else
        # Reuse _repo_root when already resolved above; otherwise call git once.
        local _rr="${_repo_root:-}"
        if [[ -z "$_rr" ]]; then
            _rr="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)" || _rr=""
        fi
        _tracker_dir="$_rr/.tickets-tracker"
    fi

    case "$mode" in
        auto)
            # Cascade: jira_key → alias → short → canonical
            local _ticket_dir2="$_tracker_dir/$ticket_id"
            if [ -d "$_ticket_dir2" ]; then
                local _create_file2 _jira_key2="" _alias2=""
                while IFS= read -r -d '' _create_file2; do
                    if [ -f "$_create_file2" ]; then
                        local _fields
                        _fields=$(python3 -c "
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
    data = ev.get('data', {})
    jira_key = data.get('jira_key', '') or ''
    alias = data.get('alias', '') or ''
    print(jira_key + '\t' + alias)
except Exception:
    print('\t')
" "$_create_file2" 2>/dev/null) || _fields="\t"
                        _jira_key2="${_fields%%$'\t'*}"
                        _alias2="${_fields#*$'\t'}"
                        if [ -n "$_jira_key2" ] || [ -n "$_alias2" ]; then
                            break
                        fi
                    fi
                done < <(find "$_ticket_dir2" -maxdepth 1 \( -name '*-CREATE.json' -o -name 'CREATE-*.json' \) -print0 2>/dev/null)
                [ -n "$_jira_key2" ] && { echo "$_jira_key2"; return 0; }
                [ -n "$_alias2" ] && { echo "$_alias2"; return 0; }
            fi
            # Try short prefix — collect all dir basenames once, then scan in bash
            # (avoids re-running find once per prefix-length iteration)
            local _nodash_a="${ticket_id//-/}"
            local _all_dirs_a=() _e_a
            while IFS= read -r -d '' _e_a; do
                local _bn_a; _bn_a=$(basename "$_e_a"); _bn_a="${_bn_a//-/}"
                _all_dirs_a+=("$_bn_a")
            done < <(find -L "$_tracker_dir" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -print0 2>/dev/null)
            local _prefix_len_a=4
            while [ "$_prefix_len_a" -le "${#_nodash_a}" ]; do
                local _candidate_a="${_nodash_a:0:$_prefix_len_a}"
                local _mc_a=0
                for _bn_a in "${_all_dirs_a[@]}"; do
                    [[ "$_bn_a" == "$_candidate_a"* ]] && _mc_a=$((_mc_a + 1))
                done
                if [ "$_mc_a" -eq 1 ]; then echo "$_candidate_a"; return 0; fi
                _prefix_len_a=$((_prefix_len_a + 1))
            done
            echo "$ticket_id"
            return 0
            ;;
        canonical)
            echo "$ticket_id"
            return 0
            ;;
        alias)
            # Scan for CREATE event in ticket directory; read data.alias field.
            local _ticket_dir="$_tracker_dir/$ticket_id"
            if [ -d "$_ticket_dir" ]; then
                local _alias=""
                local _create_file
                # Support both filename patterns: <ts>-<uuid>-CREATE.json and CREATE-<uuid>.json
                while IFS= read -r -d '' _create_file; do
                    if [ -f "$_create_file" ]; then
                        _alias=$(python3 -c "
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
    alias = ev.get('data', {}).get('alias', '') or ''
    print(alias)
except Exception:
    print('')
" "$_create_file" 2>/dev/null) || _alias=""
                        if [ -n "$_alias" ]; then
                            break
                        fi
                    fi
                done < <(find "$_ticket_dir" -maxdepth 1 \( -name '*-CREATE.json' -o -name 'CREATE-*.json' \) -print0 2>/dev/null)
                if [ -n "$_alias" ]; then
                    echo "$_alias"
                    return 0
                fi
            fi
            # Fall back to canonical when alias is absent (pre-migration ticket)
            echo "$ticket_id"
            return 0
            ;;
        short)
            # Find shortest unambiguous prefix of ticket_id (no dashes, minimum 4 chars).
            # The ticket_id may contain dashes (e.g., abcd-1234-efgh-5678); the prefix
            # scan strips dashes and works against the raw hex characters.
            local _nodash="${ticket_id//-/}"
            local _prefix_len=4
            local _found_prefix=""
            while [ "$_prefix_len" -le "${#_nodash}" ]; do
                local _candidate="${_nodash:0:$_prefix_len}"
                # Count how many ticket dirs start with this prefix (after stripping dashes)
                local _match_count=0
                local _entry
                while IFS= read -r -d '' _entry; do
                    local _base
                    # Bug 19a3-03ca: ${var##*/} param expansion — no basename subprocess per entry.
                    _base="${_entry##*/}"
                    local _base_nodash="${_base//-/}"
                    if [[ "$_base_nodash" == "$_candidate"* ]]; then
                        _match_count=$((_match_count + 1))
                    fi
                done < <(find -L "$_tracker_dir" -mindepth 1 -maxdepth 1 -type d \
                    ! -name '.*' -print0 2>/dev/null)
                if [ "$_match_count" -eq 1 ]; then
                    _found_prefix="$_candidate"
                    break
                fi
                _prefix_len=$((_prefix_len + 1))
            done
            if [ -n "$_found_prefix" ]; then
                echo "$_found_prefix"
                return 0
            fi
            # Fall back to canonical when no unique prefix exists
            echo "$ticket_id"
            return 0
            ;;
        *)
            # Unrecognized mode: warn and fall back to auto
            echo "WARN: unknown ticket.display_mode '$mode' — falling back to auto" >&2
            format_ticket_id "$ticket_id" "auto"
            return 0
            ;;
    esac
}

# ──────────────────────────────────────────────────────────────────────────────
# Scratch cleanup helper (invoked by ticket-transition.sh on close/delete)
# ──────────────────────────────────────────────────────────────────────────────

# _scratch_cleanup_for_ticket <ticket_id> [<base_dir>]
#
# Removes the per-ticket scratch directory for <ticket_id>.
#
# Args:
#   ticket_id : ticket identifier (e.g., abcd-1234-efgh-5678)
#   base_dir  : optional base directory that contains per-ticket scratch dirs
#               (defaults to SCRATCH_BASE_DIR if set, else REPO_ROOT/.claude/scratch)
#
# Behavior:
#   - If the scratch dir does not exist: logs INFO, returns 0 (idempotent).
#   - If the scratch dir exists: removes it with rm -rf; logs INFO with
#     ticket_id and path; returns 0.
#   - On rm failure (e.g., permission denied): logs WARN to stderr with
#     ticket_id and error; writes an orphan marker JSON to
#     ${SCRATCH_ORPHANS_DIR:-<tracker>/.scratch-orphans}/<ticket_id>
#     with keys {ticket_id, path, error, timestamp}; returns 0.
#
# This function ALWAYS returns 0 — cleanup failures are non-blocking.
_scratch_cleanup_for_ticket() {
    local ticket_id="${1:-}"
    local base_dir="${2:-${SCRATCH_BASE_DIR:-}}"

    # Validate ticket_id (reject empty, leading dot, path traversal, slashes,
    # control characters — mirrors _scratch_resolve_and_validate).
    if [ -z "$ticket_id" ]; then
        echo "[WARN] scratch-cleanup ticket_id must not be empty" >&2
        return 0
    fi
    case "$ticket_id" in
        .*)
            echo "[WARN] scratch-cleanup ticket_id must not start with a dot: $ticket_id" >&2
            return 0
            ;;
        *..*)
            echo "[WARN] scratch-cleanup ticket_id must not contain '..': $ticket_id" >&2
            return 0
            ;;
        */*)
            echo "[WARN] scratch-cleanup ticket_id must not contain '/': $ticket_id" >&2
            return 0
            ;;
    esac

    # Resolve base_dir
    if [ -z "$base_dir" ]; then
        local _rr
        _rr="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)" || _rr=""
        base_dir="${_rr}/.claude/scratch"
    fi

    local scratch_dir="${base_dir}/${ticket_id}"

    # If absent: no-op
    if [ ! -d "$scratch_dir" ]; then
        echo "[INFO] scratch-cleanup ticket=${ticket_id} path=${scratch_dir} result=absent" >&2
        return 0
    fi

    # Attempt rm -rf
    local rm_err
    rm_err=$(rm -rf -- "$scratch_dir" 2>&1)
    local rm_exit=$?

    if [ $rm_exit -eq 0 ]; then
        echo "[INFO] scratch-cleanup ticket=${ticket_id} path=${scratch_dir} result=removed" >&2
        return 0
    fi

    # rm failed — log WARN and write orphan marker
    echo "[WARN] scratch-cleanup ticket=${ticket_id} path=${scratch_dir} error=${rm_err}" >&2

    # Resolve orphan marker directory
    local orphan_dir="${SCRATCH_ORPHANS_DIR:-}"
    if [ -z "$orphan_dir" ]; then
        # Default: .tickets-tracker/.scratch-orphans under repo root
        local _rr2
        _rr2="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)" || _rr2=""
        orphan_dir="${_rr2}/.tickets-tracker/.scratch-orphans"
    fi

    # Write orphan marker JSON (non-blocking — ignore mkdir/write failures)
    mkdir -p "$orphan_dir" 2>/dev/null || true
    local orphan_file="${orphan_dir}/${ticket_id}"
    local timestamp
    timestamp=$(python3 -c "
import datetime
print(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
" 2>/dev/null || date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "unknown")
    python3 - "$ticket_id" "$scratch_dir" "$rm_err" "$timestamp" "$orphan_file" <<'PYEOF' 2>/dev/null || true
import json, sys
ticket_id, path, error, timestamp, orphan_file = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
marker = {"ticket_id": ticket_id, "path": path, "error": error, "timestamp": timestamp}
with open(orphan_file, 'w', encoding='utf-8') as f:
    json.dump(marker, f, ensure_ascii=False)
PYEOF

    return 0
}

# ──────────────────────────────────────────────────────────────────────────────
# Scratch helpers (private API — consumed by ticket-scratch-*.sh commands)
# ──────────────────────────────────────────────────────────────────────────────

# _scratch_resolve_and_validate <ticket_id> <key> [<base_dir>]
#
# Validates that <ticket_id> and <key> are safe filesystem components — no
# path traversal (`.`/`..`), no slashes, no control characters (0x00-0x1F),
# no null bytes, no leading dots — and resolves the target path.
#
# Args:
#   ticket_id : per-ticket namespace (e.g., abcd-1234-efgh-5678)
#   key       : scratch key name (arbitrary lowercase identifier)
#   base_dir  : optional base directory override (defaults to
#               $SCRATCH_BASE_DIR if set, else
#               REPO_ROOT/.claude/scratch/)
#
# On valid inputs:
#   Prints the resolved absolute path to stdout and exits 0.
#
# On invalid inputs:
#   Prints a JSON error envelope to stdout:
#     { "status": "error", "code": "invalid_id"|"invalid_key", "reason": "..." }
#   Exits non-zero.
_scratch_resolve_and_validate() {
    local ticket_id="$1"
    local key="$2"
    local base_dir="${3:-${SCRATCH_BASE_DIR:-}}"

    # Resolve base_dir when not explicitly provided
    if [ -z "$base_dir" ]; then
        local _rr
        _rr="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)" || _rr=""
        base_dir="${_rr}/.claude/scratch"
    fi

    # Delegate charset validation and path resolution to Python so the rules
    # are expressed clearly, testably, and without bash quoting landmines.
    python3 - "$ticket_id" "$key" "$base_dir" <<'PYEOF'
import json, os, re, sys

ticket_id = sys.argv[1]
key       = sys.argv[2]
base_dir  = sys.argv[3]

def _validate_component(value, field_name, code):
    """Reject empty, leading-dot, path-traversal, slash, or control-char values."""
    if not value:
        err = {"status": "error", "code": code,
               "reason": f"{field_name} must not be empty"}
        print(json.dumps(err))
        sys.exit(1)
    if value.startswith('.'):
        err = {"status": "error", "code": code,
               "reason": f"{field_name} must not start with a dot: {value!r}"}
        print(json.dumps(err))
        sys.exit(1)
    if '..' in value:
        err = {"status": "error", "code": code,
               "reason": f"{field_name} must not contain '..': {value!r}"}
        print(json.dumps(err))
        sys.exit(1)
    if '/' in value:
        err = {"status": "error", "code": code,
               "reason": f"{field_name} must not contain '/': {value!r}"}
        print(json.dumps(err))
        sys.exit(1)
    # Control characters: 0x00-0x1F (includes null byte)
    if re.search(r'[\x00-\x1f]', value):
        err = {"status": "error", "code": code,
               "reason": f"{field_name} must not contain control characters: {value!r}"}
        print(json.dumps(err))
        sys.exit(1)

_validate_component(ticket_id, "ticket_id", "invalid_id")
_validate_component(key, "key", "invalid_key")

abs_path = os.path.join(base_dir, ticket_id, key)
print(abs_path)
sys.exit(0)
PYEOF
}

# _scratch_atomic_write <abs_path> <payload> [<max_bytes>]
#
# Atomically writes <payload> to <abs_path> using a same-directory temporary
# file + fsync(file) + rename + fsync(parent) pattern.
#
# Enforces a byte ceiling (default: 98304 bytes / 96KB). Empirically sized
# against 200 closed+archived epics (P99 story-decomposer ~72KB, historical
# max ~91KB at epic dbbc-cf67 with 18 stories and 6 verbatim ~2,590-char SCs);
# 96KB is the next 8KB boundary above max+10% headroom. See bug 3e82 for the
# migration history (4096 → 32768 → 98304). On overflow, emits a structured
# JSON error to stdout and returns non-zero WITHOUT writing any file. The cap
# remains bounded so multi-MB payloads route via filesystem path (with a
# pointer in scratch) rather than inflating the tickets-tracker disk; the
# structural follow-up is restoring the original receipt-as-pointer pattern
# from epic 1d8b's design notes.
#
# Args:
#   abs_path  : absolute target file path
#   payload   : string content to write
#   max_bytes : optional override for the ceiling (default: 98304)
#
# On success:
#   Writes the file atomically; exits 0; no *.tmp.* siblings remain.
#
# On overflow:
#   Prints to stdout:
#     { "status": "error", "code": "oversize", "limit": N, "actual": M }
#   Exits non-zero; no file is created at abs_path.
_scratch_atomic_write() {
    local abs_path="$1"
    local payload="$2"
    local max_bytes="${3:-98304}"

    python3 - "$abs_path" "$payload" "$max_bytes" <<'PYEOF'
import json, os, sys, tempfile

abs_path  = sys.argv[1]
payload   = sys.argv[2]
max_bytes = int(sys.argv[3])

# Encode to bytes to get the true byte count
payload_bytes = payload.encode('utf-8')
actual = len(payload_bytes)

if actual > max_bytes:
    err = {"status": "error", "code": "oversize",
           "limit": max_bytes, "actual": actual}
    print(json.dumps(err))
    sys.exit(1)

# Ensure target directory exists
target_dir = os.path.dirname(abs_path)
os.makedirs(target_dir, exist_ok=True)

# Write to a same-directory temp file so rename is atomic (same filesystem)
fd, tmp_path = tempfile.mkstemp(
    dir=target_dir,
    prefix=os.path.basename(abs_path) + '.tmp.',
    suffix='.scratch'
)
try:
    os.write(fd, payload_bytes)
    os.fsync(fd)
    os.close(fd)
    os.rename(tmp_path, abs_path)
    # fsync the parent directory to flush the directory entry
    dir_fd = os.open(target_dir, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    except OSError:
        pass  # some filesystems (e.g. FAT) don't support dir fsync
    finally:
        os.close(dir_fd)
except Exception as e:
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    print(f"Error: atomic write failed: {e}", file=sys.stderr)
    sys.exit(2)

sys.exit(0)
PYEOF
}

# _scratch_read_envelope <abs_path>
#
# Reads the content of a scratch file and prints it to stdout.
# Returns non-zero if the file does not exist or is empty.
#
# Args:
#   abs_path : absolute path to the scratch file
#
# On success:
#   Prints file contents to stdout; exits 0.
#
# On missing or empty file:
#   Exits non-zero; nothing written to stdout.
_scratch_read_envelope() {
    local abs_path="$1"

    python3 - "$abs_path" <<'PYEOF'
import os, sys

abs_path = sys.argv[1]

if not os.path.isfile(abs_path):
    sys.exit(1)

try:
    with open(abs_path, 'r', encoding='utf-8') as f:
        content = f.read()
except OSError as e:
    print(f"Error: could not read {abs_path}: {e}", file=sys.stderr)
    sys.exit(1)

if not content:
    sys.exit(1)

print(content, end='')
sys.exit(0)
PYEOF
}
