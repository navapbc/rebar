#!/usr/bin/env bash
# RED: test_migrate_closure_checks_v1_adds_section
# tests/scripts/test-ticket-migrate-closure-checks-v1.sh
# Behavioral fixture test for src/rebar/_engine/ticket-migrate-closure-checks-v1.sh
#
# Testing Mode: RED — confirms the migration script does not yet exist.
# These tests MUST FAIL until ticket-migrate-closure-checks-v1.sh is implemented.
#
# Test structure:
#   - 2 epics whose descriptions LACK a "## Closure Checks" section (after "## Success Criteria")
#   - 1 epic whose description ALREADY HAS "## Closure Checks"
#
# Assertions:
#   1. Migration exits 0
#   2. 2 lacking-section epics now have "## Closure Checks" section in their descriptions
#   3. Already-migrated epic is unchanged (no second "## Closure Checks" section added)
#   4. Marker file .rebar/.closure-checks-migration-v1 written at repo root
#   5. Re-run exits 0 immediately (idempotency — marker present) with no additional changes
#   6. Dry-run: no files written, no marker created
#
# Usage: bash tests/scripts/test-ticket-migrate-closure-checks-v1.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e intentionally omitted — test functions may return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
MIGRATE_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-migrate-closure-checks-v1.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-migrate-closure-checks-v1.sh ==="

# ── Suite-runner guard: skip when migration script does not exist ─────────────
# RED tests fail by design (script not found). When auto-discovered by
# run-script-tests.sh, they would break `bash tests/run-all.sh`. Skip with
# exit 0 when ticket-migrate-closure-checks-v1.sh is absent AND running under the suite runner.
if [ "${_RUN_ALL_ACTIVE:-0}" = "1" ] && [ ! -f "$MIGRATE_SCRIPT" ]; then
    echo "SKIP: ticket-migrate-closure-checks-v1.sh not yet implemented (RED) — tests deferred"
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
# Returns: sets EPIC_NO_SECTION_1_ID, EPIC_NO_SECTION_2_ID, EPIC_HAS_SECTION_ID in caller scope
_setup_epic_fixture() {
    local tracker_dir="$1"

    # Epic 1: Has Success Criteria but LACKS ## Closure Checks
    EPIC_NO_SECTION_1_ID="epic-no-closure-checks-01"
    local dir1="$tracker_dir/$EPIC_NO_SECTION_1_ID"
    mkdir -p "$dir1"
    local desc1
    desc1='{"ticket_type": "epic", "title": "Epic lacking Closure Checks (1)", "parent_id": null, "description": "## Context\n\nSome context.\n\n## Success Criteria\n- Criterion A\n- Criterion B\n\n## Dependencies\nNone"}'
    _write_event "$dir1" "1742605100" "00000000-0000-4000-8000-closure001001" "CREATE" "$desc1"

    # Epic 2: Has Success Criteria but LACKS ## Closure Checks (different description format)
    EPIC_NO_SECTION_2_ID="epic-no-closure-checks-02"
    local dir2="$tracker_dir/$EPIC_NO_SECTION_2_ID"
    mkdir -p "$dir2"
    local desc2
    desc2='{"ticket_type": "epic", "title": "Epic lacking Closure Checks (2)", "parent_id": null, "description": "## Context\n\nAnother context.\n\n## Success Criteria\n- Criterion X\n\n## Approach\nSome approach."}'
    _write_event "$dir2" "1742605200" "00000000-0000-4000-8000-closure002001" "CREATE" "$desc2"

    # Epic 3: ALREADY HAS ## Closure Checks section
    EPIC_HAS_SECTION_ID="epic-has-closure-checks-03"
    local dir3="$tracker_dir/$EPIC_HAS_SECTION_ID"
    mkdir -p "$dir3"
    local desc3
    desc3='{"ticket_type": "epic", "title": "Epic already with Closure Checks", "parent_id": null, "description": "## Context\n\nAlready migrated.\n\n## Success Criteria\n- Criterion Z\n\n## Closure Checks\n- Some existing closure check\n\n## Dependencies\nNone"}'
    _write_event "$dir3" "1742605300" "00000000-0000-4000-8000-closure003001" "CREATE" "$desc3"
}

