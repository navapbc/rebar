#!/usr/bin/env bash
# tests/scripts/test-ticket-exists-dispatcher.sh
# RED integration tests for 'ticket exists' subcommand routing through the dispatcher.
#
# Uses TICKETS_TRACKER_DIR injection because ticket-exists.sh checks the tracker
# directory directly (O(1) directory presence check).
#
# RED STATE: Tests currently fail because the dispatcher does not have an 'exists'
# case. They will pass (GREEN) after ticket-lib-api.sh ticket_exists() and the
# dispatcher case are implemented.
#
# RED MARKER:
# tests/scripts/test-ticket-exists-dispatcher.sh [test_exists_routes_through_dispatcher]
#
# Usage: bash tests/scripts/test-ticket-exists-dispatcher.sh
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

echo "=== test-ticket-exists-dispatcher.sh ==="

# ── Fixture helper ────────────────────────────────────────────────────────────

# make_tracker_fixture — creates a minimal TICKETS_TRACKER_DIR for injection.
# Contains:
#   e1/  — ticket with a CREATE event file (exists)
#   arch1/  — ticket with a CREATE event file (exists)
#   (no directory for missing-id — so lookup returns not-found)
make_tracker_fixture() {
    local tracker_dir
    tracker_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tracker_dir")

    # e1 ticket (exists) — CREATE event carries a stored alias so we can also
    # verify alias resolution (bug: 'exists <alias>' must work like show/edit/claim).
    mkdir -p "$tracker_dir/e1"
    python3 -c "import json; json.dump({'event_type':'CREATE','ticket_id':'e1','data':{'alias':'happy-test-alias'}}, open('$tracker_dir/e1/001-CREATE.json','w'))"

    # arch1 ticket (exists)
    mkdir -p "$tracker_dir/arch1"
    python3 -c "import json; json.dump({'event_type':'CREATE','ticket_id':'arch1'}, open('$tracker_dir/arch1/001-CREATE.json','w'))"

    # NOTE: no directory for 'missing-id' — intentionally absent

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

# ── Tests 2-6: Routing and exit code contract (RED zone) ─────────────────────
test_exists_routes_through_dispatcher() {
    local _tracker _output _exit

    # Test 2: 'ticket exists e1' is recognized — verifies dispatcher routing
    # by checking that output does NOT contain "unknown.*subcommand"
    echo "Test 2: 'ticket exists e1' is recognized by dispatcher (no unknown subcommand error)"
    _tracker=$(make_tracker_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" exists e1 2>&1) || _exit=$?

    if [[ "${_output,,}" =~ unknown.*subcommand|unrecognized.*subcommand ]]; then
        echo "  FAIL: dispatcher does not recognize 'exists' subcommand (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    elif [[ $_exit -ge 5 ]]; then
        echo "  FAIL: dispatcher returned exit $_exit >= 5 for 'exists e1' (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    else
        echo "  PASS: 'exists' subcommand recognized (exit $_exit, not an unknown-subcommand error)"
        (( PASS++ ))
    fi

    # Test 3: Known ticket ID (e1 with CREATE file) exits 0
    echo "Test 3: Known ticket ID 'e1' exits 0"
    _tracker=$(make_tracker_fixture)
    _exit=0
    TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" exists e1 2>/dev/null || _exit=$?

    if [[ $_exit -eq 0 ]]; then
        echo "  PASS: 'ticket exists e1' exits 0 (ticket found)"
        (( PASS++ ))
    else
        echo "  FAIL: 'ticket exists e1' exited $_exit (expected 0 — RED, expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 4: Missing ticket ID exits non-zero (ticket not in fixture)
    echo "Test 4: Missing ticket ID 'missing-id' exits non-zero"
    _tracker=$(make_tracker_fixture)
    _exit=0
    TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" exists missing-id 2>/dev/null || _exit=$?

    if [[ $_exit -ge 1 ]]; then
        echo "  PASS: 'ticket exists missing-id' exits non-zero (exit $_exit — ticket not found)"
        (( PASS++ ))
    else
        echo "  FAIL: 'ticket exists missing-id' exited 0 (expected non-zero — RED, expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 5: arch1 ticket (exists in fixture) exits 0
    echo "Test 5: Known ticket ID 'arch1' exits 0"
    _tracker=$(make_tracker_fixture)
    _exit=0
    TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" exists arch1 2>/dev/null || _exit=$?

    if [[ $_exit -eq 0 ]]; then
        echo "  PASS: 'ticket exists arch1' exits 0 (ticket found)"
        (( PASS++ ))
    else
        echo "  FAIL: 'ticket exists arch1' exited $_exit (expected 0 — RED, expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 6: No args exits non-zero
    echo "Test 6: No args exits non-zero"
    _tracker=$(make_tracker_fixture)
    _exit=0
    TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" exists 2>/dev/null || _exit=$?

    if [[ $_exit -ge 1 ]]; then
        echo "  PASS: 'ticket exists' with no args exits non-zero (exit $_exit)"
        (( PASS++ ))
    else
        echo "  FAIL: 'ticket exists' with no args exited 0 (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 7: 'exists <alias>' resolves the alias (regression for bug #7 — exists
    # must accept aliases like show/edit/claim, not just raw ticket ids).
    echo "Test 7: 'ticket exists <alias>' resolves alias and exits 0"
    _tracker=$(make_tracker_fixture)
    _exit=0
    TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" exists happy-test-alias 2>/dev/null || _exit=$?

    if [[ $_exit -eq 0 ]]; then
        echo "  PASS: 'ticket exists happy-test-alias' exits 0 (alias resolved to e1)"
        (( PASS++ ))
    else
        echo "  FAIL: 'ticket exists happy-test-alias' exited $_exit (expected 0 — alias not resolved)" >&2
        (( FAIL++ ))
    fi

    # Test 8: 'exists <bogus-alias>' (no matching ticket) exits non-zero.
    echo "Test 8: 'ticket exists <bogus-alias>' exits non-zero"
    _tracker=$(make_tracker_fixture)
    _exit=0
    TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" exists no-such-alias 2>/dev/null || _exit=$?

    if [[ $_exit -ge 1 ]]; then
        echo "  PASS: 'ticket exists no-such-alias' exits non-zero (exit $_exit — not found)"
        (( PASS++ ))
    else
        echo "  FAIL: 'ticket exists no-such-alias' exited 0 (expected non-zero)" >&2
        (( FAIL++ ))
    fi
}

# Run the RED zone tests
test_exists_routes_through_dispatcher

# ── Results ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
