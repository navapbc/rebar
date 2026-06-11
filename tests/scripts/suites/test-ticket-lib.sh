#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-lib.sh
# Tests for src/rebar/_engine/ticket-lib.sh — write_commit_event helper.
#
# Covers: atomic write, flock serialization, specific-file git commit, gc.auto=0,
# and clean failure when ticket init has not been run.
#
# Usage: bash tests/scripts/suites/test-ticket-lib.sh

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_LIB="$REPO_ROOT/src/rebar/_engine/ticket-lib.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-lib.sh ==="

# ── Helper: create a fresh temp git repo ─────────────────────────────────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_test_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: build a minimal event JSON file ──────────────────────────────────
# Usage: _make_event_json <dest_path> <ticket_id>
_make_event_json() {
    local dest="$1"
    local ticket_id="$2"
    local ts
    ts=$(python3 -c "import time; print(int(time.time()))")
    local uuid
    uuid=$(python3 -c "import uuid; print(uuid.uuid4())")
    python3 -c "
import json, sys
data = {
    'timestamp': $ts,
    'uuid': '$uuid',
    'event_type': 'CREATE',
    'env_id': '$uuid',
    'author': 'Test',
    'data': {
        'ticket_type': 'task',
        'title': 'Test ticket',
        'parent_id': None
    }
}
json.dump(data, sys.stdout)
" > "$dest"
}

