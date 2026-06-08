#!/usr/bin/env bash
# tests/scripts/test-ticket-lifecycle.sh
# RED tests for src/rebar/_engine/ticket-lifecycle.sh — bulk compact and archive.
#
# All tests MUST FAIL until ticket-lifecycle.sh is implemented.
# Covers: bulk compaction (threshold-based), archive of eligible closed tickets,
# preservation of ineligible tickets, single git commit, configurable base path.
#
# Usage: bash tests/scripts/test-ticket-lifecycle.sh
# Returns: exit non-zero (RED) until ticket-lifecycle.sh is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
LIFECYCLE_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-lifecycle.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-lifecycle.sh ==="

# ── Suite-runner guard: skip when ticket-lifecycle.sh does not exist ──────────
# RED tests fail by design (script not found). When auto-discovered by
# run-script-tests.sh, they would break `bash tests/run-all.sh`. Skip with
# exit 0 when ticket-lifecycle.sh is absent AND running under the suite runner.
if [ "${_RUN_ALL_ACTIVE:-0}" = "1" ] && [ ! -f "$LIFECYCLE_SCRIPT" ]; then
    echo "SKIP: ticket-lifecycle.sh not yet implemented (RED) — tests deferred"
    echo ""
    printf "PASSED: 0  FAILED: 0\n"
    exit 0
fi

# ── Helper: create a fresh temp git repo with ticket system initialized ──────
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
    local ticket_dir="$repo/.tickets-tracker/$ticket_id"
    mkdir -p "$ticket_dir"

    # Write CREATE event
    local create_uuid="00000000-0000-4000-8000-create000001"
    _write_event "$ticket_dir" "1742605200" "$create_uuid" "CREATE" \
        '{"ticket_type": "task", "title": "Lifecycle test ticket", "parent_id": null}'

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

# ── Helper: add a LINK event for dependency edges ───────────────────────────
# Usage: _add_link_event <tracker_dir> <source_id> <target_id> <relation>
_add_link_event() {
    local tracker_dir="$1"
    local source_id="$2"
    local target_id="$3"
    local relation="$4"
    local ts
    ts=$(date +%s)
    local link_uuid
    link_uuid="link-$(printf '%04x%04x' $RANDOM $RANDOM)"
    local event_file="$tracker_dir/$source_id/${ts}-${link_uuid}-LINK.json"
    python3 -c "
import json, sys
event = {
    'timestamp': int(sys.argv[1]),
    'uuid': sys.argv[2],
    'event_type': 'LINK',
    'env_id': 'test',
    'author': 'test',
    'data': {
        'target_id': sys.argv[3],
        'relation': sys.argv[4]
    }
}
with open(sys.argv[5], 'w') as f:
    json.dump(event, f)
" "$ts" "$link_uuid" "$target_id" "$relation" "$event_file"
    git -C "$tracker_dir" add "$source_id/" 2>/dev/null
    git -C "$tracker_dir" commit -q --no-verify -m "test: LINK $source_id -> $target_id ($relation)" 2>/dev/null || true
}

# ── Helper: close a ticket by writing a STATUS event ────────────────────────
_close_ticket() {
    local tracker_dir="$1"
    local ticket_id="$2"
    local ticket_dir="$tracker_dir/$ticket_id"
    local ts
    ts=$(date +%s)
    local uuid
    uuid="close-$(printf '%04x%04x' $RANDOM $RANDOM)"
    _write_event "$ticket_dir" "$ts" "$uuid" "STATUS" \
        '{"status": "closed", "current_status": "in_progress"}'
    git -C "$tracker_dir" add "$ticket_id/" 2>/dev/null
    git -C "$tracker_dir" commit -q --no-verify -m "test: close $ticket_id" 2>/dev/null || true
}

# ── Helper: commit all tracker changes ──────────────────────────────────────
_commit_tracker() {
    local tracker_dir="$1"
    git -C "$tracker_dir" add -A 2>/dev/null
    git -C "$tracker_dir" commit -q --no-verify -m "test: tracker fixture setup" 2>/dev/null || true
}

