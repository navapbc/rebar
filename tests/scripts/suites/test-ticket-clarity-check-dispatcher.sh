#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-clarity-check-dispatcher.sh
# RED integration tests for 'ticket clarity-check' subcommand routing through the dispatcher.
#
# Tests verify that the dispatcher correctly routes 'ticket clarity-check' to
# ticket-clarity-check.sh and that exit codes and JSON output are passed through.
#
# Uses --stdin mode because ticket-clarity-check.sh fetches tickets via REBAR_CLI
# (not TICKET_CMD), so stub injection is not available for the ticket-id path.
# --stdin tests the full dispatcher→ticket_clarity_check()→ticket-clarity-check.sh chain.
#
# RED STATE: Tests currently fail because the dispatcher does not have a 'clarity-check'
# case. They will pass (GREEN) after ticket-lib-api.sh ticket_clarity_check() and the
# dispatcher case are implemented.
#
# RED MARKER:
# tests/scripts/suites/test-ticket-clarity-check-dispatcher.sh [test_clarity_check_routes_through_dispatcher]
#
# Usage: bash tests/scripts/suites/test-ticket-clarity-check-dispatcher.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e intentionally omitted — test assertions return non-zero by design;
# -e would abort the script on the first failing test instead of collecting all results.
# All test files in this suite use the same sourced-library initialization pattern.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DISPATCHER="$PLUGIN_ROOT/src/rebar/_engine/ticket"

source "$SCRIPT_DIR/../lib/run_test.sh"

echo "=== test-ticket-clarity-check-dispatcher.sh ==="

# ── Ticket JSON fixtures ──────────────────────────────────────────────────────
# High-quality ticket: long description, structured sections, acceptance criteria
_TICKET_PASS='{"ticket_id":"t1","title":"Implement authentication middleware for API","ticket_type":"task","status":"open","priority":2,"tags":[],"description":"## Description\n\nAdd JWT authentication middleware to the Express API gateway.\n\n## Acceptance Criteria\n- [ ] Middleware validates JWT tokens on all protected routes\n- [ ] Returns 401 for missing or invalid tokens\n- [ ] Tokens expire after 24 hours with configurable TTL\n- [ ] Refresh token endpoint implemented and tested\n- [ ] Rate limiting applied to auth endpoints\n\n## File Impact\n- src/middleware/auth.js (new)\n- src/routes/auth.js (modify)\n- tests/middleware/test-auth.js (new)\n- config/auth.yaml (new)","notes":"","deps":[]}'

# Low-quality ticket: minimal description, no structure
_TICKET_FAIL='{"ticket_id":"t2","title":"x","ticket_type":"task","status":"open","priority":3,"tags":[],"description":"short","notes":"","deps":[]}'

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
test_clarity_check_routes_through_dispatcher() {
    local _output _exit _valid

    # Test 2: 'ticket clarity-check' is recognized (not unknown subcommand)
    echo "Test 2: 'ticket clarity-check --stdin' routes through dispatcher (not unknown subcommand)"
    _exit=0
    _output=$(echo "$_TICKET_PASS" | "$DISPATCHER" clarity-check --stdin 2>&1) || _exit=$?

    if [[ "${_output,,}" =~ unknown.*subcommand|unrecognized.*subcommand ]]; then
        echo "  FAIL: dispatcher does not recognize 'clarity-check' subcommand (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    elif [[ $_exit -ge 5 ]]; then
        echo "  FAIL: dispatcher returned crash-level exit code $_exit for 'clarity-check'" >&2
        (( FAIL++ ))
    else
        echo "  PASS: 'clarity-check' routed correctly through dispatcher (exit $_exit)"
        (( PASS++ ))
    fi

    # Test 3: Well-formed ticket produces valid JSON output with required fields
    echo "Test 3: Well-formed ticket produces valid JSON output"
    _exit=0
    _output=$(echo "$_TICKET_PASS" | "$DISPATCHER" clarity-check --stdin 2>/dev/null) || _exit=$?
    _valid=0
    python3 -c "
import json, sys
data = json.loads(sys.argv[1])
assert 'score' in data, 'missing score field'
assert 'verdict' in data, 'missing verdict field'
assert 'threshold' in data, 'missing threshold field'
assert data['verdict'] in ('pass', 'fail'), f'invalid verdict: {data[\"verdict\"]}'
" "$_output" 2>/dev/null || _valid=$?

    if [[ $_valid -eq 0 ]]; then
        echo "  PASS: well-formed ticket produces valid JSON with score/verdict/threshold"
        (( PASS++ ))
    else
        echo "  FAIL: JSON output missing required fields (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 4: High-quality ticket exits 0
    echo "Test 4: High-quality ticket exits 0 (pass)"
    _exit=0
    echo "$_TICKET_PASS" | "$DISPATCHER" clarity-check --stdin 2>/dev/null || _exit=$?

    if [[ $_exit -eq 0 ]]; then
        echo "  PASS: high-quality ticket exits 0"
        (( PASS++ ))
    elif [[ $_exit -eq 1 ]]; then
        echo "  FAIL: high-quality ticket exits 1 (fail verdict) (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    else
        echo "  FAIL: unexpected exit code $_exit (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 5: Low-quality ticket exits 1
    echo "Test 5: Low-quality ticket exits 1 (fail)"
    _exit=0
    echo "$_TICKET_FAIL" | "$DISPATCHER" clarity-check --stdin 2>/dev/null || _exit=$?

    if [[ $_exit -eq 1 ]]; then
        echo "  PASS: low-quality ticket exits 1"
        (( PASS++ ))
    elif [[ $_exit -eq 0 ]]; then
        echo "  FAIL: low-quality ticket exits 0 (pass verdict) — threshold not enforced (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    else
        echo "  FAIL: unexpected exit code $_exit (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 6: No args — exits non-zero
    echo "Test 6: No ticket ID or --stdin handled gracefully (exit non-zero)"
    _exit=0
    _output=$("$DISPATCHER" clarity-check 2>&1) || _exit=$?

    if [[ $_exit -ne 0 ]]; then
        echo "  PASS: clarity-check with no args handled gracefully (exit $_exit)"
        (( PASS++ ))
    else
        echo "  FAIL: clarity-check with no args exited 0 (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi
}

# Run the RED zone tests
test_clarity_check_routes_through_dispatcher

# ── Results ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
