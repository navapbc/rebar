#!/usr/bin/env bash
# tests/scripts/test-ticket-list-epics-dispatcher.sh
# RED integration tests for 'ticket list-epics' subcommand routing through the dispatcher.
#
# Tests verify that the dispatcher correctly routes 'ticket list-epics' to
# sprint-list-epics.sh and that exit codes and output are passed through.
#
# Uses TICKETS_TRACKER_DIR injection because sprint-list-epics.sh reads
# the tracker directory via TICKETS_TRACKER_DIR (not TICKET_CMD).
#
# RED STATE: Tests 2-6 currently fail because the dispatcher does not have a
# 'list-epics' case. They will pass (GREEN) after the dispatcher case is added.
#
# RED MARKER:
# tests/scripts/test-ticket-list-epics-dispatcher.sh [test_list_epics_routes_through_dispatcher]
#
# Usage: bash tests/scripts/test-ticket-list-epics-dispatcher.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e intentionally omitted — test assertions return non-zero by design;
# -e would abort the script on the first failing test instead of collecting all results.
# All test files in this suite use the same sourced-library initialization pattern.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DISPATCHER="$PLUGIN_ROOT/src/rebar/_engine/ticket"

source "$SCRIPT_DIR/../lib/run_test.sh"

# ── Cleanup ───────────────────────────────────────────────────────────────────
_CLEANUP_DIRS=()
_cleanup() { for d in "${_CLEANUP_DIRS[@]:-}"; do rm -rf "$d"; done; }
trap _cleanup EXIT

echo "=== test-ticket-list-epics-dispatcher.sh ==="

# ── Fixture helpers ───────────────────────────────────────────────────────────

# make_tracker_epics_fixture — creates a TICKETS_TRACKER_DIR with three epics:
#   epic-open-1:    open, priority 2, title "Open Epic Alpha"
#   epic-inprog-1:  in-progress (CREATE + STATUS to in_progress), priority 1, "In Progress Beta"
#   epic-blocked-1: open, priority 3, "Blocked Epic Gamma", depends_on epic-open-1
#
# Event format uses actual reducer event types:
#   STATUS  (not TRANSITION) — data.status = target
#   LINK    (not DEPENDS_ON) — data.target_id + data.relation
make_tracker_epics_fixture() {
    local tracker_dir
    tracker_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tracker_dir")

    # ── epic-open-1: open, priority 2 ─────────────────────────────────────────
    mkdir -p "$tracker_dir/epic-open-1"
    python3 -c "
import json
event = {
    'event_type': 'CREATE',
    'ticket_id': 'epic-open-1',
    'timestamp': 1700000000000000000,
    'uuid': 'aaaaaaaa-0001-0001-0001-000000000001',
    'env_id': 'test-env',
    'author': 'Test',
    'data': {
        'ticket_id': 'epic-open-1',
        'title': 'Open Epic Alpha',
        'ticket_type': 'epic',
        'status': 'open',
        'priority': 2,
        'parent_id': None,
        'tags': [],
        'description': '',
        'notes': ''
    }
}
with open('$tracker_dir/epic-open-1/001-CREATE.json', 'w') as f:
    json.dump(event, f)
"

    # ── epic-inprog-1: CREATE then STATUS -> in_progress, priority 1 ──────────
    mkdir -p "$tracker_dir/epic-inprog-1"
    python3 -c "
import json

create_event = {
    'event_type': 'CREATE',
    'ticket_id': 'epic-inprog-1',
    'timestamp': 1700000001000000000,
    'uuid': 'bbbbbbbb-0001-0001-0001-000000000001',
    'env_id': 'test-env',
    'author': 'Test',
    'data': {
        'ticket_id': 'epic-inprog-1',
        'title': 'In Progress Beta',
        'ticket_type': 'epic',
        'status': 'open',
        'priority': 1,
        'parent_id': None,
        'tags': [],
        'description': '',
        'notes': ''
    }
}
with open('$tracker_dir/epic-inprog-1/001-CREATE.json', 'w') as f:
    json.dump(create_event, f)

status_event = {
    'event_type': 'STATUS',
    'ticket_id': 'epic-inprog-1',
    'timestamp': 1700000002000000000,
    'uuid': 'bbbbbbbb-0002-0002-0002-000000000002',
    'env_id': 'test-env',
    'author': 'Test',
    'data': {
        'status': 'in_progress',
        'current_status': 'open'
    }
}
with open('$tracker_dir/epic-inprog-1/002-STATUS.json', 'w') as f:
    json.dump(status_event, f)
"

    # ── epic-blocked-1: open, priority 3, depends_on epic-open-1 ──────────────
    mkdir -p "$tracker_dir/epic-blocked-1"
    python3 -c "
import json

create_event = {
    'event_type': 'CREATE',
    'ticket_id': 'epic-blocked-1',
    'timestamp': 1700000003000000000,
    'uuid': 'cccccccc-0001-0001-0001-000000000001',
    'env_id': 'test-env',
    'author': 'Test',
    'data': {
        'ticket_id': 'epic-blocked-1',
        'title': 'Blocked Epic Gamma',
        'ticket_type': 'epic',
        'status': 'open',
        'priority': 3,
        'parent_id': None,
        'tags': [],
        'description': '',
        'notes': ''
    }
}
with open('$tracker_dir/epic-blocked-1/001-CREATE.json', 'w') as f:
    json.dump(create_event, f)

link_event = {
    'event_type': 'LINK',
    'ticket_id': 'epic-blocked-1',
    'timestamp': 1700000004000000000,
    'uuid': 'cccccccc-0002-0002-0002-000000000002',
    'env_id': 'test-env',
    'author': 'Test',
    'data': {
        'relation': 'depends_on',
        'target_id': 'epic-open-1'
    }
}
with open('$tracker_dir/epic-blocked-1/002-LINK.json', 'w') as f:
    json.dump(link_event, f)
"

    echo "$tracker_dir"
}

