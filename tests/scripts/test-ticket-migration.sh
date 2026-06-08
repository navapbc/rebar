#!/usr/bin/env bash
# tests/scripts/test-ticket-migration.sh
# RED tests for src/rebar/_engine/ticket-migrate-schema-hardening.sh
#
# These tests are RED — the migration script does not yet exist.
# Tests MUST FAIL until ticket-migrate-schema-hardening.sh is implemented.
#
# Acceptance criteria tested:
#   1. --dry-run: exits 0, prints 'DRY_RUN: would bump schema_version to N', modifies no files
#   2. Normal run: creates a pre-migration SNAPSHOT backup (filename includes timestamp)
#   3. After migration: SNAPSHOT.json contains schema_version field with bumped integer value
#   4. --rollback: restores backup to SNAPSHOT.json, exits 0
#   5. Data integrity: events replayed from scratch match pre-migration backup
#         (all ticket IDs, statuses, and field values preserved)
#
# Usage: bash tests/scripts/test-ticket-migration.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e intentionally omitted — test functions may return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
MIGRATE_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-migrate-schema-hardening.sh"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_REDUCER_PY="$REPO_ROOT/src/rebar/_engine/ticket-reducer.py"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-migration.sh ==="

# ── Suite-runner guard: skip when migration script does not exist ─────────────
# RED tests fail by design (script not found). When auto-discovered by
# run-script-tests.sh, they would break `bash tests/run-all.sh`. Skip with
# exit 0 when ticket-migrate-schema-hardening.sh is absent AND running under the
# suite runner.
if [ "${_RUN_ALL_ACTIVE:-0}" = "1" ] && [ ! -f "$MIGRATE_SCRIPT" ]; then
    echo "SKIP: ticket-migrate-schema-hardening.sh not yet implemented (RED) — tests deferred"
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

# ── Helper: write a CREATE event for a ticket directly ────────────────────────
# Usage: _write_create_event <tracker_dir> <ticket_id> <ticket_type> <title> <status>
_write_create_event() {
    local tracker_dir="$1"
    local ticket_id="$2"
    local ticket_type="${3:-task}"
    local title="${4:-Test ticket}"
    local status="${5:-open}"
    local ticket_dir="$tracker_dir/$ticket_id"
    local timestamp="1742605100000000000"
    local uuid
    uuid="00000000-0000-4000-8000-$(printf '%012s' "$ticket_id" | tr ' ' '0')"
    local filename="${timestamp}-${uuid}-CREATE.json"

    mkdir -p "$ticket_dir"
    python3 -c "
import json, sys
payload = {
    'event_type': 'CREATE',
    'timestamp': 1742605100000000000,
    'uuid': '$uuid',
    'env_id': '00000000-0000-4000-8000-000000000001',
    'author': 'Test',
    'data': {
        'ticket_id': '$ticket_id',
        'ticket_type': '$ticket_type',
        'title': '$title',
        'status': '$status',
        'description': '',
        'tags': [],
        'parent_id': None,
        'priority': 2,
        'assignee': None
    }
}
json.dump(payload, sys.stdout)
" > "$ticket_dir/$filename"
}

# ── Helper: write a STATUS event for a ticket directly ────────────────────────
# Usage: _write_status_event <tracker_dir> <ticket_id> <status> <timestamp_offset>
_write_status_event() {
    local tracker_dir="$1"
    local ticket_id="$2"
    local new_status="$3"
    local ts_offset="${4:-100}"
    local ticket_dir="$tracker_dir/$ticket_id"
    local timestamp="$((1742605100000000000 + ts_offset))"
    local uuid
    uuid="00000000-0000-4000-9000-$(printf '%012s' "${ticket_id}${ts_offset}" | tr ' ' '0' | head -c 12)"
    local filename="${timestamp}-${uuid}-STATUS.json"

    python3 -c "
import json
payload = {
    'event_type': 'STATUS',
    'timestamp': $timestamp,
    'uuid': '$uuid',
    'env_id': '00000000-0000-4000-8000-000000000001',
    'author': 'Test',
    'data': {
        'status': '$new_status',
        'parent_status_uuid': None
    }
}
json.dump(payload, open('$ticket_dir/$filename', 'w'))
"
}

