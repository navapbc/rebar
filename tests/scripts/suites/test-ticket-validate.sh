#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-validate.sh
# RED integration tests for 'ticket validate' subcommand routing through the dispatcher.
#
# Tests verify that the dispatcher correctly routes 'ticket validate' to validate-issues.sh
# and passes through all 5 exit tier values (0-4) and flags (--output json, --terse).
#
# RED STATE: Tests currently fail because the dispatcher does not have a 'validate'
# case. They will pass (GREEN) after ticket-lib-api.sh ticket_validate() and the
# dispatcher case are implemented.
#
# RED MARKER:
# tests/scripts/suites/test-ticket-validate.sh [test_validate_exit_codes_through_dispatcher]
#
# Usage: bash tests/scripts/suites/test-ticket-validate.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented dispatcher case). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DISPATCHER="$PLUGIN_ROOT/src/rebar/_engine/ticket"

source "$SCRIPT_DIR/../lib/run_test.sh"

# ── Cleanup ───────────────────────────────────────────────────────────────────
_CLEANUP_DIRS=()
_cleanup() { for d in "${_CLEANUP_DIRS[@]:-}"; do rm -rf "$d"; done; }
trap _cleanup EXIT

echo "=== test-ticket-validate.sh ==="

# ── Fixture helpers ───────────────────────────────────────────────────────────

# make_ticket_cmd TICKETS_JSON
# Creates a temp directory with a mock `ticket` script that returns the given
# JSON array from `ticket list`. Returns the path to the mock script.
# Usage: mock=$(make_ticket_cmd '[...]'); TICKET_CMD="$mock" "$DISPATCHER" validate
make_ticket_cmd() {
    local tickets_json="${1:-[]}"
    local mock_dir
    mock_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$mock_dir")
    local mock_script="$mock_dir/ticket"
    cat > "$mock_script" << MOCK_TICKET
#!/usr/bin/env bash
SUBCMD="\${1:-}"
case "\$SUBCMD" in
    list) echo '${tickets_json//\'/\'\\\'\'}' ; exit 0 ;;
    *) exit 0 ;;
esac
MOCK_TICKET
    chmod +x "$mock_script"
    echo "$mock_script"
}

# make_ticket_json ID STATUS TYPE [PARENT] [TITLE] [HAS_BODY] [HAS_NOTES] [DEPS_JSON]
# Returns a single ticket JSON object (without outer brackets).
make_ticket_json() {
    local tid="$1" status="$2" itype="$3"
    local parent="${4:-}" title="${5:-Test Ticket $1}"
    local has_body="${6:-0}" has_notes="${7:-0}" deps_json="${8:-[]}"

    local parent_val="null"
    if [[ -n "$parent" ]]; then
        parent_val="\"$parent\""
    fi

    local description_val='""'
    if [[ "$has_body" == "1" ]]; then
        description_val='"yes"'
    fi

    local notes_val='""'
    if [[ "$has_notes" == "1" ]]; then
        notes_val='"yes"'
    fi

    echo "{\"ticket_id\":\"$tid\",\"status\":\"$status\",\"ticket_type\":\"$itype\",\"title\":\"$title\",\"parent_id\":$parent_val,\"description\":$description_val,\"notes\":$notes_val,\"deps\":$deps_json,\"created_at\":\"2026-01-01T00:00:00Z\"}"
}

# ── Build a healthy fixture (score=5, exit 0) ─────────────────────────────────
# Epic with one well-described child task — no issues expected.
_make_healthy_fixture() {
    local epic
    epic=$(make_ticket_json "healthy-epic" "open" "epic" "" "Healthy Epic" "1")
    local task
    task=$(make_ticket_json "healthy-task" "open" "task" "healthy-epic" "Healthy Task" "1" "0")
    echo "[$epic,$task]"
}

