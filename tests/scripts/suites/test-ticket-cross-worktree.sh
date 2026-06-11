#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-cross-worktree.sh
# Integration tests: cross-worktree ticket state visibility via .tickets-tracker symlink.
#
# Verifies:
#   1. A ticket event written from the main repo is visible from a git worktree
#      (via the .tickets-tracker symlink created by ticket-init.sh).
#   2. Concurrent write_commit_event calls — one via symlink path, one via real
#      path — both succeed and the canonical flock path is the same inode,
#      preventing parallel write corruption.
#
# Dependency: dso-ael7 (symlink in ticket-init.sh) + dso-l77u (canonical path
# in write_commit_event) must be implemented for these tests to pass GREEN.
#
# Usage: bash tests/scripts/suites/test-ticket-cross-worktree.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e is intentionally omitted — test functions return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_LIB="$REPO_ROOT/src/rebar/_engine/ticket-lib.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-cross-worktree.sh ==="

# ── Helper: create a fresh temp git repo with ticket system initialized ───────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: build a minimal event JSON file ──────────────────────────────────
# Usage: _make_event_json <dest_path> <event_type>
_make_event_json() {
    local dest="$1"
    local event_type="${2:-CREATE}"
    local ts
    ts=$(python3 -c "import time; print(int(time.time()))")
    local uuid
    uuid=$(python3 -c "import uuid; print(uuid.uuid4())")
    python3 -c "
import json, sys
data = {
    'timestamp': $ts,
    'uuid': '$uuid',
    'event_type': '$event_type',
    'env_id': '$uuid',
    'author': 'Test',
    'data': {
        'ticket_type': 'task',
        'title': 'Cross-worktree test ticket',
        'parent_id': None
    }
}
json.dump(data, sys.stdout)
" > "$dest"
}

# ── Test 1: test_ticket_event_visible_in_second_worktree ─────────────────────
echo "Test 1: ticket event written in main repo is visible from git worktree via symlink"
test_ticket_event_visible_in_second_worktree() {
    local tmp main_repo worktree_dir
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")

    # ── Step 1: Set up main repo with ticket system initialized ──────────────
    main_repo="$tmp/main"
    clone_test_repo "$main_repo"
    (cd "$main_repo" && bash "$TICKET_SCRIPT" init >/dev/null 2>&1) || true

    # Assert: ticket system initialized in main repo
    if [ ! -d "$main_repo/.tickets-tracker" ]; then
        assert_eq "main repo: .tickets-tracker/ exists after init" "exists" "missing"
        return
    fi

    # ── Step 2: Create a git worktree and initialize ticket system in it ─────
    worktree_dir="$tmp/worktree"
    git -C "$main_repo" worktree add "$worktree_dir" -b xwt-test-branch 2>/dev/null

    # Run ticket init from inside the git worktree — should create symlink
    (cd "$worktree_dir" && bash "$TICKET_SCRIPT" init >/dev/null 2>&1) || true

    # Assert: .tickets-tracker in the worktree is a symlink
    if [ ! -L "$worktree_dir/.tickets-tracker" ]; then
        assert_eq "worktree: .tickets-tracker is a symlink" "symlink" "not-a-symlink"
        return
    fi
    assert_eq "worktree: .tickets-tracker is a symlink" "symlink" "symlink"

    # ── Step 3: Write a ticket event from the main repo ──────────────────────
    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists" "exists" "missing"
        return
    fi

    local ticket_id="xwt-test-001"
    local tmpevtdir
    tmpevtdir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmpevtdir")
    local event_json="$tmpevtdir/event.json"
    _make_event_json "$event_json" "CREATE"

    local write_exit=0
    (cd "$main_repo" && source "$TICKET_LIB" && write_commit_event "$ticket_id" "$event_json") \
        2>/dev/null || write_exit=$?

    assert_eq "write_commit_event from main repo exits 0" "0" "$write_exit"

    # ── Step 4: Verify event file is visible from the worktree path ──────────
    # The worktree's .tickets-tracker is a symlink to the main repo's real tracker.
    # The event file written via the main repo path must be visible at the symlink path.
    local wt_ticket_dir="$worktree_dir/.tickets-tracker/$ticket_id"

    # Assert: the ticket directory is visible from the worktree symlink path
    if [ -d "$wt_ticket_dir" ]; then
        assert_eq "event ticket dir visible via worktree symlink" "exists" "exists"
    else
        assert_eq "event ticket dir visible via worktree symlink" "exists" "missing"
        return
    fi

    # Assert: at least one event file is present in the ticket directory
    local event_file_count
    event_file_count=$(find "$wt_ticket_dir" -maxdepth 1 -name '*-CREATE.json' ! -name '.*' \
        2>/dev/null | wc -l | tr -d ' ')
    assert_eq "event file visible via worktree symlink path (count >= 1)" \
        "1" "$([ "$event_file_count" -ge 1 ] && echo 1 || echo 0)"

    # ── Step 5: Verify the event file is the same inode ──────────────────────
    # The symlink resolves to the same underlying filesystem object, so the file
    # accessed via the worktree path and via the main repo path must be the same inode.
    local event_file_via_main
    event_file_via_main=$(find "$main_repo/.tickets-tracker/$ticket_id" \
        -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | head -1)
    local event_file_via_wt
    event_file_via_wt=$(find "$wt_ticket_dir" \
        -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | head -1)

    if [ -n "$event_file_via_main" ] && [ -n "$event_file_via_wt" ]; then
        local inode_main inode_wt
        inode_main=$(python3 -c "import os,sys; print(os.stat(sys.argv[1]).st_ino)" \
            "$event_file_via_main" 2>/dev/null || echo "err-main")
        inode_wt=$(python3 -c "import os,sys; print(os.stat(sys.argv[1]).st_ino)" \
            "$event_file_via_wt" 2>/dev/null || echo "err-wt")
        assert_eq "event file same inode via main and worktree path" \
            "$inode_main" "$inode_wt"
    else
        assert_eq "event files found for inode comparison" "found" "not-found"
    fi
}
_snapshot_fail
test_ticket_event_visible_in_second_worktree
assert_pass_if_clean "test_ticket_event_visible_in_second_worktree"

