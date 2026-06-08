#!/usr/bin/env bash
# tests/scripts/test-ticket-migrate-brainstorm-tags.sh
# Behavioral fixture test for src/rebar/_engine/ticket-migrate-brainstorm-tags.sh
#
# These tests are RED — the migration script does not yet exist.
# Tests MUST FAIL until ticket-migrate-brainstorm-tags.sh is implemented (task 3c41-04fc).
#
# Test structure:
#   - 2 epics with "### Planning Intelligence Log" heading (one in CREATE description,
#     one in a COMMENT event JSON file body)
#   - 1 epic with no PIL heading
#
# Assertions:
#   1. Migration exits 0
#   2. 2 PIL-bearing epics get brainstorm:complete tag added (inspect tracker state)
#   3. UNMATCHED: <epic-id> printed to stdout for the 1 non-PIL epic
#   4. Marker file .claude/.brainstorm-tag-migration-v2 written at repo root
#   5. Re-run exits 0 immediately (marker present) with no new tracker changes
#   6. Plugin-source-repo guard: exits 0 with a logged notice, no changes
#
# Usage: bash tests/scripts/test-ticket-migrate-brainstorm-tags.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e intentionally omitted — test functions may return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
MIGRATE_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-migrate-brainstorm-tags.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-migrate-brainstorm-tags.sh ==="

# ── Suite-runner guard: skip when migration script does not exist ─────────────
# RED tests fail by design (script not found). When auto-discovered by
# run-script-tests.sh, they would break `bash tests/run-all.sh`. Skip with
# exit 0 when ticket-migrate-brainstorm-tags.sh is absent AND running under the suite runner.
if [ "${_RUN_ALL_ACTIVE:-0}" = "1" ] && [ ! -f "$MIGRATE_SCRIPT" ]; then
    echo "SKIP: ticket-migrate-brainstorm-tags.sh not yet implemented (RED) — tests deferred"
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

# ── Helper: write an event file directly to a ticket dir ─────────────────────
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
    'data': json.loads(sys.argv[1])
}
json.dump(payload, sys.stdout)
" "$data_json" > "$ticket_dir/$filename"
}

# ── Helper: set up the 3-epic fixture in a tracker dir ───────────────────────
# Returns: sets EPIC_PIL_DESC_ID, EPIC_PIL_COMMENT_ID, EPIC_NO_PIL_ID in caller scope
_setup_epic_fixture() {
    local tracker_dir="$1"

    # Epic 1: PIL heading in the CREATE event description field
    EPIC_PIL_DESC_ID="epic-pil-desc-01"
    local dir1="$tracker_dir/$EPIC_PIL_DESC_ID"
    mkdir -p "$dir1"
    local desc1
    # a307-0f58: PIL must contain the three mandatory canonical fields, not
    # just a heading. Stub bodies are now rejected.
    desc1='{"ticket_type": "epic", "title": "Epic with PIL in description", "parent_id": null, "description": "## Background\n\nNotes.\n\n### Planning Intelligence Log\n- **Web research (Step 2.6)**: not triggered\n- **Scenario analysis (Step 2.75)**: not triggered\n- **LLM-instruction signal (Step 5)**: not triggered"}'
    _write_event "$dir1" "1742605100" "00000000-0000-4000-8000-pil001desc001" "CREATE" "$desc1"

    # Epic 2: PIL heading appears in a COMMENT event body (not in description)
    EPIC_PIL_COMMENT_ID="epic-pil-comment-02"
    local dir2="$tracker_dir/$EPIC_PIL_COMMENT_ID"
    mkdir -p "$dir2"
    local create2
    create2='{"ticket_type": "epic", "title": "Epic with PIL in comment", "parent_id": null, "description": "Regular description, no PIL here."}'
    _write_event "$dir2" "1742605200" "00000000-0000-4000-8000-pil002cre001" "CREATE" "$create2"
    # COMMENT event whose body contains the PIL heading
    local comment2
    comment2='{"body": "### Planning Intelligence Log\n- **Web research (Step 2.6)**: not triggered\n- **Scenario analysis (Step 2.75)**: not triggered\n- **LLM-instruction signal (Step 5)**: not triggered"}'
    _write_event "$dir2" "1742605300" "00000000-0000-4000-8000-pil002cmt001" "COMMENT" "$comment2"

    # Epic 3: No PIL heading anywhere
    EPIC_NO_PIL_ID="epic-no-pil-003"
    local dir3="$tracker_dir/$EPIC_NO_PIL_ID"
    mkdir -p "$dir3"
    local create3
    create3='{"ticket_type": "epic", "title": "Epic without PIL", "parent_id": null, "description": "This epic has no planning intelligence log."}'
    _write_event "$dir3" "1742605400" "00000000-0000-4000-8000-nopil03cr001" "CREATE" "$create3"
}

# ── Helper: check if a tracker ticket has a given tag ────────────────────────
# Reads EDIT events and/or SNAPSHOT for tags; returns 0 if tag found, 1 otherwise.
_ticket_has_tag() {
    local tracker_dir="$1"
    local ticket_id="$2"
    local tag="$3"
    local ticket_dir="$tracker_dir/$ticket_id"

    # Check all JSON event files for a tags field containing the target tag
    python3 - "$ticket_dir" "$tag" <<'PYEOF'
import json, os, sys

ticket_dir = sys.argv[1]
target_tag = sys.argv[2]

if not os.path.isdir(ticket_dir):
    sys.exit(1)

for fname in sorted(os.listdir(ticket_dir)):
    if not fname.endswith('.json') or fname.startswith('.'):
        continue
    fpath = os.path.join(ticket_dir, fname)
    try:
        with open(fpath) as f:
            event = json.load(f)
        data = event.get('data', {})
        event_type = event.get('event_type', '')
        if event_type == 'EDIT':
            tags = data.get('fields', {}).get('tags', None)
        else:
            tags = data.get('tags', None)
        if tags is not None:
            if isinstance(tags, list) and target_tag in tags:
                sys.exit(0)
            if isinstance(tags, str) and target_tag in tags.split(','):
                sys.exit(0)
    except (json.JSONDecodeError, OSError):
        pass

sys.exit(1)
PYEOF
}