# ── Helper: read the current compiled description for a ticket ────────────────
# Uses the ticket reducer directly against the tracker dir for test isolation.
# This avoids dependency on the rebar CLI being present in the
# fixture repo — the fixture repo only has .tickets-tracker/, not the full tooling.
_get_ticket_description() {
    local repo="$1"
    local ticket_id="$2"
    local tracker_dir="$repo/.tickets-tracker"
    local ticket_dir="$tracker_dir/$ticket_id"
    if [ ! -d "$ticket_dir" ]; then
        echo ""
        return
    fi
    # Use the ticket reducer from the real repo (not the fixture)
    TICKETS_TRACKER_DIR="$tracker_dir" python3 "$REPO_ROOT/src/rebar/_engine/ticket-reducer.py" "$ticket_dir" 2>/dev/null \
        | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('description',''))" 2>/dev/null || echo ""
}

# ── Helper: count ## Closure Checks occurrences in a string ──────────────────
_count_closure_checks_sections() {
    local text="$1"
    echo "$text" | grep -c '## Closure Checks' 2>/dev/null || echo "0"
}

# ── Helper: count EDIT events for a ticket ───────────────────────────────────
_count_edit_events() {
    local tracker_dir="$1"
    local ticket_id="$2"
    find "$tracker_dir/$ticket_id" -name '*-EDIT.json' 2>/dev/null | wc -l | tr -d ' '
}

# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: Migration script must exist (RED gate)
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 1: migration script exists"
test_migration_script_exists() {
    if [ -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "ticket-migrate-closure-checks-v1.sh exists" "exists" "exists"
    else
        assert_eq "ticket-migrate-closure-checks-v1.sh exists" "exists" "missing"
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
# Test 3: 2 lacking-section epics now have ## Closure Checks section
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 3: 2 lacking-section epics get ## Closure Checks section"
test_lacking_epics_get_closure_checks_section() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    _setup_epic_fixture "$repo/.tickets-tracker"

    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    # Epic 1 (lacking section) must now have ## Closure Checks
    local desc1
    desc1=$(_get_ticket_description "$repo" "$EPIC_NO_SECTION_1_ID")
    local count1
    count1=$(_count_closure_checks_sections "$desc1")
    assert_eq "epic-no-section-1: has ## Closure Checks section" "1" "$count1"

    # Epic 2 (lacking section) must now have ## Closure Checks
    local desc2
    desc2=$(_get_ticket_description "$repo" "$EPIC_NO_SECTION_2_ID")
    local count2
    count2=$(_count_closure_checks_sections "$desc2")
    assert_eq "epic-no-section-2: has ## Closure Checks section" "1" "$count2"

    assert_pass_if_clean "test_lacking_epics_get_closure_checks_section"
}
test_lacking_epics_get_closure_checks_section

# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: Already-migrated epic is unchanged (no second ## Closure Checks added)
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 4: already-migrated epic is unchanged (no duplicate section)"
test_already_migrated_epic_is_unchanged() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    _setup_epic_fixture "$repo/.tickets-tracker"

    local edits_before
    edits_before=$(_count_edit_events "$repo/.tickets-tracker" "$EPIC_HAS_SECTION_ID")

    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    # Epic 3 must still have exactly 1 ## Closure Checks section (not 2)
    local desc3
    desc3=$(_get_ticket_description "$repo" "$EPIC_HAS_SECTION_ID")
    local count3
    count3=$(_count_closure_checks_sections "$desc3")
    assert_eq "epic-has-section: still has exactly 1 ## Closure Checks section" "1" "$count3"

    # No EDIT event must have been written (no change = no edit event)
    local edits_after
    edits_after=$(_count_edit_events "$repo/.tickets-tracker" "$EPIC_HAS_SECTION_ID")
    assert_eq "epic-has-section: no new EDIT events written" "$edits_before" "$edits_after"

    assert_pass_if_clean "test_already_migrated_epic_is_unchanged"
}
test_already_migrated_epic_is_unchanged

# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: Marker file .rebar/.closure-checks-migration-v1 written at repo root
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 5: marker file .closure-checks-migration-v1 written at repo root"
test_marker_file_written() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    _setup_epic_fixture "$repo/.tickets-tracker"
    mkdir -p "$repo/.rebar"

    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    if [ -f "$repo/.rebar/.closure-checks-migration-v1" ]; then
        assert_eq "marker file written" "exists" "exists"
    else
        assert_eq "marker file written" "exists" "missing"
    fi

    assert_pass_if_clean "test_marker_file_written"
}
test_marker_file_written