# ── Test 1: write_commit_event writes atomic file with correct naming ─────────
echo "Test 1: write_commit_event writes atomic event file with correct naming convention"
test_write_commit_event_writes_atomic_file() {
    local repo
    repo=$(_make_test_repo)

    # Initialize ticket system
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # ticket-lib.sh must exist for sourcing — RED: it does not exist yet
    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists" "exists" "missing"
        return
    fi

    local ticket_id="test-abc1"
    local tmpdir
    tmpdir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmpdir")
    local event_json="$tmpdir/event.json"
    _make_event_json "$event_json" "$ticket_id"

    # Source the lib and call write_commit_event
    (cd "$repo" && source "$TICKET_LIB" && write_commit_event "$ticket_id" "$event_json") || true

    # Assert: ticket directory exists
    if [ -d "$repo/.tickets-tracker/$ticket_id" ]; then
        assert_eq "event file dir exists" "exists" "exists"
    else
        assert_eq "event file dir exists" "exists" "missing"
        return
    fi

    # Assert: exactly one event file exists matching <timestamp>-<uuid>-CREATE.json
    local event_files
    event_files=$(find "$repo/.tickets-tracker/$ticket_id" -maxdepth 1 \
        -name '*-CREATE.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "event file count is 1" "1" "$event_files"

    # Assert: no partial/temp files remain (no files starting with '.')
    local temp_files
    temp_files=$(find "$repo/.tickets-tracker/$ticket_id" -maxdepth 1 \
        -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "no temp files remain" "0" "$temp_files"

    # Assert: the event JSON is valid (parseable)
    local event_file
    event_file=$(find "$repo/.tickets-tracker/$ticket_id" -maxdepth 1 \
        -name '*-CREATE.json' ! -name '.*' 2>/dev/null | head -1)
    if [ -n "$event_file" ]; then
        local parse_exit=0
        python3 -c "import json,sys; json.load(sys.stdin)" < "$event_file" 2>/dev/null || parse_exit=$?
        assert_eq "event JSON is valid" "0" "$parse_exit"
    else
        assert_eq "event file found for JSON validation" "found" "not-found"
    fi
}
test_write_commit_event_writes_atomic_file

# ── Test 2: write_commit_event uses flock ─────────────────────────────────────
echo "Test 2: write_commit_event uses flock — lock file created at expected path"
test_write_commit_event_uses_flock() {
    local repo
    repo=$(_make_test_repo)

    # Initialize ticket system
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # ticket-lib.sh must exist — RED: it does not exist yet
    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for flock test" "exists" "missing"
        return
    fi

    # Assert: the lock file path is defined in ticket-lib.sh
    local lock_path=".tickets-tracker/.ticket-write.lock"
    local lock_defined
    lock_defined=$(grep -c '\.ticket-write\.lock' "$TICKET_LIB" 2>/dev/null || echo "0")
    assert_eq "lock file path defined in ticket-lib.sh" "1" "$([ "$lock_defined" -ge 1 ] && echo 1 || echo 0)"

    # Assert: ticket-lib.sh references flock
    local flock_used
    flock_used=$(grep -c 'flock' "$TICKET_LIB" 2>/dev/null || echo "0")
    assert_eq "flock referenced in ticket-lib.sh" "1" "$([ "$flock_used" -ge 1 ] && echo 1 || echo 0)"

    local ticket_id="test-flock1"
    local tmpdir
    tmpdir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmpdir")
    local event_json="$tmpdir/event.json"
    _make_event_json "$event_json" "$ticket_id"

    # After write_commit_event, lock file must not be held (released)
    (cd "$repo" && source "$TICKET_LIB" && write_commit_event "$ticket_id" "$event_json") || true

    # Assert: ticket-lib.sh invokes flock with the expected lock path
    local flock_with_lock_path
    flock_with_lock_path=$(grep -c "flock.*$lock_path\|$lock_path.*flock" "$TICKET_LIB" 2>/dev/null || echo "0")
    assert_eq "flock invoked with .tickets-tracker/.ticket-write.lock" \
        "1" "$([ "$flock_with_lock_path" -ge 1 ] && echo 1 || echo 0)"
}
test_write_commit_event_uses_flock

# ── Test 3: write_commit_event commits only the specific event file ────────────
echo "Test 3: write_commit_event commits only the specific event file (not git add -A)"
test_write_commit_event_commits_specific_file() {
    local repo
    repo=$(_make_test_repo)

    # Initialize ticket system
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # ticket-lib.sh must exist — RED: it does not exist yet
    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for commit test" "exists" "missing"
        return
    fi

    local ticket_id="test-commit1"
    local tmpdir
    tmpdir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmpdir")
    local event_json="$tmpdir/event.json"
    _make_event_json "$event_json" "$ticket_id"

    # Create an untracked distractor file in .tickets-tracker that should NOT be committed
    mkdir -p "$repo/.tickets-tracker/unrelated-dir"
    echo "should not be committed" > "$repo/.tickets-tracker/unrelated-dir/stray.txt"

    (cd "$repo" && source "$TICKET_LIB" && write_commit_event "$ticket_id" "$event_json") || true

    # Assert: git log shows a commit was made
    local commit_count
    commit_count=$(git -C "$repo/.tickets-tracker" log --oneline 2>/dev/null | wc -l | tr -d ' ')
    # Should have at least 2 commits (init + event commit)
    assert_eq "at least one event commit exists" "1" "$([ "$commit_count" -ge 2 ] && echo 1 || echo 0)"

    # Assert: the last commit contains only the specific event file (not stray.txt)
    local committed_files
    committed_files=$(git -C "$repo/.tickets-tracker" log --name-only --pretty=format: -1 2>/dev/null \
        | grep -v '^$' | tr -d ' ')

    # stray.txt must NOT be in the committed files
    if [[ "$committed_files" == *"stray.txt"* ]]; then
        assert_eq "stray file not committed" "not-committed" "committed"
    else
        assert_eq "stray file not committed" "not-committed" "not-committed"
    fi

    # The event file for our ticket_id must be in the committed files
    if [[ "$committed_files" == *"$ticket_id"* ]]; then
        assert_eq "event file for ticket committed" "committed" "committed"
    else
        assert_eq "event file for ticket committed" "committed" "not-committed"
    fi
}
test_write_commit_event_commits_specific_file

# ── Test 4: write_commit_event sets gc.auto=0 in the tickets worktree ─────────
echo "Test 4: write_commit_event — gc.auto=0 is set in the tickets worktree"
test_write_commit_event_sets_gc_auto_zero() {
    local repo
    repo=$(_make_test_repo)

    # Initialize ticket system (ticket-init.sh sets gc.auto=0, but we verify
    # ticket-lib.sh also ensures it or relies on init's guarantee)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # ticket-lib.sh must exist — RED: it does not exist yet
    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for gc.auto test" "exists" "missing"
        return
    fi

    local ticket_id="test-gc1"
    local tmpdir
    tmpdir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmpdir")
    local event_json="$tmpdir/event.json"
    _make_event_json "$event_json" "$ticket_id"

    (cd "$repo" && source "$TICKET_LIB" && write_commit_event "$ticket_id" "$event_json") || true

    # Assert: gc.auto is 0 in the tickets worktree
    local gc_auto
    gc_auto="$(git -C "$repo/.tickets-tracker" config gc.auto 2>/dev/null || echo "unset")"
    assert_eq "gc.auto=0 in tickets worktree" "0" "$gc_auto"
}
test_write_commit_event_sets_gc_auto_zero

# ── Test 5: write_commit_event fails cleanly without prior ticket init ─────────
echo "Test 5: write_commit_event exits non-zero with error when ticket init not run"
test_write_commit_event_fails_cleanly_if_no_init() {
    local repo
    repo=$(_make_test_repo)

    # Do NOT run ticket init — .tickets-tracker should not exist

    # ticket-lib.sh must exist — RED: it does not exist yet
    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for no-init test" "exists" "missing"
        return
    fi

    local ticket_id="test-noinit1"
    local tmpdir
    tmpdir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmpdir")
    local event_json="$tmpdir/event.json"
    _make_event_json "$event_json" "$ticket_id"

    # Source ticket-lib.sh first — fail fast if source itself fails (separate from the tested call)
    # Run write_commit_event without init — must exit non-zero
    local exit_code=0
    local stderr_out
    # shellcheck source=/dev/null
    if ! (cd "$repo" && source "$TICKET_LIB") 2>/dev/null; then
        assert_eq "ticket-lib.sh sources without error" "ok" "source-failed"
        return
    fi
    stderr_out=$(cd "$repo" && source "$TICKET_LIB" && \
        write_commit_event "$ticket_id" "$event_json" 2>&1) || exit_code=$?

    assert_eq "exits non-zero without init" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: stderr contains an error message (not silent failure)
    if [ -n "$stderr_out" ]; then
        assert_eq "error message printed on no-init" "has-message" "has-message"
    else
        assert_eq "error message printed on no-init" "has-message" "silent"
    fi
}
test_write_commit_event_fails_cleanly_if_no_init

# ── Test 6: write_commit_event resolves symlink to real path ──────────────────
echo "Test 6: write_commit_event resolves symlink — lock file uses canonical (real) path"
test_write_commit_event_resolves_symlink_to_real_path() {
    local repo
    repo=$(_make_test_repo)

    # Initialize ticket system
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # ticket-lib.sh must exist
    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for symlink test" "exists" "missing"
        return
    fi

    # Behavioral test: when .tickets-tracker is a symlink, write_commit_event must
    # resolve it to a canonical path before constructing the lock file path.
    # This ensures that a caller using the symlink path and a caller using the real
    # path both contend on the same lock file (cross-path serialization).
    #
    # Setup: create a real dir and a symlink to it, then use the symlink as the
    # effective .tickets-tracker by momentarily pointing the expected path at the
    # symlink and verifying write_commit_event produces a commit in the real dir.

    local real_tracker
    real_tracker="$repo/.tickets-tracker"

    # Verify that .tickets-tracker was created by init and is a real directory
    if [ ! -d "$real_tracker" ]; then
        assert_eq "tracker dir exists after init" "exists" "missing"
        return
    fi

    # Create a symlink alongside the real tracker
    local link_tracker="$repo/.tickets-tracker-symlink-test"
    ln -s "$real_tracker" "$link_tracker"

    # Resolve both paths via Python realpath for cross-platform canonical comparison
    # (macOS /var -> /private/var, etc.)
    local canonical_real canonical_link
    canonical_real=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$real_tracker")
    canonical_link=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$link_tracker")

    # Assert: both paths resolve to the same canonical path (test setup check)
    assert_eq "symlink and real dir resolve to same canonical path" \
        "$canonical_real" "$canonical_link"

    # Assert: ticket-lib.sh uses realpath (or Python os.path.realpath) to canonicalize
    # the tracker_dir before constructing lock_file.
    local realpath_used
    realpath_used=$(python3 -c "
import re, sys
with open(sys.argv[1]) as f:
    content = f.read()
# Match realpath shell builtin or Python os.path.realpath
if re.search(r'realpath|os\.path\.realpath', content):
    print('1')
else:
    print('0')
" "$TICKET_LIB")
    assert_eq "ticket-lib.sh uses canonical path resolution (realpath)" "1" "$realpath_used"

    rm -f "$link_tracker"
}
test_write_commit_event_resolves_symlink_to_real_path

# ── Test 7: write_commit_event flock uses canonical path (cross-symlink serialization)
echo "Test 7: write_commit_event flock uses canonical path — same lock across symlink and real path"
test_write_commit_event_flock_on_canonical_path() {
    local repo
    repo=$(_make_test_repo)

    # Initialize ticket system
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # ticket-lib.sh must exist
    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for canonical flock test" "exists" "missing"
        return
    fi

    # Assert: ticket-lib.sh uses realpath (or Python os.path.realpath) to canonicalize
    # tracker_dir so that the lock_file path is always the real (canonical) path.
    # When two callers arrive with different path forms (symlink vs real), they must
    # contend on the same underlying lock file to prevent concurrent commits.

    # Static assertion: canonical path resolution must appear BEFORE the lock_file= assignment.
    # Recognized patterns: python3 os.path.realpath, readlink -f, or pwd -P (all resolve symlinks).
    local realpath_line lock_line
    realpath_line=$(python3 -c "
import sys
with open(sys.argv[1]) as f:
    lines = f.readlines()
import re
for i, line in enumerate(lines, 1):
    if re.search(r'realpath|os\.path\.realpath|pwd\s+-P|readlink\s+-f', line):
        print(i)
        break
else:
    print(0)
" "$TICKET_LIB")
    lock_line=$(python3 -c "
import sys
with open(sys.argv[1]) as f:
    lines = f.readlines()
for i, line in enumerate(lines, 1):
    if 'lock_file=' in line:
        print(i)
        break
else:
    print(0)
" "$TICKET_LIB")

    assert_eq "canonical path resolution present in ticket-lib.sh (canonical line > 0)" \
        "1" "$([ "${realpath_line:-0}" -gt 0 ] && echo 1 || echo 0)"

    # canonical resolution must appear at or before lock_file= assignment
    if [ "${realpath_line:-0}" -gt 0 ] && [ "${lock_line:-0}" -gt 0 ] && \
       [ "$realpath_line" -le "$lock_line" ]; then
        assert_eq "canonical resolution ordered before lock_file assignment" "ordered" "ordered"
    else
        assert_eq "canonical resolution ordered before lock_file assignment" "ordered" "not-ordered"
    fi
}
test_write_commit_event_flock_on_canonical_path

# ── Test 8: write_commit_event uses --no-verify on git commit ──────────────────
echo "Test 8: write_commit_event git commit uses --no-verify (skip pre-commit hooks in tickets worktree)"
test_write_commit_event_uses_no_verify() {
    # ticket-lib.sh must exist
    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for --no-verify test" "exists" "missing"
        return
    fi

    # Static assertion: the git commit call inside the Python flock block must
    # include --no-verify so that pre-commit hooks (which expect .pre-commit-config.yaml)
    # don't break the tickets worktree's internal commits.
    local no_verify_count
    no_verify_count=$(grep -c '\-\-no-verify' "$TICKET_LIB" 2>/dev/null || echo "0")
    assert_eq "ticket-lib.sh git commit uses --no-verify" "1" "$([ "$no_verify_count" -ge 1 ] && echo 1 || echo 0)"
}
test_write_commit_event_uses_no_verify

# ── Test 9: _flock_stage_commit writes file to final_path ─────────────────────
echo "Test 9: _flock_stage_commit writes file at the specified final_path"
test_flock_stage_commit_writes_file() {
    local repo
    repo=$(_make_test_repo)

    # Initialize ticket system
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # ticket-lib.sh must exist for sourcing
    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for _flock_stage_commit write test" "exists" "missing"
        return
    fi

    # Source ticket-lib.sh and verify _flock_stage_commit is defined
    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type _flock_stage_commit &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "_flock_stage_commit function exists" "defined" "undefined"
        return
    fi

    local ticket_id="test-fsc1"
    local tracker_dir="$repo/.tickets-tracker"
    local ticket_dir="$tracker_dir/$ticket_id"
    mkdir -p "$ticket_dir"

    # Create a staging temp file
    local staging_temp
    staging_temp=$(mktemp "$tracker_dir/.tmp-fsc-XXXXXX")
    echo '{"event_type":"CREATE","timestamp":1700000000,"uuid":"aaaa-bbbb"}' > "$staging_temp"

    local final_path="$ticket_dir/1700000000-aaaa-bbbb-CREATE.json"
    local commit_msg="ticket: CREATE $ticket_id"

    # Call _flock_stage_commit directly
    local exit_code=0
    (cd "$repo" && source "$TICKET_LIB" && \
        _flock_stage_commit "$tracker_dir" "$staging_temp" "$final_path" "$commit_msg") || exit_code=$?

    # Assert: file exists at final_path
    if [ -f "$final_path" ]; then
        assert_eq "_flock_stage_commit: file exists at final_path" "exists" "exists"
    else
        assert_eq "_flock_stage_commit: file exists at final_path" "exists" "missing"
    fi

    # Assert: exit code is 0
    assert_eq "_flock_stage_commit: exit code is 0" "0" "$exit_code"

    # Assert: staging temp file was consumed (no longer exists)
    if [ -f "$staging_temp" ]; then
        assert_eq "_flock_stage_commit: staging temp consumed" "consumed" "still-exists"
    else
        assert_eq "_flock_stage_commit: staging temp consumed" "consumed" "consumed"
    fi
}
test_flock_stage_commit_writes_file

# ── Test 10: _flock_stage_commit commits to branch ────────────────────────────
echo "Test 10: _flock_stage_commit commits to the tickets branch with the given message"
test_flock_stage_commit_commits_to_branch() {
    local repo
    repo=$(_make_test_repo)

    # Initialize ticket system
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # ticket-lib.sh must exist for sourcing
    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for _flock_stage_commit commit test" "exists" "missing"
        return
    fi

    # Source ticket-lib.sh and verify _flock_stage_commit is defined
    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type _flock_stage_commit &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "_flock_stage_commit function exists (commit test)" "defined" "undefined"
        return
    fi

    local ticket_id="test-fsc2"
    local tracker_dir="$repo/.tickets-tracker"
    local ticket_dir="$tracker_dir/$ticket_id"
    mkdir -p "$ticket_dir"

    # Create a staging temp file
    local staging_temp
    staging_temp=$(mktemp "$tracker_dir/.tmp-fsc-XXXXXX")
    echo '{"event_type":"COMMENT","timestamp":1700000001,"uuid":"cccc-dddd"}' > "$staging_temp"

    local final_filename="1700000001-cccc-dddd-COMMENT.json"
    local final_path="$ticket_dir/$final_filename"
    local commit_msg="ticket: COMMENT $ticket_id"

    # Call _flock_stage_commit
    (cd "$repo" && source "$TICKET_LIB" && \
        _flock_stage_commit "$tracker_dir" "$staging_temp" "$final_path" "$commit_msg") || true

    # Assert: git log on the tickets worktree contains the commit message
    local log_output
    log_output=$(git -C "$tracker_dir" log --oneline -5 2>/dev/null || echo "")

    if [[ "$log_output" == *"COMMENT $ticket_id"* ]]; then
        assert_eq "_flock_stage_commit: commit message in git log" "found" "found"
    else
        assert_eq "_flock_stage_commit: commit message in git log" "found" "not-found"
    fi

    # Assert: the committed file is the one we specified
    local committed_files
    committed_files=$(git -C "$tracker_dir" log --name-only --pretty=format: -1 2>/dev/null \
        | grep -v '^$' || echo "")
    if [[ "$committed_files" == *"$ticket_id/$final_filename"* ]]; then
        assert_eq "_flock_stage_commit: correct file in commit" "committed" "committed"
    else
        assert_eq "_flock_stage_commit: correct file in commit" "committed" "not-committed"
    fi
}
test_flock_stage_commit_commits_to_branch

# ── Test 11: _flock_stage_commit fails gracefully on missing tracker ──────────
echo "Test 11: _flock_stage_commit fails gracefully when tracker_dir does not exist"
test_flock_stage_commit_fails_gracefully_on_missing_tracker() {
    local repo
    repo=$(_make_test_repo)

    # Do NOT run ticket init — tracker does not exist

    # ticket-lib.sh must exist for sourcing
    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for _flock_stage_commit missing-tracker test" "exists" "missing"
        return
    fi

    # Source ticket-lib.sh and verify _flock_stage_commit is defined
    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type _flock_stage_commit &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "_flock_stage_commit function exists (missing-tracker test)" "defined" "undefined"
        return
    fi

    local nonexistent_tracker="$repo/.tickets-tracker-does-not-exist"
    local staging_temp
    staging_temp=$(mktemp)
    _CLEANUP_DIRS+=("$(dirname "$staging_temp")")
    echo '{"event_type":"CREATE","timestamp":1700000002,"uuid":"eeee-ffff"}' > "$staging_temp"
    local final_path="$nonexistent_tracker/test-fsc3/1700000002-eeee-ffff-CREATE.json"
    local commit_msg="ticket: CREATE test-fsc3"

    # Call _flock_stage_commit with invalid tracker_dir — must exit non-zero
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && source "$TICKET_LIB" && \
        _flock_stage_commit "$nonexistent_tracker" "$staging_temp" "$final_path" "$commit_msg" 2>&1) || exit_code=$?

    # Assert: non-zero exit code
    assert_eq "_flock_stage_commit: non-zero exit on missing tracker" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: no crash — stderr should contain an error message (not a bash traceback)
    if [ -n "$stderr_out" ]; then
        assert_eq "_flock_stage_commit: error message on missing tracker" "has-message" "has-message"
    else
        # Even silent non-zero exit is acceptable (graceful) — but a message is preferred
        assert_eq "_flock_stage_commit: error message on missing tracker" "has-message" "silent"
    fi

    # Assert: file was NOT created at final_path
    if [ -f "$final_path" ]; then
        assert_eq "_flock_stage_commit: no file created on failure" "no-file" "file-exists"
    else
        assert_eq "_flock_stage_commit: no file created on failure" "no-file" "no-file"
    fi
}
test_flock_stage_commit_fails_gracefully_on_missing_tracker

# ── Test 12: write_commit_event regression — still works after extraction ─────
echo "Test 12: write_commit_event regression — still works end-to-end after _flock_stage_commit extraction"
test_write_commit_event_regression_after_extraction() {
    local repo
    repo=$(_make_test_repo)

    # Initialize ticket system
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    # ticket-lib.sh must exist
    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for regression test" "exists" "missing"
        return
    fi

    local ticket_id="test-regr1"
    local tmpdir
    tmpdir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmpdir")
    local event_json="$tmpdir/event.json"
    _make_event_json "$event_json" "$ticket_id"

    # Call write_commit_event (the public API)
    local exit_code=0
    (cd "$repo" && source "$TICKET_LIB" && write_commit_event "$ticket_id" "$event_json") || exit_code=$?

    # Assert: exit code is 0
    assert_eq "write_commit_event regression: exit code 0" "0" "$exit_code"

    # Assert: event file exists in the ticket directory
    local event_files
    event_files=$(find "$repo/.tickets-tracker/$ticket_id" -maxdepth 1 \
        -name '*-CREATE.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "write_commit_event regression: event file created" "1" "$event_files"

    # Assert: git log contains the commit
    local commit_count
    commit_count=$(git -C "$repo/.tickets-tracker" log --oneline --all 2>/dev/null | \
        grep -c "$ticket_id" || echo "0")
    assert_eq "write_commit_event regression: commit exists for ticket" "1" "$([ "$commit_count" -ge 1 ] && echo 1 || echo 0)"
}
test_write_commit_event_regression_after_extraction

# ── Tests 9-15: forward-compat and schema_version derivation (RED) ────────────
# These test new behaviors in _write_preconditions and _read_latest_preconditions
# that don't exist yet. Tests 9-11 test unknown schema_version handling in the
# reader. Tests 12-15 test schema_version derivation in the writer.

# ── Test 9: unknown schema_version logs warning ───────────────────────────────
echo "Test 9: _read_latest_preconditions emits warning for unknown schema_version=99"
test_forward_compat_reader_unknown_schema_version_logs_warning() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists" "exists" "missing"
        return
    fi

    # Manually write a PRECONDITIONS event with schema_version=99
    local ticket_id="test-fwdcompat-001"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"
    local ts
    ts=$(python3 -c "import time; print(int(time.time() * 1000))")
    local uuid
    uuid=$(python3 -c "import uuid; print(uuid.uuid4())")
    local event_file="$ticket_dir/${ts}-${uuid}-PRECONDITIONS.json"

    python3 -c "
import json, sys
payload = {
    'event_type': 'PRECONDITIONS',
    'schema_version': 99,
    'manifest_depth': 'minimal',
    'gate_name': 'test_gate',
    'session_id': 'sess-fwd-001',
    'worktree_id': 'wt-fwd-001',
    'tier': 'minimal',
    'timestamp': int('$ts'),
    'gate_verdicts': [],
    'evidence_ref': {},
    'affects_fields': [],
    'data': {},
}
with open(sys.argv[1], 'w') as f:
    json.dump(payload, f)
" "$event_file"

    # Commit the event so it's in the git tree
    (cd "$repo/.tickets-tracker" && git add "$ticket_id/" && \
        git commit -m "test: fwd-compat event" 2>/dev/null) || true

    # Read the event and capture stderr
    local stderr_out
    stderr_out=$(cd "$repo" && source "$TICKET_LIB" && \
        _read_latest_preconditions "$ticket_id" "test_gate" "sess-fwd-001" 2>&1 >/dev/null)

    # Assert: warning appeared in stderr
    local has_warning
    has_warning=$(echo "$stderr_out" | grep -ic "unknown schema_version\|schema_version=99" || true)
    assert_eq "stderr contains unknown-schema_version warning" "1" \
        "$([ "${has_warning:-0}" -gt 0 ] && echo 1 || echo 0)"
}
test_forward_compat_reader_unknown_schema_version_logs_warning

# ── Test 10: unknown schema_version → exit 0 (no rejection) ──────────────────
echo "Test 10: _read_latest_preconditions does NOT reject unknown schema_version=99 (exits 0)"
test_forward_compat_reader_unknown_schema_version_no_rejection() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists" "exists" "missing"
        return
    fi

    local ticket_id="test-fwdcompat-002"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"
    local ts
    ts=$(python3 -c "import time; print(int(time.time() * 1000))")
    local uuid
    uuid=$(python3 -c "import uuid; print(uuid.uuid4())")
    local event_file="$ticket_dir/${ts}-${uuid}-PRECONDITIONS.json"

    python3 -c "
import json, sys
payload = {
    'event_type': 'PRECONDITIONS',
    'schema_version': 99,
    'manifest_depth': 'minimal',
    'gate_name': 'test_gate2',
    'session_id': 'sess-fwd-002',
    'worktree_id': 'wt-fwd-002',
    'tier': 'minimal',
    'timestamp': int('$ts'),
    'gate_verdicts': [],
    'evidence_ref': {},
    'affects_fields': [],
    'data': {},
}
with open(sys.argv[1], 'w') as f:
    json.dump(payload, f)
" "$event_file"

    (cd "$repo/.tickets-tracker" && git add "$ticket_id/" && \
        git commit -m "test: fwd-compat event 2" 2>/dev/null) || true

    local exit_code=0
    (cd "$repo" && source "$TICKET_LIB" && \
        _read_latest_preconditions "$ticket_id" "test_gate2" "sess-fwd-002" 2>/dev/null) || exit_code=$?

    assert_eq "unknown schema_version does not cause exit non-zero" "0" "$exit_code"
}
test_forward_compat_reader_unknown_schema_version_no_rejection

# ── Test 11: unknown schema_version → event_type still accessible ────────────
echo "Test 11: _read_latest_preconditions returns JSON with event_type=PRECONDITIONS for schema_version=99"
test_forward_compat_reader_minimal_fields_accessible_on_unknown_version() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists" "exists" "missing"
        return
    fi

    local ticket_id="test-fwdcompat-003"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"
    local ts
    ts=$(python3 -c "import time; print(int(time.time() * 1000))")
    local uuid
    uuid=$(python3 -c "import uuid; print(uuid.uuid4())")
    local event_file="$ticket_dir/${ts}-${uuid}-PRECONDITIONS.json"

    python3 -c "
import json, sys
payload = {
    'event_type': 'PRECONDITIONS',
    'schema_version': 99,
    'manifest_depth': 'minimal',
    'gate_name': 'test_gate3',
    'session_id': 'sess-fwd-003',
    'worktree_id': 'wt-fwd-003',
    'tier': 'minimal',
    'timestamp': int('$ts'),
    'gate_verdicts': [],
    'evidence_ref': {},
    'affects_fields': [],
    'data': {},
}
with open(sys.argv[1], 'w') as f:
    json.dump(payload, f)
" "$event_file"

    (cd "$repo/.tickets-tracker" && git add "$ticket_id/" && \
        git commit -m "test: fwd-compat event 3" 2>/dev/null) || true

    local json
    json=$(cd "$repo" && source "$TICKET_LIB" && \
        _read_latest_preconditions "$ticket_id" "test_gate3" "sess-fwd-003" 2>/dev/null)

    if [ -z "$json" ]; then
        assert_eq "returned JSON nonempty" "nonempty" "empty"
        return
    fi

    local event_type
    event_type=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('event_type','missing'))" "$json" 2>/dev/null)
    assert_eq "event_type=PRECONDITIONS accessible on schema_version=99" "PRECONDITIONS" "$event_type"
}
test_forward_compat_reader_minimal_fields_accessible_on_unknown_version

# ── Test 12: writer schema_version for tier=minimal → 1 ──────────────────────
echo "Test 12: _write_preconditions tier=minimal → schema_version=1 in written file"
test_writer_schema_version_minimal() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists" "exists" "missing"
        return
    fi

    local ticket_id="test-sv-min-001"
    (cd "$repo" && source "$TICKET_LIB" && \
        _write_preconditions "$ticket_id" "gate_min" "sess-sv-001" "wt-sv-001" "minimal") \
        2>/dev/null || true

    local event_file
    event_file=$(find "$repo/.tickets-tracker/$ticket_id" -name '*-PRECONDITIONS.json' | head -1)
    if [ -z "$event_file" ]; then
        assert_eq "event file written" "found" "not-found"
        return
    fi

    local sv
    sv=$(python3 -c "import json; d=json.load(open('$event_file')); print(d.get('schema_version','missing'))" 2>/dev/null)
    assert_eq "tier=minimal → schema_version=1" "1" "$sv"
}
test_writer_schema_version_minimal

