#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-fsck.sh
# RED tests for src/rebar/_engine/ticket-fsck.sh — ticket system integrity validator.
#
# All tests MUST FAIL until ticket-fsck.sh is implemented.
# Covers: corrupt JSON detection, missing CREATE event, stale index.lock,
# SNAPSHOT consistency, and clean system validation.
#
# Usage: bash tests/scripts/suites/test-ticket-fsck.sh
# Returns: exit non-zero (RED) until ticket-fsck.sh is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
FSCK_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-fsck.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"
source "$REPO_ROOT/tests/lib/ticket-fixtures.sh"

echo "=== test-ticket-fsck.sh ==="

# ── Suite-runner guard: skip when ticket-fsck.sh does not exist ────────────
# RED tests fail by design (script not found). When auto-discovered by
# run-script-tests.sh, they would break `bash tests/run-all.sh`. Skip with
# exit 0 when ticket-fsck.sh is absent AND running under the suite runner.
if [ "${_RUN_ALL_ACTIVE:-0}" = "1" ] && [ ! -f "$FSCK_SCRIPT" ]; then
    echo "SKIP: ticket-fsck.sh not yet implemented (RED) — tests deferred"
    echo ""
    printf "PASSED: 0  FAILED: 0\n"
    exit 0
fi

# ── Helper: create a fresh temp git repo with ticket system initialized ────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Test 1: fsck detects corrupt JSON event ──────────────────────────────────
echo "Test 1: fsck detects corrupt JSON event"
test_fsck_detects_corrupt_json_event() {
    _snapshot_fail

    if [ ! -f "$FSCK_SCRIPT" ]; then
        assert_eq "ticket-fsck.sh exists" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-corrupt1"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"

    # Write a valid CREATE event
    _write_event "$ticket_dir" "1742605200" "00000000-0000-4000-8000-create000001" "CREATE" \
        '{"ticket_type": "task", "title": "Corrupt test ticket", "parent_id": null}'

    # Write a corrupt (non-JSON) event file
    echo "THIS IS NOT JSON {{{{" > "$ticket_dir/1742605300-bad-uuid-STATUS.json"

    # Run fsck
    local output exit_code=0
    output=$(cd "$repo" && bash "$FSCK_SCRIPT" 2>&1) || exit_code=$?

    # Assert: exit non-zero (issues found)
    assert_ne "fsck exits non-zero for corrupt JSON" "0" "$exit_code"

    # Assert: output contains CORRUPT line for the corrupt file
    assert_contains "output mentions CORRUPT" "CORRUPT" "$output"

    assert_pass_if_clean "test_fsck_detects_corrupt_json_event"
}
test_fsck_detects_corrupt_json_event

# ── Test 2: fsck detects missing CREATE event ────────────────────────────────
echo "Test 2: fsck detects missing CREATE event"
test_fsck_detects_missing_create_event() {
    _snapshot_fail

    if [ ! -f "$FSCK_SCRIPT" ]; then
        assert_eq "ticket-fsck.sh exists for missing CREATE test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-nocreate"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"

    # Write only a STATUS event — no CREATE
    _write_event "$ticket_dir" "1742605200" "00000000-0000-4000-8000-status000001" "STATUS" \
        '{"status": "in_progress", "current_status": "open"}'

    local output exit_code=0
    output=$(cd "$repo" && bash "$FSCK_SCRIPT" 2>&1) || exit_code=$?

    assert_ne "fsck exits non-zero for missing CREATE" "0" "$exit_code"
    assert_contains "output mentions MISSING_CREATE" "MISSING_CREATE" "$output"

    assert_pass_if_clean "test_fsck_detects_missing_create_event"
}
test_fsck_detects_missing_create_event