# make_empty_tracker — creates a TICKETS_TRACKER_DIR with no tickets (exit-1 case)
make_empty_tracker() {
    local tracker_dir
    tracker_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tracker_dir")
    echo "$tracker_dir"
}

# ── Test 1: Dispatcher exists and is executable ───────────────────────────────
echo "Test 1: Dispatcher exists and is executable"
if [[ -x "$DISPATCHER" ]]; then
    echo "  PASS: dispatcher is executable"
    (( PASS++ ))
else
    echo "  FAIL: $DISPATCHER is not executable or does not exist" >&2
    (( FAIL++ ))
fi

# ── Tests 2-6: Routing and output contract (RED zone) ────────────────────────
test_list_epics_routes_through_dispatcher() {
    local _tracker _output _exit

    # Test 2: 'ticket list-epics' is recognized — verifies dispatcher routing
    # by checking that output does NOT contain "unknown.*subcommand" and exit < 5
    echo "Test 2: 'ticket list-epics' is recognized by the dispatcher (not unknown subcommand)"
    _tracker=$(make_tracker_epics_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" list-epics 2>&1) || _exit=$?

    if [[ "${_output,,}" =~ unknown.*subcommand|unrecognized.*subcommand ]]; then
        echo "  FAIL: dispatcher does not recognize 'list-epics' subcommand (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    elif [[ $_exit -ge 5 ]]; then
        echo "  FAIL: dispatcher returned unexpected exit $_exit (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    else
        echo "  PASS: 'list-epics' recognized by dispatcher (exit $_exit)"
        (( PASS++ ))
    fi

    # Test 3: Output contains tab-separated line with P2 and "Open Epic Alpha"
    echo "Test 3: Output contains P2 priority line for 'Open Epic Alpha'"
    _tracker=$(make_tracker_epics_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" list-epics 2>&1) || _exit=$?

    if [[ "$_output" =~ P2 ]] && [[ "$_output" =~ Open\ Epic\ Alpha ]]; then
        echo "  PASS: output contains 'P2' and 'Open Epic Alpha'"
        (( PASS++ ))
    else
        echo "  FAIL: output missing P2 or 'Open Epic Alpha' (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 4: In-progress epic appears (wrapper lists open + in_progress epics;
    # the bespoke 'P*' prefix was removed with the deprecation).
    echo "Test 4: In-progress epic 'In Progress Beta' appears in list-epics"
    _tracker=$(make_tracker_epics_fixture)
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" list-epics 2>/dev/null) || true

    if [[ "$_output" =~ In\ Progress\ Beta ]]; then
        echo "  PASS: in-progress epic appears in the wrapper output"
        (( PASS++ ))
    else
        echo "  FAIL: in-progress epic missing from list-epics" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 5: --all includes blocked epics; default (unblocked-only) excludes them.
    # (The bespoke 'BLOCKED' inline marker was removed; blocked-awareness is now the
    # generic --unblocked/--blocked filter that --all opts out of.)
    echo "Test 5: --all includes blocked 'Blocked Epic Gamma'; default excludes it"
    _tracker=$(make_tracker_epics_fixture)
    _all=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" list-epics --all 2>/dev/null) || true
    _def=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" list-epics 2>/dev/null) || true

    if [[ "$_all" =~ Blocked\ Epic\ Gamma ]] && [[ ! "$_def" =~ Blocked\ Epic\ Gamma ]]; then
        echo "  PASS: --all includes the blocked epic; default excludes it"
        (( PASS++ ))
    else
        echo "  FAIL: --all/default blocked-epic filtering wrong" >&2
        echo "  all=[$_all] default=[$_def]" >&2
        (( FAIL++ ))
    fi

    # Test 6: Empty tracker -> exit 0 + deprecation warning on stderr (the wrapper is
    # a read composition; the bespoke exit 1/2 codes were removed).
    echo "Test 6: Empty tracker exits 0 with a deprecation warning on stderr"
    _tracker=$(make_empty_tracker)
    _exit=0
    _err=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" list-epics 2>&1 >/dev/null) || _exit=$?

    if [[ $_exit -eq 0 ]] && [[ "$_err" =~ deprecated ]]; then
        echo "  PASS: empty tracker exits 0 with a deprecation warning"
        (( PASS++ ))
    else
        echo "  FAIL: empty tracker exit $_exit / deprecation warning missing" >&2
        echo "  stderr: $_err" >&2
        (( FAIL++ ))
    fi
}

# Run the RED zone tests
test_list_epics_routes_through_dispatcher

# ── Results ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