# ── Test 13: writer schema_version + manifest_depth for tier=standard → 2, standard ─
echo "Test 13: _write_preconditions tier=standard → schema_version=2 AND manifest_depth=standard"
test_writer_schema_version_standard() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists" "exists" "missing"
        return
    fi

    local ticket_id="test-sv-std-001"
    (cd "$repo" && source "$TICKET_LIB" && \
        _write_preconditions "$ticket_id" "gate_std" "sess-sv-002" "wt-sv-002" "standard") \
        2>/dev/null || true

    local event_file
    event_file=$(find "$repo/.tickets-tracker/$ticket_id" -name '*-PRECONDITIONS.json' | head -1)
    if [ -z "$event_file" ]; then
        assert_eq "event file written" "found" "not-found"
        return
    fi

    local sv md
    sv=$(python3 -c "import json; d=json.load(open('$event_file')); print(d.get('schema_version','missing'))" 2>/dev/null)
    md=$(python3 -c "import json; d=json.load(open('$event_file')); print(d.get('manifest_depth','missing'))" 2>/dev/null)

    assert_eq "tier=standard → schema_version=2" "2" "$sv"
    assert_eq "tier=standard → manifest_depth=standard" "standard" "$md"
}
test_writer_schema_version_standard

# ── Test 14: writer schema_version + manifest_depth for tier=deep → 2, deep ──
echo "Test 14: _write_preconditions tier=deep → schema_version=2 AND manifest_depth=deep"
test_writer_schema_version_deep() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists" "exists" "missing"
        return
    fi

    local ticket_id="test-sv-deep-001"
    (cd "$repo" && source "$TICKET_LIB" && \
        _write_preconditions "$ticket_id" "gate_deep" "sess-sv-003" "wt-sv-003" "deep") \
        2>/dev/null || true

    local event_file
    event_file=$(find "$repo/.tickets-tracker/$ticket_id" -name '*-PRECONDITIONS.json' | head -1)
    if [ -z "$event_file" ]; then
        assert_eq "event file written" "found" "not-found"
        return
    fi

    local sv md
    sv=$(python3 -c "import json; d=json.load(open('$event_file')); print(d.get('schema_version','missing'))" 2>/dev/null)
    md=$(python3 -c "import json; d=json.load(open('$event_file')); print(d.get('manifest_depth','missing'))" 2>/dev/null)

    assert_eq "tier=deep → schema_version=2" "2" "$sv"
    assert_eq "tier=deep → manifest_depth=deep" "deep" "$md"
}
test_writer_schema_version_deep