# ── Helper: count EDIT events that set brainstorm:complete tag ───────────────
_count_brainstorm_tag_edits() {
    local tracker_dir="$1"
    local ticket_id="$2"
    local ticket_dir="$tracker_dir/$ticket_id"

    python3 - "$ticket_dir" <<'PYEOF'
import json, os, sys

ticket_dir = sys.argv[1]
count = 0

if not os.path.isdir(ticket_dir):
    print(0)
    sys.exit(0)

for fname in sorted(os.listdir(ticket_dir)):
    if not fname.endswith('-EDIT.json') or fname.startswith('.'):
        continue
    fpath = os.path.join(ticket_dir, fname)
    try:
        with open(fpath) as f:
            event = json.load(f)
        data = event.get('data', {})
        tags = data.get('fields', {}).get('tags', None)
        if tags is not None:
            tag_list = tags if isinstance(tags, list) else tags.split(',')
            if 'brainstorm:complete' in tag_list:
                count += 1
    except (json.JSONDecodeError, OSError):
        pass

print(count)
PYEOF
}

# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: Migration script must exist (RED gate)
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 1: migration script exists"
test_migration_script_exists() {
    if [ -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "ticket-migrate-brainstorm-tags.sh exists" "exists" "exists"
    else
        assert_eq "ticket-migrate-brainstorm-tags.sh exists" "exists" "missing"
    fi
}
test_migration_script_exists

# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: Migration exits 0 when run against the 3-epic fixture
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 2: migration exits 0 with 3-epic fixture"
test_migration_exits_zero() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    _setup_epic_fixture "$repo/.tickets-tracker"

    local exit_code=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || exit_code=$?
    assert_eq "migration exits 0" "0" "$exit_code"

    assert_pass_if_clean "test_migration_exits_zero"
}
test_migration_exits_zero

# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: 2 PIL-bearing epics get brainstorm:complete tag added
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 3: 2 PIL-bearing epics get brainstorm:complete tag"
test_pil_epics_get_brainstorm_tag() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    _setup_epic_fixture "$repo/.tickets-tracker"

    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    # Epic 1 (PIL in description) must have brainstorm:complete tag
    if _ticket_has_tag "$repo/.tickets-tracker" "$EPIC_PIL_DESC_ID" "brainstorm:complete"; then
        assert_eq "epic-pil-desc: has brainstorm:complete tag" "tagged" "tagged"
    else
        assert_eq "epic-pil-desc: has brainstorm:complete tag" "tagged" "not-tagged"
    fi

    # Epic 2 (PIL in comment) must have brainstorm:complete tag
    if _ticket_has_tag "$repo/.tickets-tracker" "$EPIC_PIL_COMMENT_ID" "brainstorm:complete"; then
        assert_eq "epic-pil-comment: has brainstorm:complete tag" "tagged" "tagged"
    else
        assert_eq "epic-pil-comment: has brainstorm:complete tag" "tagged" "not-tagged"
    fi

    # Epic 3 (no PIL) must NOT have brainstorm:complete tag
    if _ticket_has_tag "$repo/.tickets-tracker" "$EPIC_NO_PIL_ID" "brainstorm:complete"; then
        assert_eq "epic-no-pil: must NOT have brainstorm:complete tag" "not-tagged" "tagged"
    else
        assert_eq "epic-no-pil: must NOT have brainstorm:complete tag" "not-tagged" "not-tagged"
    fi

    assert_pass_if_clean "test_pil_epics_get_brainstorm_tag"
}
test_pil_epics_get_brainstorm_tag

# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: UNMATCHED: <epic-id> printed to stdout for the non-PIL epic
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 4: UNMATCHED line printed for non-PIL epic"
test_unmatched_printed_for_non_pil_epic() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    _setup_epic_fixture "$repo/.tickets-tracker"

    local output
    output=$(cd "$repo" && bash "$MIGRATE_SCRIPT" 2>/dev/null) || true

    # Must contain "UNMATCHED: epic-no-pil-003"
    assert_contains "stdout contains UNMATCHED line for non-PIL epic" \
        "UNMATCHED: $EPIC_NO_PIL_ID" "$output"

    # PIL epics must NOT appear in UNMATCHED output
    local unmatched_lines
    unmatched_lines=$(printf '%s\n' "$output" | grep '^UNMATCHED:' || true)
    local unmatched_count
    unmatched_count=$(printf '%s\n' "$unmatched_lines" | grep -c . || echo "0")
    assert_eq "exactly 1 UNMATCHED line printed" "1" "$unmatched_count"

    assert_pass_if_clean "test_unmatched_printed_for_non_pil_epic"
}
test_unmatched_printed_for_non_pil_epic

# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: Marker file .claude/.brainstorm-tag-migration-v2 written at repo root
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 5: marker file written in shared tracker .migrations/ after migration"
test_marker_file_written() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    _setup_epic_fixture "$repo/.tickets-tracker"
    mkdir -p "$repo/.claude"

    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    # Primary marker (shared, tickets-branch): must exist after migration
    if [ -f "$repo/.tickets-tracker/.migrations/brainstorm-tag-migration-v2" ]; then
        assert_eq "shared marker file written in tracker .migrations/" "exists" "exists"
    else
        assert_eq "shared marker file written in tracker .migrations/" "exists" "missing"
    fi

    assert_pass_if_clean "test_marker_file_written"
}
test_marker_file_written

# ═══════════════════════════════════════════════════════════════════════════════
# Test 6: Re-run exits 0 immediately with no new tracker changes (marker present)
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 6: re-run with marker present exits 0 with no new tracker changes"
test_rerun_with_marker_is_noop() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    _setup_epic_fixture "$repo/.tickets-tracker"
    mkdir -p "$repo/.claude"

    # First run — performs migration
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    # Record EDIT event counts after first run
    local edits_before_pil_desc edits_before_pil_comment edits_before_no_pil
    edits_before_pil_desc=$(_count_brainstorm_tag_edits "$repo/.tickets-tracker" "$EPIC_PIL_DESC_ID")
    edits_before_pil_comment=$(_count_brainstorm_tag_edits "$repo/.tickets-tracker" "$EPIC_PIL_COMMENT_ID")
    edits_before_no_pil=$(_count_brainstorm_tag_edits "$repo/.tickets-tracker" "$EPIC_NO_PIL_ID")

    # Second run — marker is present, must exit 0 and write no new events
    local exit2=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || exit2=$?
    assert_eq "re-run exits 0 with marker present" "0" "$exit2"

    # EDIT event counts must be unchanged after second run
    local edits_after_pil_desc edits_after_pil_comment edits_after_no_pil
    edits_after_pil_desc=$(_count_brainstorm_tag_edits "$repo/.tickets-tracker" "$EPIC_PIL_DESC_ID")
    edits_after_pil_comment=$(_count_brainstorm_tag_edits "$repo/.tickets-tracker" "$EPIC_PIL_COMMENT_ID")
    edits_after_no_pil=$(_count_brainstorm_tag_edits "$repo/.tickets-tracker" "$EPIC_NO_PIL_ID")

    assert_eq "re-run: no new EDIT events on pil-desc epic" \
        "$edits_before_pil_desc" "$edits_after_pil_desc"
    assert_eq "re-run: no new EDIT events on pil-comment epic" \
        "$edits_before_pil_comment" "$edits_after_pil_comment"
    assert_eq "re-run: no new EDIT events on no-pil epic" \
        "$edits_before_no_pil" "$edits_after_no_pil"

    assert_pass_if_clean "test_rerun_with_marker_is_noop"
}
test_rerun_with_marker_is_noop

# ═══════════════════════════════════════════════════════════════════════════════
# Test 7: Plugin-source-repo guard — exits 0 with notice, no changes
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 7: plugin-source-repo guard exits 0 with notice and makes no changes"
test_plugin_source_repo_guard() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    _setup_epic_fixture "$repo/.tickets-tracker"
    mkdir -p "$repo/.claude"

    # Simulate being inside the plugin source repo by placing plugin.json at repo root
    # (The migration script should detect this sentinel and exit without making changes)
    touch "$repo/plugin.json"

    local exit_code=0
    local output
    output=$(cd "$repo" && bash "$MIGRATE_SCRIPT" 2>&1) || exit_code=$?

    assert_eq "plugin-source-repo guard: exits 0" "0" "$exit_code"

    # Must emit a notice (not silently exit)
    if [ -n "$output" ]; then
        assert_eq "plugin-source-repo guard: emits a notice" "notice-emitted" "notice-emitted"
    else
        assert_eq "plugin-source-repo guard: emits a notice" "notice-emitted" "silent-exit"
    fi

    # Shared marker file must NOT be written (guard bailed before making changes)
    if [ -f "$repo/.tickets-tracker/.migrations/brainstorm-tag-migration-v2" ]; then
        assert_eq "plugin-source-repo guard: shared marker NOT written" "not-written" "written"
    else
        assert_eq "plugin-source-repo guard: shared marker NOT written" "not-written" "not-written"
    fi

    # No brainstorm:complete tags must have been added to any epic
    if _ticket_has_tag "$repo/.tickets-tracker" "$EPIC_PIL_DESC_ID" "brainstorm:complete"; then
        assert_eq "plugin-source-repo guard: pil-desc epic NOT tagged" "not-tagged" "tagged"
    else
        assert_eq "plugin-source-repo guard: pil-desc epic NOT tagged" "not-tagged" "not-tagged"
    fi

    if _ticket_has_tag "$repo/.tickets-tracker" "$EPIC_PIL_COMMENT_ID" "brainstorm:complete"; then
        assert_eq "plugin-source-repo guard: pil-comment epic NOT tagged" "not-tagged" "tagged"
    else
        assert_eq "plugin-source-repo guard: pil-comment epic NOT tagged" "not-tagged" "not-tagged"
    fi

    assert_pass_if_clean "test_plugin_source_repo_guard"
}
test_plugin_source_repo_guard

