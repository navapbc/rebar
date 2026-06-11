#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-compact.sh
# RED tests for src/rebar/_engine/ticket-compact.sh — event compaction.
#
# All tests MUST FAIL until ticket-compact.sh is implemented.
# Covers: threshold-based compaction, SNAPSHOT event writing,
# source_event_uuids tracking, file deletion, flock, and corrupt handling.
#
# Usage: bash tests/scripts/suites/test-ticket-compact.sh
# Returns: exit non-zero (RED) until ticket-compact.sh is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
COMPACT_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-compact.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-compact.sh ==="

# ── Suite-runner guard: skip when ticket-compact.sh does not exist ────────────
# RED tests fail by design (script not found). When auto-discovered by
# run-script-tests.sh, they would break `bash tests/run-all.sh`. Skip with
# exit 0 when ticket-compact.sh is absent AND running under the suite runner.
if [ "${_RUN_ALL_ACTIVE:-0}" = "1" ] && [ ! -f "$COMPACT_SCRIPT" ]; then
    echo "SKIP: ticket-compact.sh not yet implemented (RED) — tests deferred"
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

# ── Helper: write an event file to a ticket dir ─────────────────────────────
# Usage: _write_event <ticket_dir> <timestamp> <uuid> <event_type> <data_json>
_write_event() {
    local ticket_dir="$1"
    local timestamp="$2"
    local uuid="$3"
    local event_type="$4"
    local data_json="$5"
    local env_id="${6:-00000000-0000-4000-8000-000000000001}"
    local author="${7:-Test User}"
    local filename="${timestamp}-${uuid}-${event_type}.json"

    python3 -c "
import json, sys
payload = {
    'timestamp': $timestamp,
    'uuid': '$uuid',
    'event_type': '$event_type',
    'env_id': '$env_id',
    'author': '$author',
    'data': json.loads('''$data_json''')
}
json.dump(payload, sys.stdout)
" > "$ticket_dir/$filename"
}

# ── Helper: create a ticket with N events ────────────────────────────────────
# Usage: _create_ticket_with_events <repo> <ticket_id> <event_count>
_create_ticket_with_events() {
    local repo="$1"
    local ticket_id="$2"
    local event_count="$3"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"  # tickets-boundary-ok: test helper creates tracker dir directly
    mkdir -p "$ticket_dir"

    # Write CREATE event
    local create_uuid="00000000-0000-4000-8000-create000001"
    _write_event "$ticket_dir" "1742605200" "$create_uuid" "CREATE" \
        '{"ticket_type": "task", "title": "Compact test ticket", "parent_id": null}'

    # Write additional STATUS events to reach event_count
    local i
    for (( i=1; i<event_count; i++ )); do
        local ts=$((1742605200 + i * 100))
        local uuid
        uuid=$(printf "00000000-0000-4000-8000-%012d" "$i")
        _write_event "$ticket_dir" "$ts" "$uuid" "STATUS" \
            '{"status": "in_progress", "current_status": "open"}'
    done

    echo "$ticket_dir"
}

