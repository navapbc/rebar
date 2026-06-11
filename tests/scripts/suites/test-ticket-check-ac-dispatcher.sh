#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-check-ac-dispatcher.sh
# RED integration tests for 'ticket check-ac' subcommand routing through the dispatcher.
#
# Tests verify that the dispatcher correctly routes 'ticket check-ac' to
# check-acceptance-criteria.sh and that exit codes and output are passed through.
#
# Uses TICKET_CMD injection because check-acceptance-criteria.sh reads tickets
# via TICKET_CMD (injectable for tests), unlike ticket-clarity-check.sh.
#
# RED STATE: Tests currently fail because the dispatcher does not have a 'check-ac'
# case. They will pass (GREEN) after ticket-lib-api.sh ticket_check_ac() and the
# dispatcher case are implemented.
#
# RED MARKER:
# tests/scripts/suites/test-ticket-check-ac-dispatcher.sh [test_check_ac_routes_through_dispatcher]
#
# Usage: bash tests/scripts/suites/test-ticket-check-ac-dispatcher.sh
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

echo "=== test-ticket-check-ac-dispatcher.sh ==="

# ── Fixture helpers ───────────────────────────────────────────────────────────

# make_ticket_mock_with_ac — creates a mock ticket command for TICKET_CMD injection.
# t1: has a proper ## Acceptance Criteria section with checklist items (AC_CHECK: pass)
# t2: has no Acceptance Criteria section (AC_CHECK: fail)
make_ticket_mock_with_ac() {
    local mock_dir
    mock_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$mock_dir")
    local mock_script="$mock_dir/ticket"
    cat > "$mock_script" << 'MOCK_EOF'
#!/usr/bin/env bash
SUBCMD="${1:-}"
TICKET_ID="${2:-}"
case "$SUBCMD" in
    show)
        case "$TICKET_ID" in
            t1) echo '{"ticket_id":"t1","title":"Implement login","ticket_type":"task","status":"open","priority":2,"tags":[],"description":"## Acceptance Criteria\n- [ ] User can log in\n- [ ] Session persists 24h","notes":"","deps":[],"comments":[]}' ; exit 0 ;;
            t2) echo '{"ticket_id":"t2","title":"Short task","ticket_type":"task","status":"open","priority":3,"tags":[],"description":"Do something","notes":"","deps":[],"comments":[]}' ; exit 0 ;;
            *) exit 1 ;;
        esac ;;
    *) exit 0 ;;
esac
MOCK_EOF
    chmod +x "$mock_script"
    echo "$mock_script"
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
test_check_ac_routes_through_dispatcher() {
    local _mock _output _exit

    # Test 2: 'ticket check-ac' is recognized (not unknown subcommand)
    echo "Test 2: 'ticket check-ac' routes through dispatcher (not unknown subcommand)"
    _mock=$(make_ticket_mock_with_ac)
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" check-ac t1 2>&1) || _exit=$?

    if [[ "${_output,,}" =~ unknown.*subcommand|unrecognized.*subcommand ]]; then
        echo "  FAIL: dispatcher does not recognize 'check-ac' subcommand (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    elif [[ $_exit -ge 5 ]]; then
        echo "  FAIL: dispatcher returned crash-level exit code $_exit for 'check-ac'" >&2
        (( FAIL++ ))
    else
        echo "  PASS: 'check-ac' routed correctly through dispatcher (exit $_exit)"
        (( PASS++ ))
    fi

    # Test 3: Ticket with AC section exits 0 and outputs AC_CHECK: pass
    echo "Test 3: Ticket with AC section exits 0 and outputs AC_CHECK: pass"
    _mock=$(make_ticket_mock_with_ac)
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" check-ac t1 2>/dev/null) || _exit=$?

    if [[ $_exit -eq 0 ]] && [[ "$_output" =~ AC_CHECK:\ pass ]]; then
        echo "  PASS: ticket with AC section exits 0 with AC_CHECK: pass"
        (( PASS++ ))
    else
        echo "  FAIL: ticket with AC section did not pass (exit $_exit, output: $_output) (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 4: Ticket without AC section exits 1 and outputs AC_CHECK: fail
    echo "Test 4: Ticket without AC section exits 1 and outputs AC_CHECK: fail"
    _mock=$(make_ticket_mock_with_ac)
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" check-ac t2 2>/dev/null) || _exit=$?

    if [[ $_exit -eq 1 ]] && [[ "$_output" =~ AC_CHECK:\ fail ]]; then
        echo "  PASS: ticket without AC section exits 1 with AC_CHECK: fail"
        (( PASS++ ))
    else
        echo "  FAIL: ticket without AC section did not fail as expected (exit $_exit, output: $_output) (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 5: Unknown ticket ID exits non-zero
    echo "Test 5: Unknown ticket ID exits non-zero with AC_CHECK: fail"
    _mock=$(make_ticket_mock_with_ac)
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" check-ac unknown-id 2>/dev/null) || _exit=$?

    if [[ $_exit -ne 0 ]] && [[ "$_output" =~ AC_CHECK:\ fail ]]; then
        echo "  PASS: unknown ticket ID exits non-zero with AC_CHECK: fail"
        (( PASS++ ))
    else
        echo "  FAIL: unknown ticket ID did not produce expected failure (exit $_exit) (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 6: No args — exits non-zero
    echo "Test 6: No ticket ID handled gracefully (exit non-zero)"
    _exit=0
    _output=$("$DISPATCHER" check-ac 2>&1) || _exit=$?

    if [[ $_exit -ne 0 ]]; then
        echo "  PASS: check-ac with no args handled gracefully (exit $_exit)"
        (( PASS++ ))
    else
        echo "  FAIL: check-ac with no args exited 0 (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi
}

# Run the RED zone tests
test_check_ac_routes_through_dispatcher

# ── Results ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