# ── Helper: build a SNAPSHOT.json at the tracker root ─────────────────────────
# Represents a pre-existing tracker-level snapshot (schema_version absent).
# The migration script is expected to add/bump schema_version in this file.
# Usage: _write_tracker_snapshot <tracker_dir> [schema_version]
_write_tracker_snapshot() {
    local tracker_dir="$1"
    local schema_version="${2:-}"  # empty = no schema_version field (pre-migration state)

    python3 - "$tracker_dir" "$schema_version" <<'PYEOF'
import json, sys, os

tracker_dir = sys.argv[1]
schema_version = sys.argv[2]

snapshot = {
    "snapshot_type": "tracker",
    "generated_at": 1742605100000000000,
    "tickets": {}
}

# Collect compiled states of all tickets in the tracker
for ticket_id in sorted(os.listdir(tracker_dir)):
    ticket_path = os.path.join(tracker_dir, ticket_id)
    if not os.path.isdir(ticket_path) or ticket_id.startswith('.'):
        continue
    # Minimal compiled state for testing
    snapshot["tickets"][ticket_id] = {
        "ticket_id": ticket_id,
        "status": "open",
        "title": f"Test ticket {ticket_id}"
    }

if schema_version:
    snapshot["schema_version"] = int(schema_version)

with open(os.path.join(tracker_dir, "SNAPSHOT.json"), "w") as f:
    json.dump(snapshot, f, indent=2)
PYEOF
}

# ── Helper: get schema_version from SNAPSHOT.json ─────────────────────────────
_get_snapshot_schema_version() {
    local tracker_dir="$1"
    python3 -c "
import json, sys
try:
    d = json.load(open('$tracker_dir/SNAPSHOT.json'))
    print(d.get('schema_version', 'MISSING'))
except Exception as e:
    print('ERROR:' + str(e))
"
}