# ═══════════════════════════════════════════════════════════════════════════════
# Test 6: Idempotency — re-run exits 0 immediately with no additional changes
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 6: re-run with marker present exits 0 with no additional changes"
test_rerun_with_marker_is_noop() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    _setup_epic_fixture "$repo/.tickets-tracker"
    mkdir -p "$repo/.rebar"

    # First run — performs migration
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    # Record EDIT event counts after first run
    local edits1_before edits2_before edits3_before
    edits1_before=$(_count_edit_events "$repo/.tickets-tracker" "$EPIC_NO_SECTION_1_ID")
    edits2_before=$(_count_edit_events "$repo/.tickets-tracker" "$EPIC_NO_SECTION_2_ID")
    edits3_before=$(_count_edit_events "$repo/.tickets-tracker" "$EPIC_HAS_SECTION_ID")

    # Second run — marker is present, must exit 0 and write no new events
    local exit2=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || exit2=$?
    assert_eq "re-run exits 0 with marker present" "0" "$exit2"

    # EDIT event counts must be unchanged after second run
    local edits1_after edits2_after edits3_after
    edits1_after=$(_count_edit_events "$repo/.tickets-tracker" "$EPIC_NO_SECTION_1_ID")
    edits2_after=$(_count_edit_events "$repo/.tickets-tracker" "$EPIC_NO_SECTION_2_ID")
    edits3_after=$(_count_edit_events "$repo/.tickets-tracker" "$EPIC_HAS_SECTION_ID")

    assert_eq "re-run: no new EDIT events on epic-no-section-1" \
        "$edits1_before" "$edits1_after"
    assert_eq "re-run: no new EDIT events on epic-no-section-2" \
        "$edits2_before" "$edits2_after"
    assert_eq "re-run: no new EDIT events on epic-has-section" \
        "$edits3_before" "$edits3_after"

    # Also confirm descriptions have not changed (no duplicate sections)
    local desc1
    desc1=$(_get_ticket_description "$repo" "$EPIC_NO_SECTION_1_ID")
    local count1
    count1=$(_count_closure_checks_sections "$desc1")
    assert_eq "re-run: epic-no-section-1 still has exactly 1 ## Closure Checks" "1" "$count1"

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
    mkdir -p "$repo/.rebar"

    # Simulate being inside the plugin source repo by placing plugin.json at repo root
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

    # Marker file must NOT be written (guard bailed before making changes)
    if [ -f "$repo/.rebar/.closure-checks-migration-v1" ]; then
        assert_eq "plugin-source-repo guard: marker NOT written" "not-written" "written"
    else
        assert_eq "plugin-source-repo guard: marker NOT written" "not-written" "not-written"
    fi

    # No EDIT events on any epic
    local edits1
    edits1=$(_count_edit_events "$repo/.tickets-tracker" "$EPIC_NO_SECTION_1_ID")
    assert_eq "plugin-source-repo guard: epic-no-section-1 NOT modified" "0" "$edits1"

    assert_pass_if_clean "test_plugin_source_repo_guard"
}
test_plugin_source_repo_guard