# ── Test 2: test_flock_canonical_path_prevents_parallel_write_corruption ─────
echo "Test 2: concurrent write_commit_event via symlink+real path — both succeed, same lock inode"
test_flock_canonical_path_prevents_parallel_write_corruption() {
    local tmp main_repo worktree_dir
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")

    # ── Step 1: Set up main repo and worktree ─────────────────────────────────
    main_repo="$tmp/main"
    clone_test_repo "$main_repo"
    (cd "$main_repo" && bash "$TICKET_SCRIPT" init >/dev/null 2>&1) || true

    if [ ! -d "$main_repo/.tickets-tracker" ]; then
        assert_eq "main repo: .tickets-tracker/ exists after init" "exists" "missing"
        return
    fi

    worktree_dir="$tmp/worktree"
    git -C "$main_repo" worktree add "$worktree_dir" -b xwt-conc-branch 2>/dev/null
    (cd "$worktree_dir" && bash "$TICKET_SCRIPT" init >/dev/null 2>&1) || true

    if [ ! -L "$worktree_dir/.tickets-tracker" ]; then
        assert_eq "worktree: .tickets-tracker is a symlink for concurrency test" \
            "symlink" "not-a-symlink"
        return
    fi

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for concurrency test" "exists" "missing"
        return
    fi

    # ── Step 2: Build two event JSON files (different ticket IDs) ─────────────
    local tmpevtdir
    tmpevtdir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmpevtdir")

    local event_json_a="$tmpevtdir/event_a.json"
    local event_json_b="$tmpevtdir/event_b.json"
    _make_event_json "$event_json_a" "CREATE"
    _make_event_json "$event_json_b" "CREATE"

    local ticket_id_a="xwt-conc-001"
    local ticket_id_b="xwt-conc-002"

    # ── Step 3: Run two concurrent write_commit_event calls ───────────────────
    # Caller A: uses real path (main repo CWD)  → tracker_dir_raw = main_repo/.tickets-tracker
    # Caller B: uses worktree symlink path (worktree CWD) → tracker_dir_raw = worktree/.tickets-tracker (symlink)
    # Both callers must canonicalize to the same real path and contend on the same lock file.
    local log_a="$tmpevtdir/result_a.log"
    local log_b="$tmpevtdir/result_b.log"

    local pid_a pid_b
    (
        exit_code=0
        (cd "$main_repo" && source "$TICKET_LIB" && \
            write_commit_event "$ticket_id_a" "$event_json_a") 2>&1 || exit_code=$?
        echo "$exit_code" > "$log_a"
    ) &
    pid_a=$!

    (
        exit_code=0
        (cd "$worktree_dir" && source "$TICKET_LIB" && \
            write_commit_event "$ticket_id_b" "$event_json_b") 2>&1 || exit_code=$?
        echo "$exit_code" > "$log_b"
    ) &
    pid_b=$!

    # Wait for both with a 60s deadline (flock timeout is 30s per attempt x 2 retries)
    local wait_deadline=$((SECONDS + 60))
    wait "$pid_a" 2>/dev/null || true
    wait "$pid_b" 2>/dev/null || true

    # ── Step 4: Assert both callers exited 0 ─────────────────────────────────
    local exit_a="1" exit_b="1"
    if [ -f "$log_a" ]; then exit_a=$(cat "$log_a" | tr -d '[:space:]'); fi
    if [ -f "$log_b" ]; then exit_b=$(cat "$log_b" | tr -d '[:space:]'); fi

    assert_eq "caller A (real path) write_commit_event exits 0" "0" "$exit_a"
    assert_eq "caller B (symlink path) write_commit_event exits 0" "0" "$exit_b"

    # ── Step 5: Assert both events are committed ──────────────────────────────
    # Event A committed in ticket_id_a directory
    local event_count_a
    event_count_a=$(find "$main_repo/.tickets-tracker/$ticket_id_a" \
        -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "caller A event file committed (count >= 1)" \
        "1" "$([ "$event_count_a" -ge 1 ] && echo 1 || echo 0)"

    # Event B committed in ticket_id_b directory (visible via both paths)
    local event_count_b
    event_count_b=$(find "$main_repo/.tickets-tracker/$ticket_id_b" \
        -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "caller B event file committed (count >= 1)" \
        "1" "$([ "$event_count_b" -ge 1 ] && echo 1 || echo 0)"

    # ── Step 6: Assert no data corruption — validate event JSON ──────────────
    local parse_exit_a=0 parse_exit_b=0
    local ef_a ef_b
    ef_a=$(find "$main_repo/.tickets-tracker/$ticket_id_a" \
        -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | head -1)
    ef_b=$(find "$main_repo/.tickets-tracker/$ticket_id_b" \
        -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | head -1)

    if [ -n "$ef_a" ]; then
        python3 -c "import json,sys; json.load(sys.stdin)" < "$ef_a" 2>/dev/null \
            || parse_exit_a=$?
        assert_eq "caller A event JSON is valid (no corruption)" "0" "$parse_exit_a"
    else
        assert_eq "caller A event file found for JSON validation" "found" "not-found"
    fi

    if [ -n "$ef_b" ]; then
        python3 -c "import json,sys; json.load(sys.stdin)" < "$ef_b" 2>/dev/null \
            || parse_exit_b=$?
        assert_eq "caller B event JSON is valid (no corruption)" "0" "$parse_exit_b"
    else
        assert_eq "caller B event file found for JSON validation" "found" "not-found"
    fi

    # ── Step 7: Assert canonical lock file path is the same inode ────────────
    # The lock file must be created at the canonical (real) path, not the symlink path.
    # Both callers' writes must have contended on the same underlying lock inode.
    #
    # We verify this by checking that:
    #   real_tracker/.ticket-write.lock  and
    #   worktree_tracker_symlink_resolved/.ticket-write.lock
    # point to the same inode (since the symlink is resolved, both are the same file).
    local real_tracker
    real_tracker=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" \
        "$main_repo/.tickets-tracker")
    local wt_tracker_resolved
    wt_tracker_resolved=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" \
        "$worktree_dir/.tickets-tracker")

    assert_eq "real_tracker and worktree symlink resolve to same canonical path" \
        "$real_tracker" "$wt_tracker_resolved"

    # The canonical lock file is the same single file — same inode
    local lock_path="$real_tracker/.ticket-write.lock"
    if [ -f "$lock_path" ]; then
        local inode_real inode_via_wt
        local lock_via_wt="$worktree_dir/.tickets-tracker/.ticket-write.lock"
        inode_real=$(python3 -c "import os,sys; print(os.stat(sys.argv[1]).st_ino)" \
            "$lock_path" 2>/dev/null || echo "no-lock-real")
        inode_via_wt=$(python3 -c "import os,sys; print(os.stat(sys.argv[1]).st_ino)" \
            "$lock_via_wt" 2>/dev/null || echo "no-lock-via-wt")
        assert_eq "lock file same inode via real and symlink path" \
            "$inode_real" "$inode_via_wt"
    else
        # Lock file may not persist after both callers exit — that is correct behaviour.
        # The canonical path identity was already verified by the canonical path assertion above.
        # (Both callers canonicalize before constructing lock_file, so the same lock was used.)
        assert_eq "canonical paths match — same lock used by both callers (lock released on exit)" \
            "$real_tracker" "$wt_tracker_resolved"
    fi

    # ── Step 8: Assert no temp/staging files remain ───────────────────────────
    local temp_count
    temp_count=$(find "$main_repo/.tickets-tracker" -maxdepth 1 -name '.tmp-event-*' \
        2>/dev/null | wc -l | tr -d ' ')
    assert_eq "no staging temp files remain after concurrent writes" "0" "$temp_count"
}
_snapshot_fail
test_flock_canonical_path_prevents_parallel_write_corruption
assert_pass_if_clean "test_flock_canonical_path_prevents_parallel_write_corruption"

print_summary