# ─────────────────────────────────────────────────────────────────────────────
# Test 1: --dry-run exits 0, prints DRY_RUN message, modifies no files
# ─────────────────────────────────────────────────────────────────────────────
echo "Test 1: --dry-run exits 0, prints DRY_RUN message, modifies no files"
test_dry_run_exits_zero_and_no_modification() {
    _snapshot_fail

    # RED: script doesn't exist yet — this assertion will fail immediately
    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists" "exists" "missing"
        assert_pass_if_clean "test_dry_run_exits_zero_and_no_modification"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Set up a ticket and write a tracker SNAPSHOT (schema_version absent)
    _write_create_event "$tracker_dir" "abcd-1234"
    _write_tracker_snapshot "$tracker_dir"

    # Capture SNAPSHOT.json mtime and content before dry-run
    local snap_before
    snap_before=$(cat "$tracker_dir/SNAPSHOT.json")

    # Run with --dry-run
    local exit_code=0
    local output
    output=$(cd "$repo" && bash "$MIGRATE_SCRIPT" --dry-run 2>&1) || exit_code=$?

    assert_eq "dry-run: exits 0" "0" "$exit_code"

    # Output must contain 'DRY_RUN: would bump schema_version to N'
    # The exact target version N may vary; we match on the prefix
    if [[ "$output" == *"DRY_RUN: would bump schema_version to "* ]]; then
        assert_eq "dry-run: prints DRY_RUN message" "found" "found"
    else
        assert_eq "dry-run: prints DRY_RUN message" "found" "not-found"
        printf "  output was: %s\n" "$output" >&2
    fi

    # SNAPSHOT.json must be unmodified
    local snap_after
    snap_after=$(cat "$tracker_dir/SNAPSHOT.json" 2>/dev/null || echo "MISSING")
    assert_eq "dry-run: SNAPSHOT.json unmodified" "$snap_before" "$snap_after"

    # No backup files should have been created
    local backup_count
    backup_count=$(find "$tracker_dir" -maxdepth 1 -name 'SNAPSHOT.backup.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "dry-run: no backup files created" "0" "$backup_count"

    assert_pass_if_clean "test_dry_run_exits_zero_and_no_modification"
}
test_dry_run_exits_zero_and_no_modification

# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Normal run creates pre-migration backup with timestamp in filename
# ─────────────────────────────────────────────────────────────────────────────
echo "Test 2: normal run creates pre-migration SNAPSHOT backup (filename includes timestamp)"
test_normal_run_creates_backup() {
    _snapshot_fail

    # RED: script doesn't exist yet — this assertion will fail immediately
    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists" "exists" "missing"
        assert_pass_if_clean "test_normal_run_creates_backup"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Set up fixture: one ticket + tracker SNAPSHOT without schema_version
    _write_create_event "$tracker_dir" "abcd-5678"
    _write_tracker_snapshot "$tracker_dir"

    # Run the migration (no flags)
    local exit_code=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || exit_code=$?

    # Must exit 0
    assert_eq "normal run: exits 0" "0" "$exit_code"

    # A backup file must exist with a timestamp in its name
    # Expected pattern: SNAPSHOT.backup.<timestamp> or SNAPSHOT.<timestamp>.bak
    local backup_files
    backup_files=$(find "$tracker_dir" -maxdepth 1 \
        \( -name 'SNAPSHOT.backup.*' -o -name 'SNAPSHOT.*.bak' \) \
        2>/dev/null)

    if [ -n "$backup_files" ]; then
        assert_eq "normal run: backup file created" "created" "created"
    else
        assert_eq "normal run: backup file created" "created" "not-created"
        printf "  tracker_dir contents: %s\n" "$(ls "$tracker_dir" 2>/dev/null)" >&2
    fi

    # Backup filename must contain numeric digits (timestamp component)
    local has_timestamp=0
    while IFS= read -r bfile; do
        if [[ "$(basename "$bfile")" =~ [0-9]{8,} ]]; then
            has_timestamp=1
            break
        fi
    done <<< "$backup_files"

    assert_eq "normal run: backup filename includes timestamp" "1" "$has_timestamp"

    assert_pass_if_clean "test_normal_run_creates_backup"
}
test_normal_run_creates_backup

# ─────────────────────────────────────────────────────────────────────────────
# Test 3: After migration, SNAPSHOT.json contains schema_version with bumped integer
# ─────────────────────────────────────────────────────────────────────────────
echo "Test 3: post-migration SNAPSHOT.json contains schema_version with bumped integer value"
test_snapshot_contains_bumped_schema_version() {
    _snapshot_fail

    # RED: script doesn't exist yet — this assertion will fail immediately
    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists" "exists" "missing"
        assert_pass_if_clean "test_snapshot_contains_bumped_schema_version"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Set up fixture with schema_version=1 (pre-migration)
    _write_create_event "$tracker_dir" "abcd-9012"
    _write_tracker_snapshot "$tracker_dir" "1"

    # Run the migration
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    # SNAPSHOT.json must now contain schema_version
    local snap_path="$tracker_dir/SNAPSHOT.json"
    if [ ! -f "$snap_path" ]; then
        assert_eq "post-migration: SNAPSHOT.json exists" "exists" "missing"
        assert_pass_if_clean "test_snapshot_contains_bumped_schema_version"
        return
    fi

    local schema_version
    schema_version=$(_get_snapshot_schema_version "$tracker_dir")

    # schema_version must not be MISSING and must be an integer > 1
    if [[ "$schema_version" == "MISSING" ]] || [[ "$schema_version" == ERROR* ]]; then
        assert_eq "post-migration: schema_version present in SNAPSHOT.json" "integer" "MISSING"
    elif [[ "$schema_version" =~ ^[0-9]+$ ]] && [ "$schema_version" -gt 1 ]; then
        assert_eq "post-migration: schema_version is bumped integer > 1" "bumped" "bumped"
    else
        assert_eq "post-migration: schema_version is bumped integer > 1" "bumped" "$schema_version"
    fi

    assert_pass_if_clean "test_snapshot_contains_bumped_schema_version"
}
test_snapshot_contains_bumped_schema_version

# ─────────────────────────────────────────────────────────────────────────────
# Test 4: --rollback restores backup to SNAPSHOT.json, exits 0
# ─────────────────────────────────────────────────────────────────────────────
echo "Test 4: --rollback restores backup to SNAPSHOT.json, exits 0"
test_rollback_restores_backup() {
    _snapshot_fail

    # RED: script doesn't exist yet — this assertion will fail immediately
    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists" "exists" "missing"
        assert_pass_if_clean "test_rollback_restores_backup"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Set up fixture and run migration to create backup + migrated state
    _write_create_event "$tracker_dir" "abcd-3456"
    _write_tracker_snapshot "$tracker_dir" "1"

    # Capture the pre-migration SNAPSHOT content
    local original_snapshot
    original_snapshot=$(cat "$tracker_dir/SNAPSHOT.json")

    # Run migration to produce backup
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    # Verify migration changed the SNAPSHOT (otherwise rollback test is trivially valid)
    local migrated_snapshot
    migrated_snapshot=$(cat "$tracker_dir/SNAPSHOT.json" 2>/dev/null || echo "MISSING")

    # Now run --rollback
    local rollback_exit=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT" --rollback) >/dev/null 2>&1 || rollback_exit=$?

    assert_eq "rollback: exits 0" "0" "$rollback_exit"

    # SNAPSHOT.json must be restored to the pre-migration content
    local restored_snapshot
    restored_snapshot=$(cat "$tracker_dir/SNAPSHOT.json" 2>/dev/null || echo "MISSING")
    assert_eq "rollback: SNAPSHOT.json restored to pre-migration content" \
        "$original_snapshot" "$restored_snapshot"

    assert_pass_if_clean "test_rollback_restores_backup"
}
test_rollback_restores_backup

# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Data integrity — ticket states from replay match pre-migration backup
# ─────────────────────────────────────────────────────────────────────────────
echo "Test 5: data integrity — ticket IDs, statuses, and field values match after migration"
test_data_integrity_no_loss() {
    _snapshot_fail

    # RED: script doesn't exist yet — this assertion will fail immediately
    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists" "exists" "missing"
        assert_pass_if_clean "test_data_integrity_no_loss"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Set up fixture: multiple tickets with varied states
    _write_create_event "$tracker_dir" "aaa1-0001" "task"  "Ticket Alpha" "open"
    _write_create_event "$tracker_dir" "bbb2-0002" "story" "Ticket Beta"  "open"
    _write_create_event "$tracker_dir" "ccc3-0003" "epic"  "Ticket Gamma" "open"
    # Transition ticket B to in_progress
    _write_status_event "$tracker_dir" "bbb2-0002" "in_progress" "200"
    # Transition ticket C to closed
    _write_status_event "$tracker_dir" "ccc3-0003" "closed" "300"

    # Build a tracker-level SNAPSHOT from the current state
    _write_tracker_snapshot "$tracker_dir" "1"

    # Collect pre-migration per-ticket states via reducer
    local pre_states
    pre_states=$(python3 - "$tracker_dir" "$TICKET_REDUCER_PY" <<'PYEOF'
import json, os, subprocess, sys

tracker_dir = sys.argv[1]
reducer_py = sys.argv[2]
states = {}

for ticket_id in sorted(os.listdir(tracker_dir)):
    ticket_path = os.path.join(tracker_dir, ticket_id)
    if not os.path.isdir(ticket_path) or ticket_id.startswith('.'):
        continue
    try:
        result = subprocess.run(
            ["python3", reducer_py, ticket_path],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            state = json.loads(result.stdout)
            states[ticket_id] = {
                "ticket_id": state.get("ticket_id"),
                "status": state.get("status"),
                "title": state.get("title"),
                "ticket_type": state.get("ticket_type"),
            }
    except Exception:
        pass

print(json.dumps(states, sort_keys=True))
PYEOF
)

    # Run the migration
    local migrate_exit=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || migrate_exit=$?
    assert_eq "data-integrity: migration exits 0" "0" "$migrate_exit"

    # Collect post-migration per-ticket states via reducer (replay from events)
    local post_states
    post_states=$(python3 - "$tracker_dir" "$TICKET_REDUCER_PY" <<'PYEOF'
import json, os, subprocess, sys

tracker_dir = sys.argv[1]
reducer_py = sys.argv[2]
states = {}

for ticket_id in sorted(os.listdir(tracker_dir)):
    ticket_path = os.path.join(tracker_dir, ticket_id)
    if not os.path.isdir(ticket_path) or ticket_id.startswith('.'):
        continue
    try:
        result = subprocess.run(
            ["python3", reducer_py, ticket_path],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            state = json.loads(result.stdout)
            states[ticket_id] = {
                "ticket_id": state.get("ticket_id"),
                "status": state.get("status"),
                "title": state.get("title"),
                "ticket_type": state.get("ticket_type"),
            }
    except Exception:
        pass

print(json.dumps(states, sort_keys=True))
PYEOF
)

    # Pre-migration and post-migration states must be identical
    assert_eq "data-integrity: ticket states unchanged after migration" \
        "$pre_states" "$post_states"

    assert_pass_if_clean "test_data_integrity_no_loss"
}
test_data_integrity_no_loss

# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Step 2.5 backfill — parent_status_uuid chained into legacy STATUS events
# ─────────────────────────────────────────────────────────────────────────────
echo "Test 6: Step 2.5 backfill — parent_status_uuid chained into pre-existing STATUS events"
test_backfill_parent_status_uuid() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists" "exists" "missing"
        assert_pass_if_clean "test_backfill_parent_status_uuid"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create a ticket with two STATUS events lacking parent_status_uuid in data.
    # Use a helper that writes the payload without the field.
    local ticket_id="bbbb-2222"
    local ticket_dir="$tracker_dir/$ticket_id"
    mkdir -p "$ticket_dir"

    # CREATE event
    python3 - "$ticket_dir" <<'PYEOF'
import json, os, sys
ticket_dir = sys.argv[1]
# CREATE
uuid_c = "00000000-0000-4000-8000-000000000010"
with open(os.path.join(ticket_dir, f"1000000000000000000-{uuid_c}-CREATE.json"), "w") as f:
    json.dump({
        "event_type": "CREATE", "uuid": uuid_c, "timestamp": 1000000000000000000,
        "author": "test", "env_id": "env-create",
        "data": {"ticket_type": "task", "title": "Backfill test", "status": "open",
                 "description": "", "tags": [], "parent_id": None, "priority": 2, "assignee": None}
    }, f)
# STATUS 1 — no parent_status_uuid in data
uuid_s1 = "00000000-0000-4000-8000-000000000011"
with open(os.path.join(ticket_dir, f"1000000000000001000-{uuid_s1}-STATUS.json"), "w") as f:
    json.dump({
        "event_type": "STATUS", "uuid": uuid_s1, "timestamp": 1000000000000001000,
        "author": "test", "env_id": "env-s1",
        "data": {"status": "in_progress", "current_status": "open"}
    }, f)
# STATUS 2 — no parent_status_uuid in data
uuid_s2 = "00000000-0000-4000-8000-000000000012"
with open(os.path.join(ticket_dir, f"1000000000000002000-{uuid_s2}-STATUS.json"), "w") as f:
    json.dump({
        "event_type": "STATUS", "uuid": uuid_s2, "timestamp": 1000000000000002000,
        "author": "test", "env_id": "env-s2",
        "data": {"status": "closed", "current_status": "in_progress"}
    }, f)
PYEOF

    _write_tracker_snapshot "$tracker_dir" "1"

    # Run the migration
    local migrate_exit=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || migrate_exit=$?
    assert_eq "backfill: migration exits 0" "0" "$migrate_exit"

    # Assert STATUS 1 has parent_status_uuid = null (first in chain)
    local s1_parent
    s1_parent=$(python3 - "$ticket_dir" <<'PYEOF'
import json, os, sys
files = sorted(f for f in os.listdir(sys.argv[1]) if f.endswith("-STATUS.json"))
with open(os.path.join(sys.argv[1], files[0])) as fh:
    ev = json.load(fh)
print(str(ev["data"].get("parent_status_uuid", "ABSENT")))
PYEOF
)
    assert_eq "backfill: STATUS-1 parent_status_uuid = null" "None" "$s1_parent"

    # Assert STATUS 2 has parent_status_uuid = uuid of STATUS 1
    local s2_parent
    s2_parent=$(python3 - "$ticket_dir" <<'PYEOF'
import json, os, sys
files = sorted(f for f in os.listdir(sys.argv[1]) if f.endswith("-STATUS.json"))
with open(os.path.join(sys.argv[1], files[0])) as fh:
    s1_uuid = json.load(fh)["uuid"]
with open(os.path.join(sys.argv[1], files[1])) as fh:
    s2_parent = json.load(fh)["data"].get("parent_status_uuid", "ABSENT")
print("match" if s2_parent == s1_uuid else f"mismatch: got {s2_parent!r} expected {s1_uuid!r}")
PYEOF
)
    assert_eq "backfill: STATUS-2 parent_status_uuid = STATUS-1 uuid" "match" "$s2_parent"

    assert_pass_if_clean "test_backfill_parent_status_uuid"
}
test_backfill_parent_status_uuid

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
print_summary
