#!/usr/bin/env bash
# tests/scripts/test-ticket-purge-bridge-dispatcher.sh
# RED integration tests for 'ticket purge-bridge' subcommand routing through the dispatcher.
#
# Tests verify that the dispatcher correctly routes 'ticket purge-bridge' to
# purge-non-project-tickets.sh and that exit codes and output are passed through.
#
# Uses TICKETS_TRACKER_DIR injection because purge-non-project-tickets.sh reads
# the tracker directory via TICKETS_TRACKER_DIR (not TICKET_CMD).
#
# RED STATE: Tests currently fail because the dispatcher does not have a 'purge-bridge'
# case. They will pass (GREEN) after the dispatcher case is implemented.
#
# RED MARKER:
# tests/scripts/test-ticket-purge-bridge-dispatcher.sh [test_purge_bridge_routes_through_dispatcher]
#
# Usage: bash tests/scripts/test-ticket-purge-bridge-dispatcher.sh
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

echo "=== test-ticket-purge-bridge-dispatcher.sh ==="

# ── Fixture helper ────────────────────────────────────────────────────────────

# make_tracker_dir — creates a minimal TICKETS_TRACKER_DIR for injection.
# Contains two jira-* directories (DSO project and OTHER project) and one
# non-jira ticket directory that should never be deleted.
make_tracker_dir() {
    local tracker_dir
    tracker_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tracker_dir")

    # DSO project ticket (should be kept when --keep=DSO)
    mkdir -p "$tracker_dir/jira-dso-1"
    python3 -c "import json; json.dump({'event':'CREATE','ticket_id':'jira-dso-1','data':{'jira_key':'DSO-1'}}, open('$tracker_dir/jira-dso-1/001-CREATE.json','w'))"

    # OTHER project ticket (should be purged when --keep=DSO)
    mkdir -p "$tracker_dir/jira-other-2"
    python3 -c "import json; json.dump({'event':'CREATE','ticket_id':'jira-other-2','data':{'jira_key':'OTHER-2'}}, open('$tracker_dir/jira-other-2/001-CREATE.json','w'))"

    # Non-jira ticket (never touched by purge)
    mkdir -p "$tracker_dir/dso-abc-1234"

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
test_purge_bridge_routes_through_dispatcher() {
    local _tracker _output _exit

    # Test 2: 'ticket purge-bridge' is recognized — verifies dispatcher routing
    # by checking for output unique to purge-non-project-tickets.sh ("Scanning for non-")
    echo "Test 2: 'ticket purge-bridge' routes through dispatcher to purge-non-project-tickets.sh"
    _tracker=$(make_tracker_dir)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" purge-bridge --keep=DSO --dry-run 2>&1) || _exit=$?

    if [[ "${_output,,}" =~ unknown.*subcommand|unrecognized.*subcommand ]]; then
        echo "  FAIL: dispatcher does not recognize 'purge-bridge' subcommand (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    elif [[ $_exit -ne 0 ]]; then
        echo "  FAIL: dispatcher returned non-zero exit $_exit for valid purge-bridge --dry-run (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    elif [[ ! "$_output" =~ Scanning\ for\ non- ]]; then
        echo "  FAIL: output missing 'Scanning for non-' — purge-non-project-tickets.sh was not invoked (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    else
        echo "  PASS: 'purge-bridge' routed to purge-non-project-tickets.sh (exit $_exit, output confirmed)"
        (( PASS++ ))
    fi

    # Test 3: --dry-run shows DRY RUN output and does not delete files
    echo "Test 3: --dry-run shows DRY RUN output and does not delete"
    _tracker=$(make_tracker_dir)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" purge-bridge --keep=DSO --dry-run 2>&1) || _exit=$?

    if [[ $_exit -eq 0 ]] && [[ "$_output" =~ DRY\ RUN ]] && [[ "$_output" =~ Scanning\ for\ non- ]] && [[ -d "$_tracker/jira-other-2" ]]; then
        echo "  PASS: --dry-run shows DRY RUN output and jira-other-2 still exists"
        (( PASS++ ))
    else
        echo "  FAIL: --dry-run did not behave as expected (exit $_exit, jira-other-2 exists: $([ -d "$_tracker/jira-other-2" ] && echo yes || echo no)) (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 4: Missing --keep exits non-zero with error
    echo "Test 4: Missing --keep flag exits non-zero"
    _tracker=$(make_tracker_dir)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" purge-bridge 2>&1) || _exit=$?

    if [[ $_exit -ne 0 ]]; then
        echo "  PASS: purge-bridge with missing --keep exits non-zero (exit $_exit)"
        (( PASS++ ))
    else
        echo "  FAIL: purge-bridge with missing --keep exited 0 (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 5: Non-jira tickets are not deleted
    echo "Test 5: Non-jira tickets are never deleted"
    _tracker=$(make_tracker_dir)
    _exit=0
    TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" purge-bridge --keep=DSO 2>/dev/null || _exit=$?

    if [[ -d "$_tracker/dso-abc-1234" ]]; then
        echo "  PASS: non-jira ticket dso-abc-1234 was not deleted"
        (( PASS++ ))
    else
        echo "  FAIL: non-jira ticket was deleted — safety violation (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 6: No args — exits non-zero
    echo "Test 6: No args handled gracefully (exit non-zero)"
    _exit=0
    # Sandbox the tracker: the no-args arm still runs _ensure_initialized, which
    # would auto-init .tickets-tracker into the checkout (REPO_ROOT leak in CI).
    # A set TICKETS_TRACKER_DIR skips auto-init; the arity error fires before the
    # tracker is touched, so output/exit are unchanged.
    _output=$(TICKETS_TRACKER_DIR="$(mktemp -d)" "$DISPATCHER" purge-bridge 2>&1) || _exit=$?

    if [[ $_exit -ne 0 ]]; then
        echo "  PASS: purge-bridge with no args handled gracefully (exit $_exit)"
        (( PASS++ ))
    else
        echo "  FAIL: purge-bridge with no args exited 0 (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi
}

# Run the RED zone tests
test_purge_bridge_routes_through_dispatcher

# ── Results ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