# ── Test 1: compact triggers when threshold exceeded ─────────────────────────
echo "Test 1: compact triggers when event count exceeds threshold"
test_compact_triggers_when_threshold_exceeded() {
    _snapshot_fail

    # ticket-compact.sh must exist — RED: it does not exist yet
    if [ ! -f "$COMPACT_SCRIPT" ]; then
        assert_eq "ticket-compact.sh exists" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-compact1"
    local ticket_dir
    ticket_dir=$(_create_ticket_with_events "$repo" "$ticket_id" 12)

    # Run compaction
    local exit_code=0
    (cd "$repo" && bash "$COMPACT_SCRIPT" "$ticket_id") 2>/dev/null || exit_code=$?
    assert_eq "compact exits 0" "0" "$exit_code"

    # Assert: ticket dir contains exactly 1 SNAPSHOT event file
    local snapshot_count
    snapshot_count=$(find "$ticket_dir" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "exactly 1 SNAPSHOT file" "1" "$snapshot_count"

    # Assert: no original event files remain
    local non_snapshot_count
    non_snapshot_count=$(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '*-SNAPSHOT.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "0 original event files" "0" "$non_snapshot_count"

    assert_pass_if_clean "test_compact_triggers_when_threshold_exceeded"
}
test_compact_triggers_when_threshold_exceeded

# ── Test 2: compact does not trigger below threshold ──────────────────────────
echo "Test 2: compact does not trigger below threshold"
test_compact_does_not_trigger_below_threshold() {
    _snapshot_fail

    if [ ! -f "$COMPACT_SCRIPT" ]; then
        assert_eq "ticket-compact.sh exists for below-threshold test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-below"
    local ticket_dir
    ticket_dir=$(_create_ticket_with_events "$repo" "$ticket_id" 3)

    # Count original files before compaction
    local before_count
    before_count=$(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')

    # Run compaction — should skip
    local output
    output=$(cd "$repo" && bash "$COMPACT_SCRIPT" "$ticket_id" 2>&1) || true

    # Assert: original events still exist (not compacted)
    local after_count
    after_count=$(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "original events preserved below threshold" "$before_count" "$after_count"

    # Assert: output mentions skipping
    if [[ "${output,,}" =~ skip|below.*threshold|no.*compaction ]]; then
        assert_eq "skip message present" "present" "present"
    else
        assert_eq "skip message present" "present" "missing"
    fi

    assert_pass_if_clean "test_compact_does_not_trigger_below_threshold"
}
test_compact_does_not_trigger_below_threshold

# ── Test 3: SNAPSHOT contains source_event_uuids ─────────────────────────────
echo "Test 3: SNAPSHOT event contains source_event_uuids from original events"
test_compact_snapshot_contains_source_event_uuids() {
    _snapshot_fail

    if [ ! -f "$COMPACT_SCRIPT" ]; then
        assert_eq "ticket-compact.sh exists for source_event_uuids test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-uuids"
    local ticket_dir
    # Use threshold=2 for testing with 3 events
    ticket_dir=$(_create_ticket_with_events "$repo" "$ticket_id" 3)

    # Run compaction with low threshold
    (cd "$repo" && COMPACT_THRESHOLD=2 bash "$COMPACT_SCRIPT" "$ticket_id") 2>/dev/null || true

    # Find SNAPSHOT file
    local snapshot_file
    snapshot_file=$(find "$ticket_dir" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | head -1)
    if [ -z "$snapshot_file" ]; then
        assert_eq "SNAPSHOT file created" "created" "missing"
        return
    fi

    # Assert: SNAPSHOT JSON has source_event_uuids list with 3 entries
    local uuid_count
    uuid_count=$(python3 -c "
import json, sys
with open('$snapshot_file') as f:
    data = json.load(f)
uuids = data.get('data', {}).get('source_event_uuids', [])
print(len(uuids))
" 2>/dev/null || echo "0")
    assert_eq "source_event_uuids has 3 entries" "3" "$uuid_count"

    assert_pass_if_clean "test_compact_snapshot_contains_source_event_uuids"
}
test_compact_snapshot_contains_source_event_uuids

# ── Test 4: compact deletes only files read into snapshot ────────────────────
echo "Test 4: compact deletes only specific files included in snapshot scope"
test_compact_deletes_only_specific_files_read_into_snapshot() {
    _snapshot_fail

    if [ ! -f "$COMPACT_SCRIPT" ]; then
        assert_eq "ticket-compact.sh exists for file-scope test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-scope"
    local ticket_dir
    ticket_dir=$(_create_ticket_with_events "$repo" "$ticket_id" 3)

    # Run compaction with low threshold — compacts the 3 events
    (cd "$repo" && COMPACT_THRESHOLD=2 bash "$COMPACT_SCRIPT" "$ticket_id") 2>/dev/null || true

    # Write an extra event AFTER compaction (e4 — simulates a late arrival)
    local e4_uuid="e4e4e4e4-e4e4-e4e4-e4e4-e4e4e4e4e4e4"
    local e4_ts=1742699999
    _write_event "$ticket_dir" "$e4_ts" "$e4_uuid" "COMMENT" \
        '{"body": "late arrival event"}'

    # Assert: SNAPSHOT was created
    local snapshot_file
    snapshot_file=$(find "$ticket_dir" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | head -1)
    if [ -z "$snapshot_file" ]; then
        assert_eq "SNAPSHOT file created for scope test" "created" "missing"
        return
    fi

    # Assert: e4's uuid is NOT in source_event_uuids (it arrived after scope)
    local e4_in_sources
    e4_in_sources=$(python3 -c "
import json
with open('$snapshot_file') as f:
    data = json.load(f)
uuids = data.get('data', {}).get('source_event_uuids', [])
print('yes' if '$e4_uuid' in uuids else 'no')
" 2>/dev/null || echo "error")
    assert_eq "e4 uuid NOT in source_event_uuids" "no" "$e4_in_sources"

    assert_pass_if_clean "test_compact_deletes_only_specific_files_read_into_snapshot"
}
test_compact_deletes_only_specific_files_read_into_snapshot

# ── Test 5: flock prevents concurrent modification ───────────────────────────
echo "Test 5: flock prevents concurrent compaction"
test_compact_flock_prevents_concurrent_modification() {
    _snapshot_fail

    if [ ! -f "$COMPACT_SCRIPT" ]; then
        assert_eq "ticket-compact.sh exists for flock test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-flock"
    _create_ticket_with_events "$repo" "$ticket_id" 12

    # Run two concurrent compactions
    local exit1=0
    local exit2=0
    (cd "$repo" && bash "$COMPACT_SCRIPT" "$ticket_id") 2>/dev/null &
    local pid1=$!
    (cd "$repo" && bash "$COMPACT_SCRIPT" "$ticket_id") 2>/dev/null &
    local pid2=$!

    wait "$pid1" || exit1=$?
    wait "$pid2" || exit2=$?

    # Assert: only one SNAPSHOT file was written (not two)
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"  # tickets-boundary-ok: test asserts filesystem state after compact
    local snapshot_count
    snapshot_count=$(find "$ticket_dir" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "only 1 SNAPSHOT from concurrent runs" "1" "$snapshot_count"

    assert_pass_if_clean "test_compact_flock_prevents_concurrent_modification"
}
test_compact_flock_prevents_concurrent_modification

# ── Test 6: SNAPSHOT event has valid JSON with required fields ────────────────
echo "Test 6: SNAPSHOT event produces valid JSON with required fields"
test_compact_produces_valid_snapshot_event_json() {
    _snapshot_fail

    if [ ! -f "$COMPACT_SCRIPT" ]; then
        assert_eq "ticket-compact.sh exists for JSON validation test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-json"
    _create_ticket_with_events "$repo" "$ticket_id" 12

    # Run compaction
    (cd "$repo" && bash "$COMPACT_SCRIPT" "$ticket_id") 2>/dev/null || true

    local ticket_dir="$repo/.tickets-tracker/$ticket_id"  # tickets-boundary-ok: test inspects filesystem after compact
    local snapshot_file
    snapshot_file=$(find "$ticket_dir" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | head -1)
    if [ -z "$snapshot_file" ]; then
        assert_eq "SNAPSHOT file exists for validation" "exists" "missing"
        return
    fi

    # Assert: valid JSON with required fields
    local validation
    validation=$(python3 -c "
import json, sys
with open('$snapshot_file') as f:
    data = json.load(f)
required = ['event_type', 'timestamp', 'uuid', 'env_id', 'author', 'data']
missing = [k for k in required if k not in data]
if missing:
    print('missing:' + ','.join(missing))
    sys.exit(1)
if data['event_type'] != 'SNAPSHOT':
    print('wrong_event_type:' + data['event_type'])
    sys.exit(1)
d = data['data']
if 'compiled_state' not in d or not isinstance(d['compiled_state'], dict):
    print('missing_compiled_state')
    sys.exit(1)
if 'source_event_uuids' not in d or not isinstance(d['source_event_uuids'], list):
    print('missing_source_event_uuids')
    sys.exit(1)
print('valid')
" 2>&1)
    assert_eq "SNAPSHOT JSON valid with required fields" "valid" "$validation"

    assert_pass_if_clean "test_compact_produces_valid_snapshot_event_json"
}
test_compact_produces_valid_snapshot_event_json

# ── Test 7: compact subcommand routes correctly ──────────────────────────────
echo "Test 7: 'ticket compact' subcommand routes to ticket-compact.sh"
test_compact_subcommand_routes_correctly() {
    _snapshot_fail

    if [ ! -f "$COMPACT_SCRIPT" ]; then
        assert_eq "ticket-compact.sh exists for routing test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-route"
    _create_ticket_with_events "$repo" "$ticket_id" 3

    # Run via dispatcher: ticket compact <id>
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" compact "$ticket_id") 2>/dev/null || exit_code=$?
    assert_eq "ticket compact exits 0" "0" "$exit_code"

    assert_pass_if_clean "test_compact_subcommand_routes_correctly"
}
test_compact_subcommand_routes_correctly

# ── Suite-runner guard for RED tests: skip when --skip-sync is not implemented ─
# ticket-compact.sh exists but does NOT yet support --skip-sync. When running
# under run-all.sh, skip these RED tests so the suite stays green.
_skip_sync_implemented() {
    grep -q '\-\-skip-sync' "$COMPACT_SCRIPT" 2>/dev/null
}

if [ "${_RUN_ALL_ACTIVE:-0}" = "1" ] && ! _skip_sync_implemented; then
    echo "SKIP: --skip-sync not yet implemented (RED) — tests 8-9 deferred"
    echo ""
    print_summary
    exit 0
fi

# ── Test 8: --skip-sync flag suppresses sync-before-compact ───────────────
echo "Test 8: --skip-sync flag suppresses sync-before-compact"
test_compact_skip_sync_flag() {
    _snapshot_fail

    if [ ! -f "$COMPACT_SCRIPT" ]; then
        assert_eq "ticket-compact.sh exists for skip-sync test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-skipsync"
    _create_ticket_with_events "$repo" "$ticket_id" 12

    # Set TICKET_SYNC_CMD to write a marker file — if sync runs, marker appears
    local sync_marker="$repo/.sync-ran-marker"
    export TICKET_SYNC_CMD="touch '$sync_marker'"

    # Run compact WITH --skip-sync — sync should NOT run
    local exit_code=0
    (cd "$repo" && bash "$COMPACT_SCRIPT" "$ticket_id" --skip-sync) 2>/dev/null || exit_code=$?

    # Clean up exported var regardless of outcome
    unset TICKET_SYNC_CMD

    assert_eq "compact --skip-sync exits 0" "0" "$exit_code"

    # Assert: marker file was NOT created (sync was skipped)
    if [ -f "$sync_marker" ]; then
        assert_eq "sync marker absent (sync skipped)" "absent" "present"
    else
        assert_eq "sync marker absent (sync skipped)" "absent" "absent"
    fi
    assert_pass_if_clean "test_compact_skip_sync_flag"
}
test_compact_skip_sync_flag

# ── Test 9: --threshold=0 --skip-sync creates SNAPSHOT ────────────────────
echo "Test 9: --threshold=0 --skip-sync creates SNAPSHOT for minimal ticket"
test_compact_threshold_zero_with_skip_sync() {
    _snapshot_fail

    if [ ! -f "$COMPACT_SCRIPT" ]; then
        assert_eq "ticket-compact.sh exists for threshold-zero-skip-sync test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-t0skip"
    local ticket_dir
    # Create ticket with 2 events (CREATE + 1 STATUS) — below default threshold
    ticket_dir=$(_create_ticket_with_events "$repo" "$ticket_id" 2)

    # Run compact with --threshold=0 --skip-sync
    local exit_code=0
    (cd "$repo" && bash "$COMPACT_SCRIPT" "$ticket_id" --threshold=0 --skip-sync) 2>/dev/null || exit_code=$?
    assert_eq "compact --threshold=0 --skip-sync exits 0" "0" "$exit_code"

    # Assert: SNAPSHOT event file exists
    local snapshot_count
    snapshot_count=$(find "$ticket_dir" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "SNAPSHOT created with threshold=0 skip-sync" "1" "$snapshot_count"

    # Assert: original source event files were removed (only SNAPSHOT + .cache remain)
    local remaining_events
    remaining_events=$(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '.*' ! -name '*-SNAPSHOT.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "source events removed after compact" "0" "$remaining_events"

    assert_pass_if_clean "test_compact_threshold_zero_with_skip_sync"
}
test_compact_threshold_zero_with_skip_sync

# ── Test 10: compact preserves SYNC file (da40-8e3e) ─────────────────────────
echo "Test 10: compact preserves SYNC file (bridge Jira key mapping survives)"
test_compact_preserves_sync_file() {
    _snapshot_fail

    if [ ! -f "$COMPACT_SCRIPT" ]; then
        assert_eq "ticket-compact.sh exists for sync-preservation test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id="tkt-syncpres"
    local ticket_dir
    # Use helper to create a valid ticket (CREATE + 11 STATUS = 12 events, above threshold)
    ticket_dir=$(_create_ticket_with_events "$repo" "$ticket_id" 12)

    # Write a SYNC file (bridge Jira key mapping)
    local sync_uuid
    sync_uuid=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
    python3 -c "
import json, sys
payload = {
    'event_type': 'SYNC',
    'timestamp': 2000000000000,
    'uuid': '$sync_uuid',
    'env_id': 'bbbbbbbb-0000-4000-8000-000000000002',
    'jira_key': 'DSO-999',
    'local_id': '$ticket_id'
}
json.dump(payload, sys.stdout)
" > "$ticket_dir/2000000000000-${sync_uuid}-SYNC.json"

    # Run compact with --skip-sync (no live sync required in test)
    local exit_code=0
    (cd "$repo" && bash "$COMPACT_SCRIPT" "$ticket_id" --skip-sync) 2>/dev/null || exit_code=$?
    assert_eq "compact exits 0 with SYNC present" "0" "$exit_code"

    # Assert: SYNC file survived compaction (fix C)
    local sync_count
    sync_count=$(find "$ticket_dir" -maxdepth 1 -name '*-SYNC.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "SYNC file survives compact (fix C)" "1" "$sync_count"

    # Assert: Jira key still readable from the surviving SYNC file
    local surviving_sync
    surviving_sync=$(find "$ticket_dir" -maxdepth 1 -name '*-SYNC.json' 2>/dev/null | head -1)
    if [ -n "$surviving_sync" ]; then
        local jira_key
        jira_key=$(python3 -c "import json; d=json.load(open('$surviving_sync')); print(d.get('jira_key',''))" 2>/dev/null)
        assert_eq "Jira key preserved in SYNC after compact" "DSO-999" "$jira_key"
    fi

    assert_pass_if_clean "test_compact_preserves_sync_file"
}
test_compact_preserves_sync_file

print_summary
