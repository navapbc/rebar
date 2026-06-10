#!/usr/bin/env bash
# tests/scripts/test-ticket-next-batch-dispatcher.sh
# Integration tests for 'ticket next-batch' subcommand routing through the dispatcher.
#
# Tests verify that the dispatcher correctly routes 'ticket next-batch' to
# sprint-next-batch.sh and that output matches the expected text/JSON format.
#
# Uses TICKETS_TRACKER_DIR injection — sprint-next-batch.sh respects this env var
# directly, and the ticket CLI it calls (TICKET_CMD defaults to same-dir ticket)
# also respects TICKETS_TRACKER_DIR.
#
# Usage: bash tests/scripts/test-ticket-next-batch-dispatcher.sh
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

echo "=== test-ticket-next-batch-dispatcher.sh ==="

# ── Fixture helpers ───────────────────────────────────────────────────────────

# make_next_batch_fixture — creates a .tickets-tracker/ with:
#   nb-epic          (epic, open)
#     nb-story-1     (story, open, no blockers)
#       nb-task-1    (task, open, child of nb-story-1)
#       nb-task-2    (task, open, child of nb-story-1)
#     nb-story-2     (story, open, depends_on nb-story-1 → blocked)
#       nb-task-3    (task, open, child of nb-story-2)
make_next_batch_fixture() {
    local tracker_dir
    tracker_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tracker_dir")

    python3 - "$tracker_dir" <<'PYEOF'
import json, sys, os
base = sys.argv[1]
ts = 1700000000000000000

def write(tid, ts_offset, event_type, data):
    d = os.path.join(base, tid)
    os.makedirs(d, exist_ok=True)
    idx = len(os.listdir(d)) + 1
    evt = {
        "event_type": event_type,
        "ticket_id": tid,
        "timestamp": ts + ts_offset,
        "uuid": f"test-{tid}-{idx:04d}",
        "env_id": "test",
        "author": "test",
        "data": data,
    }
    with open(os.path.join(d, f"{idx:03d}-{event_type}.json"), "w") as f:
        json.dump(evt, f)

# nb-epic
write("nb-epic", 0, "CREATE", {
    "ticket_id": "nb-epic", "title": "NB Epic", "ticket_type": "epic",
    "status": "open", "priority": 1, "parent_id": None,
    "tags": [], "description": "", "notes": "",
})
# nb-story-1 (open, no blockers)
write("nb-story-1", 1, "CREATE", {
    "ticket_id": "nb-story-1", "title": "NB Story One", "ticket_type": "story",
    "status": "open", "priority": 2, "parent_id": "nb-epic",
    "tags": [], "description": "", "notes": "",
})
# nb-task-1
write("nb-task-1", 2, "CREATE", {
    "ticket_id": "nb-task-1", "title": "NB Task One", "ticket_type": "task",
    "status": "open", "priority": 2, "parent_id": "nb-story-1",
    "tags": [], "description": "", "notes": "",
})
# nb-task-2
write("nb-task-2", 3, "CREATE", {
    "ticket_id": "nb-task-2", "title": "NB Task Two", "ticket_type": "task",
    "status": "open", "priority": 2, "parent_id": "nb-story-1",
    "tags": [], "description": "", "notes": "",
})
# nb-story-2 (open, depends_on nb-story-1 → blocked)
write("nb-story-2", 4, "CREATE", {
    "ticket_id": "nb-story-2", "title": "NB Story Two Blocked", "ticket_type": "story",
    "status": "open", "priority": 3, "parent_id": "nb-epic",
    "tags": [], "description": "", "notes": "",
})
write("nb-story-2", 5, "LINK", {
    "relation": "depends_on", "target_id": "nb-story-1",
})
# nb-task-3 (child of blocked nb-story-2)
write("nb-task-3", 6, "CREATE", {
    "ticket_id": "nb-task-3", "title": "NB Task Three Blocked", "ticket_type": "task",
    "status": "open", "priority": 3, "parent_id": "nb-story-2",
    "tags": [], "description": "", "notes": "",
})
PYEOF

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
test_next_batch_routes_through_dispatcher() {
    local _tracker _output _exit

    # Test 2: 'ticket next-batch nb-epic' is recognized — not "unknown subcommand"
    echo "Test 2: 'ticket next-batch nb-epic' recognized by dispatcher (not unknown subcommand)"
    _tracker=$(make_next_batch_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" next-batch nb-epic 2>&1) || _exit=$?

    if [[ "${_output,,}" =~ unknown.*subcommand|unrecognized.*subcommand ]]; then
        echo "  FAIL: dispatcher does not recognize 'next-batch' subcommand (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    elif [[ $_exit -ge 5 ]]; then
        echo "  FAIL: dispatcher returned unexpected exit $_exit (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    else
        echo "  PASS: 'next-batch' recognized by dispatcher (exit $_exit)"
        (( PASS++ ))
    fi

    # Test 3: Output contains EPIC: line with the epic ID
    echo "Test 3: Output contains EPIC: line with epic ID"
    _tracker=$(make_next_batch_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" next-batch nb-epic 2>&1) || _exit=$?

    if [[ "$_output" =~ EPIC:.*nb-epic ]]; then
        echo "  PASS: output contains 'EPIC: ... nb-epic'"
        (( PASS++ ))
    else
        echo "  FAIL: output missing EPIC: line with nb-epic (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 4: Output contains BATCH_SIZE: line
    echo "Test 4: Output contains BATCH_SIZE: line"
    _tracker=$(make_next_batch_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" next-batch nb-epic 2>&1) || _exit=$?

    if [[ "$_output" =~ BATCH_SIZE: ]]; then
        echo "  PASS: output contains 'BATCH_SIZE:' line"
        (( PASS++ ))
    else
        echo "  FAIL: output missing BATCH_SIZE: line (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 5: nb-task-3 (child of blocked nb-story-2) produces SKIPPED_BLOCKED_STORY
    echo "Test 5: nb-task-3 (child of blocked story) produces SKIPPED_BLOCKED_STORY"
    _tracker=$(make_next_batch_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" next-batch nb-epic 2>&1) || _exit=$?

    if [[ "$_output" =~ SKIPPED_BLOCKED_STORY.*nb-task-3 ]]; then
        echo "  PASS: nb-task-3 correctly deferred as SKIPPED_BLOCKED_STORY"
        (( PASS++ ))
    else
        echo "  FAIL: nb-task-3 not deferred as SKIPPED_BLOCKED_STORY (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 6: --output json flag produces valid JSON with expected keys
    # Note: sprint-next-batch.sh prints a conflict matrix to stderr; capture stdout only.
    echo "Test 6: --output json flag produces valid JSON with expected keys"
    _tracker=$(make_next_batch_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" next-batch nb-epic --output json 2>/dev/null) || _exit=$?

    if echo "$_output" | python3 -c "
import json, sys
d = json.load(sys.stdin)
required = ['available_pool', 'skipped_overlap', 'skipped_blocked_story']
missing = [k for k in required if k not in d]
if missing:
    print('Missing keys: ' + ', '.join(missing), file=sys.stderr)
    sys.exit(1)
# Decoupled: the DSO agent-routing overlay must be gone.
if 'opus_cap' in d:
    print('Routing key opus_cap should be absent', file=sys.stderr)
    sys.exit(1)
routing_fields = {'model', 'subagent', 'class', 'complexity'}
for entry in d.get('batch', []):
    leaked = routing_fields & set(entry)
    if leaked:
        print('Routing fields leaked: ' + ', '.join(sorted(leaked)), file=sys.stderr)
        sys.exit(1)
" 2>/dev/null; then
        echo "  PASS: --output json output is valid JSON with required keys"
        (( PASS++ ))
    else
        echo "  FAIL: --output json output is not valid JSON or missing required keys (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi
}

test_next_batch_no_epic_exits_nonzero() {
    local _exit _output

    # Test 7: No args exits non-zero (usage error)
    echo "Test 7: No args exits non-zero"
    _exit=0
    _output=$("$DISPATCHER" next-batch 2>&1) || _exit=$?

    if [[ $_exit -ne 0 ]]; then
        echo "  PASS: 'ticket next-batch' with no args exits non-zero (exit $_exit)"
        (( PASS++ ))
    else
        echo "  FAIL: 'ticket next-batch' with no args exited 0 (RED — expected before GREEN)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi
}

# make_overlap_fixture — creates a .tickets-tracker/ with:
#   nb-overlap-epic     (epic, open)
#     nb-overlap-story  (story, open, no blockers)
#       nb-overlap-a    (task, open, title references src/rebar/_engine/sprint-next-batch.sh)
#       nb-overlap-b    (task, open, title references src/rebar/_engine/sprint-next-batch.sh)
# Both tasks reference the same file in their title so one is deferred as SKIPPED_OVERLAP.
make_overlap_fixture() {
    local tracker_dir
    tracker_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tracker_dir")

    python3 - "$tracker_dir" <<'PYEOF'
import json, sys, os
base = sys.argv[1]
ts = 1700001000000000000

def write(tid, ts_offset, event_type, data):
    d = os.path.join(base, tid)
    os.makedirs(d, exist_ok=True)
    idx = len(os.listdir(d)) + 1
    evt = {
        "event_type": event_type,
        "ticket_id": tid,
        "timestamp": ts + ts_offset,
        "uuid": f"test-{tid}-{idx:04d}",
        "env_id": "test",
        "author": "test",
        "data": data,
    }
    with open(os.path.join(d, f"{idx:03d}-{event_type}.json"), "w") as f:
        json.dump(evt, f)

write("nb-overlap-epic", 0, "CREATE", {
    "ticket_id": "nb-overlap-epic", "title": "NB Overlap Epic", "ticket_type": "epic",
    "status": "open", "priority": 1, "parent_id": None,
    "tags": [], "description": "", "notes": "",
})
write("nb-overlap-story", 1, "CREATE", {
    "ticket_id": "nb-overlap-story", "title": "NB Overlap Story", "ticket_type": "story",
    "status": "open", "priority": 2, "parent_id": "nb-overlap-epic",
    "tags": [], "description": "", "notes": "",
})
# Both tasks reference the same .sh file in their title. The prose-path regex
# in extract_files() matches plugins/**/*.sh from the title text returned by
# _load_ticket_body(), so both candidates end up with the same file in their
# conflict set, causing the second to be deferred as SKIPPED_OVERLAP.
shared_file = "src/rebar/_engine/sprint-next-batch.sh"
write("nb-overlap-a", 2, "CREATE", {
    "ticket_id": "nb-overlap-a",
    "title": f"NB Overlap Task A - modifies {shared_file}",
    "ticket_type": "task",
    "status": "open", "priority": 2, "parent_id": "nb-overlap-story",
    "tags": [], "description": "", "notes": "",
})
write("nb-overlap-b", 3, "CREATE", {
    "ticket_id": "nb-overlap-b",
    "title": f"NB Overlap Task B - modifies {shared_file}",
    "ticket_type": "task",
    "status": "open", "priority": 2, "parent_id": "nb-overlap-story",
    "tags": [], "description": "", "notes": "",
})
PYEOF

    echo "$tracker_dir"
}

test_next_batch_skipped_overlap() {
    local _tracker _output _exit

    # Test 8: Two tasks sharing a file — one is deferred as SKIPPED_OVERLAP
    echo "Test 8: Two tasks sharing a file produce SKIPPED_OVERLAP for the second task"
    _tracker=$(make_overlap_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" next-batch nb-overlap-epic 2>&1) || _exit=$?

    if [[ "$_output" =~ SKIPPED_OVERLAP ]]; then
        echo "  PASS: SKIPPED_OVERLAP emitted for overlapping-file task"
        (( PASS++ ))
    else
        echo "  FAIL: SKIPPED_OVERLAP not emitted for overlapping tasks" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi
}

# make_file_impact_fixture — creates a .tickets-tracker/ with:
#   nb-fi-epic         (epic, open)
#     nb-fi-story      (story, open, no blockers)
#       nb-fi-a        (task, open, generic title)
#       nb-fi-b        (task, open, generic title)
# Both tasks record an *identical* recorded file_impact (FILE_IMPACT event) of
# [{"path":"src/shared.py","reason":"edit"}]. Their titles share NO file path, so
# the only overlap signal is the recorded file_impact — exercising the bug.
make_file_impact_fixture() {
    local tracker_dir
    tracker_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tracker_dir")

    python3 - "$tracker_dir" <<'PYEOF'
import json, sys, os
base = sys.argv[1]
ts = 1700002000000000000

def write(tid, ts_offset, event_type, data):
    d = os.path.join(base, tid)
    os.makedirs(d, exist_ok=True)
    idx = len(os.listdir(d)) + 1
    evt = {
        "event_type": event_type,
        "ticket_id": tid,
        "timestamp": ts + ts_offset,
        "uuid": f"test-{tid}-{idx:04d}",
        "env_id": "test",
        "author": "test",
        "data": data,
    }
    with open(os.path.join(d, f"{idx:03d}-{event_type}.json"), "w") as f:
        json.dump(evt, f)

write("nb-fi-epic", 0, "CREATE", {
    "ticket_id": "nb-fi-epic", "title": "NB FI Epic", "ticket_type": "epic",
    "status": "open", "priority": 1, "parent_id": None,
    "tags": [], "description": "", "notes": "",
})
write("nb-fi-story", 1, "CREATE", {
    "ticket_id": "nb-fi-story", "title": "NB FI Story", "ticket_type": "story",
    "status": "open", "priority": 2, "parent_id": "nb-fi-epic",
    "tags": [], "description": "", "notes": "",
})
# Generic titles with NO file paths — overlap must come from recorded file_impact.
write("nb-fi-a", 2, "CREATE", {
    "ticket_id": "nb-fi-a", "title": "NB FI Task A", "ticket_type": "task",
    "status": "open", "priority": 2, "parent_id": "nb-fi-story",
    "tags": [], "description": "", "notes": "",
})
write("nb-fi-a", 3, "FILE_IMPACT", {
    "file_impact": [{"path": "src/shared.py", "reason": "edit"}],
})
write("nb-fi-b", 4, "CREATE", {
    "ticket_id": "nb-fi-b", "title": "NB FI Task B", "ticket_type": "task",
    "status": "open", "priority": 2, "parent_id": "nb-fi-story",
    "tags": [], "description": "", "notes": "",
})
write("nb-fi-b", 5, "FILE_IMPACT", {
    "file_impact": [{"path": "src/shared.py", "reason": "edit"}],
})
PYEOF

    echo "$tracker_dir"
}

# make_no_impact_fixture — control: two tasks, generic titles, NO file_impact.
# They must co-schedule (no overlap), proving the fix does not over-serialize.
make_no_impact_fixture() {
    local tracker_dir
    tracker_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tracker_dir")

    python3 - "$tracker_dir" <<'PYEOF'
import json, sys, os
base = sys.argv[1]
ts = 1700003000000000000

def write(tid, ts_offset, event_type, data):
    d = os.path.join(base, tid)
    os.makedirs(d, exist_ok=True)
    idx = len(os.listdir(d)) + 1
    evt = {
        "event_type": event_type,
        "ticket_id": tid,
        "timestamp": ts + ts_offset,
        "uuid": f"test-{tid}-{idx:04d}",
        "env_id": "test",
        "author": "test",
        "data": data,
    }
    with open(os.path.join(d, f"{idx:03d}-{event_type}.json"), "w") as f:
        json.dump(evt, f)

write("nb-ni-epic", 0, "CREATE", {
    "ticket_id": "nb-ni-epic", "title": "NB NI Epic", "ticket_type": "epic",
    "status": "open", "priority": 1, "parent_id": None,
    "tags": [], "description": "", "notes": "",
})
write("nb-ni-story", 1, "CREATE", {
    "ticket_id": "nb-ni-story", "title": "NB NI Story", "ticket_type": "story",
    "status": "open", "priority": 2, "parent_id": "nb-ni-epic",
    "tags": [], "description": "", "notes": "",
})
write("nb-ni-a", 2, "CREATE", {
    "ticket_id": "nb-ni-a", "title": "NB NI Task A", "ticket_type": "task",
    "status": "open", "priority": 2, "parent_id": "nb-ni-story",
    "tags": [], "description": "", "notes": "",
})
write("nb-ni-b", 3, "CREATE", {
    "ticket_id": "nb-ni-b", "title": "NB NI Task B", "ticket_type": "task",
    "status": "open", "priority": 2, "parent_id": "nb-ni-story",
    "tags": [], "description": "", "notes": "",
})
PYEOF

    echo "$tracker_dir"
}

test_next_batch_file_impact_overlap() {
    local _tracker _output _exit

    # Test 9: Two tasks with identical RECORDED file_impact (and no file paths in
    # their titles) → exactly ONE batched, the other in skipped_overlap, naming
    # the shared path as conflict_file. This is RED against current code: today
    # recorded file_impact never reaches the scheduler, so BOTH get batched.
    echo "Test 9: Recorded file_impact participates in overlap detection (one batched, one skipped_overlap)"
    _tracker=$(make_file_impact_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" next-batch nb-fi-epic --output json 2>/dev/null) || _exit=$?

    if echo "$_output" | python3 -c "
import json, sys
d = json.load(sys.stdin)
batch_ids = {e.get('id') for e in d.get('batch', [])}
overlap = d.get('skipped_overlap', [])
overlap_ids = {e.get('id') for e in overlap}
errs = []
if len(batch_ids) != 1:
    errs.append('expected exactly 1 batched, got %d: %s' % (len(batch_ids), sorted(batch_ids)))
if len(overlap_ids) != 1:
    errs.append('expected exactly 1 skipped_overlap, got %d: %s' % (len(overlap_ids), sorted(overlap_ids)))
if batch_ids and overlap_ids and (batch_ids & overlap_ids):
    errs.append('same ticket both batched and skipped: %s' % sorted(batch_ids & overlap_ids))
# The conflict must name the shared declared path.
conflict_files = {e.get('conflict_file') for e in overlap}
if 'src/shared.py' not in conflict_files:
    errs.append('conflict_file did not name shared declared path; got %s' % sorted(conflict_files))
if errs:
    print('; '.join(errs), file=sys.stderr)
    sys.exit(1)
" 2>/tmp/_fi_err; then
        echo "  PASS: recorded file_impact serialized the two tasks (1 batched, 1 skipped_overlap)"
        (( PASS++ ))
    else
        echo "  FAIL: recorded file_impact ignored — both tasks batched (RED — expected before GREEN)" >&2
        echo "  Reason: $(cat /tmp/_fi_err 2>/dev/null)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 10: Control — two tasks with NO recorded file_impact and no file paths
    # in their titles must co-schedule (both batched, none skipped_overlap).
    echo "Test 10: No-file-impact control case co-schedules both tasks"
    _tracker=$(make_no_impact_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" next-batch nb-ni-epic --output json 2>/dev/null) || _exit=$?

    if echo "$_output" | python3 -c "
import json, sys
d = json.load(sys.stdin)
batch_ids = {e.get('id') for e in d.get('batch', [])}
overlap_ids = {e.get('id') for e in d.get('skipped_overlap', [])}
errs = []
if batch_ids != {'nb-ni-a', 'nb-ni-b'}:
    errs.append('expected both tasks batched, got %s' % sorted(batch_ids))
if overlap_ids:
    errs.append('expected no skipped_overlap, got %s' % sorted(overlap_ids))
if errs:
    print('; '.join(errs), file=sys.stderr)
    sys.exit(1)
" 2>/tmp/_ni_err; then
        echo "  PASS: no-file-impact tasks co-scheduled (both batched, none skipped_overlap)"
        (( PASS++ ))
    else
        echo "  FAIL: no-file-impact control over-serialized" >&2
        echo "  Reason: $(cat /tmp/_ni_err 2>/dev/null)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi

    # Test 11: --output json keys unchanged (validate batch items against schema's
    # batch_item shape — files / files_likely_read remain present, no new keys).
    echo "Test 11: --output json batch_item keys unchanged after file_impact fix"
    _tracker=$(make_file_impact_fixture)
    _exit=0
    _output=$(TICKETS_TRACKER_DIR="$_tracker" "$DISPATCHER" next-batch nb-fi-epic --output json 2>/dev/null) || _exit=$?

    if echo "$_output" | python3 -c "
import json, sys
d = json.load(sys.stdin)
# Top-level keys the contract guarantees.
for k in ('available_pool', 'skipped_overlap', 'skipped_blocked_story', 'batch'):
    if k not in d:
        print('missing top-level key: ' + k, file=sys.stderr); sys.exit(1)
# batch_item allowed keys per common.schema.json#/\$defs/batch_item (no routing leak).
allowed = {'id', 'title', 'priority', 'type', 'files', 'files_likely_read'}
routing = {'model', 'subagent', 'class', 'complexity'}
for e in d.get('batch', []):
    extra = set(e) - allowed
    if extra:
        print('unexpected batch_item keys: ' + ', '.join(sorted(extra)), file=sys.stderr); sys.exit(1)
    if routing & set(e):
        print('routing fields leaked', file=sys.stderr); sys.exit(1)
" 2>/tmp/_keys_err; then
        echo "  PASS: --output json batch_item keys unchanged"
        (( PASS++ ))
    else
        echo "  FAIL: --output json keys changed" >&2
        echo "  Reason: $(cat /tmp/_keys_err 2>/dev/null)" >&2
        echo "  Output: $_output" >&2
        (( FAIL++ ))
    fi
}

# Run the RED zone tests
test_next_batch_routes_through_dispatcher
test_next_batch_no_epic_exits_nonzero
test_next_batch_skipped_overlap
test_next_batch_file_impact_overlap

# ── Results ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
