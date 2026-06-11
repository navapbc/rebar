#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-list-epics-alias.sh
# RED test for bug 1fe1-e703: ticket list-epics shows UUID instead of user-friendly alias.
#
# Tests verify that when an epic has an alias set, ticket list-epics displays
# the alias (not the raw UUID) in its output.
#
# RED STATE: Tests will FAIL before the fix because _build_index() never extracts
# the alias field, so output always shows the raw UUID.
#
# RED MARKER:
# tests/scripts/suites/test-ticket-list-epics-alias.sh [test_list_epics_shows_alias]
#
# Usage: bash tests/scripts/suites/test-ticket-list-epics-alias.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e intentionally omitted — test assertions return non-zero by design
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LIST_EPICS="$PLUGIN_ROOT/src/rebar/_engine/ticket-list-epics.sh"

source "$SCRIPT_DIR/../lib/run_test.sh"

# ── Cleanup ───────────────────────────────────────────────────────────────────
_CLEANUP_DIRS=()
_cleanup() { for d in "${_CLEANUP_DIRS[@]:-}"; do rm -rf "$d"; done; }
trap _cleanup EXIT

echo "=== test-ticket-list-epics-alias.sh ==="

# ── Fixture helpers ───────────────────────────────────────────────────────────

# make_tracker_with_aliased_epic — creates a TICKETS_TRACKER_DIR with one open
# epic that has an alias set in its CREATE event.
#   ticket_id: epic-alias-1
#   alias:     swift-falcon-forest
#   title:     Epic With Alias
make_tracker_with_aliased_epic() {
    local tracker_dir
    tracker_dir=$(mktemp -d "${TMPDIR:-/tmp}/test-ticket-list-epics-alias.XXXXXX")
    _CLEANUP_DIRS+=("$tracker_dir")

    mkdir -p "$tracker_dir/epic-alias-1"
    python3 -c "
import json
event = {
    'event_type': 'CREATE',
    'ticket_id': 'epic-alias-1',
    'timestamp': 1700000000000000000,
    'uuid': 'dddddddd-0001-0001-0001-000000000001',
    'env_id': 'test-env',
    'author': 'Test',
    'data': {
        'ticket_id': 'epic-alias-1',
        'title': 'Epic With Alias',
        'ticket_type': 'epic',
        'status': 'open',
        'priority': 2,
        'parent_id': None,
        'tags': [],
        'description': '',
        'notes': '',
        'alias': 'swift-falcon-forest'
    }
}
with open('$tracker_dir/epic-alias-1/001-CREATE.json', 'w') as f:
    json.dump(event, f)
"
    echo "$tracker_dir"
}

# make_tracker_no_alias — creates a TICKETS_TRACKER_DIR with one open epic
# that has NO alias set (alias computed from ticket_id by reducer).
make_tracker_no_alias() {
    local tracker_dir
    tracker_dir=$(mktemp -d "${TMPDIR:-/tmp}/test-ticket-list-epics-alias.XXXXXX")
    _CLEANUP_DIRS+=("$tracker_dir")

    mkdir -p "$tracker_dir/epic-noalias-1"
    python3 -c "
import json
event = {
    'event_type': 'CREATE',
    'ticket_id': 'epic-noalias-1',
    'timestamp': 1700000010000000000,
    'uuid': 'eeeeeeee-0001-0001-0001-000000000001',
    'env_id': 'test-env',
    'author': 'Test',
    'data': {
        'ticket_id': 'epic-noalias-1',
        'title': 'Epic Without Alias',
        'ticket_type': 'epic',
        'status': 'open',
        'priority': 3,
        'parent_id': None,
        'tags': [],
        'description': '',
        'notes': ''
    }
}
with open('$tracker_dir/epic-noalias-1/001-CREATE.json', 'w') as f:
    json.dump(event, f)
"
    echo "$tracker_dir"
}

# ── Tests ─────────────────────────────────────────────────────────────────────

test_list_epics_shows_alias() {
    local _tracker _output

    # Test 1: When an epic has an alias, list-epics shows the alias (not the UUID)
    # Given an epic with alias 'swift-falcon-forest'
    # When ticket list-epics is run
    # Then the output contains 'swift-falcon-forest' and NOT 'epic-alias-1'
    echo "Test 1: Epic with alias shows alias in output (not raw ticket ID)"
    _tracker=$(make_tracker_with_aliased_epic)
    _output=$(TICKETS_TRACKER_DIR="$_tracker" bash "$LIST_EPICS" 2>&1) || true

    if [[ "$_output" =~ "swift-falcon-forest" ]]; then
        echo "  PASS: alias 'swift-falcon-forest' appears in output"
        (( PASS++ ))
    else
        echo "  FAIL: alias 'swift-falcon-forest' missing from output (alias must appear instead of raw ticket ID)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 2: When an epic has an alias, the raw ticket ID does NOT appear as the first field
    # (i.e., alias substituted the UUID in display)
    # Given an epic with alias 'swift-falcon-forest' and ticket_id 'epic-alias-1'
    # When ticket list-epics is run
    # Then the first field of the output line is NOT 'epic-alias-1'
    echo "Test 2: Raw ticket ID is NOT used as display identifier when alias exists"
    _tracker=$(make_tracker_with_aliased_epic)
    _output=$(TICKETS_TRACKER_DIR="$_tracker" bash "$LIST_EPICS" 2>&1) || true

    # The first tab-separated field should be the alias, not the raw ID
    local first_field
    first_field=$(echo "$_output" | awk -F'\t' 'NF>=2{print $1; exit}')
    if [[ "$first_field" == "swift-falcon-forest" ]]; then
        echo "  PASS: first field is alias 'swift-falcon-forest'"
        (( PASS++ ))
    elif [[ "$first_field" == "epic-alias-1" ]]; then
        echo "  FAIL: first field is raw ticket ID 'epic-alias-1' instead of alias (alias must be used as display identifier)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    else
        echo "  FAIL: unexpected first field: '$first_field' (output: $_output)" >&2
        (( FAIL++ ))
    fi

    # Test 3: Epic title still appears alongside the alias
    echo "Test 3: Epic title 'Epic With Alias' still appears in output"
    _tracker=$(make_tracker_with_aliased_epic)
    _output=$(TICKETS_TRACKER_DIR="$_tracker" bash "$LIST_EPICS" 2>&1) || true

    if [[ "$_output" =~ "Epic With Alias" ]]; then
        echo "  PASS: title 'Epic With Alias' appears in output"
        (( PASS++ ))
    else
        echo "  FAIL: title 'Epic With Alias' missing from output" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 4: When no alias is stored, the ticket ID (computed alias) is shown
    # (regression guard — epics without aliases must still display something)
    echo "Test 4: Epic without alias shows computed alias or ticket ID (regression guard)"
    _tracker=$(make_tracker_no_alias)
    _output=$(TICKETS_TRACKER_DIR="$_tracker" bash "$LIST_EPICS" 2>&1) || true

    if [[ "$_output" =~ "Epic Without Alias" ]]; then
        echo "  PASS: epic without alias still appears in output"
        (( PASS++ ))
    else
        echo "  FAIL: epic without alias missing from output" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi
}

test_list_epics_shows_alias

# ── Results ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