# ═══════════════════════════════════════════════════════════════════════════════
# Test 8: --dry-run flag shows what would change without making changes
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 8: --dry-run flag makes no actual changes"
test_dryrun_no_changes() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    _setup_epic_fixture "$repo/.tickets-tracker"
    mkdir -p "$repo/.rebar"

    # Record state before dry-run
    local edits1_before edits2_before
    edits1_before=$(_count_edit_events "$repo/.tickets-tracker" "$EPIC_NO_SECTION_1_ID")
    edits2_before=$(_count_edit_events "$repo/.tickets-tracker" "$EPIC_NO_SECTION_2_ID")

    # Run with --dry-run
    local exit_code=0
    local output
    output=$(cd "$repo" && bash "$MIGRATE_SCRIPT" --dry-run 2>/dev/null) || exit_code=$?

    # Must exit 0
    assert_eq "dry-run: exits 0" "0" "$exit_code"

    # Must print output indicating what would change
    assert_contains "dry-run: prints DRY-RUN output" \
        "DRY-RUN" "$output"

    # No EDIT event files must have been created
    local edits1_after edits2_after
    edits1_after=$(_count_edit_events "$repo/.tickets-tracker" "$EPIC_NO_SECTION_1_ID")
    edits2_after=$(_count_edit_events "$repo/.tickets-tracker" "$EPIC_NO_SECTION_2_ID")
    assert_eq "dry-run: no EDIT events on epic-no-section-1" "$edits1_before" "$edits1_after"
    assert_eq "dry-run: no EDIT events on epic-no-section-2" "$edits2_before" "$edits2_after"

    # Marker file must NOT be written
    if [ -f "$repo/.rebar/.closure-checks-migration-v1" ]; then
        assert_eq "dry-run: marker file NOT written" "not-written" "written"
    else
        assert_eq "dry-run: marker file NOT written" "not-written" "not-written"
    fi

    # A subsequent normal run must still perform the migration (marker absent = not idempotent-blocked)
    local exit2=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || exit2=$?
    assert_eq "dry-run: subsequent normal run succeeds" "0" "$exit2"

    # After normal run, desc has ## Closure Checks
    local desc1
    desc1=$(_get_ticket_description "$repo" "$EPIC_NO_SECTION_1_ID")
    local count1
    count1=$(_count_closure_checks_sections "$desc1")
    assert_eq "dry-run: subsequent normal run actually migrates epic-no-section-1" "1" "$count1"

    assert_pass_if_clean "test_dryrun_no_changes"
}
test_dryrun_no_changes

# ═══════════════════════════════════════════════════════════════════════════════
# Test 9: Section insertion places ## Closure Checks AFTER ## Success Criteria
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 9: ## Closure Checks section placed after ## Success Criteria"
test_closure_checks_placed_after_success_criteria() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    _setup_epic_fixture "$repo/.tickets-tracker"

    (cd "$repo" && bash "$MIGRATE_SCRIPT") >/dev/null 2>&1 || true

    # Check ordering: ## Success Criteria must appear before ## Closure Checks
    local desc1
    desc1=$(_get_ticket_description "$repo" "$EPIC_NO_SECTION_1_ID")

    local sc_line cc_line
    sc_line=$(echo "$desc1" | grep -n '## Success Criteria' | head -1 | cut -d: -f1)
    cc_line=$(echo "$desc1" | grep -n '## Closure Checks' | head -1 | cut -d: -f1)

    if [ -n "$sc_line" ] && [ -n "$cc_line" ] && [ "$cc_line" -gt "$sc_line" ]; then
        assert_eq "## Closure Checks placed after ## Success Criteria" "after" "after"
    else
        assert_eq "## Closure Checks placed after ## Success Criteria" "after" "not-after-or-missing"
    fi

    assert_pass_if_clean "test_closure_checks_placed_after_success_criteria"
}
test_closure_checks_placed_after_success_criteria

# ═══════════════════════════════════════════════════════════════════════════════
# Test 10: No-ticket-system guard — exits 0 with NOTICE when .tickets-tracker absent
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 10: no-ticket-system guard exits 0 when .tickets-tracker absent"
test_no_ticket_system_guard() {
    _snapshot_fail

    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local tmp_dir
    tmp_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp_dir")

    # A bare git repo with no .tickets-tracker
    git init -q -b main "$tmp_dir/bare-repo"
    git -C "$tmp_dir/bare-repo" config user.email "test@test.com"
    git -C "$tmp_dir/bare-repo" config user.name "Test"
    git -C "$tmp_dir/bare-repo" config commit.gpgsign false
    echo "init" > "$tmp_dir/bare-repo/README.md"
    git -C "$tmp_dir/bare-repo" add -A
    git -C "$tmp_dir/bare-repo" commit -q -m "init"

    local exit_code=0
    local output
    output=$(cd "$tmp_dir/bare-repo" && bash "$MIGRATE_SCRIPT" 2>&1) || exit_code=$?

    assert_eq "no-ticket-system guard: exits 0" "0" "$exit_code"

    # Must emit a notice (not silently exit without explanation)
    if [ -n "$output" ]; then
        assert_eq "no-ticket-system guard: emits output" "output-emitted" "output-emitted"
    else
        assert_eq "no-ticket-system guard: emits output" "output-emitted" "silent-exit"
    fi

    assert_pass_if_clean "test_no_ticket_system_guard"
}
test_no_ticket_system_guard

# ═══════════════════════════════════════════════════════════════════════════════
print_summary