# ═══════════════════════════════════════════════════════════════════════════════
# Test 8: PIL in SNAPSHOT compiled_state.description gets brainstorm:complete tag
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 8: PIL in SNAPSHOT compiled_state.description triggers brainstorm:complete tag"
test_pil_in_snapshot_compiled_state_description() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local tracker_dir="$repo/.tickets-tracker"
    local epic_id="epic-pil-snapshot-desc-08"
    local ticket_dir="$tracker_dir/$epic_id"
    mkdir -p "$ticket_dir"

    # CREATE event with no PIL in description (ordinary text only)
    local create_data
    create_data='{"ticket_type": "epic", "title": "Snapshot PIL desc epic", "parent_id": null, "description": "No PIL here."}'
    _write_event "$ticket_dir" "1742606100" "00000000-0000-4000-8000-snap0desc0001" "CREATE" "$create_data"

    # SNAPSHOT event: PIL lives in compiled_state.description (not in top-level data)
    python3 -c "
import json
payload = {
    'timestamp': 1742606200,
    'uuid': '00000000-0000-4000-8000-snap0desc0002',
    'event_type': 'SNAPSHOT',
    'env_id': '00000000-0000-4000-8000-000000000001',
    'author': 'Test User',
    'data': {
        'compiled_state': {
            'description': '## Background\n\n### Planning Intelligence Log\n- **Web research (Step 2.6)**: not triggered\n- **Scenario analysis (Step 2.75)**: not triggered\n- **LLM-instruction signal (Step 5)**: not triggered',
            'comments': []
        }
    }
}
print(json.dumps(payload))
" > "$ticket_dir/1742606200-00000000-0000-4000-8000-snap0desc0002-SNAPSHOT.json"

    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    if _ticket_has_tag "$tracker_dir" "$epic_id" "brainstorm:complete"; then
        assert_eq "snapshot-compiled_state.description: has brainstorm:complete tag" "tagged" "tagged"
    else
        assert_eq "snapshot-compiled_state.description: has brainstorm:complete tag" "tagged" "not-tagged"
    fi

    assert_pass_if_clean "test_pil_in_snapshot_compiled_state_description"
}
test_pil_in_snapshot_compiled_state_description

# ═══════════════════════════════════════════════════════════════════════════════
# Test 9: PIL in SNAPSHOT compiled_state.comments[0].body gets brainstorm:complete tag
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 9: PIL in SNAPSHOT compiled_state.comments[0].body triggers brainstorm:complete tag"
test_pil_in_snapshot_compiled_state_comment_body() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local tracker_dir="$repo/.tickets-tracker"
    local epic_id="epic-pil-snapshot-cmt-09"
    local ticket_dir="$tracker_dir/$epic_id"
    mkdir -p "$ticket_dir"

    # CREATE event with no PIL anywhere
    local create_data
    create_data='{"ticket_type": "epic", "title": "Snapshot PIL comment epic", "parent_id": null, "description": "No PIL here."}'
    _write_event "$ticket_dir" "1742606300" "00000000-0000-4000-8000-snap1cmt00001" "CREATE" "$create_data"

    # SNAPSHOT event: PIL lives in compiled_state.comments[0].body (not in description)
    python3 -c "
import json
payload = {
    'timestamp': 1742606400,
    'uuid': '00000000-0000-4000-8000-snap1cmt00002',
    'event_type': 'SNAPSHOT',
    'env_id': '00000000-0000-4000-8000-000000000001',
    'author': 'Test User',
    'data': {
        'compiled_state': {
            'description': 'No PIL in the description.',
            'comments': [
                {'body': '### Planning Intelligence Log\n- **Web research (Step 2.6)**: not triggered\n- **Scenario analysis (Step 2.75)**: not triggered\n- **LLM-instruction signal (Step 5)**: not triggered'},
                {'body': 'Unrelated follow-up comment.'}
            ]
        }
    }
}
print(json.dumps(payload))
" > "$ticket_dir/1742606400-00000000-0000-4000-8000-snap1cmt00002-SNAPSHOT.json"

    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    if _ticket_has_tag "$tracker_dir" "$epic_id" "brainstorm:complete"; then
        assert_eq "snapshot-compiled_state.comments[0].body: has brainstorm:complete tag" "tagged" "tagged"
    else
        assert_eq "snapshot-compiled_state.comments[0].body: has brainstorm:complete tag" "tagged" "not-tagged"
    fi

    assert_pass_if_clean "test_pil_in_snapshot_compiled_state_comment_body"
}
test_pil_in_snapshot_compiled_state_comment_body

# ═══════════════════════════════════════════════════════════════════════════════
# Test 10: PIL in EDIT fields.description gets brainstorm:complete tag
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 10: PIL in EDIT fields.description triggers brainstorm:complete tag"
test_pil_in_edit_fields_description() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local tracker_dir="$repo/.tickets-tracker"
    local epic_id="epic-pil-edit-desc-010"
    local ticket_dir="$tracker_dir/$epic_id"
    mkdir -p "$ticket_dir"

    # CREATE event with no PIL in description
    local create_data
    create_data='{"ticket_type": "epic", "title": "Edit PIL desc epic", "parent_id": null, "description": "Original description — no PIL."}'
    _write_event "$ticket_dir" "1742606500" "00000000-0000-4000-8000-editdesc0001" "CREATE" "$create_data"

    # EDIT event: PIL is in data.fields.description (description was later edited to include PIL)
    python3 -c "
import json
payload = {
    'timestamp': 1742606600,
    'uuid': '00000000-0000-4000-8000-editdesc0002',
    'event_type': 'EDIT',
    'env_id': '00000000-0000-4000-8000-000000000001',
    'author': 'Test User',
    'data': {
        'fields': {
            'description': '## Background\n\n### Planning Intelligence Log\n- **Web research (Step 2.6)**: not triggered\n- **Scenario analysis (Step 2.75)**: not triggered\n- **LLM-instruction signal (Step 5)**: not triggered'
        }
    }
}
print(json.dumps(payload))
" > "$ticket_dir/1742606600-00000000-0000-4000-8000-editdesc0002-EDIT.json"

    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    if _ticket_has_tag "$tracker_dir" "$epic_id" "brainstorm:complete"; then
        assert_eq "edit-fields.description: has brainstorm:complete tag" "tagged" "tagged"
    else
        assert_eq "edit-fields.description: has brainstorm:complete tag" "tagged" "not-tagged"
    fi

    assert_pass_if_clean "test_pil_in_edit_fields_description"
}
test_pil_in_edit_fields_description

