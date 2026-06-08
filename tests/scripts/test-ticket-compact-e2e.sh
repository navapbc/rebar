#!/usr/bin/env bash
# tests/scripts/test-ticket-compact-e2e.sh
# End-to-end test for the full compaction flow:
#   init → create → add events → compact → verify SNAPSHOT and state
#
# Covers:
#   test_compact_e2e_full_flow           — full compaction: SNAPSHOT only, correct state
#   test_compact_e2e_below_threshold_skips — below threshold: original events preserved
#   test_compact_e2e_configurable_threshold — custom COMPACT_THRESHOLD respected
#   test_compact_e2e_new_events_after_compaction — new events work after compaction
#
# Usage: bash tests/scripts/test-ticket-compact-e2e.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e is intentionally omitted — test functions return non-zero by design.
# -e would abort the runner on expected assertion mismatches.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
COMPACT_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-compact.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-compact-e2e.sh ==="

# ── Suite-runner guard: skip when ticket-compact.sh does not exist ────────────
if [ "${_RUN_ALL_ACTIVE:-0}" = "1" ] && [ ! -f "$COMPACT_SCRIPT" ]; then
    echo "SKIP: ticket-compact.sh not yet implemented — E2E tests deferred"
    echo ""
    printf "PASSED: 0  FAILED: 0\n"
    exit 0
fi

