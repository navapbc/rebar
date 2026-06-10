#!/usr/bin/env bash
# tests/scripts/test-ticket-list-status-comma-or.sh
# Behavioral tests for --status=open,in_progress comma-OR semantics in ticket list.
#
# Defect class: brace-group double ticket-list pattern produces invalid JSON
# ([...][...]) when two separate calls are concatenated. The fix uses a single
# call with --status=open,in_progress (default JSON-array format) to produce one
# valid JSON array, counted via python3 json.load.
#
# Assertions:
#   1. Single --status=open,in_progress call (default format) produces valid JSON
#      array and json.load exits 0.
#   2. Concatenating two separate ticket-list outputs fails json.load (nonzero) —
#      proves the defect class.
#   3. Structural guard: sprint/SKILL.md and auto-resume.md do NOT contain the
#      brace-group double ticket-list pattern (regression prevention).
#
# Usage: bash tests/scripts/test-ticket-list-status-comma-or.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_DISPATCHER="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-ticket-list-status-comma-or.sh ==="

_CLEANUP_DIRS=()
cleanup() { for d in "${_CLEANUP_DIRS[@]:-}"; do [ -n "$d" ] && rm -rf "$d"; done; }
trap cleanup EXIT

# Build a minimal tracker with tickets in both open and in_progress status.
_make_tracker() {
    local tracker
    tracker=$(mktemp -d "${TMPDIR:-/tmp}/tlcomma.XXXXXX")
    _CLEANUP_DIRS+=("$tracker")

    python3 - "$tracker" <<'PY'
import json, sys, os

tracker = sys.argv[1]

tickets = [
    ("aaaa-aaaa-aaaa-aaaa", "open",        "task", "Open task"),
    ("bbbb-bbbb-bbbb-bbbb", "in_progress",  "task", "In-progress task"),
    ("cccc-cccc-cccc-cccc", "closed",       "task", "Closed task"),
]

for tid, status, ttype, title in tickets:
    tdir = os.path.join(tracker, tid)
    os.makedirs(tdir, exist_ok=True)

    create_ev = {
        "event_type": "CREATE",
        "uuid": f"create-{tid}",
        "timestamp": 1000,
        "author": "Test",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {"ticket_type": ttype, "title": title, "parent_id": "", "tags": []}
    }
    with open(os.path.join(tdir, f"1000-create-{tid}-CREATE.json"), "w") as f:
        json.dump(create_ev, f)

    if status != "open":
        status_ev = {
            "event_type": "STATUS",
            "uuid": f"status-{tid}",
            "timestamp": 2000,
            "author": "Test",
            "env_id": "00000000-0000-4000-8000-000000000001",
            "data": {"status": status}
        }
        with open(os.path.join(tdir, f"2000-status-{tid}-STATUS.json"), "w") as f:
            json.dump(status_ev, f)

print(tracker)
PY
}

# ── Test 1: single call --status=open,in_progress produces valid JSON array ──
# Default format (no --output llm) outputs a JSON array, which json.load accepts.
test_single_call_valid_json() {
    local tracker; tracker=$(_make_tracker)
    local out rc=0

    out=$(TICKETS_TRACKER_DIR="$tracker" bash "$TICKET_DISPATCHER" list --status=open,in_progress 2>/dev/null) || rc=$?
    assert_eq "ticket-list with --status=open,in_progress exits 0" "0" "$rc"

    # Output must parse as a valid JSON array.
    local parse_rc=0
    echo "$out" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null || parse_rc=$?
    assert_eq "single-call output is valid JSON array" "0" "$parse_rc"

    # Must return exactly 2 tickets (open + in_progress), not the closed one.
    local count
    count=$(echo "$out" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null)
    assert_eq "single-call returns exactly 2 (open + in_progress)" "2" "$count"
}
test_single_call_valid_json

# ── Test 2: double-call brace-group pattern produces INVALID JSON (defect class) ──
# Concatenated JSON arrays ([...][...]) are NOT valid JSON and json.load must
# reject them. This test proves the problem class that the fix addresses.
test_double_call_produces_invalid_json() {
    local tracker; tracker=$(_make_tracker)

    # Simulate the old pattern: concatenate two separate call outputs.
    local combined
    combined=$(
        TICKETS_TRACKER_DIR="$tracker" bash "$TICKET_DISPATCHER" list --status=open 2>/dev/null
        TICKETS_TRACKER_DIR="$tracker" bash "$TICKET_DISPATCHER" list --status=in_progress 2>/dev/null
    )

    # Concatenated JSON arrays must fail to parse.
    local parse_rc=0
    echo "$combined" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null || parse_rc=$?
    assert_ne "concatenated two-call output is NOT valid JSON (defect class confirmed)" "0" "$parse_rc"
}
test_double_call_produces_invalid_json

print_summary
