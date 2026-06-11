#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-summary-dispatcher.sh
# RED integration tests for 'ticket summary' subcommand routing through the dispatcher.
#
# Tests verify that the dispatcher correctly routes 'ticket summary' to
# issue-summary.sh and that exit codes and output are passed through.
#
# Uses TICKET_CMD injection because issue-summary.sh reads tickets via
# TICKET_CMD (injectable for tests).
#
# RED STATE: Tests currently fail because the dispatcher does not have a 'summary'
# case. They will pass (GREEN) after ticket-lib-api.sh ticket_summary() and the
# dispatcher case are implemented.
#
# RED MARKER:
# tests/scripts/suites/test-ticket-summary-dispatcher.sh [test_summary_routes_through_dispatcher]
#
# Usage: bash tests/scripts/suites/test-ticket-summary-dispatcher.sh
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

echo "=== test-ticket-summary-dispatcher.sh ==="

# ── Fixture helpers ───────────────────────────────────────────────────────────

# make_ticket_mock_summary — creates a mock ticket command for TICKET_CMD injection.
# t1: open ticket with no blockers (ready)
# t2: in_progress ticket (ready)
make_ticket_mock_summary() {
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
            t1) echo '{"ticket_id":"t1","title":"Implement login","ticket_type":"task","status":"open","priority":2,"tags":[],"description":"Add login flow","notes":"","deps":[],"comments":[]}' ; exit 0 ;;
            t2) echo '{"ticket_id":"t2","title":"Write unit tests","ticket_type":"task","status":"in_progress","priority":3,"tags":[],"description":"Unit tests for parser","notes":"","deps":[],"comments":[]}' ; exit 0 ;;
            *) exit 1 ;;
        esac ;;
    *) exit 0 ;;
esac
MOCK_EOF
    chmod +x "$mock_script"
    echo "$mock_script"
}

# make_ticket_mock_with_deps — like make_ticket_mock_summary but also implements
# the `deps <id>` subcommand (GAP-8). t1 is blocked (non-empty blockers,
# ready_to_work=false); t2 is ready (empty blockers, ready_to_work=true).
make_ticket_mock_with_deps() {
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
            t1) echo '{"ticket_id":"t1","title":"Implement login","ticket_type":"task","status":"open","priority":2,"tags":[],"description":"Add login flow","notes":"","deps":[],"comments":[]}' ; exit 0 ;;
            t2) echo '{"ticket_id":"t2","title":"Write unit tests","ticket_type":"task","status":"in_progress","priority":3,"tags":[],"description":"Unit tests for parser","notes":"","deps":[],"comments":[]}' ; exit 0 ;;
            *) exit 1 ;;
        esac ;;
    deps)
        case "$TICKET_ID" in
            # t1 is blocked by t9: non-empty blockers + ready_to_work=false.
            t1) echo '{"ticket_id":"t1","blockers":["t9"],"ready_to_work":false}' ; exit 0 ;;
            # t2 has no blockers and is ready to work.
            t2) echo '{"ticket_id":"t2","blockers":[],"ready_to_work":true}' ; exit 0 ;;
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
test_summary_routes_through_dispatcher() {
    local _mock _output _exit

    # Test 2: 'ticket summary' is recognized (not unknown subcommand)
    echo "Test 2: 'ticket summary' routes through dispatcher (not unknown subcommand)"
    _mock=$(make_ticket_mock_summary)
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" summary t1 2>&1) || _exit=$?

    if [[ "${_output,,}" =~ unknown.*subcommand|unrecognized.*subcommand ]]; then
        echo "  FAIL: dispatcher does not recognize 'summary' subcommand (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    elif [[ $_exit -ge 5 ]]; then
        echo "  FAIL: dispatcher returned crash-level exit code $_exit for 'summary'" >&2
        (( FAIL++ ))
    else
        echo "  PASS: 'summary' routed correctly through dispatcher (exit $_exit)"
        (( PASS++ ))
    fi

    # Test 3: Single ID produces one-line output with ticket ID and status
    echo "Test 3: Single ticket ID produces one-line output with ID and status"
    _mock=$(make_ticket_mock_summary)
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" summary t1 2>/dev/null) || _exit=$?

    if [[ $_exit -eq 0 ]] && [[ "$_output" =~ t1 ]] && [[ "$(echo "$_output" | wc -l | tr -d ' ')" -eq 1 ]]; then
        echo "  PASS: single ID produces one-line output containing ticket ID"
        (( PASS++ ))
    else
        echo "  FAIL: single ID did not produce expected one-line output (exit $_exit, lines: $(echo "$_output" | wc -l | tr -d ' ')) (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 4: Multiple IDs produce one line per ticket
    echo "Test 4: Multiple ticket IDs produce one line per ticket"
    _mock=$(make_ticket_mock_summary)
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" summary t1 t2 2>/dev/null) || _exit=$?
    local _lines
    _lines=$(echo "$_output" | wc -l | tr -d ' ')

    if [[ $_exit -eq 0 ]] && [[ "$_output" =~ t1 ]] && [[ "$_output" =~ t2 ]] && [[ "$_lines" -eq 2 ]]; then
        echo "  PASS: two IDs produced two-line output"
        (( PASS++ ))
    else
        echo "  FAIL: two IDs did not produce two-line output (exit $_exit, lines: $_lines) (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 5: Unknown ticket ID produces fallback output (not crash)
    echo "Test 5: Unknown ticket ID produces fallback output (not crash)"
    _mock=$(make_ticket_mock_summary)
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" summary unknown-id 2>/dev/null) || _exit=$?

    if [[ $_exit -eq 0 ]] && [[ "$_output" =~ unknown-id ]]; then
        echo "  PASS: unknown ticket ID produces fallback output with ID"
        (( PASS++ ))
    else
        echo "  FAIL: unknown ticket ID did not produce expected fallback (exit $_exit) (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi

    # Test 6: No args — exits non-zero
    echo "Test 6: No ticket ID handled gracefully (exit non-zero)"
    _exit=0
    _output=$("$DISPATCHER" summary 2>&1) || _exit=$?

    if [[ $_exit -ne 0 ]]; then
        echo "  PASS: summary with no args handled gracefully (exit $_exit)"
        (( PASS++ ))
    else
        echo "  FAIL: summary with no args exited 0 (RED — expected before GREEN)" >&2
        (( FAIL++ ))
    fi
}