# ── Helper: create a fresh temp git repo with ticket system initialized ────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: write an event file directly to a ticket dir ──────────────────────
# Usage: _write_event_file <ticket_dir> <timestamp> <uuid> <event_type> <data_json>
_write_event_file() {
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

# ── Helper: create a ticket with N total events (1 CREATE + N-1 STATUS) ───────
# Usage: _setup_ticket_with_events <repo> <ticket_id> <event_count>
# Returns the ticket dir path via stdout.
_setup_ticket_with_events() {
    local repo="$1"
    local ticket_id="$2"
    local event_count="$3"
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"

    # Write CREATE event
    local create_uuid="00000000-0000-4000-8000-create000001"
    _write_event_file "$ticket_dir" "1742605200" "$create_uuid" "CREATE" \
        '{"ticket_type": "task", "title": "Compaction E2E test", "parent_id": null}'

    # Write additional STATUS events to reach event_count
    local i
    for (( i=1; i<event_count; i++ )); do
        local ts=$((1742605200 + i * 100))
        local uuid
        uuid=$(printf "00000000-0000-4000-8000-%012d" "$i")
        _write_event_file "$ticket_dir" "$ts" "$uuid" "STATUS" \
            '{"status": "in_progress", "current_status": "open"}'
    done

    echo "$ticket_dir"
}

# ── Test 1: Full compaction flow ───────────────────────────────────────────────
echo "Test 1: full compaction flow — init → create → add events → compact → verify"
test_compact_e2e_full_flow() {
    _snapshot_fail

    if [ ! -f "$COMPACT_SCRIPT" ]; then
        assert_eq "ticket-compact.sh exists" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # Create ticket with 13 events (1 CREATE + 12 STATUS — above default threshold of 10)
    local ticket_id="tkt-e2e-full"
    local ticket_dir
    ticket_dir=$(_setup_ticket_with_events "$repo" "$ticket_id" 13)

    # Verify pre-compaction: 13 event files exist
    local pre_count
    pre_count=$(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "pre-compaction: 13 event files" "13" "$pre_count"

    # Run compaction
    local compact_exit=0
    (cd "$repo" && bash "$COMPACT_SCRIPT" "$ticket_id") 2>/dev/null || compact_exit=$?
    assert_eq "compact exits 0" "0" "$compact_exit"

    # Verify: exactly 1 SNAPSHOT event file remains
    local snapshot_count
    snapshot_count=$(find "$ticket_dir" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "exactly 1 SNAPSHOT file after compaction" "1" "$snapshot_count"

    # Verify: no original event files remain (only SNAPSHOT and hidden files)
    local non_snapshot_count
    non_snapshot_count=$(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '*-SNAPSHOT.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "0 original event files after compaction" "0" "$non_snapshot_count"

    # Verify: ticket show returns correct state (title and status)
    local show_out
    show_out=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    local title_check
    title_check=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    print(data.get('title', ''))
except Exception:
    print('')
" "$show_out" 2>/dev/null) || true
    assert_eq "ticket show: title preserved after compaction" "Compaction E2E test" "$title_check"

    local type_check
    type_check=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    print(data.get('ticket_type', ''))
except Exception:
    print('')
" "$show_out" 2>/dev/null) || true
    assert_eq "ticket show: ticket_type preserved after compaction" "task" "$type_check"

    # Verify: SNAPSHOT has source_event_uuids with 13 entries
    local snapshot_file
    snapshot_file=$(find "$ticket_dir" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | head -1)
    if [ -n "$snapshot_file" ]; then
        local uuid_count
        uuid_count=$(python3 -c "
import json
with open('$snapshot_file') as f:
    data = json.load(f)
uuids = data.get('data', {}).get('source_event_uuids', [])
print(len(uuids))
" 2>/dev/null || echo "0")
        assert_eq "SNAPSHOT source_event_uuids has 13 entries" "13" "$uuid_count"
    else
        assert_eq "SNAPSHOT file found for uuid count check" "found" "missing"
    fi

    assert_pass_if_clean "test_compact_e2e_full_flow"
}
test_compact_e2e_full_flow

# ── Test 2: Below threshold — original events preserved ────────────────────────
echo "Test 2: below_threshold — original events preserved when count <= threshold"
test_compact_e2e_below_threshold_skips() {
    _snapshot_fail

    if [ ! -f "$COMPACT_SCRIPT" ]; then
        assert_eq "ticket-compact.sh exists for below-threshold test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # Create ticket with 5 events (below default threshold of 10)
    local ticket_id="tkt-e2e-below"
    local ticket_dir
    ticket_dir=$(_setup_ticket_with_events "$repo" "$ticket_id" 5)

    local before_count
    before_count=$(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')

    # Run compaction — should skip with "below threshold" message
    local output
    output=$(cd "$repo" && bash "$COMPACT_SCRIPT" "$ticket_id" 2>&1) || true

    # Verify: original events still intact
    local after_count
    after_count=$(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "below_threshold: original events preserved" "$before_count" "$after_count"

    # Verify: output mentions skipping / below threshold
    if [[ "${output,,}" =~ skip|below.*threshold|no.*compaction ]]; then
        assert_eq "below_threshold: skip message present" "present" "present"
    else
        assert_eq "below_threshold: skip message present" "present" "missing"
    fi

    # Verify: no SNAPSHOT written
    local snapshot_count
    snapshot_count=$(find "$ticket_dir" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "below_threshold: no SNAPSHOT written" "0" "$snapshot_count"

    assert_pass_if_clean "test_compact_e2e_below_threshold_skips"
}
test_compact_e2e_below_threshold_skips

# ── Test 3: Configurable threshold via COMPACT_THRESHOLD env var ───────────────
echo "Test 3: configurable_threshold — COMPACT_THRESHOLD=3 triggers on 4 events"
test_compact_e2e_configurable_threshold() {
    _snapshot_fail

    if [ ! -f "$COMPACT_SCRIPT" ]; then
        assert_eq "ticket-compact.sh exists for configurable-threshold test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # Create ticket with 4 events (above custom threshold of 3)
    local ticket_id="tkt-e2e-threshold"
    local ticket_dir
    ticket_dir=$(_setup_ticket_with_events "$repo" "$ticket_id" 4)

    # Run compaction with custom threshold=3
    local compact_exit=0
    (cd "$repo" && COMPACT_THRESHOLD=3 bash "$COMPACT_SCRIPT" "$ticket_id") 2>/dev/null || compact_exit=$?
    assert_eq "configurable_threshold: compact exits 0" "0" "$compact_exit"

    # Verify: compaction triggered (SNAPSHOT exists)
    local snapshot_count
    snapshot_count=$(find "$ticket_dir" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "configurable_threshold: SNAPSHOT created (4 > 3)" "1" "$snapshot_count"

    # Verify: original events removed
    local non_snapshot_count
    non_snapshot_count=$(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '*-SNAPSHOT.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "configurable_threshold: original events removed" "0" "$non_snapshot_count"

    assert_pass_if_clean "test_compact_e2e_configurable_threshold"
}
test_compact_e2e_configurable_threshold

# ── Test 4: New events after compaction work correctly ─────────────────────────
echo "Test 4: new events after compaction work correctly"
test_compact_e2e_new_events_after_compaction() {
    _snapshot_fail

    if [ ! -f "$COMPACT_SCRIPT" ]; then
        assert_eq "ticket-compact.sh exists for post-compaction events test" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # Create ticket with 13 events (above default threshold of 10)
    local ticket_id="tkt-e2e-post"
    local ticket_dir
    ticket_dir=$(_setup_ticket_with_events "$repo" "$ticket_id" 13)

    # Run compaction
    local compact_exit=0
    (cd "$repo" && bash "$COMPACT_SCRIPT" "$ticket_id") 2>/dev/null || compact_exit=$?
    assert_eq "post-compaction events: compact exits 0" "0" "$compact_exit"

    # Verify SNAPSHOT was written before adding new events
    local snapshot_count
    snapshot_count=$(find "$ticket_dir" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "post-compaction events: SNAPSHOT present" "1" "$snapshot_count"

    # Write a new STATUS event after compaction.
    # Use a far-future timestamp so the event file sorts lexicographically AFTER
    # the SNAPSHOT file (ticket-compact.sh uses current epoch for SNAPSHOT filename).
    # The reducer's two-pass logic skips events whose filename sorts before the
    # latest SNAPSHOT, so post-compaction events must have later timestamps.
    local post_ts=9999999999
    local post_uuid="post0000-post-post-post-post00000001"
    _write_event_file "$ticket_dir" "$post_ts" "$post_uuid" "STATUS" \
        '{"status": "closed", "current_status": "in_progress"}'

    # Verify: ticket show returns updated state reflecting new event
    local show_out
    show_out=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    local status_check
    status_check=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    print(data.get('status', ''))
except Exception:
    print('')
" "$show_out" 2>/dev/null) || true
    assert_eq "post-compaction events: new STATUS applied" "closed" "$status_check"

    # Verify: title still correct (SNAPSHOT state preserved)
    local title_check
    title_check=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    print(data.get('title', ''))
except Exception:
    print('')
" "$show_out" 2>/dev/null) || true
    assert_eq "post-compaction events: title from SNAPSHOT preserved" "Compaction E2E test" "$title_check"

    assert_pass_if_clean "test_compact_e2e_new_events_after_compaction"
}
test_compact_e2e_new_events_after_compaction

print_summary