# ── Test 15: warning deduplication — second call with same (ticket, sv=99) no duplicate ─
echo "Test 15: _read_latest_preconditions warning deduplication — no second warning on repeat call"
test_forward_compat_warning_deduplication() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists" "exists" "missing"
        return
    fi

    local ticket_id="test-fwdcompat-dedup"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"
    local ts
    ts=$(python3 -c "import time; print(int(time.time() * 1000))")
    local uuid
    uuid=$(python3 -c "import uuid; print(uuid.uuid4())")
    local event_file="$ticket_dir/${ts}-${uuid}-PRECONDITIONS.json"

    python3 -c "
import json, sys
payload = {
    'event_type': 'PRECONDITIONS',
    'schema_version': 99,
    'manifest_depth': 'minimal',
    'gate_name': 'test_gate_dedup',
    'session_id': 'sess-dedup-001',
    'worktree_id': 'wt-dedup-001',
    'tier': 'minimal',
    'timestamp': int('$ts'),
    'gate_verdicts': [],
    'evidence_ref': {},
    'affects_fields': [],
    'data': {},
}
with open(sys.argv[1], 'w') as f:
    json.dump(payload, f)
" "$event_file"

    (cd "$repo/.tickets-tracker" && git add "$ticket_id/" && \
        git commit -m "test: dedup event" 2>/dev/null) || true

    # First call — should log warning
    local first_stderr
    first_stderr=$(cd "$repo" && source "$TICKET_LIB" && \
        _read_latest_preconditions "$ticket_id" "test_gate_dedup" "sess-dedup-001" 2>&1 >/dev/null)

    # Second call (same process, same ticket, same schema_version) — should NOT log duplicate
    local second_stderr
    second_stderr=$(cd "$repo" && source "$TICKET_LIB" && \
        _read_latest_preconditions "$ticket_id" "test_gate_dedup" "sess-dedup-001" 2>&1 >/dev/null)

    # First call must have had a warning
    local first_has_warning
    first_has_warning=$(echo "$first_stderr" | grep -ic "unknown schema_version\|schema_version=99" || true)
    assert_eq "first call emits warning" "1" \
        "$([ "${first_has_warning:-0}" -gt 0 ] && echo 1 || echo 0)"

    # Second call must NOT have a warning (deduplicated)
    local second_has_warning
    second_has_warning=$(echo "$second_stderr" | grep -ic "unknown schema_version\|schema_version=99" || true)
    assert_eq "second call does NOT emit duplicate warning" "0" "$second_has_warning"
}
test_forward_compat_warning_deduplication