# ═══════════════════════════════════════════════════════════════════════════════
# Test 11: Marker file written is v2 (not v1) after successful migration
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 11: marker file is brainstorm-tag-migration-v2 in tracker .migrations/ (not v1, not old .claude/ path)"
test_marker_file_is_v2_not_v1() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    _setup_epic_fixture "$repo/.tickets-tracker"
    mkdir -p "$repo/.claude"

    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    # v2 shared marker must exist at tracker .migrations/ path
    if [ -f "$repo/.tickets-tracker/.migrations/brainstorm-tag-migration-v2" ]; then
        assert_eq "v2 shared marker exists at tracker .migrations/" "exists" "exists"
    else
        assert_eq "v2 shared marker exists at tracker .migrations/" "exists" "missing"
    fi

    # v1 marker must NOT exist (script was bumped to v2)
    if [ -f "$repo/.tickets-tracker/.migrations/brainstorm-tag-migration-v1" ]; then
        assert_eq "v1 tracker marker must not be created" "absent" "present"
    else
        assert_eq "v1 tracker marker must not be created" "absent" "absent"
    fi

    # Old per-worktree .claude/ marker must NOT be written by the new script
    if [ -f "$repo/.claude/.brainstorm-tag-migration-v2" ]; then
        assert_eq "old per-worktree .claude/ marker must not be written" "absent" "present"
    else
        assert_eq "old per-worktree .claude/ marker must not be written" "absent" "absent"
    fi

    assert_pass_if_clean "test_marker_file_is_v2_not_v1"
}
test_marker_file_is_v2_not_v1

# ═══════════════════════════════════════════════════════════════════════════════
# Test 12: Compacted epic (SNAPSHOT-only, ticket_type + tags in compiled_state)
#          gets brainstorm:complete tag when PIL is present in compiled_state
# ═══════════════════════════════════════════════════════════════════════════════
# Real-world compaction: older tickets have had their CREATE/EDIT/COMMENT events
# collapsed into a single SNAPSHOT event. ticket_type, tags, description, and
# comments all live under data.compiled_state.*. The migration must still detect
# these as epics and tag them — otherwise snapshotted epics are silently skipped.
echo "Test 12: compacted SNAPSHOT-only epic (ticket_type + tags in compiled_state) gets tagged"
test_compacted_epic_in_compiled_state_gets_tagged() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local tracker_dir="$repo/.tickets-tracker"
    local epic_id="epic-compacted-snapshot-only-12"
    local ticket_dir="$tracker_dir/$epic_id"
    mkdir -p "$ticket_dir"

    # SNAPSHOT-only epic: no CREATE, no EDIT, no COMMENT.
    # ticket_type, tags, description all live under data.compiled_state.
    python3 -c "
import json
payload = {
    'timestamp': 1742606700,
    'uuid': '00000000-0000-4000-8000-compactedsn01',
    'event_type': 'SNAPSHOT',
    'env_id': '00000000-0000-4000-8000-000000000001',
    'author': 'Test User',
    'data': {
        'compiled_state': {
            'ticket_id': 'epic-compacted-snapshot-only-12',
            'ticket_type': 'epic',
            'title': 'Compacted epic with PIL in compiled_state',
            'status': 'open',
            'tags': [],
            'description': '## Problem\n\n### Planning Intelligence Log\n- **Web research (Step 2.6)**: not triggered\n- **Scenario analysis (Step 2.75)**: not triggered\n- **LLM-instruction signal (Step 5)**: not triggered',
            'comments': []
        }
    }
}
print(json.dumps(payload))
" > "$ticket_dir/1742606700-00000000-0000-4000-8000-compactedsn01-SNAPSHOT.json"

    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    if _ticket_has_tag "$tracker_dir" "$epic_id" "brainstorm:complete"; then
        assert_eq "compacted-snapshot-only epic: has brainstorm:complete tag" "tagged" "tagged"
    else
        assert_eq "compacted-snapshot-only epic: has brainstorm:complete tag" "tagged" "not-tagged"
    fi

    assert_pass_if_clean "test_compacted_epic_in_compiled_state_gets_tagged"
}
test_compacted_epic_in_compiled_state_gets_tagged

# ═══════════════════════════════════════════════════════════════════════════════
# Test 13: Compacted epic ALREADY tagged brainstorm:complete (tags in
#          compiled_state.tags) is NOT re-tagged — idempotency across SNAPSHOT
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 13: compacted SNAPSHOT epic already tagged (tags in compiled_state) is left alone"
test_compacted_epic_already_tagged_is_noop() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local tracker_dir="$repo/.tickets-tracker"
    local epic_id="epic-compacted-already-tagged-13"
    local ticket_dir="$tracker_dir/$epic_id"
    mkdir -p "$ticket_dir"

    python3 -c "
