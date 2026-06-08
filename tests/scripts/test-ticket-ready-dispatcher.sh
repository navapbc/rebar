#!/usr/bin/env bash
# tests/scripts/test-ticket-ready-dispatcher.sh
# RED integration tests for 'ticket ready' subcommand routing through the dispatcher.
#
# Tests verify that the dispatcher correctly routes 'ticket ready' to a bulk-scan
# implementation script that returns tickets which are ready_to_work:
#   - status is "open" or "in_progress"
#   - all direct blockers (deps with relation=="depends_on") are closed
#
# RED STATE: Tests 2-6 currently fail because the dispatcher does not have a 'ready'
# case. They will pass (GREEN) after the dispatcher case and implementation are added.
#
# RED MARKER:
# tests/scripts/test-ticket-ready-dispatcher.sh [test_ready_routes_through_dispatcher]
#
# Usage: bash tests/scripts/test-ticket-ready-dispatcher.sh
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

echo "=== test-ticket-ready-dispatcher.sh ==="

# ── Fixture helpers ───────────────────────────────────────────────────────────

# make_ready_fixture — builds a tracker dir with:
#   task-a: open, no deps → READY
#   task-b: open, depends_on task-a (task-a is open, not closed) → NOT READY
#   task-c: open, no deps → READY
#   epic-parent: open epic, parent of task-a and task-c (for scope test)
make_ready_fixture() {
    local tracker_dir
    tracker_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tracker_dir")

    # task-a: open, no deps → READY
    mkdir -p "$tracker_dir/task-a"
    python3 -c "
import json
event = {
    'event_type': 'CREATE',
    'ticket_id': 'task-a',
    'uuid': 'create-task-a',
    'timestamp': 1700000000000000000,
    'author': 'test',
    'data': {
        'ticket_type': 'task',
        'title': 'Task A (no blockers)',
        'priority': 2,
        'parent_id': 'epic-parent'
    }
}
json.dump(event, open('$tracker_dir/task-a/001-CREATE.json', 'w'))
"

    # task-b: open, depends_on task-a (task-a open) → NOT READY
    mkdir -p "$tracker_dir/task-b"
    python3 -c "
import json
event = {
    'event_type': 'CREATE',
    'ticket_id': 'task-b',
    'uuid': 'create-task-b',
    'timestamp': 1700000000000000001,
    'author': 'test',
    'data': {
        'ticket_type': 'task',
        'title': 'Task B (blocked by task-a)',
        'priority': 2
    }
}
json.dump(event, open('$tracker_dir/task-b/001-CREATE.json', 'w'))
link = {
    'event_type': 'LINK',
    'ticket_id': 'task-b',
    'uuid': 'link-task-b-task-a',
    'timestamp': 1700000001000000000,
    'author': 'test',
    'data': {
        'source_id': 'task-b',
        'target_id': 'task-a',
        'relation': 'depends_on'
    }
}
json.dump(link, open('$tracker_dir/task-b/002-LINK.json', 'w'))
"

    # task-c: open, no deps → READY
    mkdir -p "$tracker_dir/task-c"
    python3 -c "
import json
event = {
    'event_type': 'CREATE',
    'ticket_id': 'task-c',
    'uuid': 'create-task-c',
    'timestamp': 1700000000000000002,
    'author': 'test',
    'data': {
        'ticket_type': 'task',
        'title': 'Task C (no blockers)',
        'priority': 3,
        'parent_id': 'epic-parent'
    }
}
json.dump(event, open('$tracker_dir/task-c/001-CREATE.json', 'w'))
"

    # epic-parent: open epic, parent of task-a and task-c
    mkdir -p "$tracker_dir/epic-parent"
    python3 -c "
import json
event = {
    'event_type': 'CREATE',
    'ticket_id': 'epic-parent',
    'uuid': 'create-epic-parent',
    'timestamp': 1699999999000000000,
    'author': 'test',
    'data': {
        'ticket_type': 'epic',
        'title': 'Epic Parent',
        'priority': 1
    }
}
json.dump(event, open('$tracker_dir/epic-parent/001-CREATE.json', 'w'))
"

    echo "$tracker_dir"
}