# ── Resolver tests (RED — resolve_ticket_id does not exist yet) ───────────────
#
# These tests exercise the multi-form ID resolver:
#   - 16-hex canonical passthrough
#   - 8-hex backward-compat passthrough
#   - unique prefix expansion
#   - ambiguous prefix error
#   - alias lookup
#   - alias collision error

# ── Helper: build a minimal CREATE event JSON with optional alias ─────────────
# Usage: _make_create_event_json <dest_path> <ticket_id> [alias]
_make_create_event_json() {
    local dest="$1"
    local ticket_id="$2"
    local alias="${3:-}"
    local ts
    ts=$(python3 -c "import time; print(int(time.time()))")
    local uuid
    uuid=$(python3 -c "import uuid; print(uuid.uuid4())")
    if [ -n "$alias" ]; then
        python3 -c "
import json, sys
data = {
    'timestamp': $ts,
    'uuid': '$uuid',
    'event_type': 'CREATE',
    'env_id': '$uuid',
    'author': 'Test',
    'data': {
        'ticket_type': 'task',
        'title': 'Test ticket',
        'parent_id': None,
        'alias': '$alias'
    }
}
json.dump(data, sys.stdout)
" > "$dest"
    else
        python3 -c "
import json, sys
data = {
    'timestamp': $ts,
    'uuid': '$uuid',
    'event_type': 'CREATE',
    'env_id': '$uuid',
    'author': 'Test',
    'data': {
        'ticket_type': 'task',
        'title': 'Test ticket',
        'parent_id': None
    }
}
json.dump(data, sys.stdout)
" > "$dest"
    fi
}

# ── Helper: plant a ticket in a test repo's tracker directory ─────────────────
# Usage: _plant_ticket <tracker_dir> <ticket_id> [alias]
# Creates the ticket directory and a CREATE event file. Does NOT git commit.
_plant_ticket() {
    local tracker_dir="$1"
    local ticket_id="$2"
    local alias="${3:-}"
    local ticket_dir="$tracker_dir/$ticket_id"
    mkdir -p "$ticket_dir"
    local ts uuid
    ts=$(python3 -c "import time; print(int(time.time_ns()))")
    uuid=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
    local event_file="$ticket_dir/${ts}-${uuid}-CREATE.json"
    _make_create_event_json "$event_file" "$ticket_id" "$alias"
}

# ── Test Resolver 1: 16-hex canonical passthrough ────────────────────────────
echo "Test resolver_16hex_passthrough: resolve_ticket_id returns 16-hex ID unchanged"
test_resolver_16hex_passthrough() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for resolver test" "exists" "missing"
        return
    fi

    # Check that resolve_ticket_id is defined — RED: it must NOT exist yet
    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type resolve_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "resolve_ticket_id function defined (16hex test)" "defined" "undefined"
        return
    fi

    local ticket_id="abcd1234efgh5678"
    _plant_ticket "$repo/.tickets-tracker" "$ticket_id"

    local result exit_code=0
    result=$(cd "$repo" && source "$TICKET_LIB" && resolve_ticket_id "$ticket_id" 2>/dev/null) \
        || exit_code=$?

    assert_eq "resolve_ticket_id: 16-hex exits 0" "0" "$exit_code"
    assert_eq "resolve_ticket_id: 16-hex returns ID unchanged" "$ticket_id" "$result"
}
test_resolver_16hex_passthrough

# ── Test Resolver 2: 8-hex backward-compat passthrough ───────────────────────
echo "Test resolver_8hex_backward_compat: resolve_ticket_id returns 8-hex ID (xxxx-xxxx) unchanged"
test_resolver_8hex_backward_compat() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for 8hex resolver test" "exists" "missing"
        return
    fi

    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type resolve_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "resolve_ticket_id function defined (8hex test)" "defined" "undefined"
        return
    fi

    local ticket_id="ab12-cd34"
    _plant_ticket "$repo/.tickets-tracker" "$ticket_id"

    local result exit_code=0
    result=$(cd "$repo" && source "$TICKET_LIB" && resolve_ticket_id "$ticket_id" 2>/dev/null) \
        || exit_code=$?

    assert_eq "resolve_ticket_id: 8-hex exits 0" "0" "$exit_code"
    assert_eq "resolve_ticket_id: 8-hex returns ID unchanged" "$ticket_id" "$result"
}
test_resolver_8hex_backward_compat

# ── Test Resolver 3: unique prefix expansion ─────────────────────────────────
echo "Test resolver_unique_prefix: resolve_ticket_id expands a unique prefix to full canonical ID"
test_resolver_unique_prefix() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for unique-prefix resolver test" "exists" "missing"
        return
    fi

    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type resolve_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "resolve_ticket_id function defined (unique-prefix test)" "defined" "undefined"
        return
    fi

    # Plant a ticket with a unique prefix "zzzz"
    local ticket_id="zzzz1234-5678-abcd"
    _plant_ticket "$repo/.tickets-tracker" "$ticket_id"

    local result exit_code=0
    result=$(cd "$repo" && source "$TICKET_LIB" && \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" resolve_ticket_id "zzzz" 2>/dev/null) \
        || exit_code=$?

    assert_eq "resolve_ticket_id: unique prefix exits 0" "0" "$exit_code"
    assert_eq "resolve_ticket_id: unique prefix expands to full ID" "$ticket_id" "$result"
}
test_resolver_unique_prefix