# ── Build a fixture with 4+ critical issues (score=1, exit 4) ────────────────
# Use child->parent dep anti-pattern. We need >=4 CRITICAL issues to force score=1.
# Each child ticket that has a dep pointing at its parent counts as one CRITICAL.
_make_critical_fixture() {
    python3 - << 'PYEOF'
import json

tickets = []
# Parent epic
epic = {
    "ticket_id": "crit-epic",
    "status": "open",
    "ticket_type": "epic",
    "title": "Critical Epic",
    "parent_id": None,
    "description": "yes",
    "notes": "",
    "deps": [],
    "created_at": "2026-01-01T00:00:00Z",
}
tickets.append(epic)

# 4 child tasks, each with a dep pointing back at the parent (CRITICAL each)
for i in range(1, 5):
    t = {
        "ticket_id": f"crit-child-{i}",
        "status": "open",
        "ticket_type": "task",
        "title": f"Critical Child {i}",
        "parent_id": "crit-epic",
        "description": "yes",
        "notes": "",
        "deps": [{"target_id": "crit-epic", "relation": "blocks"}],
        "created_at": "2026-01-01T00:00:00Z",
    }
    tickets.append(t)

print(json.dumps(tickets))
PYEOF
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

# ── Test 2: 'ticket validate' is not an unknown subcommand (RED: will fail) ───
# test_validate_exit_codes_through_dispatcher — RED MARKER
echo "Test 2: 'ticket validate' routes through dispatcher (not unknown subcommand)"
HEALTHY_JSON=$(_make_healthy_fixture)
MOCK=$(make_ticket_cmd "$HEALTHY_JSON")

dispatch_output=""
dispatch_exit=0
dispatch_output=$(TICKET_CMD="$MOCK" "$DISPATCHER" validate --terse 2>&1) || dispatch_exit=$?

# An unrecognized subcommand exits non-zero and prints "unknown subcommand".
# When the validate case is properly implemented, this should exit 0 or 1 (health score)
# NOT print "unknown subcommand".
if [[ "${dispatch_output,,}" =~ unknown.*subcommand|unrecognized.*subcommand ]]; then
    echo "  FAIL: dispatcher does not recognize 'validate' subcommand (RED — expected before GREEN)" >&2
    echo "  Output: $dispatch_output" >&2
    (( FAIL++ ))
elif [[ $dispatch_exit -ge 5 ]]; then
    echo "  FAIL: dispatcher returned crash-level exit code $dispatch_exit for 'validate'" >&2
    echo "  Output: $dispatch_output" >&2
    (( FAIL++ ))
else
    echo "  PASS: 'validate' routed correctly through dispatcher (exit $dispatch_exit)"
    (( PASS++ ))
fi

# ── Test 3: exit 0 — healthy fixture, score=5 ────────────────────────────────
echo "Test 3: exit 0 (score=5, zero issues) passes through dispatcher"
HEALTHY_JSON=$(_make_healthy_fixture)
MOCK=$(make_ticket_cmd "$HEALTHY_JSON")

exit_code=0
TICKET_CMD="$MOCK" "$DISPATCHER" validate --terse 2>/dev/null || exit_code=$?

if [[ $exit_code -eq 0 ]]; then
    echo "  PASS: exit 0 returned for healthy fixture"
    (( PASS++ ))
else
    echo "  FAIL: expected exit 0 for healthy fixture, got exit $exit_code (RED — expected before GREEN)" >&2
    (( FAIL++ ))
fi

# ── Test 4: exit 4 — critical fixture, score=1 ───────────────────────────────
echo "Test 4: exit 4 (score=1, critical issues) passes through dispatcher"
CRITICAL_JSON=$(_make_critical_fixture)
MOCK=$(make_ticket_cmd "$CRITICAL_JSON")

crit_exit=0
TICKET_CMD="$MOCK" "$DISPATCHER" validate --terse 2>/dev/null || crit_exit=$?

if [[ $crit_exit -eq 4 ]]; then
    echo "  PASS: exit 4 returned for critical fixture"
    (( PASS++ ))
else
    echo "  FAIL: expected exit 4 for critical fixture, got exit $crit_exit (RED — expected before GREEN)" >&2
    (( FAIL++ ))
fi

# ── Test 5: exit tier 1 — minor issues (score=4, one MINOR) ──────────────────
echo "Test 5: exit 1 (score=4, minor issues) passes through dispatcher"
# Duplicate titles generate 1 MINOR. calculate_score(): MINOR deducts ceil(count/5) points.
# 1 MINOR → ceil(1/5)=1 point deduction → score=4 → exit=1.
DUP_EPIC=$(make_ticket_json "dup-epic" "open" "epic" "" "Dup Epic" "1")
DUP_1=$(make_ticket_json "dup-task-1" "open" "task" "dup-epic" "Duplicate Title" "1")
DUP_2=$(make_ticket_json "dup-task-2" "open" "task" "dup-epic" "Duplicate Title" "1")
MOCK=$(make_ticket_cmd "[$DUP_EPIC,$DUP_1,$DUP_2]")

tier1_exit=0
TICKET_CMD="$MOCK" "$DISPATCHER" validate --terse 2>/dev/null || tier1_exit=$?

# score=4 (1 MINOR × ceil(1/5)=-1 = 5-1=4 → exit 1)
if [[ $tier1_exit -eq 1 ]]; then
    echo "  PASS: exit 1 returned for minor-issues fixture"
    (( PASS++ ))
else
    echo "  FAIL: expected exit 1 for minor-issues fixture, got exit $tier1_exit (RED — expected before GREEN)" >&2
    (( FAIL++ ))
fi

# ── Test 6: exit tier 2 — moderate issues (score=3) ──────────────────────────
echo "Test 6: exit 2 (score=3, moderate issues) passes through dispatcher"
# 1 CRITICAL issue (child dep pointing back at parent epic) = -2 from score 5 → score=3 → exit=2.
# calculate_score(): CRITICAL deducts 2 points each.
T6_EPIC=$(make_ticket_json "t6-epic" "open" "epic" "" "T6 Epic" "1")
T6_CHILD=$(make_ticket_json "t6-child" "open" "task" "t6-epic" "T6 Child" "1" "0" '[{"target_id":"t6-epic","relation":"blocks"}]')
MOCK=$(make_ticket_cmd "[$T6_EPIC,$T6_CHILD]")

tier2_exit=0
TICKET_CMD="$MOCK" "$DISPATCHER" validate --terse 2>/dev/null || tier2_exit=$?

# score=3 (1 CRITICAL × -2 = 5-2=3 → exit 2)
if [[ $tier2_exit -eq 2 ]]; then
    echo "  PASS: exit 2 returned for moderate-issues fixture"
    (( PASS++ ))
else
    echo "  FAIL: expected exit 2 for moderate-issues fixture, got exit $tier2_exit (RED — expected before GREEN)" >&2
    (( FAIL++ ))
fi

# ── Test 7: exit tier 3 — significant issues (score=2) ───────────────────────
echo "Test 7: exit 3 (score=2, significant issues) passes through dispatcher"
# 1 CRITICAL (-2) + 1 orphaned task (1 WARNING, -1 via ceil(1/10)=1) → score=5-2-1=2 → exit=3.
# calculate_score(): WARNINGs deduct 1 point per 10 (ceiling division).
T7_EPIC=$(make_ticket_json "t7-epic" "open" "epic" "" "T7 Epic" "1")
T7_CHILD=$(make_ticket_json "t7-child" "open" "task" "t7-epic" "T7 Child" "1" "0" '[{"target_id":"t7-epic","relation":"blocks"}]')
T7_ORPHAN=$(make_ticket_json "t7-orphan" "open" "task" "" "T7 Orphaned Task" "1")
MOCK=$(make_ticket_cmd "[$T7_EPIC,$T7_CHILD,$T7_ORPHAN]")

tier3_exit=0
TICKET_CMD="$MOCK" "$DISPATCHER" validate --terse 2>/dev/null || tier3_exit=$?

# score=2 (1 CRITICAL × -2 + 1 WARNING × -1 = 5-2-1=2 → exit 3)
if [[ $tier3_exit -eq 3 ]]; then
    echo "  PASS: exit 3 returned for significant-issues fixture"
    (( PASS++ ))
else
    echo "  FAIL: expected exit 3 for significant-issues fixture, got exit $tier3_exit (RED — expected before GREEN)" >&2
    (( FAIL++ ))
fi

# ── Test 8: --output json flag produces JSON-parseable output through dispatcher ─────
echo "Test 8: --output json flag produces JSON-parseable output through dispatcher"
HEALTHY_JSON=$(_make_healthy_fixture)
MOCK=$(make_ticket_cmd "$HEALTHY_JSON")

json_output=""
json_exit=0
json_output=$(TICKET_CMD="$MOCK" "$DISPATCHER" validate --output json 2>/dev/null) || json_exit=$?

# Must parse as valid JSON
json_valid=0
python3 -c "import json,sys; json.loads(sys.argv[1])" "$json_output" 2>/dev/null || json_valid=$?

if [[ $json_valid -eq 0 ]]; then
    echo "  PASS: --output json flag produced JSON-parseable output through dispatcher"
    (( PASS++ ))
else
    echo "  FAIL: --output json flag did not produce parseable JSON through dispatcher (RED — expected before GREEN)" >&2
    echo "  Output (first 5 lines): $(printf '%s\n' "$json_output" | head -5)" >&2
    (( FAIL++ ))
fi

# ── Test 9: --terse flag produces abbreviated output through dispatcher ────────
echo "Test 9: --terse flag produces abbreviated output through dispatcher"
HEALTHY_JSON=$(_make_healthy_fixture)
MOCK=$(make_ticket_cmd "$HEALTHY_JSON")

terse_output=""
terse_exit=0
terse_output=$(TICKET_CMD="$MOCK" "$DISPATCHER" validate --terse 2>&1) || terse_exit=$?

# Terse mode on a healthy fixture should produce a single line (not multi-line report).
terse_lines=$(printf '%s\n' "$terse_output" | wc -l | tr -d ' ')

if [[ $terse_lines -le 3 ]]; then
    echo "  PASS: --terse flag produced abbreviated output ($terse_lines line(s)) through dispatcher"
    (( PASS++ ))
else
    echo "  FAIL: --terse flag through dispatcher produced $terse_lines lines (expected <=3, RED — expected before GREEN)" >&2
    echo "  Output: $terse_output" >&2
    (( FAIL++ ))
fi

# ── Test 10: dispatcher does NOT reach validate-issues.sh when subcommand unknown ─
# Structural boundary test: when 'validate' is not yet in the dispatcher,
# the dispatcher prints "unknown subcommand" and does NOT delegate to validate-issues.sh.
# This assertion holds in RED state. In GREEN state the dispatcher will route correctly.
echo "Test 10: Dispatcher output comes from dispatcher (not validate-issues.sh directly)"
HEALTHY_JSON=$(_make_healthy_fixture)
MOCK=$(make_ticket_cmd "$HEALTHY_JSON")

boundary_output=""
boundary_exit=0
boundary_output=$(TICKET_CMD="$MOCK" "$DISPATCHER" validate --terse 2>&1) || boundary_exit=$?

# In GREEN state: no "unknown subcommand" in output.
# In RED state:  "unknown subcommand" present — test fails as required.
if [[ "${boundary_output,,}" =~ unknown.*subcommand ]]; then
    echo "  FAIL: dispatcher output contains 'unknown subcommand' — validate not yet routed (RED)" >&2
    (( FAIL++ ))
else
    echo "  PASS: dispatcher routed 'validate' without 'unknown subcommand' error"
    (( PASS++ ))
fi

# ── Results ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