# ── Test 3: fsck detects stale index.lock (dead PID) ─────────────────────────
echo "Test 3: fsck detects and removes stale index.lock with dead PID"
test_fsck_detects_stale_index_lock() {
    _snapshot_fail

    if [ ! -f "$FSCK_SCRIPT" ]; then
        assert_eq "ticket-fsck.sh exists for stale lock test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_git_dir="$repo/.tickets-tracker/.git"

    # The .git file in a worktree is just a pointer; get the actual git dir
    local lock_dir
    if [ -f "$tracker_git_dir" ]; then
        # It's a gitdir pointer file — extract the path
        lock_dir=$(sed 's/^gitdir: //' "$tracker_git_dir")
        # Handle relative paths
        if [[ "$lock_dir" != /* ]]; then
            lock_dir="$repo/.tickets-tracker/$lock_dir"
        fi
    else
        lock_dir="$tracker_git_dir"
    fi

    # Create a stale index.lock — empty file (git lock files carry no PID),
    # backdated so the age-based check (>5 min) reliably fires.
    : > "$lock_dir/index.lock"
    touch -t 202501010000 "$lock_dir/index.lock"

    local output exit_code=0
    output=$(cd "$repo" && bash "$FSCK_SCRIPT" 2>&1) || exit_code=$?

    # Assert: lock file was removed
    local lock_exists="no"
    [ -f "$lock_dir/index.lock" ] && lock_exists="yes"
    assert_eq "stale lock removed" "no" "$lock_exists"

    # Assert: output mentions FIXED
    assert_contains "output mentions FIXED" "FIXED" "$output"

    assert_pass_if_clean "test_fsck_detects_stale_index_lock"
}
test_fsck_detects_stale_index_lock

# ── Test 4: fsck does NOT remove a fresh index.lock (younger than 5 min) ─────
echo "Test 4: fsck does NOT remove a fresh index.lock (younger than 5 min)"
test_fsck_detects_live_index_lock() {
    _snapshot_fail

    if [ ! -f "$FSCK_SCRIPT" ]; then
        assert_eq "ticket-fsck.sh exists for live lock test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_git_dir="$repo/.tickets-tracker/.git"

    local lock_dir
    if [ -f "$tracker_git_dir" ]; then
        lock_dir=$(sed 's/^gitdir: //' "$tracker_git_dir")
        if [[ "$lock_dir" != /* ]]; then
            lock_dir="$repo/.tickets-tracker/$lock_dir"
        fi
    else
        lock_dir="$tracker_git_dir"
    fi

    # Create a fresh (current mtime) index.lock — age < 5 min → must NOT be removed
    : > "$lock_dir/index.lock"

    local output exit_code=0
    output=$(cd "$repo" && bash "$FSCK_SCRIPT" 2>&1) || exit_code=$?

    # Assert: lock file was NOT removed
    local lock_exists="no"
    [ -f "$lock_dir/index.lock" ] && lock_exists="yes"
    assert_eq "live lock NOT removed" "yes" "$lock_exists"

    # Assert: output mentions WARN
    assert_contains "output mentions WARN for live lock" "WARN" "$output"

    # Clean up the lock file so it doesn't interfere
    rm -f "$lock_dir/index.lock"

    assert_pass_if_clean "test_fsck_detects_live_index_lock"
}
test_fsck_detects_live_index_lock

# ── Test 5: fsck detects SNAPSHOT with source UUID still on disk ─────────────
echo "Test 5: fsck detects SNAPSHOT inconsistency (source UUID still exists)"
test_fsck_reports_snapshot_orphaned_source_uuid() {
    _snapshot_fail

    if [ ! -f "$FSCK_SCRIPT" ]; then
        assert_eq "ticket-fsck.sh exists for snapshot consistency test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-snapbad"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"

    # Write a CREATE event
    local create_uuid="00000000-0000-4000-8000-create000001"
    _write_event "$ticket_dir" "1742605200" "$create_uuid" "CREATE" \
        '{"ticket_type": "task", "title": "Snapshot test", "parent_id": null}'

    # Write a SNAPSHOT that claims the CREATE uuid as a source
    python3 -c "
import json, sys
snapshot = {
    'timestamp': 1742605300,
    'uuid': 'snapshot-uuid-0001',
    'event_type': 'SNAPSHOT',
    'env_id': '00000000-0000-4000-8000-000000000001',
    'author': 'Test',
    'data': {
        'compiled_state': {
            'ticket_id': 'tkt-snapbad',
            'ticket_type': 'task',
            'title': 'Snapshot test',
            'status': 'open',
            'author': 'Test',
            'created_at': 1742605200,
            'env_id': '00000000-0000-4000-8000-000000000001',
            'parent_id': None,
            'comments': [],
            'deps': []
        },
        'source_event_uuids': ['$create_uuid']
    }
}
json.dump(snapshot, sys.stdout)
" > "$ticket_dir/1742605300-snapshot-uuid-0001-SNAPSHOT.json"

    # The CREATE event file STILL EXISTS on disk — this is the inconsistency.
    # After compaction, source events should have been deleted.

    local output exit_code=0
    output=$(cd "$repo" && bash "$FSCK_SCRIPT" 2>&1) || exit_code=$?

    assert_ne "fsck exits non-zero for snapshot inconsistency" "0" "$exit_code"
    assert_contains "output mentions SNAPSHOT_INCONSISTENT" "SNAPSHOT_INCONSISTENT" "$output"

    assert_pass_if_clean "test_fsck_reports_snapshot_orphaned_source_uuid"
}
test_fsck_reports_snapshot_orphaned_source_uuid

# ── Test 6: fsck detects orphan pre-snapshot event not in source_event_uuids ──
echo "Test 6: fsck detects orphan pre-snapshot event"
test_fsck_reports_snapshot_missing_source_uuid() {
    _snapshot_fail

    if [ ! -f "$FSCK_SCRIPT" ]; then
        assert_eq "ticket-fsck.sh exists for orphan event test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-orphan"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"

    # Write a CREATE event
    local create_uuid="00000000-0000-4000-8000-create000001"
    _write_event "$ticket_dir" "1742605200" "$create_uuid" "CREATE" \
        '{"ticket_type": "task", "title": "Orphan test", "parent_id": null}'

    # Write an extra STATUS event before the SNAPSHOT timestamp
    local status_uuid="00000000-0000-4000-8000-status000001"
    _write_event "$ticket_dir" "1742605250" "$status_uuid" "STATUS" \
        '{"status": "in_progress", "current_status": "open"}'

    # Write a SNAPSHOT that only references create_uuid (NOT status_uuid)
    python3 -c "
import json, sys
snapshot = {
    'timestamp': 1742605300,
    'uuid': 'snapshot-uuid-0002',
    'event_type': 'SNAPSHOT',
    'env_id': '00000000-0000-4000-8000-000000000001',
    'author': 'Test',
    'data': {
        'compiled_state': {
            'ticket_id': 'tkt-orphan',
            'ticket_type': 'task',
            'title': 'Orphan test',
            'status': 'in_progress',
            'author': 'Test',
            'created_at': 1742605200,
            'env_id': '00000000-0000-4000-8000-000000000001',
            'parent_id': None,
            'comments': [],
            'deps': []
        },
        'source_event_uuids': ['00000000-0000-4000-8000-create000001']
    }
}
json.dump(snapshot, sys.stdout)
" > "$ticket_dir/1742605300-snapshot-uuid-0002-SNAPSHOT.json"

    # The STATUS event is pre-snapshot and NOT in source_event_uuids = orphan

    local output exit_code=0
    output=$(cd "$repo" && bash "$FSCK_SCRIPT" 2>&1) || exit_code=$?

    assert_ne "fsck exits non-zero for orphan event" "0" "$exit_code"
    assert_contains "output mentions ORPHAN_EVENT" "ORPHAN_EVENT" "$output"

    assert_pass_if_clean "test_fsck_reports_snapshot_missing_source_uuid"
}
test_fsck_reports_snapshot_missing_source_uuid

# ── Test 7: fsck exits zero on clean system ──────────────────────────────────
echo "Test 7: fsck exits zero on a clean ticket system"
test_fsck_exits_zero_on_clean_system() {
    _snapshot_fail

    if [ ! -f "$FSCK_SCRIPT" ]; then
        assert_eq "ticket-fsck.sh exists for clean system test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # Create a valid ticket via the dispatcher
    local create_output
    create_output=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Clean ticket" 2>&1) || true

    local output exit_code=0
    output=$(cd "$repo" && bash "$FSCK_SCRIPT" 2>&1) || exit_code=$?

    assert_eq "fsck exits 0 on clean system" "0" "$exit_code"
    assert_contains "output mentions no issues" "no issues" "$output"

    assert_pass_if_clean "test_fsck_exits_zero_on_clean_system"
}
test_fsck_exits_zero_on_clean_system

# ── Test 8: fsck is non-destructive on valid events ──────────────────────────
echo "Test 8: fsck does not modify valid event files"
test_fsck_is_nondestructive_on_valid_events() {
    _snapshot_fail

    if [ ! -f "$FSCK_SCRIPT" ]; then
        assert_eq "ticket-fsck.sh exists for non-destructive test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-nodelete"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"

    # Write valid events
    _write_event "$ticket_dir" "1742605200" "00000000-0000-4000-8000-create000001" "CREATE" \
        '{"ticket_type": "task", "title": "Nondestructive test", "parent_id": null}'
    _write_event "$ticket_dir" "1742605300" "00000000-0000-4000-8000-status000001" "STATUS" \
        '{"status": "in_progress", "current_status": "open"}'

    # Record file checksums before fsck (shasum is cross-platform: macOS + Linux)
    local before_checksums
    before_checksums=$(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '.*' -exec shasum {} + | sort)

    # Run fsck
    (cd "$repo" && bash "$FSCK_SCRIPT") >/dev/null 2>&1 || true

    # Record file checksums after fsck
    local after_checksums
    after_checksums=$(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '.*' -exec shasum {} + | sort)

    assert_eq "event files unchanged after fsck" "$before_checksums" "$after_checksums"

    # Assert file count is the same
    local before_count after_count
    before_count=2
    after_count=$(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "same number of event files" "$before_count" "$after_count"

    assert_pass_if_clean "test_fsck_is_nondestructive_on_valid_events"
}
test_fsck_is_nondestructive_on_valid_events

print_summary