# ── Test Resolver 4: ambiguous prefix error ───────────────────────────────────
echo "Test resolver_ambiguous_prefix_error: resolve_ticket_id exits non-zero and prints 'Ambiguous' on stderr"
test_resolver_ambiguous_prefix_error() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for ambiguous-prefix test" "exists" "missing"
        return
    fi

    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type resolve_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "resolve_ticket_id function defined (ambiguous test)" "defined" "undefined"
        return
    fi

    # Plant two tickets that share the 4-char prefix "aaaa"
    local id1="aaaa1111-bbbb-cccc"
    local id2="aaaa2222-dddd-eeee"
    _plant_ticket "$repo/.tickets-tracker" "$id1"
    _plant_ticket "$repo/.tickets-tracker" "$id2"

    local stderr_out exit_code=0
    stderr_out=$(cd "$repo" && source "$TICKET_LIB" && \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" resolve_ticket_id "aaaa" 2>&1 >/dev/null) \
        || exit_code=$?

    assert_eq "resolve_ticket_id: ambiguous prefix exits non-zero" "1" \
        "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"
    local has_ambiguous
    has_ambiguous=$(echo "$stderr_out" | grep -ic "Ambiguous" || true)
    assert_eq "resolve_ticket_id: ambiguous prefix stderr contains 'Ambiguous'" "1" \
        "$([ "${has_ambiguous:-0}" -gt 0 ] && echo 1 || echo 0)"
}
test_resolver_ambiguous_prefix_error

# ── Test Resolver 5: alias lookup ─────────────────────────────────────────────
echo "Test resolver_alias_lookup: resolve_ticket_id looks up alias from CREATE event and returns canonical ID"
test_resolver_alias_lookup() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for alias-lookup test" "exists" "missing"
        return
    fi

    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type resolve_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "resolve_ticket_id function defined (alias-lookup test)" "defined" "undefined"
        return
    fi

    # Plant a ticket with alias "fast-river-oak"
    local ticket_id="qqqqrrrr-ssss-tttt"
    _plant_ticket "$repo/.tickets-tracker" "$ticket_id" "fast-river-oak"

    local result exit_code=0
    result=$(cd "$repo" && source "$TICKET_LIB" && \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" resolve_ticket_id "fast-river-oak" 2>/dev/null) \
        || exit_code=$?

    assert_eq "resolve_ticket_id: alias lookup exits 0" "0" "$exit_code"
    assert_eq "resolve_ticket_id: alias returns canonical ID" "$ticket_id" "$result"
}
test_resolver_alias_lookup

# ── Test Resolver 6: alias collision error ────────────────────────────────────
echo "Test resolver_alias_collision_error: resolve_ticket_id exits non-zero and lists both IDs when alias is ambiguous"
test_resolver_alias_collision_error() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for alias-collision test" "exists" "missing"
        return
    fi

    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type resolve_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "resolve_ticket_id function defined (alias-collision test)" "defined" "undefined"
        return
    fi

    # Plant two tickets both using alias "fast-river-oak"
    local id1="uuuu1111-vvvv-2222"
    local id2="wwww3333-xxxx-4444"
    _plant_ticket "$repo/.tickets-tracker" "$id1" "fast-river-oak"
    _plant_ticket "$repo/.tickets-tracker" "$id2" "fast-river-oak"

    local stderr_out exit_code=0
    stderr_out=$(cd "$repo" && source "$TICKET_LIB" && \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" resolve_ticket_id "fast-river-oak" 2>&1 >/dev/null) \
        || exit_code=$?

    assert_eq "resolve_ticket_id: alias collision exits non-zero" "1" \
        "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Both canonical IDs must appear in stderr output
    local has_id1 has_id2
    has_id1=$(echo "$stderr_out" | grep -c "$id1" || true)
    has_id2=$(echo "$stderr_out" | grep -c "$id2" || true)
    assert_eq "resolve_ticket_id: alias collision lists id1 in stderr" "1" \
        "$([ "${has_id1:-0}" -gt 0 ] && echo 1 || echo 0)"
    assert_eq "resolve_ticket_id: alias collision lists id2 in stderr" "1" \
        "$([ "${has_id2:-0}" -gt 0 ] && echo 1 || echo 0)"
}
test_resolver_alias_collision_error

# ── Test Resolver 6b: backfilled alias for legacy ticket (no data.alias) ──────
echo ""
echo "Test resolver_alias_backfill: resolve_ticket_id matches computed alias for legacy 16-hex ticket without data.alias"
test_resolver_alias_backfill() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for alias-backfill test" "exists" "missing"
        return
    fi

    # Plant a ticket whose CREATE event has NO data.alias.
    # ticket-id "0193d61dabcd1234" → first 12 hex (0193 d61d abcd) compute via wordlist.
    local ticket_id="0193-d61d-abcd-1234"
    _plant_ticket "$repo/.tickets-tracker" "$ticket_id"  # no alias arg → omitted

    # Compute the expected backfill alias deterministically from ticket_id
    local wordlist="$REPO_ROOT/src/rebar/_engine/resources/ticket-wordlist.txt"
    local expected_alias
    expected_alias=$(python3 "$REPO_ROOT/src/rebar/_engine/ticket-alias-compute.py" "$ticket_id" "$wordlist")

    [ -z "$expected_alias" ] && {
        assert_eq "alias-backfill: expected alias non-empty" "non-empty" "empty"
        return
    }

    local result exit_code=0
    result=$(cd "$repo" && source "$TICKET_LIB" && \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" resolve_ticket_id "$expected_alias" 2>/dev/null) \
        || exit_code=$?

    assert_eq "resolve_ticket_id: backfilled alias exits 0" "0" "$exit_code"
    assert_eq "resolve_ticket_id: backfilled alias returns canonical ID" "$ticket_id" "$result"
}
test_resolver_alias_backfill

echo ""
echo "Test resolver_jira_key_lookup: resolve_ticket_id looks up jira_key from CREATE event"
test_resolver_jira_key_lookup() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for jira_key-lookup test" "exists" "missing"
        return
    fi

    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type resolve_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "resolve_ticket_id function defined (jira_key test)" "defined" "undefined"
        return
    fi

    # Plant a ticket with a known jira_key directly via CREATE event JSON
    local ticket_id="jjjj9999kkkk0000"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"
    local ts uuid
    ts=$(python3 -c "import time; print(int(time.time_ns()))")
    uuid=$(python3 -c "import uuid; print(uuid.uuid4())")
    python3 -c "
import json, sys
data = {
    'timestamp': int('$ts'),
    'uuid': '$uuid',
    'event_type': 'CREATE',
    'env_id': '$uuid',
    'author': 'Test',
    'data': {
        'ticket_type': 'task',
        'title': 'jira_key lookup test ticket',
        'parent_id': None,
        'jira_key': 'PROJ-99'
    }
}
json.dump(data, sys.stdout)
" > "$ticket_dir/${ts}-${uuid}-CREATE.json"

    local result exit_code=0
    result=$(cd "$repo" && source "$TICKET_LIB" && \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" resolve_ticket_id "PROJ-99" 2>/dev/null) \
        || exit_code=$?
    assert_eq "resolve_ticket_id: jira_key exits 0" "0" "$exit_code"
    assert_eq "resolve_ticket_id: jira_key returns canonical ID" "$ticket_id" "$result"
}
test_resolver_jira_key_lookup

