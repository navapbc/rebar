#!/usr/bin/env bash
# tests/scripts/test-ticket-archive-markers-backfill.sh
# RED tests for ticket-archive-markers-backfill.sh — .archived marker backfill utility.
#
# Tests:
#   1. Script creates .archived marker for each ticket dir that has an ARCHIVED
#      event but no existing .archived file
#   2. Script skips ticket dirs that already have .archived marker (idempotent)
#   3. Script is idempotent — running twice produces the same result
#   4. Script does not create marker for tickets with no ARCHIVED event
#   5. Script outputs count of markers written
#
# All tests MUST FAIL until ticket-archive-markers-backfill.sh is implemented.
# Testing mode: RED
#
# Usage: bash tests/scripts/test-ticket-archive-markers-backfill.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
BACKFILL_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-archive-markers-backfill.sh"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-archive-markers-backfill.sh ==="
echo ""

# ── Suite-runner guard: skip RED tests when backfill script not yet present ───
if [[ "${_RUN_ALL_ACTIVE:-0}" == "1" ]]; then
    if [ ! -f "$BACKFILL_SCRIPT" ]; then
        echo "SKIP: ticket-archive-markers-backfill.sh not yet implemented (RED tests)"
        echo ""
        printf "PASSED: 0  FAILED: 0\n"
        exit 0
    fi
fi

# ── Helper: create a fresh temp git repo with ticket system initialized ────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: create a ticket and return its ID ─────────────────────────────────
_create_ticket() {
    local repo="$1"
    local ticket_type="${2:-task}"
    local title="${3:-Test ticket}"
    local out
    out=$(cd "$repo" && bash "$TICKET_SCRIPT" create "$ticket_type" "$title" 2>/dev/null) || true
    echo "$out" | tail -1
}

# ── Helper: write an ARCHIVED event JSON directly into a ticket dir ───────────
_write_archived_event() {
    local tracker_dir="$1"
    local ticket_id="$2"
    local ts
    ts=$(date +%s)
    local uuid
    uuid="arch-$(printf '%04x' $RANDOM)"
    local event_file="$tracker_dir/$ticket_id/${ts}-${uuid}-ARCHIVED.json"
    python3 -c "
import json, sys
event = {
    'timestamp': int(sys.argv[1]),
    'uuid': sys.argv[2],
    'event_type': 'ARCHIVED',
    'env_id': 'test',
    'author': 'test',
    'data': {}
}
with open(sys.argv[3], 'w') as f:
    json.dump(event, f)
" "$ts" "$uuid" "$event_file"
    git -C "$tracker_dir" add "$ticket_id/" 2>/dev/null
    git -C "$tracker_dir" commit -q --no-verify -m "test: ARCHIVED event for $ticket_id" 2>/dev/null || true
}

# ── Helper: count .archived markers in tracker dir ────────────────────────────
_marker_count() {
    local tracker_dir="$1"
    find "$tracker_dir" -maxdepth 2 -name '.archived' 2>/dev/null | wc -l | tr -d ' '
}

# ── Helper: run backfill with TICKET_TRACKER_DIR injection ────────────────────
_run_backfill() {
    local tracker_dir="$1"
    shift
    TICKET_TRACKER_DIR="$tracker_dir" bash "$BACKFILL_SCRIPT" "$@"
}

# ===========================================================================
# test_creates_marker_for_ticket_with_archived_event
#
# Given: a ticket directory that has an ARCHIVED event but no .archived file
# When: ticket-archive-markers-backfill.sh runs
# Then: .archived marker file is created in that ticket directory
# ===========================================================================
echo "Test 1: creates .archived marker for ticket with ARCHIVED event"
test_creates_marker_for_ticket_with_archived_event() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Ticket to archive")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_creates_marker_for_ticket_with_archived_event"
        return
    fi

    # Write ARCHIVED event — no .archived marker yet
    _write_archived_event "$tracker_dir" "$ticket_id"

    assert_eq "before: no .archived marker" "0" "$(_marker_count "$tracker_dir")"

    # Run backfill
    local exit_code=0
    _run_backfill "$tracker_dir" >/dev/null 2>&1 || exit_code=$?

    assert_eq "backfill exits 0" "0" "$exit_code"

    # Assert: .archived marker was created
    local marker_file="$tracker_dir/$ticket_id/.archived"
    if [ -f "$marker_file" ]; then
        assert_eq "marker file exists" "yes" "yes"
    else
        assert_eq "marker file exists" "yes" "no"
    fi

    assert_pass_if_clean "test_creates_marker_for_ticket_with_archived_event"
}
test_creates_marker_for_ticket_with_archived_event