import json
payload = {
    'timestamp': 1742606800,
    'uuid': '00000000-0000-4000-8000-compactedsn02',
    'event_type': 'SNAPSHOT',
    'env_id': '00000000-0000-4000-8000-000000000001',
    'author': 'Test User',
    'data': {
        'compiled_state': {
            'ticket_type': 'epic',
            'status': 'open',
            'tags': ['brainstorm:complete'],
            'description': '### Planning Intelligence Log\n- **Web research (Step 2.6)**: not triggered\n- **Scenario analysis (Step 2.75)**: not triggered\n- **LLM-instruction signal (Step 5)**: not triggered',
            'comments': []
        }
    }
}
print(json.dumps(payload))
" > "$ticket_dir/1742606800-00000000-0000-4000-8000-compactedsn02-SNAPSHOT.json"

    # Count EDIT events before (should be zero)
    local edit_count_before
    edit_count_before=$(find "$ticket_dir" -name '*-EDIT.json' 2>/dev/null | wc -l | tr -d ' ')

    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    local edit_count_after
    edit_count_after=$(find "$ticket_dir" -name '*-EDIT.json' 2>/dev/null | wc -l | tr -d ' ')

    # Migration must NOT have written an EDIT event for an already-tagged epic
    assert_eq "already-tagged compacted epic: no new EDIT events" "$edit_count_before" "$edit_count_after"

    assert_pass_if_clean "test_compacted_epic_already_tagged_is_noop"
}
test_compacted_epic_already_tagged_is_noop

# ═══════════════════════════════════════════════════════════════════════════════
# Test 14: Write-failure containment (3cb1-429e architecture-adapted)
# ═══════════════════════════════════════════════════════════════════════════════
# Original test (main 08efcf49) mocked `python3 -c` to simulate tag-computation
# failure in the old per-ticket bash loop. The single-pass python rewrite moves
# tag computation inside one Python scan, so PATH-mocking python3 no longer
# exercises the guard path. This adapted test preserves the behavioral intent:
# when an individual ticket's event write fails (OSError from read-only dir),
# the migration must skip that ticket without emitting a WROTE: line, so the
# bash commit loop never attempts to commit a partially-written event.
echo "Test 14: migration skips ticket when event write fails, does not mis-commit"
test_write_failure_containment() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local tracker_dir="$repo/.tickets-tracker"
    # One healthy epic — should get tagged normally
    local good_id="epic-containment-good-14a"
    local good_dir="$tracker_dir/$good_id"
    mkdir -p "$good_dir"
    local good_desc
    good_desc='{"ticket_type": "epic", "title": "Good epic", "parent_id": null, "description": "### Planning Intelligence Log\n- **Web research (Step 2.6)**: not triggered\n- **Scenario analysis (Step 2.75)**: not triggered\n- **LLM-instruction signal (Step 5)**: not triggered"}'
    _write_event "$good_dir" "1742700100" "00000000-0000-4000-8000-good001cr001" "CREATE" "$good_desc"

    # One epic that cannot be written to — forces OSError inside single-pass python
    # (the write_edit_event call's open(fpath, 'w') will fail on a read-only dir)
    local bad_id="epic-containment-bad-14b"
    local bad_dir="$tracker_dir/$bad_id"
    mkdir -p "$bad_dir"
    local bad_desc
    bad_desc='{"ticket_type": "epic", "title": "Bad epic", "parent_id": null, "description": "### Planning Intelligence Log\n- **Web research (Step 2.6)**: not triggered\n- **Scenario analysis (Step 2.75)**: not triggered\n- **LLM-instruction signal (Step 5)**: not triggered"}'
    _write_event "$bad_dir" "1742700200" "00000000-0000-4000-8000-bad0001cr001" "CREATE" "$bad_desc"
    # Make the ticket dir read-only so the python write_edit_event raises OSError
    chmod 555 "$bad_dir"
    # Ensure cleanup can still remove it (trap rm -rf 2>/dev/null || true tolerates)

    local exit_code=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || exit_code=$?

    # Restore perms so the test cleanup trap can remove the dir
    chmod 755 "$bad_dir" 2>/dev/null || true

    # Migration must still exit 0 — OSError on one ticket does not abort the run
    assert_eq "test_write_failure_containment: migration exits 0 despite per-ticket write failure" "0" "$exit_code"

    # Good ticket was tagged
    if _ticket_has_tag "$tracker_dir" "$good_id" "brainstorm:complete"; then
        assert_eq "test_write_failure_containment: healthy ticket tagged" "tagged" "tagged"
    else
        assert_eq "test_write_failure_containment: healthy ticket tagged" "tagged" "not-tagged"
    fi

    # Bad ticket was NOT tagged (no EDIT event committed — would have been ignored
    # by the bash commit loop since python emits ERROR: not WROTE: on failure)
    if _ticket_has_tag "$tracker_dir" "$bad_id" "brainstorm:complete"; then
        assert_eq "test_write_failure_containment: failed-write ticket not tagged" "not-tagged" "tagged"
    else
        assert_eq "test_write_failure_containment: failed-write ticket not tagged" "not-tagged" "not-tagged"
    fi

    assert_pass_if_clean "test_write_failure_containment"
}
test_write_failure_containment