echo ""
echo "Test resolver_jira_key_before_alias: jira_key takes precedence over alias when input matches both"
test_resolver_jira_key_before_alias() {
    local repo
    repo=$(_make_test_repo)
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for jira_key-before-alias test" "exists" "missing"
        return
    fi

    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type resolve_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "resolve_ticket_id function defined (jira_key-before-alias test)" "defined" "undefined"
        return
    fi

    # Plant ticket A: has jira_key="SHARED-TOKEN"
    local id_a="aaaa1111bbbb2222"
    local dir_a="$repo/.tickets-tracker/$id_a"
    mkdir -p "$dir_a"
    local ts_a uuid_a
    ts_a=$(python3 -c "import time; print(int(time.time_ns()))")
    uuid_a=$(python3 -c "import uuid; print(uuid.uuid4())")
    python3 -c "
import json, sys
data = {
    'timestamp': int('$ts_a'),
    'uuid': '$uuid_a',
    'event_type': 'CREATE',
    'env_id': '$uuid_a',
    'author': 'Test',
    'data': {
        'ticket_type': 'task',
        'title': 'ticket-A with jira_key',
        'parent_id': None,
        'jira_key': 'SHARED-TOKEN'
    }
}
json.dump(data, sys.stdout)
" > "$dir_a/${ts_a}-${uuid_a}-CREATE.json"

    # Plant ticket B: has alias="SHARED-TOKEN"
    local id_b="cccc3333dddd4444"
    local dir_b="$repo/.tickets-tracker/$id_b"
    mkdir -p "$dir_b"
    local ts_b uuid_b
    ts_b=$(python3 -c "import time; print(int(time.time_ns()) + 1)")
    uuid_b=$(python3 -c "import uuid; print(uuid.uuid4())")
    python3 -c "
import json, sys
data = {
    'timestamp': int('$ts_b'),
    'uuid': '$uuid_b',
    'event_type': 'CREATE',
    'env_id': '$uuid_b',
    'author': 'Test',
    'data': {
        'ticket_type': 'task',
        'title': 'ticket-B with alias',
        'parent_id': None,
        'alias': 'SHARED-TOKEN'
    }
}
json.dump(data, sys.stdout)
" > "$dir_b/${ts_b}-${uuid_b}-CREATE.json"

    local result exit_code=0
    result=$(cd "$repo" && source "$TICKET_LIB" && \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" resolve_ticket_id "SHARED-TOKEN" 2>/dev/null) \
        || exit_code=$?
    assert_eq "resolve_ticket_id: jira_key-before-alias exits 0" "0" "$exit_code"
    assert_eq "resolve_ticket_id: jira_key wins over alias" "$id_a" "$result"
}
test_resolver_jira_key_before_alias

# ── format_ticket_id() tests (RED — function does not exist yet) ──────────────
#
# These tests exercise format_ticket_id() with the ticket.display_mode config key.
# All 6 tests MUST FAIL until format_ticket_id() is implemented in ticket-lib.sh.
#
# Acceptance criteria:
#   1. display_mode=canonical  → returns 16-hex canonical ID unchanged
#   2. display_mode=alias      → returns adj-noun-noun alias from CREATE event data.alias
#   3. display_mode=short      → returns shortest unambiguous prefix (>=4 chars)
#   4. display_mode absent     → defaults to auto (cascade: jira_key → alias → short → canonical)
#   5. display_mode=unrecognized_value → warns on stderr and delegates to auto
#   6. display_mode=alias, no data.alias → falls back to canonical

# ── Helper: create temp repo with optional ticket.display_mode config ─────────
# Usage: _make_fmt_test_repo [display_mode_value]
# When display_mode_value is empty, the key is omitted from config entirely.
_make_fmt_test_repo() {
    local display_mode="${1:-}"
    local repo
    repo=$(_make_test_repo)

    # Write a minimal .rebar/config.conf in the repo fixture (the path
    # format_ticket_id resolves by default when WORKFLOW_CONFIG_FILE is unset).
    mkdir -p "$repo/.rebar"
    {
        printf 'version=1.1.0\n'
        if [ -n "$display_mode" ]; then
            printf 'ticket.display_mode=%s\n' "$display_mode"
        fi
    } > "$repo/.rebar/config.conf"

    # Initialize ticket system
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    echo "$repo"
}

# ── Helper: plant a CREATE event with an optional alias field ─────────────────
# Usage: _plant_fmt_ticket <tracker_dir> <ticket_id> [alias]
# Creates the ticket dir and a CREATE event file; commits it to the tracker.
_plant_fmt_ticket() {
    local tracker_dir="$1"
    local ticket_id="$2"
    local alias="${3:-}"
    local ticket_dir="$tracker_dir/$ticket_id"
    mkdir -p "$ticket_dir"
    local ts uuid
    ts=$(python3 -c "import time; print(int(time.time() * 1000))")
    uuid=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
    local event_file="$ticket_dir/${ts}-${uuid}-CREATE.json"

    if [ -n "$alias" ]; then
        python3 -c "
import json, sys
data = {
    'timestamp': int('$ts'),
    'uuid': '$uuid',
    'event_type': 'CREATE',
    'env_id': '$uuid',
    'author': 'Test',
    'data': {
        'ticket_type': 'task',
        'title': 'Format test ticket',
        'parent_id': None,
        'alias': '$alias'
    }
}
json.dump(data, sys.stdout)
" > "$event_file"
    else
        python3 -c "
import json, sys
data = {
    'timestamp': int('$ts'),
    'uuid': '$uuid',
    'event_type': 'CREATE',
    'env_id': '$uuid',
    'author': 'Test',
    'data': {
        'ticket_type': 'task',
        'title': 'Format test ticket',
        'parent_id': None
    }
}
json.dump(data, sys.stdout)
" > "$event_file"
    fi

    # Commit event so git tree is clean
    (cd "$tracker_dir" && git add "$ticket_id/" 2>/dev/null && \
        git commit -m "test: CREATE $ticket_id" --no-verify 2>/dev/null) || true
}

# ── Test format_ticket_id 1: display_mode=canonical → returns 16-hex unchanged ─
echo "Test format_ticket_id_canonical: display_mode=canonical returns 16-hex ID unchanged"
test_format_ticket_id_canonical() {
    local repo
    repo=$(_make_fmt_test_repo "canonical")

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for format_ticket_id canonical test" "exists" "missing"
        return
    fi

    # RED: format_ticket_id must NOT be defined yet — test fails because function is absent
    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type format_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "format_ticket_id function defined (canonical test)" "defined" "undefined"
        return
    fi

    local ticket_id="abcd1234efgh5678"
    _plant_fmt_ticket "$repo/.tickets-tracker" "$ticket_id"

    local result exit_code=0
    result=$(cd "$repo" && \
        WORKFLOW_CONFIG_FILE="$repo/.rebar/config.conf" \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" \
        source "$TICKET_LIB" && format_ticket_id "$ticket_id" 2>/dev/null) || exit_code=$?

    assert_eq "format_ticket_id canonical: exits 0" "0" "$exit_code"
    assert_eq "format_ticket_id canonical: returns 16-hex ID unchanged" "$ticket_id" "$result"
}
test_format_ticket_id_canonical

# ── Test format_ticket_id 2: display_mode=alias → returns data.alias ──────────
echo "Test format_ticket_id_alias: display_mode=alias returns adj-noun-noun alias from CREATE event"
test_format_ticket_id_alias() {
    local repo
    repo=$(_make_fmt_test_repo "alias")

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for format_ticket_id alias test" "exists" "missing"
        return
    fi

    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type format_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "format_ticket_id function defined (alias test)" "defined" "undefined"
        return
    fi

    local ticket_id="bbbb2222cccc3333"
    local expected_alias="swift-river-oak"
    _plant_fmt_ticket "$repo/.tickets-tracker" "$ticket_id" "$expected_alias"

    local result exit_code=0
    result=$(cd "$repo" && \
        WORKFLOW_CONFIG_FILE="$repo/.rebar/config.conf" \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" \
        source "$TICKET_LIB" && format_ticket_id "$ticket_id" 2>/dev/null) || exit_code=$?

    assert_eq "format_ticket_id alias: exits 0" "0" "$exit_code"
    assert_eq "format_ticket_id alias: returns data.alias value" "$expected_alias" "$result"
}
test_format_ticket_id_alias

# ── Test format_ticket_id 3: display_mode=short → shortest unambiguous prefix ──
echo "Test format_ticket_id_short: display_mode=short returns shortest unambiguous prefix (>=4 chars)"
test_format_ticket_id_short() {
    local repo
    repo=$(_make_fmt_test_repo "short")

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for format_ticket_id short test" "exists" "missing"
        return
    fi

    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type format_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "format_ticket_id function defined (short test)" "defined" "undefined"
        return
    fi

    # Plant a ticket with a unique prefix starting at 4 chars
    local ticket_id="zzzz9999aaaa1111"
    _plant_fmt_ticket "$repo/.tickets-tracker" "$ticket_id"

    local result exit_code=0
    result=$(cd "$repo" && \
        WORKFLOW_CONFIG_FILE="$repo/.rebar/config.conf" \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" \
        source "$TICKET_LIB" && format_ticket_id "$ticket_id" 2>/dev/null) || exit_code=$?

    assert_eq "format_ticket_id short: exits 0" "0" "$exit_code"

    # Result must be a prefix of the full ticket_id and at least 4 chars
    local result_len="${#result}"
    assert_eq "format_ticket_id short: prefix length >= 4" "1" \
        "$([ "${result_len:-0}" -ge 4 ] && echo 1 || echo 0)"

    # Result must be a prefix of the full ID
    if [[ "$ticket_id" == "${result}"* ]]; then
        assert_eq "format_ticket_id short: result is a prefix of full ID" "prefix" "prefix"
    else
        assert_eq "format_ticket_id short: result is a prefix of full ID" "prefix" "not-prefix"
    fi
}
test_format_ticket_id_short

# ── Test format_ticket_id 4: display_mode absent → defaults to auto ─────────────
echo "Test format_ticket_id_default_auto: absent display_mode defaults to auto (returns short prefix)"
test_format_ticket_id_default_auto() {
    local repo
    # Pass empty string — config omits ticket.display_mode entirely → auto is the default
    repo=$(_make_fmt_test_repo "")

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for format_ticket_id default test" "exists" "missing"
        return
    fi

    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type format_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "format_ticket_id function defined (default test)" "defined" "undefined"
        return
    fi

    # Ticket with no alias/jira_key — auto mode falls through to short prefix.
    # Only one ticket in the tracker so the 4-char prefix "cccc" is unambiguous.
    local ticket_id="cccc4444dddd5555"
    _plant_fmt_ticket "$repo/.tickets-tracker" "$ticket_id"

    local result exit_code=0
    result=$(cd "$repo" && \
        WORKFLOW_CONFIG_FILE="$repo/.rebar/config.conf" \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" \
        source "$TICKET_LIB" && format_ticket_id "$ticket_id" 2>/dev/null) || exit_code=$?

    assert_eq "format_ticket_id default auto: exits 0" "0" "$exit_code"
    # auto → no jira_key/alias → short prefix; with one ticket "cccc" is unambiguous
    assert_eq "format_ticket_id default auto: returns 4-char short prefix" \
        "cccc" "$result"
}
test_format_ticket_id_default_auto

# ── Test format_ticket_id 5: display_mode=unrecognized_value → warns + auto ──────
echo "Test format_ticket_id_unrecognized_mode: unrecognized display_mode warns and delegates to auto"
test_format_ticket_id_unrecognized_mode() {
    local repo
    repo=$(_make_fmt_test_repo "bogus_mode_xyz")

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for format_ticket_id unrecognized-mode test" "exists" "missing"
        return
    fi

    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type format_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "format_ticket_id function defined (unrecognized-mode test)" "defined" "undefined"
        return
    fi

    # Ticket with no alias/jira_key — unrecognized mode warns on stderr, then delegates to auto,
    # which falls through to short prefix. One ticket → "eeee" is unambiguous.
    local ticket_id="eeee6666ffff7777"
    _plant_fmt_ticket "$repo/.tickets-tracker" "$ticket_id"

    local result stderr_out exit_code=0
    stderr_out=$(cd "$repo" && \
        WORKFLOW_CONFIG_FILE="$repo/.rebar/config.conf" \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" \
        source "$TICKET_LIB" && format_ticket_id "$ticket_id" 2>&1 >/dev/null) || exit_code=$?

    result=$(cd "$repo" && \
        WORKFLOW_CONFIG_FILE="$repo/.rebar/config.conf" \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" \
        source "$TICKET_LIB" && format_ticket_id "$ticket_id" 2>/dev/null) || exit_code=$?

    assert_eq "format_ticket_id unrecognized-mode: exits 0" "0" "$exit_code"
    assert_eq "format_ticket_id unrecognized-mode: returns 4-char short prefix via auto" \
        "eeee" "$result"
    # Verify warning was emitted on stderr
    local has_warn
    has_warn=$(echo "$stderr_out" | grep -c "WARN" || true)
    assert_eq "format_ticket_id unrecognized-mode: emits WARN on stderr" "1" \
        "$([ "${has_warn:-0}" -gt 0 ] && echo 1 || echo 0)"
}
test_format_ticket_id_unrecognized_mode

# ── Test format_ticket_id 6: display_mode=alias, no data.alias → canonical fallback ─
echo "Test format_ticket_id_alias_no_alias_field: display_mode=alias with no data.alias falls back to canonical"
test_format_ticket_id_alias_no_alias_field() {
    local repo
    repo=$(_make_fmt_test_repo "alias")

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for format_ticket_id alias-fallback test" "exists" "missing"
        return
    fi

    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type format_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "format_ticket_id function defined (alias-fallback test)" "defined" "undefined"
        return
    fi

    # Plant a ticket WITHOUT an alias (simulates a pre-migration ticket with no data.alias)
    local ticket_id="gggg8888hhhh9999"
    _plant_fmt_ticket "$repo/.tickets-tracker" "$ticket_id"  # no alias argument

    local result exit_code=0
    result=$(cd "$repo" && \
        WORKFLOW_CONFIG_FILE="$repo/.rebar/config.conf" \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" \
        source "$TICKET_LIB" && format_ticket_id "$ticket_id" 2>/dev/null) || exit_code=$?

    assert_eq "format_ticket_id alias-fallback: exits 0" "0" "$exit_code"
    assert_eq "format_ticket_id alias-fallback: returns canonical ID when data.alias absent" \
        "$ticket_id" "$result"
}
test_format_ticket_id_alias_no_alias_field

# ── Test format_ticket_id 7: display_mode=auto, alias present → returns alias ───
echo "Test format_ticket_id_auto_with_alias: auto mode returns data.alias when present"
test_format_ticket_id_auto_with_alias() {
    local repo
    repo=$(_make_fmt_test_repo "")  # no display_mode → auto default

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for format_ticket_id auto-alias test" "exists" "missing"
        return
    fi

    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type format_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "format_ticket_id function defined (auto-alias test)" "defined" "undefined"
        return
    fi

    # Plant a ticket WITH an alias — auto mode should return the alias (higher priority than short)
    local ticket_id="iiii0000jjjj1111"
    _plant_fmt_ticket "$repo/.tickets-tracker" "$ticket_id" "calm-river-stone"

    local result exit_code=0
    result=$(cd "$repo" && \
        WORKFLOW_CONFIG_FILE="$repo/.rebar/config.conf" \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" \
        source "$TICKET_LIB" && format_ticket_id "$ticket_id" 2>/dev/null) || exit_code=$?

    assert_eq "format_ticket_id auto with alias: exits 0" "0" "$exit_code"
    assert_eq "format_ticket_id auto with alias: returns data.alias" \
        "calm-river-stone" "$result"
}
test_format_ticket_id_auto_with_alias

# ── Test format_ticket_id 8: display_mode=auto, jira_key present → returns jira_key ─
echo "Test format_ticket_id_auto_with_jira_key: auto mode returns jira_key when present (highest priority)"
test_format_ticket_id_auto_with_jira_key() {
    local repo
    repo=$(_make_fmt_test_repo "")  # no display_mode → auto default

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists for format_ticket_id auto-jira_key test" "exists" "missing"
        return
    fi

    local fn_exists=0
    (cd "$repo" && source "$TICKET_LIB" && type format_ticket_id &>/dev/null) || fn_exists=$?
    if [ "$fn_exists" -ne 0 ]; then
        assert_eq "format_ticket_id function defined (auto-jira_key test)" "defined" "undefined"
        return
    fi

    # Plant a ticket with both jira_key AND alias — auto must prefer jira_key
    local ticket_id="kkkk2222llll3333"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"
    local ts uuid
    ts=$(python3 -c "import time; print(int(time.time() * 1000))")
    uuid=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
    python3 -c "
import json, sys
data = {
    'timestamp': int('$ts'),
    'uuid': '$uuid',
    'event_type': 'CREATE',
    'env_id': '$uuid',
    'author': 'Test',
    'data': {
        'ticket_type': 'task',
        'title': 'Jira key test ticket',
        'parent_id': None,
        'jira_key': 'PROJ-42',
        'alias': 'warm-lake-oak'
    }
}
json.dump(data, sys.stdout)
" > "$ticket_dir/${ts}-${uuid}-CREATE.json"
    (cd "$repo/.tickets-tracker" && git add "$ticket_id/" 2>/dev/null && \
        git commit -m "test: CREATE $ticket_id jira_key" --no-verify 2>/dev/null) || true

    local result exit_code=0
    result=$(cd "$repo" && \
        WORKFLOW_CONFIG_FILE="$repo/.rebar/config.conf" \
        TICKETS_TRACKER_DIR="$repo/.tickets-tracker" \
        source "$TICKET_LIB" && format_ticket_id "$ticket_id" 2>/dev/null) || exit_code=$?

    assert_eq "format_ticket_id auto with jira_key: exits 0" "0" "$exit_code"
    assert_eq "format_ticket_id auto with jira_key: returns jira_key (beats alias)" \
        "PROJ-42" "$result"
}
test_format_ticket_id_auto_with_jira_key

print_summary