# ── Test 1: bulk compact only compacts tickets above threshold ───────────────
echo "Test 1: lifecycle bulk compact applies only to tickets above 10-event threshold"
test_lifecycle_bulk_compact() {
    _snapshot_fail

    # ticket-lifecycle.sh must exist — RED: it does not exist yet
    if [ ! -f "$LIFECYCLE_SCRIPT" ]; then
        assert_eq "ticket-lifecycle.sh exists" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create 3 tickets: 15 events, 5 events, 12 events
    local dir_a dir_b dir_c
    dir_a=$(_create_ticket_with_events "$repo" "tkt-15ev" 15)
    dir_b=$(_create_ticket_with_events "$repo" "tkt-05ev" 5)
    dir_c=$(_create_ticket_with_events "$repo" "tkt-12ev" 12)

    # Commit fixtures to tracker
    _commit_tracker "$tracker_dir"

    # Run lifecycle
    local exit_code=0
    (cd "$repo" && bash "$LIFECYCLE_SCRIPT") 2>/dev/null || exit_code=$?
    assert_eq "lifecycle exits 0" "0" "$exit_code"

    # Assert: tkt-15ev got compacted (SNAPSHOT created)
    local snap_15
    snap_15=$(find "$dir_a" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "tkt-15ev has SNAPSHOT" "1" "$snap_15"

    # Assert: tkt-05ev was NOT compacted (below threshold)
    local snap_05
    snap_05=$(find "$dir_b" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "tkt-05ev has no SNAPSHOT" "0" "$snap_05"

    # Assert: tkt-12ev got compacted (SNAPSHOT created)
    local snap_12
    snap_12=$(find "$dir_c" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "tkt-12ev has SNAPSHOT" "1" "$snap_12"

    assert_pass_if_clean "test_lifecycle_bulk_compact"
}
test_lifecycle_bulk_compact

# ── Test 2: lifecycle archives eligible closed tickets ───────────────────────
echo "Test 2: lifecycle archives eligible closed tickets"
test_lifecycle_archives_eligible_tickets() {
    _snapshot_fail

    if [ ! -f "$LIFECYCLE_SCRIPT" ]; then
        assert_eq "ticket-lifecycle.sh exists" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create a closed ticket with >10 events and no open dependents
    local ticket_id="tkt-archive"
    local ticket_dir
    ticket_dir=$(_create_ticket_with_events "$repo" "$ticket_id" 12)
    _commit_tracker "$tracker_dir"

    # Close the ticket
    _close_ticket "$tracker_dir" "$ticket_id"

    # Run lifecycle (compact phase runs first, then archive)
    local exit_code=0
    (cd "$repo" && bash "$LIFECYCLE_SCRIPT") 2>/dev/null || exit_code=$?
    assert_eq "lifecycle exits 0" "0" "$exit_code"

    # Assert: ARCHIVED event written for the eligible closed ticket
    local archived_count
    archived_count=$(find "$ticket_dir" -maxdepth 1 -name '*-ARCHIVED.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "ARCHIVED event written" "1" "$archived_count"

    assert_pass_if_clean "test_lifecycle_archives_eligible_tickets"
}
test_lifecycle_archives_eligible_tickets

# ── Test 3: lifecycle preserves ineligible tickets ───────────────────────────
echo "Test 3: lifecycle preserves ineligible tickets (open dependents)"
test_lifecycle_preserves_ineligible_tickets() {
    _snapshot_fail

    if [ ! -f "$LIFECYCLE_SCRIPT" ]; then
        assert_eq "ticket-lifecycle.sh exists" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create ticket A (open) that depends_on ticket B (closed)
    # B is reachable from open A, so B is ineligible for archive
    local dir_a dir_b
    dir_a=$(_create_ticket_with_events "$repo" "tkt-open-a" 12)
    dir_b=$(_create_ticket_with_events "$repo" "tkt-closed-b" 12)
    _commit_tracker "$tracker_dir"

    # A depends_on B
    _add_link_event "$tracker_dir" "tkt-open-a" "tkt-closed-b" "depends_on"

    # Close B (A stays open)
    _close_ticket "$tracker_dir" "tkt-closed-b"

    # Run lifecycle
    local exit_code=0
    (cd "$repo" && bash "$LIFECYCLE_SCRIPT") 2>/dev/null || exit_code=$?
    assert_eq "lifecycle exits 0" "0" "$exit_code"

    # Assert: NO ARCHIVED event on B (it's reachable from open A)
    local archived_count
    archived_count=$(find "$dir_b" -maxdepth 1 -name '*-ARCHIVED.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "no ARCHIVED event on ineligible ticket" "0" "$archived_count"

    assert_pass_if_clean "test_lifecycle_preserves_ineligible_tickets"
}
test_lifecycle_preserves_ineligible_tickets

# ── Test 4: lifecycle produces a single git commit ───────────────────────────
echo "Test 4: lifecycle produces a single git commit for all changes"
test_lifecycle_single_git_commit() {
    _snapshot_fail

    if [ ! -f "$LIFECYCLE_SCRIPT" ]; then
        assert_eq "ticket-lifecycle.sh exists" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create tickets: one compactable (>10 events) and one archivable (closed, >10 events, no deps)
    _create_ticket_with_events "$repo" "tkt-compact-only" 15
    local archive_dir
    archive_dir=$(_create_ticket_with_events "$repo" "tkt-archive-too" 12)
    _commit_tracker "$tracker_dir"

    # Close the archivable ticket
    _close_ticket "$tracker_dir" "tkt-archive-too"

    # Record git log state before lifecycle
    local commits_before
    commits_before=$(git -C "$tracker_dir" rev-list --count HEAD 2>/dev/null || echo "0")

    # Run lifecycle
    local exit_code=0
    (cd "$repo" && bash "$LIFECYCLE_SCRIPT") 2>/dev/null || exit_code=$?
    assert_eq "lifecycle exits 0" "0" "$exit_code"

    # Record git log state after lifecycle
    local commits_after
    commits_after=$(git -C "$tracker_dir" rev-list --count HEAD 2>/dev/null || echo "0")

    # Assert: exactly 1 new commit was added (all compact + archive in one commit)
    local new_commits=$(( commits_after - commits_before ))
    assert_eq "exactly 1 new commit" "1" "$new_commits"

    assert_pass_if_clean "test_lifecycle_single_git_commit"
}
test_lifecycle_single_git_commit

# ── Test 5: lifecycle supports configurable base path ────────────────────────
echo "Test 5: lifecycle supports --base-path flag"
test_lifecycle_configurable_base_path() {
    _snapshot_fail

    if [ ! -f "$LIFECYCLE_SCRIPT" ]; then
        assert_eq "ticket-lifecycle.sh exists" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # Create a custom tracker directory (not the default .tickets-tracker)
    local custom_tracker="$repo/custom-tracker"
    mkdir -p "$custom_tracker"
    git -C "$custom_tracker" init -q -b main 2>/dev/null
    git -C "$custom_tracker" config user.name "Test"
    git -C "$custom_tracker" config user.email "test@test.com"
    git -C "$custom_tracker" commit -q --allow-empty --no-verify -m "init custom tracker" 2>/dev/null

    # Create a ticket with >10 events in the custom tracker
    local ticket_id="tkt-custom"
    local ticket_dir="$custom_tracker/$ticket_id"
    mkdir -p "$ticket_dir"

    # Write CREATE event
    local create_uuid="00000000-0000-4000-8000-create000001"
    _write_event "$ticket_dir" "1742605200" "$create_uuid" "CREATE" \
        '{"ticket_type": "task", "title": "Custom path ticket", "parent_id": null}'

    # Write 11 more STATUS events (12 total, above threshold)
    local i
    for (( i=1; i<12; i++ )); do
        local ts=$((1742605200 + i * 100))
        local uuid
        uuid=$(printf "00000000-0000-4000-8000-%012d" "$i")
        _write_event "$ticket_dir" "$ts" "$uuid" "STATUS" \
            '{"status": "in_progress", "current_status": "open"}'
    done

    git -C "$custom_tracker" add -A 2>/dev/null
    git -C "$custom_tracker" commit -q --no-verify -m "test: custom tracker fixtures" 2>/dev/null

    # Run lifecycle with --base-path pointing to custom tracker
    local exit_code=0
    (cd "$repo" && bash "$LIFECYCLE_SCRIPT" --base-path="$custom_tracker") 2>/dev/null || exit_code=$?
    assert_eq "lifecycle --base-path exits 0" "0" "$exit_code"

    # Assert: SNAPSHOT was created in the custom tracker (compact operated on custom path)
    local snap_count
    snap_count=$(find "$ticket_dir" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "SNAPSHOT in custom tracker path" "1" "$snap_count"

    assert_pass_if_clean "test_lifecycle_configurable_base_path"
}
test_lifecycle_configurable_base_path

# ── Test 6: lifecycle writes .archived marker after ARCHIVED event commit ──────
echo "Test 6: lifecycle writes .archived marker after ARCHIVED event commit"
test_lifecycle_writes_archived_marker() {
    _snapshot_fail

    if [ ! -f "$LIFECYCLE_SCRIPT" ]; then
        assert_eq "ticket-lifecycle.sh exists" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create a closed, archive-eligible ticket (CREATE + STATUS=closed, no ARCHIVED event)
    local ticket_id="tkt-marker"
    local ticket_dir
    ticket_dir=$(_create_ticket_with_events "$repo" "$ticket_id" 12)
    _commit_tracker "$tracker_dir"

    # Close the ticket so it becomes archive-eligible
    _close_ticket "$tracker_dir" "$ticket_id"

    # Run lifecycle
    local exit_code=0
    (cd "$repo" && bash "$LIFECYCLE_SCRIPT") 2>/dev/null || exit_code=$?
    assert_eq "lifecycle exits 0" "0" "$exit_code"

    # Assert: ARCHIVED event file exists
    local archived_event
    archived_event=$(find "$ticket_dir" -maxdepth 1 -name '*-ARCHIVED.json' 2>/dev/null | head -1)
    local archived_count
    archived_count=$(find "$ticket_dir" -maxdepth 1 -name '*-ARCHIVED.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "ARCHIVED event file exists" "1" "$archived_count"

    # Assert: .archived marker file exists in the ticket directory
    local marker_file="$ticket_dir/.archived"
    if [ -f "$marker_file" ]; then
        assert_eq ".archived marker exists" "exists" "exists"
    else
        assert_eq ".archived marker exists" "exists" "missing"
        return
    fi

    assert_pass_if_clean "test_lifecycle_writes_archived_marker"
}
test_lifecycle_writes_archived_marker

# ── Test 7: .archived marker NOT written when git commit fails (SC2 write-ordering) ──
echo "Test 7: .archived marker is NOT written when git commit fails"
test_lifecycle_no_marker_on_commit_failure() {
    _snapshot_fail

    if [ ! -f "$LIFECYCLE_SCRIPT" ]; then
        assert_eq "ticket-lifecycle.sh exists" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create a closed, archive-eligible ticket
    local ticket_id="tkt-commit-fail"
    local ticket_dir
    ticket_dir=$(_create_ticket_with_events "$repo" "$ticket_id" 12)
    _commit_tracker "$tracker_dir"
    _close_ticket "$tracker_dir" "$ticket_id"

    # Create a fake git shim that fails on "commit" but delegates everything else
    # to the real git binary. This simulates a git commit failure mid-lifecycle.
    local fake_bin
    fake_bin=$(mktemp -d)
    _CLEANUP_DIRS+=("$fake_bin")
    local real_git
    real_git=$(command -v git)
    cat > "$fake_bin/git" <<FAKE_GIT
#!/usr/bin/env bash
# Fake git: fail on "commit", delegate all other commands to real git
for arg in "\$@"; do
    if [ "\$arg" = "commit" ]; then
        echo "fake git: commit intentionally failed" >&2
        exit 1
    fi
done
exec "$real_git" "\$@"
FAKE_GIT
    chmod +x "$fake_bin/git"

    # Run lifecycle with the fake git first in PATH
    local exit_code=0
    (cd "$repo" && PATH="$fake_bin:$PATH" bash "$LIFECYCLE_SCRIPT") 2>/dev/null || exit_code=$?

    # lifecycle should exit non-zero because commit failed
    assert_ne "lifecycle exits non-zero on commit failure" "0" "$exit_code"

    # Assert: .archived marker must NOT exist (commit failed — marker must not be written)
    local marker_file="$ticket_dir/.archived"
    if [ -f "$marker_file" ]; then
        assert_eq ".archived marker absent when commit fails" "absent" "present"
    else
        assert_eq ".archived marker absent when commit fails" "absent" "absent"
    fi

    assert_pass_if_clean "test_lifecycle_no_marker_on_commit_failure"
}
test_lifecycle_no_marker_on_commit_failure

print_summary
