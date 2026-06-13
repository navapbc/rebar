#!/usr/bin/env bash
# tests/scripts/test-ticket-list-descendants-dispatcher.sh
# RED integration tests for 'ticket list-descendants' subcommand routing through the dispatcher.
#
# Tests verify that the dispatcher correctly routes 'ticket list-descendants' to
# ticket-list-descendants.py/sh and that output matches the expected JSON schema.
#
# RED STATE: Tests 2-7 currently fail because the dispatcher does not have a
# 'list-descendants' case. They will pass (GREEN) after the dispatcher case and
# ticket-list-descendants implementation are added.
#
# RED MARKER:
# tests/scripts/test-ticket-list-descendants-dispatcher.sh [test_list_descendants_routes_through_dispatcher]
#
# Usage: bash tests/scripts/test-ticket-list-descendants-dispatcher.sh
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

echo "=== test-ticket-list-descendants-dispatcher.sh ==="

# ── Fixture helpers ───────────────────────────────────────────────────────────

# make_hierarchy_fixture — creates a .tickets-tracker/ event store with a 5-ticket
# hierarchy for BFS descendant walk testing:
#
#   epic-root        (ticket_type: epic, no parent)
#     story-a        (ticket_type: story, parent: epic-root)
#       task-1       (ticket_type: task, parent: story-a)
#       task-2       (ticket_type: task, parent: story-a)
#     story-b        (ticket_type: story, parent: epic-root)
#       bug-1        (ticket_type: bug, parent: story-b)
# Performance + isolation notes (flakiness mitigation):
#   - mktemp -d (no path prefix) honors $TMPDIR, which suite-engine.sh sets
#     per-test for isolation. Matches the established pattern used by ~300
#     other tests in tests/scripts/.
#   - All six fixture tickets are built in ONE python3 invocation (was 6).
#     Python cold-start is ~50–100ms per invocation; consolidating saves
#     ~300–600ms per fixture build. With four hierarchy builds per test run,
#     that compounds to ~1–2s saved — material under parallel CI load.
make_hierarchy_fixture() {
    local tracker_dir
    tracker_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tracker_dir")

    mkdir -p "$tracker_dir/epic-root" "$tracker_dir/story-a" "$tracker_dir/task-1" \
        "$tracker_dir/task-2" "$tracker_dir/story-b" "$tracker_dir/bug-1"

    TRACKER_DIR="$tracker_dir" python3 - <<'PYEOF'
import json, os
tracker = os.environ['TRACKER_DIR']
tickets = [
    ('epic-root', 1000, 'epic',  'Root Epic', None),
    ('story-a',   1001, 'story', 'Story A',  'epic-root'),
    ('task-1',    1002, 'task',  'Task 1',   'story-a'),
    ('task-2',    1003, 'task',  'Task 2',   'story-a'),
    ('story-b',   1004, 'story', 'Story B',  'epic-root'),
    ('bug-1',     1005, 'bug',   'Bug 1',    'story-b'),
]
for ticket_id, ts, ttype, title, parent_id in tickets:
    event = {
        'event_type': 'CREATE',
        'ticket_id': ticket_id,
        'timestamp': ts,
        'author': 'test',
        'data': {
            'ticket_type': ttype,
            'title': title,
            'parent_id': parent_id,
        },
    }
    path = os.path.join(tracker, ticket_id, '001-CREATE.json')
    with open(path, 'w') as f:
        json.dump(event, f)
PYEOF

    echo "$tracker_dir"
}