# make_ready_fixture_with_closed_blocker — builds a tracker dir with:
#   task-d: open, depends_on task-e → READY (task-e is closed)
#   task-e: closed → should NOT appear in output (closed ticket)
make_ready_fixture_with_closed_blocker() {
    local tracker_dir
    tracker_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tracker_dir")

    # task-e: starts open, then TRANSITION to closed
    mkdir -p "$tracker_dir/task-e"
    python3 -c "
import json
event = {
    'event_type': 'CREATE',
    'ticket_id': 'task-e',
    'uuid': 'create-task-e',
    'timestamp': 1700000000000000000,
    'author': 'test',
    'data': {
        'ticket_type': 'task',
        'title': 'Task E (will be closed)',
        'priority': 2
    }
}
json.dump(event, open('$tracker_dir/task-e/001-CREATE.json', 'w'))
status = {
    'event_type': 'STATUS',
    'ticket_id': 'task-e',
    'uuid': 'status-task-e-close',
    'timestamp': 1700000001000000000,
    'author': 'test',
    'data': {'status': 'closed'}
}
json.dump(status, open('$tracker_dir/task-e/002-STATUS.json', 'w'))
"

    # task-d: open, depends_on task-e (task-e is closed) → READY
    mkdir -p "$tracker_dir/task-d"
    python3 -c "
import json
event = {
    'event_type': 'CREATE',
    'ticket_id': 'task-d',
    'uuid': 'create-task-d',
    'timestamp': 1700000000000000001,
    'author': 'test',
    'data': {
        'ticket_type': 'task',
        'title': 'Task D (blocker closed)',
        'priority': 2
    }
}
json.dump(event, open('$tracker_dir/task-d/001-CREATE.json', 'w'))
link = {
    'event_type': 'LINK',
    'ticket_id': 'task-d',
    'uuid': 'link-task-d-task-e',
    'timestamp': 1700000001000000001,
    'author': 'test',
    'data': {
        'source_id': 'task-d',
        'target_id': 'task-e',
        'relation': 'depends_on'
    }
}
json.dump(link, open('$tracker_dir/task-d/002-LINK.json', 'w'))
"

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
test_ready_routes_through_dispatcher() {
    local _tracker _output _exit

    # Test 2: 'ticket ready' is recognized — NOT "unknown.*subcommand", exit 0 or small non-zero
    echo "Test 2: 'ticket ready' is recognized by dispatcher (not unknown subcommand)"
    _tracker=$(make_ready_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" ready 2>&1) || _exit=$?

    if [[ "${_output,,}" =~ unknown.*subcommand|unrecognized.*subcommand ]]; then
        echo "  FAIL: dispatcher does not recognize 'ready' subcommand (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    elif [[ $_exit -gt 1 ]]; then
        echo "  FAIL: dispatcher returned unexpected exit $_exit for 'ticket ready' (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    else
        echo "  PASS: 'ready' subcommand recognized (exit $_exit)"
        (( PASS++ ))
    fi

    # Test 3: Output includes task-a and task-c (no blockers), does NOT include task-b (blocked)
    echo "Test 3: Ready tickets (task-a, task-c) listed; blocked ticket (task-b) excluded"
    _tracker=$(make_ready_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" ready 2>&1) || _exit=$?

    local _has_a _has_c _has_b
    _has_a=0; _has_c=0; _has_b=0
    grep -q "task-a" <<< "$_output" && _has_a=1 || true
    grep -q "task-c" <<< "$_output" && _has_c=1 || true
    grep -q "task-b" <<< "$_output" && _has_b=1 || true

    if [[ $_has_a -eq 1 ]] && [[ $_has_c -eq 1 ]] && [[ $_has_b -eq 0 ]]; then
        echo "  PASS: task-a and task-c present; task-b absent (exit $_exit)"
        (( PASS++ ))
    else
        echo "  FAIL: expected task-a=1 task-c=1 task-b=0, got task-a=$_has_a task-c=$_has_c task-b=$_has_b (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 4: Closed tickets (task-e) not listed in ready output
    echo "Test 4: Closed ticket (task-e) not included in ready output"
    _tracker=$(make_ready_fixture_with_closed_blocker)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" ready 2>&1) || _exit=$?

    if grep -q "task-e" <<< "$_output"; then
        echo "  FAIL: closed ticket task-e appears in ready output (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    else
        echo "  PASS: closed ticket task-e correctly excluded from ready output (exit $_exit)"
        (( PASS++ ))
    fi

    # Test 5: --format=llm outputs valid JSONL (each non-empty line is parseable JSON)
    echo "Test 5: --format=llm outputs valid JSONL"
    _tracker=$(make_ready_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" ready --format=llm 2>&1) || _exit=$?

    local _jsonl_valid=1
    if [[ -z "$_output" ]]; then
        echo "  FAIL: --format=llm produced empty output (RED — expected before GREEN)" >&2
        (( FAIL++ ))
        _jsonl_valid=0
    else
        local _invalid_lines
        _invalid_lines=$(echo "$_output" | grep -v '^[[:space:]]*$' | while IFS= read -r line; do
            echo "$line" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null || echo "INVALID: $line"
        done | grep "^INVALID:" || true)

        if [[ -n "$_invalid_lines" ]]; then
            echo "  FAIL: --format=llm output contains non-JSON lines (RED — expected before GREEN)" >&2
            echo "  Invalid lines: $_invalid_lines" >&2
            (( FAIL++ ))
            _jsonl_valid=0
        fi
    fi

    if [[ $_jsonl_valid -eq 1 ]]; then
        echo "  PASS: --format=llm output is valid JSONL (exit $_exit)"
        (( PASS++ ))
    fi

    # Test 6: --epic=epic-parent scopes output to descendants (task-a, task-c); task-b excluded
    echo "Test 6: --epic=epic-parent scopes ready output to epic descendants"
    _tracker=$(make_ready_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" ready --epic=epic-parent 2>&1) || _exit=$?

    local _scope_a _scope_c _scope_b
    _scope_a=0; _scope_c=0; _scope_b=0
    grep -q "task-a" <<< "$_output" && _scope_a=1 || true
    grep -q "task-c" <<< "$_output" && _scope_c=1 || true
    grep -q "task-b" <<< "$_output" && _scope_b=1 || true

    if [[ $_scope_a -eq 1 ]] && [[ $_scope_c -eq 1 ]] && [[ $_scope_b -eq 0 ]]; then
        echo "  PASS: --epic scope returns task-a and task-c; excludes task-b (exit $_exit)"
        (( PASS++ ))
    else
        echo "  FAIL: --epic=epic-parent: expected task-a=1 task-c=1 task-b=0, got task-a=$_scope_a task-c=$_scope_c task-b=$_scope_b (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi
}

# Run the RED zone tests
test_ready_routes_through_dispatcher

# ── Results ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