# ── Test 7: status + title are parsed from JSON show output (bug aa2e-3dcd) ──
# The original parser scraped a long-retired human-readable `show` layout, so
# against the current JSON output every ticket rendered as "[unknown] {". Assert
# the real status and title surface so that regression can't return silently.
test_summary_parses_status_and_title() {
    local _mock _output _exit
    echo "Test 7: summary renders parsed status and title (not [unknown] / '{')"
    _mock=$(make_ticket_mock_summary)
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" summary t1 2>/dev/null) || _exit=$?

    if [[ $_exit -eq 0 ]] \
        && [[ "$_output" == *"[open]"* ]] \
        && [[ "$_output" == *"Implement login"* ]] \
        && [[ "$_output" != *"[unknown]"* ]] \
        && [[ "$_output" != *" { "* ]]; then
        echo "  PASS: status [open] and title 'Implement login' rendered"
        (( PASS++ ))
    else
        echo "  FAIL: expected '[open]' and 'Implement login', got: $_output" >&2
        (( FAIL++ ))
    fi
}

# ── GAP-8: summary renders 'blocked by: <id>' from deps; ready tickets '(ready)' ─
# issue-summary.sh also calls `<ticket> deps <id>` and, when deps returns
# non-empty blockers with ready_to_work=false, renders a 'blocked by: <ids>'
# suffix. A ready ticket (empty blockers / ready_to_work=true) still renders
# '(ready)'. The base mock only implements `show`; here we use a mock that also
# implements `deps`.
test_summary_renders_blocked_by_from_deps() {
    local _mock _output _exit
    echo "GAP-8: summary renders 'blocked by: <id>' for a blocked ticket and '(ready)' for a ready one"
    _mock=$(make_ticket_mock_with_deps)

    # Blocked ticket t1 — must show 'blocked by: t9', not '(ready)'.
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" summary t1 2>/dev/null) || _exit=$?
    if [[ $_exit -eq 0 ]] \
        && [[ "$_output" == *"blocked by: t9"* ]] \
        && [[ "$_output" != *"(ready)"* ]]; then
        echo "  PASS: blocked ticket renders 'blocked by: t9' suffix"
        (( PASS++ ))
    else
        echo "  FAIL: expected 'blocked by: t9' (not '(ready)'), got: $_output" >&2
        (( FAIL++ ))
    fi

    # Ready ticket t2 — must still render '(ready)'.
    _exit=0
    _output=$(TICKET_CMD="$_mock" "$DISPATCHER" summary t2 2>/dev/null) || _exit=$?
    if [[ $_exit -eq 0 ]] \
        && [[ "$_output" == *"(ready)"* ]] \
        && [[ "$_output" != *"blocked by"* ]]; then
        echo "  PASS: ready ticket still renders '(ready)' suffix"
        (( PASS++ ))
    else
        echo "  FAIL: expected '(ready)' (no 'blocked by'), got: $_output" >&2
        (( FAIL++ ))
    fi
}

# Run the RED zone tests
test_summary_routes_through_dispatcher
test_summary_parses_status_and_title
test_summary_renders_blocked_by_from_deps

# ── Results ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
