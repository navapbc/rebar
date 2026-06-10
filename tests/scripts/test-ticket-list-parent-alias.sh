#!/usr/bin/env bash
# tests/scripts/test-ticket-list-parent-alias.sh
# Behavioral tests for `ticket list --parent=<alias|short-id>` alias resolution.
#
# Root cause A fix: apply_ticket_filters() used to compare raw CLI parent_filter
# against canonical parent_id; aliases and short IDs returned empty results. The
# fix resolves the parent filter via centralized resolve_ticket_id before filtering.
#
# Test axes:
#   1. --parent=<alias>           resolves alias → canonical ID  (script + inproc × default + llm)
#   2. --parent=<canonical-id>    already canonical; unaffected   (GREEN control)
#   3. --parent=<8-hex-short-id>  resolve_ticket_id 8-hex path   (script + inproc; skipped if
#                                  resolver does not support 8-hex; assertion carries note)
#
# Usage: bash tests/scripts/test-ticket-list-parent-alias.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
LIST_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-list.sh"
LIB_API="$REPO_ROOT/src/rebar/_engine/ticket-lib-api.sh"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-ticket-list-parent-alias.sh ==="

# Source the in-process library once
# shellcheck source=/dev/null
source "$LIB_API"

# Canonical IDs
EPIC_ID="eeee-eeee-eeee-eeee"     # epic with alias "test-epic-alias"
TASK1_ID="1111-1111-1111-1111"    # child task
TASK2_ID="2222-2222-2222-2222"    # child task

_CLEANUP_DIRS=()
cleanup() { for d in "${_CLEANUP_DIRS[@]:-}"; do [ -n "$d" ] && rm -rf "$d"; done; }
trap cleanup EXIT

# Build an isolated tracker:
#   - epic eeee-eeee-eeee-eeee with data.alias="test-epic-alias"
#   - 2 child tasks with parent_id=eeee-eeee-eeee-eeee
_make_tracker() {
    local tracker
    tracker=$(mktemp -d "${TMPDIR:-/tmp}/tl-parent-alias.XXXXXX")
    _CLEANUP_DIRS+=("$tracker")

    python3 - "$tracker" "$EPIC_ID" "$TASK1_ID" "$TASK2_ID" <<'PY'
import json, sys, os

tracker, epic_id, task1_id, task2_id = sys.argv[1:5]

def write_event(ticket_dir, event):
    os.makedirs(ticket_dir, exist_ok=True)
    # Convention: <timestamp>-<lowercase-uuid>-<EVENT_TYPE>.json
    # The alias resolver requires filenames ending in "-CREATE.json".
    tid = os.path.basename(ticket_dir)
    etype = event["event_type"]
    fname = f"{event['timestamp']}-{tid}-{etype}.json"
    open(os.path.join(ticket_dir, fname), "w").write(json.dumps(event))

# Epic with alias
write_event(
    os.path.join(tracker, epic_id),
    {
        "event_type": "CREATE",
        "uuid": f"create-{epic_id}",
        "timestamp": 1000,
        "author": "Test",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "ticket_type": "epic",
            "title": "Test Epic",
            "parent_id": "",
            "tags": [],
            "alias": "test-epic-alias",
        },
    },
)

# Child task 1
write_event(
    os.path.join(tracker, task1_id),
    {
        "event_type": "CREATE",
        "uuid": f"create-{task1_id}",
        "timestamp": 1001,
        "author": "Test",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "ticket_type": "task",
            "title": "Child Task 1",
            "parent_id": epic_id,
            "tags": [],
        },
    },
)

# Child task 2
write_event(
    os.path.join(tracker, task2_id),
    {
        "event_type": "CREATE",
        "uuid": f"create-{task2_id}",
        "timestamp": 1002,
        "author": "Test",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "ticket_type": "task",
            "title": "Child Task 2",
            "parent_id": epic_id,
            "tags": [],
        },
    },
)
PY

    echo "$tracker"
}

# Run ticket list via a chosen implementation.
#   $1 = impl: "script" | "inproc"
#   $2 = tracker dir; rest = args passed to ticket list
_run_list() {
    local impl="$1" tracker="$2"; shift 2
    if [ "$impl" = "script" ]; then
        TICKETS_TRACKER_DIR="$tracker" bash "$LIST_SCRIPT" "$@"
    else
        TICKETS_TRACKER_DIR="$tracker" ticket_list "$@"
    fi
}

# Count tickets in JSON-array output
_json_count() {
    python3 -c "import json,sys; print(len(json.load(sys.stdin)))"
}

# Count tickets in llm-format (JSONL) output
_llm_count() {
    python3 -c "
import sys, json
count = 0
for line in sys.stdin:
    line = line.strip()
    if line:
        json.loads(line)  # parse to validate
        count += 1
print(count)
"
}

# ── Tests ────────────────────────────────────────────────────────────────────

for IMPL in script inproc; do
    TR=$(_make_tracker)

    # 1a. Alias filter — default (JSON array) format: should return 2 children
    got=$(_run_list "$IMPL" "$TR" --parent=test-epic-alias 2>/dev/null | _json_count)
    assert_eq "alias filter default-format returns 2 children [$IMPL]" "2" "$got"

    # 1b. Alias filter — llm format: should return 2 children
    got=$(_run_list "$IMPL" "$TR" --parent=test-epic-alias --output llm 2>/dev/null | _llm_count)
    assert_eq "alias filter llm-format returns 2 children [$IMPL]" "2" "$got"

    # 2a. Canonical ID filter — default format (GREEN control; must not regress)
    got=$(_run_list "$IMPL" "$TR" --parent="$EPIC_ID" 2>/dev/null | _json_count)
    assert_eq "canonical-id filter default-format returns 2 [$IMPL]" "2" "$got"

    # 2b. Canonical ID filter — llm format (GREEN control)
    got=$(_run_list "$IMPL" "$TR" --parent="$EPIC_ID" --output llm 2>/dev/null | _llm_count)
    assert_eq "canonical-id filter llm-format returns 2 [$IMPL]" "2" "$got"
done

# 3. 8-hex short-ID filter — probe whether resolve_ticket_id supports it
#    (eeee-eeee is the 8-hex prefix of eeee-eeee-eeee-eeee)
SHORT_ID="eeee-eeee"
TR_SHORT=$(_make_tracker)

# Probe: run ticket list --parent=eeee-eeee with the script and see if it returns results.
# If resolve_ticket_id supports 8-hex, we get 2 children; if not, we get 0.
# We assert 2 — if the resolver doesn't support 8-hex the test fails and we note it.
got=$(TICKETS_TRACKER_DIR="$TR_SHORT" bash "$LIST_SCRIPT" --parent="$SHORT_ID" 2>/dev/null | _json_count)
assert_eq "8-hex short-id filter returns 2 children [script]" "2" "$got"

got=$(TICKETS_TRACKER_DIR="$TR_SHORT" ticket_list --parent="$SHORT_ID" 2>/dev/null | _json_count)
assert_eq "8-hex short-id filter returns 2 children [inproc]" "2" "$got"

print_summary