# ═══════════════════════════════════════════════════════════════════════════════
# Test 15: scrutiny:pending tag removed during migration
# ═══════════════════════════════════════════════════════════════════════════════
# An epic with scrutiny:pending tag and a PIL heading should have scrutiny:pending
# removed from its tags while brainstorm:complete is added. The EDIT event must
# contain the new tag list without scrutiny:pending.
echo "Test 15: scrutiny:pending tag removed during migration"
test_scrutiny_pending_removed() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local tracker_dir="$repo/.tickets-tracker"
    local epic_id="epic-scrutiny-pending-15"
    local ticket_dir="$tracker_dir/$epic_id"
    mkdir -p "$ticket_dir"

    # Epic with scrutiny:pending and another tag; PIL heading in description.
    # The migration must preserve the other tag, add brainstorm:complete, and drop scrutiny:pending.
    local create_data
    create_data='{"ticket_type": "epic", "title": "Epic with scrutiny:pending", "parent_id": null, "description": "## Background\n\n### Planning Intelligence Log\n- **Web research (Step 2.6)**: not triggered\n- **Scenario analysis (Step 2.75)**: not triggered\n- **LLM-instruction signal (Step 5)**: not triggered", "tags": ["scrutiny:pending", "some-other-tag"]}'
    _write_event "$ticket_dir" "1742800100" "00000000-0000-4000-8000-scrut001cr01" "CREATE" "$create_data"

    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    # Must have brainstorm:complete tag
    if _ticket_has_tag "$tracker_dir" "$epic_id" "brainstorm:complete"; then
        assert_eq "scrutiny-pending epic: has brainstorm:complete" "tagged" "tagged"
    else
        assert_eq "scrutiny-pending epic: has brainstorm:complete" "tagged" "not-tagged"
    fi

    # Must NOT have scrutiny:pending tag in the EDIT event
    # Inspect the EDIT event files directly for the tag list
    local has_scrutiny_in_edit
    has_scrutiny_in_edit=$(python3 - "$ticket_dir" <<PYEOF_INNER
import json, os, sys

ticket_dir = sys.argv[1]
found = False
for fname in sorted(os.listdir(ticket_dir)):
    if not fname.endswith('-EDIT.json') or fname.startswith('.'):
        continue
    try:
        with open(os.path.join(ticket_dir, fname)) as f:
            event = json.load(f)
        tags = event.get('data', {}).get('fields', {}).get('tags', None)
        if tags is not None:
            tag_list = tags if isinstance(tags, list) else tags.split(',')
            if 'scrutiny:pending' in tag_list:
                found = True
    except (json.JSONDecodeError, OSError):
        pass
print("yes" if found else "no")
PYEOF_INNER
)

    assert_eq "scrutiny-pending: removed from EDIT event tags" "no" "$has_scrutiny_in_edit"

    # Must still have some-other-tag (scrutiny:pending removal is precise, not destructive)
    if _ticket_has_tag "$tracker_dir" "$epic_id" "some-other-tag"; then
        assert_eq "scrutiny-pending: other tags preserved" "preserved" "preserved"
    else
        assert_eq "scrutiny-pending: other tags preserved" "preserved" "lost"
    fi

    assert_pass_if_clean "test_scrutiny_pending_removed"
}
test_scrutiny_pending_removed

