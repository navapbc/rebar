#!/usr/bin/env bash
# tests/scripts/test-ticket-quality-check-dispatcher.sh
# RED integration tests for 'ticket quality-check' subcommand routing through the dispatcher.
#
# Tests verify that the dispatcher correctly routes 'ticket quality-check' to
# issue-quality-check.sh and that exit codes and output are passed through.
#
# Uses TICKET_CMD injection because issue-quality-check.sh reads tickets via
# TICKET_CMD (injectable for tests).
#
# RED STATE: Tests currently fail because the dispatcher does not have a 'quality-check'
# case. They will pass (GREEN) after ticket-lib-api.sh ticket_quality_check() and the
# dispatcher case are implemented.
#
# RED MARKER:
# tests/scripts/test-ticket-quality-check-dispatcher.sh [test_quality_check_routes_through_dispatcher]
#
# Usage: bash tests/scripts/test-ticket-quality-check-dispatcher.sh
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

echo "=== test-ticket-quality-check-dispatcher.sh ==="

# ── Fixture helpers ───────────────────────────────────────────────────────────

# make_ticket_mock_quality — creates a mock ticket command for TICKET_CMD injection.
# t1: high-quality ticket with long description, AC section, and keywords
# t2: sparse ticket with minimal description
make_ticket_mock_quality() {
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
            t1) echo '{"ticket_id":"t1","title":"Implement JWT authentication middleware","ticket_type":"task","status":"open","priority":2,"tags":[],"description":"## Description\n\nAdd JWT authentication middleware to the Express API.\n\n## Acceptance Criteria\n- [ ] Middleware validates JWT tokens\n- [ ] Returns 401 for invalid tokens\n- [ ] Tokens expire after 24 hours\n\n## File Impact\n- src/middleware/auth.js (new)\n- tests/middleware/test-auth.js (new)\n\n## Notes\nUse jsonwebtoken library. Must integrate with existing Express middleware chain.","notes":"","deps":[],"comments":[]}' ; exit 0 ;;
            t2) echo '{"ticket_id":"t2","title":"x","ticket_type":"task","status":"open","priority":3,"tags":[],"description":"short","notes":"","deps":[],"comments":[]}' ; exit 0 ;;
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
test_quality_check_routes_through_dispatcher() {
    local _mock _output _exit

    # Test 2: 'ticket quality-check' is recognized (not unknown subcommand)
    echo "Test 2: 'ticket quality-check' routes through dispatcher (not unknown subcommand)"
    _mock=$(make_ticket_mock_quality)
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" quality-check t1 2>&1) || _exit=$?

    if [[ "${_output,,}" =~ unknown.*subcommand|unrecognized.*subcommand ]]; then
        echo "  FAIL: dispatcher does not recognize 'quality-check' subcommand (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    elif [[ $_exit -ge 5 ]]; then
        echo "  FAIL: dispatcher returned crash-level exit code $_exit for 'quality-check'" >&2
        (( FAIL++ ))
    else
        echo "  PASS: 'quality-check' routed correctly through dispatcher (exit $_exit)"
        (( PASS++ ))
    fi

    # Test 3: High-quality ticket exits 0 and outputs QUALITY: pass
    echo "Test 3: High-quality ticket exits 0 and outputs QUALITY: pass"
    _mock=$(make_ticket_mock_quality)
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" quality-check t1 2>/dev/null) || _exit=$?

    if [[ $_exit -eq 0 ]] && [[ "$_output" =~ QUALITY:\ pass ]]; then
        echo "  PASS: high-quality ticket exits 0 with QUALITY: pass"
        (( PASS++ ))
    else
        echo "  FAIL: high-quality ticket did not pass (exit $_exit, output: $_output) (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 4: Sparse ticket exits 1 and outputs QUALITY: fail
    echo "Test 4: Sparse ticket exits 1 and outputs QUALITY: fail"
    _mock=$(make_ticket_mock_quality)
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" quality-check t2 2>/dev/null) || _exit=$?

    if [[ $_exit -eq 1 ]] && [[ "$_output" =~ QUALITY:\ fail ]]; then
        echo "  PASS: sparse ticket exits 1 with QUALITY: fail"
        (( PASS++ ))
    else
        echo "  FAIL: sparse ticket did not fail as expected (exit $_exit, output: $_output) (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 5: Unknown ticket ID exits non-zero
    echo "Test 5: Unknown ticket ID exits non-zero with QUALITY: fail"
    _mock=$(make_ticket_mock_quality)
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" quality-check unknown-id 2>/dev/null) || _exit=$?

    if [[ $_exit -ne 0 ]] && [[ "$_output" =~ QUALITY:\ fail ]]; then
        echo "  PASS: unknown ticket ID exits non-zero with QUALITY: fail"
        (( PASS++ ))
    else
        echo "  FAIL: unknown ticket ID did not produce expected failure (exit $_exit) (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 6: No args — exits non-zero
    echo "Test 6: No ticket ID handled gracefully (exit non-zero)"
    _exit=0
    _output=$("$DISPATCHER" quality-check 2>&1) || _exit=$?

    if [[ $_exit -ne 0 ]]; then
        echo "  PASS: quality-check with no args handled gracefully (exit $_exit)"
        (( PASS++ ))
    else
        echo "  FAIL: quality-check with no args exited 0 (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi
}

# Run the RED zone tests
test_quality_check_routes_through_dispatcher

# ── Results ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