# make_single_ticket_fixture — creates a .tickets-tracker/ with a single ticket
# that has no children. Used to verify graceful empty-result handling.
make_single_ticket_fixture() {
    local tracker_dir
    tracker_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tracker_dir")

    mkdir -p "$tracker_dir/solo-epic"
    python3 -c "
import json
event = {
    'event_type': 'CREATE',
    'ticket_id': 'solo-epic',
    'timestamp': 2000,
    'author': 'test',
    'data': {
        'ticket_type': 'epic',
        'title': 'Solo Epic',
        'parent_id': None
    }
}
json.dump(event, open('$tracker_dir/solo-epic/001-CREATE.json', 'w'))
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

# ── Tests 2-7: Routing and output contract (RED zone) ────────────────────────
test_list_descendants_routes_through_dispatcher() {
    local _tracker _output _exit

    # Test 2: 'ticket list-descendants epic-root' is recognized by the dispatcher
    # (NOT an "unknown subcommand" error) and exits with code < 5
    echo "Test 2: 'ticket list-descendants epic-root' recognized (not unknown subcommand, exit < 5)"
    _tracker=$(make_hierarchy_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" list-descendants epic-root 2>&1) || _exit=$?

    if [[ "${_output,,}" =~ unknown.*subcommand|unrecognized.*subcommand ]]; then
        echo "  FAIL: dispatcher does not recognize 'list-descendants' subcommand (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    elif [[ $_exit -ge 5 ]]; then
        echo "  FAIL: dispatcher returned exit $_exit (>= 5) for valid list-descendants call (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    else
        echo "  PASS: 'list-descendants' recognized by dispatcher (exit $_exit)"
        (( PASS++ ))
    fi

    # Test 3: Output is valid JSON with required top-level keys
    echo "Test 3: Output is valid JSON with keys: epics, stories, tasks, bugs, parents_with_children"
    _tracker=$(make_hierarchy_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" list-descendants epic-root 2>&1) || _exit=$?

    if printf '%s' "$_output" | jq -e 'has("epics") and has("stories") and has("tasks") and has("bugs") and has("parents_with_children")' >/dev/null 2>&1; then
        echo "  PASS: output is valid JSON with all required keys"
        (( PASS++ ))
    else
        echo "  FAIL: output is not valid JSON or missing required keys (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 4: Descendant arrays contain the correct ticket IDs
    # stories: story-a, story-b; tasks: task-1, task-2; bugs: bug-1
    echo "Test 4: Descendant arrays contain correct ticket IDs (stories, tasks, bugs)"
    _tracker=$(make_hierarchy_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" list-descendants epic-root 2>&1) || _exit=$?

    if printf '%s' "$_output" | jq -e '(.stories | index("story-a")) and (.stories | index("story-b")) and (.tasks | index("task-1")) and (.tasks | index("task-2")) and (.bugs | index("bug-1"))' >/dev/null 2>&1; then
        echo "  PASS: stories, tasks, and bugs arrays contain expected descendant IDs"
        (( PASS++ ))
    else
        echo "  FAIL: descendant arrays missing expected IDs (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 5: parents_with_children includes story-a (which has task-1 and task-2)
    echo "Test 5: parents_with_children includes story-a (has child tasks)"
    _tracker=$(make_hierarchy_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" list-descendants epic-root 2>&1) || _exit=$?

    if printf '%s' "$_output" | jq -e '.parents_with_children | index("story-a")' >/dev/null 2>&1; then
        echo "  PASS: parents_with_children includes story-a"
        (( PASS++ ))
    else
        echo "  FAIL: parents_with_children does not include story-a (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 6: No args (missing root ID) exits non-zero
    echo "Test 6: No args exits non-zero"
    _exit=0
    # Sandbox the tracker: the no-args arm still runs _ensure_initialized, which
    # would auto-init .tickets-tracker into the checkout (REPO_ROOT leak in CI).
    # A set TICKETS_TRACKER_DIR makes the dispatcher skip auto-init; the arity
    # error fires before the tracker is touched, so output/exit are unchanged.
    _output=$(TICKETS_TRACKER_DIR="$(mktemp -d)" "$DISPATCHER" list-descendants 2>&1) || _exit=$?

    if [[ $_exit -ne 0 ]]; then
        echo "  PASS: list-descendants with no args exits non-zero (exit $_exit)"
        (( PASS++ ))
    else
        echo "  FAIL: list-descendants with no args exited 0 (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 7: Unknown root ID on a fixture that has no matching ticket produces
    # valid JSON with all-empty arrays (graceful empty result, exit 0)
    echo "Test 7: Unknown root ID returns valid JSON with empty arrays (graceful, exit 0)"
    _tracker=$(make_single_ticket_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" list-descendants unknown-id 2>&1) || _exit=$?

    if printf '%s' "$_output" | jq -e '(.epics == []) and (.stories == []) and (.tasks == []) and (.bugs == []) and (.parents_with_children == [])' >/dev/null 2>&1 && [[ $_exit -eq 0 ]]; then
        echo "  PASS: unknown root ID returns empty JSON arrays with exit 0"
        (( PASS++ ))
    else
        echo "  FAIL: unknown root ID did not return empty JSON arrays with exit 0 (RED — expected before GREEN)" >&2
        echo "  Exit: $_exit  Output: $_output" >&2
        (( FAIL++ ))
    fi
}

# Run the RED zone tests
test_list_descendants_routes_through_dispatcher

# ── Results ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
