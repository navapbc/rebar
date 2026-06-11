#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-fsck-e2e.sh
# E2E tests for 'ticket fsck' — exercises the full workflow via the dispatcher.
#
# Tests:
#   1. Happy path: clean system → exit 0, "no issues found"
#   2. Corrupt event detection → exit non-zero, CORRUPT line
#   3. Missing CREATE detection → exit non-zero, MISSING_CREATE line
#   4. Stale lock cleanup → lock removed, FIXED line
#   5. SNAPSHOT consistency → exit non-zero, SNAPSHOT_INCONSISTENT line
#
# Usage: bash tests/scripts/suites/test-ticket-fsck-e2e.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
FSCK_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-fsck.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"
source "$REPO_ROOT/tests/lib/ticket-fixtures.sh"

echo "=== test-ticket-fsck-e2e.sh ==="

# ── Suite-runner guard ─────────────────────────────────────────────────────
if [ "${_RUN_ALL_ACTIVE:-0}" = "1" ] && [ ! -f "$FSCK_SCRIPT" ]; then
    echo "SKIP: ticket-fsck.sh not found — tests deferred"
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

# ── Helper: resolve tracker git dir ──────────────────────────────────────────
_resolve_tracker_git_dir() {
    local repo="$1"
    local tracker_git="$repo/.tickets-tracker/.git"
    if [ -f "$tracker_git" ]; then
        local gitdir
        gitdir=$(sed 's/^gitdir: //' "$tracker_git")
        if [[ "$gitdir" != /* ]]; then
            gitdir="$repo/.tickets-tracker/$gitdir"
        fi
        echo "$gitdir"
    elif [ -d "$tracker_git" ]; then
        echo "$tracker_git"
    else
        echo ""
    fi
}

# ── E2E Test 1: Happy path — clean system ───────────────────────────────────
echo "E2E Test 1: Happy path — clean system reports no issues"
test_e2e_happy_path() {
    _snapshot_fail

    if [ ! -f "$FSCK_SCRIPT" ]; then
        assert_eq "ticket-fsck.sh exists" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # Create a valid ticket via the dispatcher
    (cd "$repo" && bash "$TICKET_SCRIPT" create task "E2E happy ticket" >/dev/null 2>&1) || true

    # Run fsck via dispatcher
    local output exit_code=0
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" fsck 2>&1) || exit_code=$?

    assert_eq "fsck exits 0 on clean system" "0" "$exit_code"
    assert_contains "output mentions no issues" "no issues" "$output"

    assert_pass_if_clean "test_e2e_happy_path"
}
test_e2e_happy_path

# ── E2E Test 2: Corrupt event detection ─────────────────────────────────────
echo "E2E Test 2: Corrupt event detection via dispatcher"
test_e2e_corrupt_event() {
    _snapshot_fail

    if [ ! -f "$FSCK_SCRIPT" ]; then
        assert_eq "ticket-fsck.sh exists for corrupt E2E" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # Create a valid ticket
    (cd "$repo" && bash "$TICKET_SCRIPT" create task "E2E corrupt ticket" >/dev/null 2>&1) || true

    # Find the ticket dir (first one)
    local ticket_id
    ticket_id=$(ls "$repo/.tickets-tracker/" | head -1)
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"

    # Overwrite one event with invalid JSON
    local first_event
    first_event=$(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '.*' | sort | tail -1)
    echo "NOT VALID JSON {{{" > "$first_event"

    # Clear cache
    rm -f "$ticket_dir/.cache.json"

    # Run fsck via dispatcher
    local output exit_code=0
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" fsck 2>&1) || exit_code=$?

    assert_ne "fsck exits non-zero for corrupt event" "0" "$exit_code"
    assert_contains "output mentions CORRUPT" "CORRUPT" "$output"

    assert_pass_if_clean "test_e2e_corrupt_event"
}
test_e2e_corrupt_event

# ── E2E Test 3: Missing CREATE detection ────────────────────────────────────
echo "E2E Test 3: Missing CREATE detection via dispatcher"
test_e2e_missing_create() {
    _snapshot_fail

    if [ ! -f "$FSCK_SCRIPT" ]; then
        assert_eq "ticket-fsck.sh exists for missing CREATE E2E" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # Manually create a ticket directory with only a STATUS event (no CREATE)
    local ticket_id="tkt-e2e-nocreate"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"

    _write_event "$ticket_dir" "1742605200" "00000000-0000-4000-8000-status000001" "STATUS" \
        '{"status": "in_progress", "current_status": "open"}'

    # Run fsck via dispatcher
    local output exit_code=0
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" fsck 2>&1) || exit_code=$?

    assert_ne "fsck exits non-zero for missing CREATE" "0" "$exit_code"
    assert_contains "output mentions MISSING_CREATE" "MISSING_CREATE" "$output"

    assert_pass_if_clean "test_e2e_missing_create"
}
test_e2e_missing_create

# ── E2E Test 4: Stale lock cleanup ──────────────────────────────────────────
echo "E2E Test 4: Stale lock cleanup via dispatcher"
test_e2e_stale_lock() {
    _snapshot_fail

    if [ ! -f "$FSCK_SCRIPT" ]; then
        assert_eq "ticket-fsck.sh exists for stale lock E2E" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # Create a valid ticket so there's no other issue
    (cd "$repo" && bash "$TICKET_SCRIPT" create task "E2E lock ticket" >/dev/null 2>&1) || true

    local lock_dir
    lock_dir=$(_resolve_tracker_git_dir "$repo")

    # Create a stale index.lock — empty file (git lock files carry no PID),
    # backdated so the age-based check (>5 min) reliably fires.
    : > "$lock_dir/index.lock"
    touch -t 202501010000 "$lock_dir/index.lock"

    # Run fsck via dispatcher
    local output exit_code=0
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" fsck 2>&1) || exit_code=$?

    # Lock file should be removed; with valid ticket, exit 0
    local lock_exists="no"
    [ -f "$lock_dir/index.lock" ] && lock_exists="yes"
    assert_eq "stale lock removed" "no" "$lock_exists"
    assert_contains "output mentions FIXED" "FIXED" "$output"

    assert_pass_if_clean "test_e2e_stale_lock"
}
test_e2e_stale_lock

# ── E2E Test 5: SNAPSHOT consistency ────────────────────────────────────────
echo "E2E Test 5: SNAPSHOT consistency check via dispatcher"
test_e2e_snapshot_consistency() {
    _snapshot_fail

    if [ ! -f "$FSCK_SCRIPT" ]; then
        assert_eq "ticket-fsck.sh exists for snapshot E2E" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # Create a ticket with enough events to compact
    local ticket_id="tkt-e2e-snap"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"

    # Write CREATE event
    local create_uuid="00000000-0000-4000-8000-create000001"
    _write_event "$ticket_dir" "1742605200" "$create_uuid" "CREATE" \
        '{"ticket_type": "task", "title": "Snapshot E2E test", "parent_id": null}'

    # Write a SNAPSHOT that references the create_uuid as a source
    python3 -c "
import json, sys
snapshot = {
    'timestamp': 1742605300,
    'uuid': 'snapshot-uuid-e2e1',
    'event_type': 'SNAPSHOT',
    'env_id': '00000000-0000-4000-8000-000000000001',
    'author': 'Test',
    'data': {
        'compiled_state': {
            'ticket_id': 'tkt-e2e-snap',
            'ticket_type': 'task',
            'title': 'Snapshot E2E test',
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
" > "$ticket_dir/1742605300-snapshot-uuid-e2e1-SNAPSHOT.json"

    # The CREATE event file still exists — inconsistency

    # Run fsck via dispatcher
    local output exit_code=0
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" fsck 2>&1) || exit_code=$?

    assert_ne "fsck exits non-zero for snapshot inconsistency" "0" "$exit_code"
    assert_contains "output mentions SNAPSHOT_INCONSISTENT" "SNAPSHOT_INCONSISTENT" "$output"

    assert_pass_if_clean "test_e2e_snapshot_consistency"
}
test_e2e_snapshot_consistency

print_summary