# ===========================================================================
# test_skips_ticket_with_existing_marker
#
# Given: a ticket that has an ARCHIVED event and already has a .archived file
# When: ticket-archive-markers-backfill.sh runs
# Then: the existing .archived marker is preserved; no error; still exactly 1 marker
# ===========================================================================
echo "Test 2: skips ticket dirs that already have .archived marker"
test_skips_ticket_with_existing_marker() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Already marked ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_skips_ticket_with_existing_marker"
        return
    fi

    # Write ARCHIVED event and pre-place .archived marker
    _write_archived_event "$tracker_dir" "$ticket_id"
    touch "$tracker_dir/$ticket_id/.archived"

    assert_eq "before: marker already present" "1" "$(_marker_count "$tracker_dir")"

    # Record mtime before backfill to confirm marker is not replaced
    local mtime_before
    mtime_before=$(stat -f "%m" "$tracker_dir/$ticket_id/.archived" 2>/dev/null || stat -c "%Y" "$tracker_dir/$ticket_id/.archived" 2>/dev/null || echo "unknown")

    # Run backfill
    local exit_code=0
    _run_backfill "$tracker_dir" >/dev/null 2>&1 || exit_code=$?

    assert_eq "backfill exits 0" "0" "$exit_code"

    # Assert: still exactly 1 marker (not doubled)
    assert_eq "still exactly 1 marker after backfill" "1" "$(_marker_count "$tracker_dir")"

    assert_pass_if_clean "test_skips_ticket_with_existing_marker"
}
test_skips_ticket_with_existing_marker

# ===========================================================================
# test_idempotent_running_twice_same_result
#
# Given: a ticket dir with an ARCHIVED event and no .archived marker
# When: ticket-archive-markers-backfill.sh runs twice
# Then: both runs succeed; marker count stays at 1 after both runs
# ===========================================================================
echo "Test 3: idempotent — running twice produces same result"
test_idempotent_running_twice_same_result() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Idempotent test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_idempotent_running_twice_same_result"
        return
    fi

    _write_archived_event "$tracker_dir" "$ticket_id"

    # First run
    local exit_code1=0
    _run_backfill "$tracker_dir" >/dev/null 2>&1 || exit_code1=$?

    local markers_after_first
    markers_after_first=$(_marker_count "$tracker_dir")

    # Second run
    local exit_code2=0
    _run_backfill "$tracker_dir" >/dev/null 2>&1 || exit_code2=$?

    local markers_after_second
    markers_after_second=$(_marker_count "$tracker_dir")

    assert_eq "first run exits 0" "0" "$exit_code1"
    assert_eq "second run exits 0" "0" "$exit_code2"
    assert_eq "first run: 1 marker" "1" "$markers_after_first"
    assert_eq "second run: still 1 marker" "$markers_after_first" "$markers_after_second"

    assert_pass_if_clean "test_idempotent_running_twice_same_result"
}
test_idempotent_running_twice_same_result

# ===========================================================================
# test_no_marker_for_ticket_without_archived_event
#
# Given: a ticket directory that has no ARCHIVED event
# When: ticket-archive-markers-backfill.sh runs
# Then: no .archived marker is created for that ticket
# ===========================================================================
echo "Test 4: does not create marker for ticket with no ARCHIVED event"
test_no_marker_for_ticket_without_archived_event() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create ticket but do NOT write an ARCHIVED event
    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Non-archived ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_no_marker_for_ticket_without_archived_event"
        return
    fi

    assert_eq "before: no .archived marker" "0" "$(_marker_count "$tracker_dir")"

    local exit_code=0
    _run_backfill "$tracker_dir" >/dev/null 2>&1 || exit_code=$?

    assert_eq "backfill exits 0" "0" "$exit_code"

    # Assert: no marker was created
    assert_eq "after: still no .archived marker" "0" "$(_marker_count "$tracker_dir")"

    assert_pass_if_clean "test_no_marker_for_ticket_without_archived_event"
}
test_no_marker_for_ticket_without_archived_event

# ===========================================================================
# test_output_reports_count_of_markers_written
#
# Given: two tickets with ARCHIVED events and no .archived markers,
#        one ticket with no ARCHIVED event
# When: ticket-archive-markers-backfill.sh runs
# Then: output contains a count of 2 markers written
# ===========================================================================
echo "Test 5: outputs count of markers written"
test_output_reports_count_of_markers_written() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Two tickets to be marked
    local id_a id_b id_c
    id_a=$(_create_ticket "$repo" task "Archived one")
    id_b=$(_create_ticket "$repo" task "Archived two")
    id_c=$(_create_ticket "$repo" task "Not archived")

    if [ -z "$id_a" ] || [ -z "$id_b" ] || [ -z "$id_c" ]; then
        assert_eq "all three tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_output_reports_count_of_markers_written"
        return
    fi

    _write_archived_event "$tracker_dir" "$id_a"
    _write_archived_event "$tracker_dir" "$id_b"
    # id_c: no ARCHIVED event

    local output
    local exit_code=0
    output=$(_run_backfill "$tracker_dir" 2>&1) || exit_code=$?

    assert_eq "backfill exits 0" "0" "$exit_code"

    # Assert: output mentions 2 markers written.
    # The script emits a line like "Wrote 2 markers, skipped 0 (already present)".
    local count_line
    count_line=$(echo "$output" | grep -iE 'wrote 2|written[: ]+2|2 marker' | head -1 || echo "")
    if [ -n "$count_line" ]; then
        assert_eq "output contains count 2" "yes" "yes"
    else
        assert_eq "output contains count 2" "yes (a line containing '2')" "no match in: $output"
    fi

    assert_pass_if_clean "test_output_reports_count_of_markers_written"
}
test_output_reports_count_of_markers_written

# ── Run summary ───────────────────────────────────────────────────────────────
print_summary