# ═══════════════════════════════════════════════════════════════════════════════
# Test 16: --dry-run gate makes no changes (read-only)
# ═══════════════════════════════════════════════════════════════════════════════
# With --dry-run: exits 0, prints DRY-RUN lines for eligible epics, writes no
# EDIT event files, creates no git commits in the tracker, and does not write
# the marker file. A subsequent normal run must still perform the migration.
echo "Test 16: --dry-run gate makes no actual changes"
test_dryrun_gate_no_changes() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local tracker_dir="$repo/.tickets-tracker"
    local epic_id="epic-dryrun-test-16"
    local ticket_dir="$tracker_dir/$epic_id"
    mkdir -p "$ticket_dir"

    # Epic with PIL heading and scrutiny:pending (both behaviors exercised)
    local create_data
    create_data='{"ticket_type": "epic", "title": "Epic for dry-run test", "parent_id": null, "description": "## Background\n\n### Planning Intelligence Log\n- **Web research (Step 2.6)**: not triggered\n- **Scenario analysis (Step 2.75)**: not triggered\n- **LLM-instruction signal (Step 5)**: not triggered", "tags": ["scrutiny:pending"]}'
    _write_event "$ticket_dir" "1742900100" "00000000-0000-4000-8000-dryrun01cr01" "CREATE" "$create_data"

    # Record state before dry-run: count EDIT event files and commits
    local edit_count_before
    edit_count_before=$(find "$ticket_dir" -name '*-EDIT.json' 2>/dev/null | wc -l | tr -d ' ')
    local commit_count_before
    commit_count_before=$(git -C "$tracker_dir" log --oneline 2>/dev/null | wc -l | tr -d ' ')

    # Run with --dry-run
    local exit_code=0
    local output
    output=$(cd "$repo" && bash "$MIGRATE_SCRIPT" --dry-run 2>/dev/null) || exit_code=$?

    # Must exit 0
    assert_eq "dry-run: exits 0" "0" "$exit_code"

    # Must print a DRY-RUN line for the eligible epic
    assert_contains "dry-run: prints DRY-RUN line for eligible epic" \
        "DRY-RUN:" "$output"

    # No EDIT event files must have been created
    local edit_count_after
    edit_count_after=$(find "$ticket_dir" -name '*-EDIT.json' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "dry-run: no EDIT event files created" "$edit_count_before" "$edit_count_after"

    # No new git commits in the tracker branch
    local commit_count_after
    commit_count_after=$(git -C "$tracker_dir" log --oneline 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "dry-run: no new git commits in tracker" "$commit_count_before" "$commit_count_after"

    # Shared marker file must NOT be written (dry-run must not commit to tracker)
    if [ -f "$tracker_dir/.migrations/brainstorm-tag-migration-v2" ]; then
        assert_eq "dry-run: shared marker file NOT written" "not-written" "written"
    else
        assert_eq "dry-run: shared marker file NOT written" "not-written" "not-written"
    fi

    # A subsequent normal run must still perform the migration (marker absent = not idempotent-blocked)
    local exit2=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || exit2=$?
    assert_eq "dry-run: subsequent normal run succeeds" "0" "$exit2"

    if _ticket_has_tag "$tracker_dir" "$epic_id" "brainstorm:complete"; then
        assert_eq "dry-run: subsequent normal run actually migrates" "tagged" "tagged"
    else
        assert_eq "dry-run: subsequent normal run actually migrates" "tagged" "not-tagged"
    fi

    assert_pass_if_clean "test_dryrun_gate_no_changes"
}
test_dryrun_gate_no_changes

# ═══════════════════════════════════════════════════════════════════════════════
# Test 17: untag-then-remigrate does NOT re-apply brainstorm:complete tag
# ═══════════════════════════════════════════════════════════════════════════════
# Regression guard for bug 01b9-3359-7df3-45cf: the migration marker was stored
# per-worktree in .claude/.brainstorm-tag-migration-v2 (gitignored). Fresh
# worktrees lack the marker, so migration re-runs and re-applies a deliberately-
# removed brainstorm:complete tag. Fix: move marker to the shared tickets tracker
# branch at $TRACKER_DIR/.migrations/brainstorm-tag-migration-v2.
#
# Steps:
#   1. Fresh fixture repo with PIL epic, run migration → tag applied, shared
#      marker written at $TRACKER_DIR/.migrations/brainstorm-tag-migration-v2
#   2. Untag brainstorm:complete (via EDIT event with tags=[])
#   3. Remove the OLD local .claude/.brainstorm-tag-migration-v2 if present
#      (simulates a fresh worktree that never had the per-worktree marker)
#   4. Re-run migration → assert tag NOT re-applied (shared marker blocks re-run)
echo "Test 17: untag-then-remigrate does not re-apply brainstorm:complete tag"
test_untag_then_remigrate_does_not_retag() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local tracker_dir="$repo/.tickets-tracker"
    local epic_id="epic-untag-remigrate-17"
    local ticket_dir="$tracker_dir/$epic_id"
    mkdir -p "$ticket_dir"

    # Epic with a full PIL so migration will tag it on first run
    local create_data
    create_data='{"ticket_type": "epic", "title": "Epic for untag-remigrate test", "parent_id": null, "description": "## Background\n\n### Planning Intelligence Log\n- **Web research (Step 2.6)**: not triggered\n- **Scenario analysis (Step 2.75)**: not triggered\n- **LLM-instruction signal (Step 5)**: not triggered"}'
    _write_event "$ticket_dir" "1743000100" "00000000-0000-4000-8000-untagremi001" "CREATE" "$create_data"

    # Step 1: First migration run — tag must be applied
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    if _ticket_has_tag "$tracker_dir" "$epic_id" "brainstorm:complete"; then
        assert_eq "step1: brainstorm:complete tag applied after first migration" "tagged" "tagged"
    else
        assert_eq "step1: brainstorm:complete tag applied after first migration" "tagged" "not-tagged"
        # Cannot proceed if step 1 failed
        assert_pass_if_clean "test_untag_then_remigrate_does_not_retag"
        return
    fi

    # Step 1b: assert shared marker exists at tracker path (not just local .claude/)
    local shared_marker="$tracker_dir/.migrations/brainstorm-tag-migration-v2"
    if [ -f "$shared_marker" ]; then
        assert_eq "step1b: shared marker written at tracker .migrations/ path" "exists" "exists"
    else
        assert_eq "step1b: shared marker written at tracker .migrations/ path" "exists" "missing"
    fi

    # Step 2: Simulate deliberate untag — write an EDIT event setting tags=[]
    # and commit it to the tracker.
    local untag_data
    untag_data='{"fields": {"tags": []}}'
    _write_event "$ticket_dir" "1743000200" "00000000-0000-4000-8000-untagremi002" "EDIT" "$untag_data"
    git -C "$tracker_dir" add "$epic_id/" >/dev/null 2>&1
    git -C "$tracker_dir" commit -m "untag brainstorm:complete for test" >/dev/null 2>&1 || true

    # Record EDIT count with brainstorm:complete tag after untag (baseline).
    # Note: _ticket_has_tag scans all historical events so will see the
    # migration EDIT; we use _count_brainstorm_tag_edits instead to detect
    # re-application (count would increase if re-migration wrote a new EDIT).
    local edits_before
    edits_before=$(_count_brainstorm_tag_edits "$tracker_dir" "$epic_id")
    assert_eq "step2: at least 1 brainstorm:complete EDIT exists from initial migration" \
        "1" "$edits_before"

    # Step 3: Remove the OLD local per-worktree marker (simulates fresh worktree)
    rm -f "$repo/.claude/.brainstorm-tag-migration-v2" 2>/dev/null || true

    # Step 4: Re-run migration — must be a no-op because shared marker exists
    local exit_code=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || exit_code=$?
    assert_eq "step4: migration re-run exits 0" "0" "$exit_code"

    # Assert no new EDIT events with brainstorm:complete tag were written
    # (re-migration must not re-apply the tag; shared marker gates the run)
    local edits_after
    edits_after=$(_count_brainstorm_tag_edits "$tracker_dir" "$epic_id")
    assert_eq "step4: no new brainstorm:complete EDIT events after re-migration" "$edits_before" "$edits_after"

    assert_pass_if_clean "test_untag_then_remigrate_does_not_retag"
}
test_untag_then_remigrate_does_not_retag

# ═══════════════════════════════════════════════════════════════════════════════
print_summary
