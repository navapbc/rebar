#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-list.sh
# RED tests for src/rebar/_engine/ticket-list.sh — `ticket list` subcommand.
#
# All test functions MUST FAIL until ticket-list.sh is implemented.
# Covers: JSON array output, required per-ticket fields, ghost ticket inclusion
# with error status, empty system, and corrupt CREATE event (fsck_needed status).
#
# Usage: bash tests/scripts/suites/test-ticket-list.sh
# Returns: exit non-zero (RED) until ticket-list.sh is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
# Single-source read (story 23d2-e0f3): the standalone ticket-list.sh shim was
# collapsed into the dispatcher's `list` arm (-> ticket-reads.py). These tests
# already exercise behavior through `$TICKET_SCRIPT list`; the existence gates
# below now point at the dispatcher (the read entrypoint that must exist).
TICKET_LIST_SCRIPT="$TICKET_SCRIPT"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-list.sh ==="

# ── Helper: create a fresh temp git repo with ticket system initialized ────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: create a ticket and return its ID ─────────────────────────────────
_create_ticket() {
    local repo="$1"
    local ticket_type="${2:-task}"
    local title="${3:-Test ticket}"
    local out
    out=$(cd "$repo" && bash "$TICKET_SCRIPT" create "$ticket_type" "$title" 2>/dev/null) || true
    echo "$out" | tail -1
}

# ── Helper: count COMMENT event files in a ticket directory ───────────────────
_count_comment_events() {
    local tracker_dir="$1"
    local ticket_id="$2"
    find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-COMMENT.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' '
}

# ── Test 1: ticket list with two tickets → outputs valid JSON array with both ──
echo "Test 1: ticket list with two tickets returns JSON array containing both tickets"
test_ticket_list_returns_all_tickets() {
    _snapshot_fail

    # RED: ticket-list.sh must not exist yet
    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_list_returns_all_tickets"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local id1 id2
    id1=$(_create_ticket "$repo" task "First ticket")
    id2=$(_create_ticket "$repo" task "Second ticket")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "both tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_list_returns_all_tickets"
        return
    fi

    local list_output
    local exit_code=0
    list_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "ticket list exits 0" "0" "$exit_code"

    # Assert: output is a JSON array containing both ticket IDs
    local check_result
    check_result=$(python3 - "$list_output" "$id1" "$id2" <<'PYEOF'
import json, sys

try:
    tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

id1 = sys.argv[2]
id2 = sys.argv[3]

errors = []

if not isinstance(tickets, list):
    print(f"NOT_ARRAY: got {type(tickets).__name__}")
    sys.exit(2)

ticket_ids = [t.get("ticket_id") for t in tickets if isinstance(t, dict)]

if id1 not in ticket_ids:
    errors.append(f"ticket_id {id1!r} not found in list")
if id2 not in ticket_ids:
    errors.append(f"ticket_id {id2!r} not found in list")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(3)
else:
    print("OK")
PYEOF
) || true

    if [ "$check_result" = "OK" ]; then
        assert_eq "list contains both ticket IDs" "OK" "OK"
    else
        assert_eq "list contains both ticket IDs" "OK" "$check_result"
    fi

    assert_pass_if_clean "test_ticket_list_returns_all_tickets"
}
test_ticket_list_returns_all_tickets

# ── Test 2: ticket list with empty tracker → outputs empty JSON array '[]' ─────
echo "Test 2: ticket list with empty tracker returns empty JSON array"
test_ticket_list_empty_system() {
    _snapshot_fail

    # RED: ticket-list.sh must not exist yet
    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_list_empty_system"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local list_output
    local exit_code=0
    list_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "empty list exits 0" "0" "$exit_code"

    # Assert: output is the empty JSON array
    local normalized
    normalized=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]))" "$list_output" 2>/dev/null) || true
    assert_eq "empty system returns []" "[]" "$normalized"

    assert_pass_if_clean "test_ticket_list_empty_system"
}
test_ticket_list_empty_system

# ── Test 3: each ticket has required fields: ticket_id, ticket_type, title, status
echo "Test 3: each ticket in list has ticket_id, ticket_type, title, status fields"
test_ticket_list_has_required_fields() {
    _snapshot_fail

    # RED: ticket-list.sh must not exist yet
    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_list_has_required_fields"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Fields test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for field check" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_list_has_required_fields"
        return
    fi

    local list_output
    local exit_code=0
    list_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || exit_code=$?

    assert_eq "ticket list exits 0 for field check" "0" "$exit_code"

    local field_check
    field_check=$(python3 - "$list_output" "$ticket_id" <<'PYEOF'
import json, sys

try:
    tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

target_id = sys.argv[2]

if not isinstance(tickets, list):
    print(f"NOT_ARRAY: {type(tickets).__name__}")
    sys.exit(2)

# Find our ticket
ticket = next((t for t in tickets if isinstance(t, dict) and t.get("ticket_id") == target_id), None)
if ticket is None:
    print(f"TICKET_NOT_FOUND:{target_id}")
    sys.exit(3)

errors = []
required_fields = ["ticket_id", "ticket_type", "title", "status"]
for field in required_fields:
    if field not in ticket:
        errors.append(f"missing field: {field!r}")
    elif ticket[field] is None:
        errors.append(f"field is None: {field!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(4)
else:
    print("OK")
PYEOF
) || true

    if [ "$field_check" = "OK" ]; then
        assert_eq "ticket has all required fields" "OK" "OK"
    else
        assert_eq "ticket has all required fields" "OK" "$field_check"
    fi

    assert_pass_if_clean "test_ticket_list_has_required_fields"
}
test_ticket_list_has_required_fields

# ── Test 4: ghost ticket (dir exists, no CREATE event) appears in list with error status
echo "Test 4: ghost ticket dir (no CREATE event) appears in list with error status"
test_ticket_list_ghost_ticket_in_output() {
    _snapshot_fail

    # RED: ticket-list.sh must not exist yet
    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_list_ghost_ticket_in_output"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Manually create a ghost ticket dir with a non-parseable event file
    local ghost_id="ghost-test1"
    mkdir -p "$tracker_dir/$ghost_id"
    # Write a corrupt JSON file so reduce_ticket returns status='error'
    printf 'not-valid-json' > "$tracker_dir/$ghost_id/0000000001-aaaa-COMMENT.json"
    git -C "$tracker_dir" add "$ghost_id/0000000001-aaaa-COMMENT.json" 2>/dev/null
    git -C "$tracker_dir" commit -q -m "test: add ghost ticket dir" 2>/dev/null || true

    local list_output
    local exit_code=0
    list_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || exit_code=$?

    # Assert: exits 0 (ghost tickets should not crash the list command)
    assert_eq "list exits 0 even with ghost ticket" "0" "$exit_code"

    # Assert: ghost ticket with error status is EXCLUDED from default list output (d145-e1a9)
    local ghost_check
    ghost_check=$(python3 - "$list_output" "$ghost_id" <<'PYEOF'
import json, sys

try:
    tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

ghost_id = sys.argv[2]

if not isinstance(tickets, list):
    print(f"NOT_ARRAY: {type(tickets).__name__}")
    sys.exit(2)

ghost = next((t for t in tickets if isinstance(t, dict) and t.get("ticket_id") == ghost_id), None)
if ghost is None:
    print("OK")  # Ghost ticket correctly excluded from default output
    sys.exit(0)

print(f"GHOST_STILL_IN_LIST:{ghost_id} status={ghost.get('status','?')}")
sys.exit(3)
PYEOF
) || true

    if [ "$ghost_check" = "OK" ]; then
        assert_eq "ghost ticket excluded from default list" "OK" "OK"
    else
        assert_eq "ghost ticket excluded from default list" "OK" "$ghost_check"
    fi

    assert_pass_if_clean "test_ticket_list_ghost_ticket_in_output"
}
test_ticket_list_ghost_ticket_in_output

# ── Test 5: corrupt CREATE event → ticket appears with status='fsck_needed' ────
echo "Test 5: ticket with corrupt CREATE event appears in list with status=fsck_needed"
test_ticket_list_corrupt_create_event() {
    _snapshot_fail

    # RED: ticket-list.sh must not exist yet
    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_list_corrupt_create_event"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Manually create a ticket dir with a parseable but corrupt CREATE event
    # (missing required fields ticket_type and title)
    local corrupt_id="corrupt-tkt1"
    mkdir -p "$tracker_dir/$corrupt_id"
    python3 -c "
import json, time
event = {
    'timestamp': int(time.time()),
    'uuid': 'aaaa-bbbb-cccc',
    'event_type': 'CREATE',
    'env_id': 'test-env',
    'author': 'test-author',
    'data': {}
}
with open('$tracker_dir/$corrupt_id/0000000001-aaaa-CREATE.json', 'w') as f:
    json.dump(event, f)
" 2>/dev/null
    git -C "$tracker_dir" add "$corrupt_id/" 2>/dev/null
    git -C "$tracker_dir" commit -q -m "test: add corrupt CREATE ticket" 2>/dev/null || true

    local list_output
    local exit_code=0
    list_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || exit_code=$?

    # Assert: exits 0 (corrupt tickets must not crash list)
    assert_eq "list exits 0 with corrupt CREATE ticket" "0" "$exit_code"

    # Assert: corrupt ticket with fsck_needed status is EXCLUDED from default list output (d145-e1a9)
    local corrupt_check
    corrupt_check=$(python3 - "$list_output" "$corrupt_id" <<'PYEOF'
import json, sys

try:
    tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

corrupt_id = sys.argv[2]

if not isinstance(tickets, list):
    print(f"NOT_ARRAY: {type(tickets).__name__}")
    sys.exit(2)

ticket = next((t for t in tickets if isinstance(t, dict) and t.get("ticket_id") == corrupt_id), None)
if ticket is None:
    print("OK")  # Corrupt ticket correctly excluded from default output
    sys.exit(0)

print(f"CORRUPT_TICKET_STILL_IN_LIST:{corrupt_id} status={ticket.get('status','?')}")
sys.exit(3)
PYEOF
) || true

    if [ "$corrupt_check" = "OK" ]; then
        assert_eq "corrupt ticket excluded from default list" "OK" "OK"
    else
        assert_eq "corrupt ticket excluded from default list" "OK" "$corrupt_check"
    fi

    assert_pass_if_clean "test_ticket_list_corrupt_create_event"
}
test_ticket_list_corrupt_create_event

# ── Test 6: ticket list --output llm outputs JSONL (one ticket per line) ──────
echo "Test 6: ticket list --output llm outputs JSONL (one ticket per line)"
test_ticket_list_llm_format_jsonl() {
    _snapshot_fail

    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_list_llm_format_jsonl"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local id1 id2
    id1=$(_create_ticket "$repo" task "LLM JSONL ticket one")
    id2=$(_create_ticket "$repo" task "LLM JSONL ticket two")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "both tickets created for llm list test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_list_llm_format_jsonl"
        return
    fi

    local llm_output
    local exit_code=0
    llm_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list --output llm 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "ticket list --output llm exits 0" "0" "$exit_code"

    # Assert: each line is valid JSON (JSONL format)
    local check_result
    check_result=$(python3 - "$llm_output" "$id1" "$id2" <<'PYEOF'
import json, sys

raw = sys.argv[1]
id1 = sys.argv[2]
id2 = sys.argv[3]

lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]

if not lines:
    print("ERROR:no output lines")
    sys.exit(1)

errors = []
parsed = []
for i, line in enumerate(lines):
    try:
        obj = json.loads(line)
        parsed.append(obj)
    except json.JSONDecodeError as e:
        errors.append(f"line {i+1} is not valid JSON: {e}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(2)

# Assert: not a JSON array (that would be standard format, not JSONL)
try:
    maybe_array = json.loads(raw)
    if isinstance(maybe_array, list):
        errors.append("output is a JSON array — should be JSONL (one object per line)")
except json.JSONDecodeError:
    pass  # Not parseable as single JSON — good (it's JSONL)

# Assert: both ticket IDs are present (using shortened 'id' key)
found_ids = set()
for obj in parsed:
    tid = obj.get("id") or obj.get("ticket_id")
    if tid:
        found_ids.add(tid)

if id1 not in found_ids:
    errors.append(f"ticket {id1!r} not found in JSONL output")
if id2 not in found_ids:
    errors.append(f"ticket {id2!r} not found in JSONL output")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(3)

print("OK")
PYEOF
) || true

    if [ "$check_result" = "OK" ]; then
        assert_eq "llm list format is valid JSONL with both tickets" "OK" "OK"
    else
        assert_eq "llm list format is valid JSONL with both tickets" "OK" "$check_result"
    fi

    assert_pass_if_clean "test_ticket_list_llm_format_jsonl"
}
test_ticket_list_llm_format_jsonl

# ── Test 7: ticket list --output llm uses shortened keys ──────────────────────
echo "Test 7: ticket list --output llm uses shortened keys (id, t, ttl, st)"
test_ticket_list_llm_format_shortened_keys() {
    _snapshot_fail

    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_list_llm_format_shortened_keys"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "LLM keys test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for llm keys test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_list_llm_format_shortened_keys"
        return
    fi

    local llm_output
    local exit_code=0
    llm_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list --output llm 2>/dev/null) || exit_code=$?

    assert_eq "ticket list --output llm exits 0" "0" "$exit_code"

    # Assert: each line uses shortened keys and has no null values
    local check_result
    check_result=$(python3 - "$llm_output" "$ticket_id" <<'PYEOF'
import json, sys

raw = sys.argv[1]
target_id = sys.argv[2]

lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
errors = []

for line in lines:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        errors.append(f"invalid JSON line: {e}")
        continue

    # Check for no null values
    for k, v in obj.items():
        if v is None:
            errors.append(f"null value not stripped for key {k!r}")

    # Check shortened keys on the target ticket
    if obj.get("id") == target_id:
        if "ticket_id" in obj:
            errors.append("full key 'ticket_id' present — should use 'id'")
        if "ticket_type" in obj:
            errors.append("full key 'ticket_type' present — should use 't'")
        if "title" in obj:
            errors.append("full key 'title' present — should use 'ttl'")
        if "status" in obj:
            errors.append("full key 'status' present — should use 'st'")
        if "id" not in obj:
            errors.append("missing shortened key 'id'")
        if "t" not in obj:
            errors.append("missing shortened key 't'")
        if "ttl" not in obj:
            errors.append("missing shortened key 'ttl'")
        if "st" not in obj:
            errors.append("missing shortened key 'st'")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(1)

print("OK")
PYEOF
) || true

    if [ "$check_result" = "OK" ]; then
        assert_eq "llm list uses shortened keys and no nulls" "OK" "OK"
    else
        assert_eq "llm list uses shortened keys and no nulls" "OK" "$check_result"
    fi

    assert_pass_if_clean "test_ticket_list_llm_format_shortened_keys"
}
test_ticket_list_llm_format_shortened_keys

# ── Test 8: ticket list --output llm with empty tracker → outputs nothing ──────
echo "Test 8: ticket list --output llm with empty tracker outputs nothing (not [])"
test_ticket_list_llm_format_empty_tracker() {
    _snapshot_fail

    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_list_llm_format_empty_tracker"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local llm_output
    local exit_code=0
    llm_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list --output llm 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "empty llm list exits 0" "0" "$exit_code"

    # Assert: output is empty (not "[]" or any JSON array — matches documented 'nothing' behavior)
    assert_eq "empty tracker --output llm outputs nothing" "" "$llm_output"

    assert_pass_if_clean "test_ticket_list_llm_format_empty_tracker"
}
test_ticket_list_llm_format_empty_tracker

# ── Test 9: default ticket list excludes archived tickets ─────────────────────
# GREEN: This test verifies EXISTING behavior — ticket-list.sh already passes
# --exclude-archived to the reducer (line 68). This test passes today.
# The RED marker below (Test 10) marks the first FAILING test.
echo "Test 9: default ticket list excludes archived tickets"
test_default_excludes_archived() {
    _snapshot_fail

    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_default_excludes_archived"
        return
    fi

    local tmp_dir
    tmp_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp_dir")
    local repo
    clone_test_repo "$tmp_dir/repo"
    repo="$tmp_dir/repo"
    (cd "$repo" && bash "$TICKET_SCRIPT" init >/dev/null 2>/dev/null) || true

    local tracker_dir="$repo/.tickets-tracker"

    # Create 2 open tickets via the ticket CLI
    local id1 id2
    id1=$(_create_ticket "$repo" task "Open ticket one")
    id2=$(_create_ticket "$repo" task "Open ticket two")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "both open tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_default_excludes_archived"
        return
    fi

    # Create 1 archived ticket: write CREATE event then ARCHIVED event
    local arch_id="arch-test01"
    mkdir -p "$tracker_dir/$arch_id"
    python3 - "$tracker_dir/$arch_id" <<'PYEOF'
import json, time, sys
base = sys.argv[1]
ts = int(time.time())
create_event = {
    "timestamp": ts,
    "uuid": "aaaa-arch-0001",
    "event_type": "CREATE",
    "env_id": "test-env",
    "author": "test-author",
    "data": {
        "ticket_type": "task",
        "title": "Archived ticket",
        "status": "open",
        "priority": 2,
        "assignee": "test-author"
    }
}
archive_event = {
    "timestamp": ts + 1,
    "uuid": "aaaa-arch-0002",
    "event_type": "ARCHIVED",
    "env_id": "test-env",
    "author": "test-author",
    "data": {}
}
with open(f"{base}/0000000001-aaaa-CREATE.json", "w") as f:
    json.dump(create_event, f)
with open(f"{base}/0000000002-aaaa-ARCHIVED.json", "w") as f:
    json.dump(archive_event, f)
PYEOF
    git -C "$tracker_dir" add "$arch_id/" 2>/dev/null
    git -C "$tracker_dir" commit -q -m "test: add archived ticket" 2>/dev/null || true

    local list_output
    local exit_code=0
    list_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || exit_code=$?

    assert_eq "default list exits 0" "0" "$exit_code"

    # Assert: archived ticket NOT in default list output; open tickets ARE present
    local check_result
    check_result=$(python3 - "$list_output" "$id1" "$id2" "$arch_id" <<'PYEOF'
import json, sys

try:
    tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

id1, id2, arch_id = sys.argv[2], sys.argv[3], sys.argv[4]

if not isinstance(tickets, list):
    print(f"NOT_ARRAY: {type(tickets).__name__}")
    sys.exit(2)

ticket_ids = [t.get("ticket_id") for t in tickets if isinstance(t, dict)]
errors = []

if id1 not in ticket_ids:
    errors.append(f"open ticket {id1!r} missing from default list")
if id2 not in ticket_ids:
    errors.append(f"open ticket {id2!r} missing from default list")
if arch_id in ticket_ids:
    errors.append(f"archived ticket {arch_id!r} present in default list (should be excluded)")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(3)

print("OK")
PYEOF
) || true

    if [ "$check_result" = "OK" ]; then
        assert_eq "default list excludes archived ticket" "OK" "OK"
    else
        assert_eq "default list excludes archived ticket" "OK" "$check_result"
    fi

    assert_pass_if_clean "test_default_excludes_archived"
}
test_default_excludes_archived

# ── Test 10: --include-archived flag returns all tickets including archived ────
echo "Test 10: --include-archived flag returns all tickets including archived ones"
test_include_archived_flag_returns_all_tickets() {
    _snapshot_fail

    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_include_archived_flag_returns_all_tickets"
        return
    fi

    local tmp_dir
    tmp_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp_dir")
    local repo
    clone_test_repo "$tmp_dir/repo"
    repo="$tmp_dir/repo"
    (cd "$repo" && bash "$TICKET_SCRIPT" init >/dev/null 2>/dev/null) || true

    local tracker_dir="$repo/.tickets-tracker"

    # Create 2 open tickets via the ticket CLI
    local id1 id2
    id1=$(_create_ticket "$repo" task "Open ticket alpha")
    id2=$(_create_ticket "$repo" task "Open ticket beta")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "both open tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_include_archived_flag_returns_all_tickets"
        return
    fi

    # Create 1 archived ticket: write CREATE event then ARCHIVED event
    local arch_id="arch-test02"
    mkdir -p "$tracker_dir/$arch_id"
    python3 - "$tracker_dir/$arch_id" <<'PYEOF'
import json, time, sys
base = sys.argv[1]
ts = int(time.time())
create_event = {
    "timestamp": ts,
    "uuid": "bbbb-arch-0001",
    "event_type": "CREATE",
    "env_id": "test-env",
    "author": "test-author",
    "data": {
        "ticket_type": "task",
        "title": "Archived ticket for include test",
        "status": "open",
        "priority": 2,
        "assignee": "test-author"
    }
}
archive_event = {
    "timestamp": ts + 1,
    "uuid": "bbbb-arch-0002",
    "event_type": "ARCHIVED",
    "env_id": "test-env",
    "author": "test-author",
    "data": {}
}
with open(f"{base}/0000000001-bbbb-CREATE.json", "w") as f:
    json.dump(create_event, f)
with open(f"{base}/0000000002-bbbb-ARCHIVED.json", "w") as f:
    json.dump(archive_event, f)
PYEOF
    git -C "$tracker_dir" add "$arch_id/" 2>/dev/null
    git -C "$tracker_dir" commit -q -m "test: add archived ticket for include test" 2>/dev/null || true

    local list_output
    local exit_code=0
    # RED: --include-archived flag is not yet recognized; ticket-list.sh will error
    list_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list --include-archived 2>/dev/null) || exit_code=$?

    assert_eq "--include-archived list exits 0" "0" "$exit_code"

    # Assert: all 3 tickets present — 2 open + 1 archived
    local check_result
    check_result=$(python3 - "$list_output" "$id1" "$id2" "$arch_id" <<'PYEOF'
import json, sys

try:
    tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

id1, id2, arch_id = sys.argv[2], sys.argv[3], sys.argv[4]

if not isinstance(tickets, list):
    print(f"NOT_ARRAY: {type(tickets).__name__}")
    sys.exit(2)

ticket_ids = [t.get("ticket_id") for t in tickets if isinstance(t, dict)]
errors = []

if id1 not in ticket_ids:
    errors.append(f"open ticket {id1!r} missing from --include-archived list")
if id2 not in ticket_ids:
    errors.append(f"open ticket {id2!r} missing from --include-archived list")
if arch_id not in ticket_ids:
    errors.append(f"archived ticket {arch_id!r} missing from --include-archived list (should be included)")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(3)

# Also verify archived ticket has archived=true flag
arch_ticket = next((t for t in tickets if isinstance(t, dict) and t.get("ticket_id") == arch_id), None)
if arch_ticket and not arch_ticket.get("archived"):
    errors.append(f"archived ticket {arch_id!r} missing archived=true flag")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(4)

print("OK")
PYEOF
) || true

    if [ "$check_result" = "OK" ]; then
        assert_eq "--include-archived list contains all 3 tickets" "OK" "OK"
    else
        assert_eq "--include-archived list contains all 3 tickets" "OK" "$check_result"
    fi

    assert_pass_if_clean "test_include_archived_flag_returns_all_tickets"
}
test_include_archived_flag_returns_all_tickets

# ── Test 11: --type=bug filter returns only bug-type tickets ─────────────────
echo "Test 11: --type=bug filter returns only bug-type tickets"
test_type_filter_returns_only_matching_type() {
    _snapshot_fail

    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_type_filter_returns_only_matching_type"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # Create one bug ticket and one task ticket
    local bug_id task_id
    bug_id=$(_create_ticket "$repo" bug "A bug ticket")
    task_id=$(_create_ticket "$repo" task "A task ticket")

    if [ -z "$bug_id" ] || [ -z "$task_id" ]; then
        assert_eq "both tickets created for type filter test" "non-empty" "empty"
        assert_pass_if_clean "test_type_filter_returns_only_matching_type"
        return
    fi

    # RED: --type= flag is not recognized; ticket-list.sh will print an error and exit 1
    local list_output
    local exit_code=0
    list_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list --type=bug 2>/dev/null) || exit_code=$?

    assert_eq "--type=bug exits 0" "0" "$exit_code"

    local check_result
    check_result=$(python3 - "$list_output" "$bug_id" "$task_id" <<'PYEOF'
import json, sys

try:
    tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

bug_id = sys.argv[2]
task_id = sys.argv[3]

if not isinstance(tickets, list):
    print(f"NOT_ARRAY: {type(tickets).__name__}")
    sys.exit(2)

ticket_ids = [t.get("ticket_id") for t in tickets if isinstance(t, dict)]
errors = []

if bug_id not in ticket_ids:
    errors.append(f"bug ticket {bug_id!r} missing from --type=bug output")
if task_id in ticket_ids:
    errors.append(f"task ticket {task_id!r} present in --type=bug output (should be excluded)")

# Every returned ticket must have ticket_type == 'bug'
for t in tickets:
    if isinstance(t, dict) and t.get("ticket_type") != "bug":
        errors.append(f"non-bug ticket in output: ticket_id={t.get('ticket_id')!r} type={t.get('ticket_type')!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(3)

print("OK")
PYEOF
) || true

    if [ "$check_result" = "OK" ]; then
        assert_eq "--type=bug output contains only bug tickets" "OK" "OK"
    else
        assert_eq "--type=bug output contains only bug tickets" "OK" "$check_result"
    fi

    assert_pass_if_clean "test_type_filter_returns_only_matching_type"
}
test_type_filter_returns_only_matching_type

# ── Test 12: --status=open filter returns only open tickets ──────────────────
echo "Test 12: --status=open filter returns only open tickets"
test_status_filter_returns_only_matching_status() {
    _snapshot_fail

    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_status_filter_returns_only_matching_status"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create one open ticket via the CLI (default status is open)
    local open_id
    open_id=$(_create_ticket "$repo" task "An open ticket")

    if [ -z "$open_id" ]; then
        assert_eq "open ticket created for status filter test" "non-empty" "empty"
        assert_pass_if_clean "test_status_filter_returns_only_matching_status"
        return
    fi

    # Manually create a closed ticket by writing CREATE + STATUS events
    local closed_id="closed-tkt1"
    mkdir -p "$tracker_dir/$closed_id"
    python3 - "$tracker_dir/$closed_id" <<'PYEOF'
import json, time, sys
base = sys.argv[1]
ts = int(time.time())
create_event = {
    "timestamp": ts,
    "uuid": "cccc-stat-0001",
    "event_type": "CREATE",
    "env_id": "test-env",
    "author": "test-author",
    "data": {
        "ticket_type": "task",
        "title": "A closed ticket",
        "status": "open",
        "priority": 2,
        "assignee": "test-author"
    }
}
status_event = {
    "timestamp": ts + 1,
    "uuid": "cccc-stat-0002",
    "event_type": "STATUS",
    "env_id": "test-env",
    "author": "test-author",
    "data": {"status": "closed"}
}
with open(f"{base}/0000000001-cccc-CREATE.json", "w") as f:
    json.dump(create_event, f)
with open(f"{base}/0000000002-cccc-STATUS.json", "w") as f:
    json.dump(status_event, f)
PYEOF
    git -C "$tracker_dir" add "$closed_id/" 2>/dev/null
    git -C "$tracker_dir" commit -q -m "test: add closed ticket for status filter test" 2>/dev/null || true

    # RED: --status= flag is not recognized; ticket-list.sh will print an error and exit 1
    local list_output
    local exit_code=0
    list_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list --status=open 2>/dev/null) || exit_code=$?

    assert_eq "--status=open exits 0" "0" "$exit_code"

    local check_result
    check_result=$(python3 - "$list_output" "$open_id" "$closed_id" <<'PYEOF'
import json, sys

try:
    tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

open_id = sys.argv[2]
closed_id = sys.argv[3]

if not isinstance(tickets, list):
    print(f"NOT_ARRAY: {type(tickets).__name__}")
    sys.exit(2)

ticket_ids = [t.get("ticket_id") for t in tickets if isinstance(t, dict)]
errors = []

if open_id not in ticket_ids:
    errors.append(f"open ticket {open_id!r} missing from --status=open output")
if closed_id in ticket_ids:
    errors.append(f"closed ticket {closed_id!r} present in --status=open output (should be excluded)")

# Every returned ticket must have status == 'open'
for t in tickets:
    if isinstance(t, dict) and t.get("status") != "open":
        errors.append(f"non-open ticket in output: ticket_id={t.get('ticket_id')!r} status={t.get('status')!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(3)

print("OK")
PYEOF
) || true

    if [ "$check_result" = "OK" ]; then
        assert_eq "--status=open output contains only open tickets" "OK" "OK"
    else
        assert_eq "--status=open output contains only open tickets" "OK" "$check_result"
    fi

    assert_pass_if_clean "test_status_filter_returns_only_matching_status"
}
test_status_filter_returns_only_matching_status

# ── Test 13: --type=bug --status=open combined filter ────────────────────────
echo "Test 13: --type=bug --status=open combined filter returns only open bug tickets"
test_combined_type_and_status_filter() {
    _snapshot_fail

    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_combined_type_and_status_filter"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create an open bug ticket via the CLI
    local open_bug_id
    open_bug_id=$(_create_ticket "$repo" bug "Open bug ticket")

    # Create an open task ticket (should be excluded by --type=bug)
    local open_task_id
    open_task_id=$(_create_ticket "$repo" task "Open task ticket")

    if [ -z "$open_bug_id" ] || [ -z "$open_task_id" ]; then
        assert_eq "tickets created for combined filter test" "non-empty" "empty"
        assert_pass_if_clean "test_combined_type_and_status_filter"
        return
    fi

    # Manually create a closed bug ticket (should be excluded by --status=open)
    local closed_bug_id="closed-bug1"
    mkdir -p "$tracker_dir/$closed_bug_id"
    python3 - "$tracker_dir/$closed_bug_id" <<'PYEOF'
import json, time, sys
base = sys.argv[1]
ts = int(time.time())
create_event = {
    "timestamp": ts,
    "uuid": "dddd-comb-0001",
    "event_type": "CREATE",
    "env_id": "test-env",
    "author": "test-author",
    "data": {
        "ticket_type": "bug",
        "title": "A closed bug ticket",
        "status": "open",
        "priority": 1,
        "assignee": "test-author"
    }
}
status_event = {
    "timestamp": ts + 1,
    "uuid": "dddd-comb-0002",
    "event_type": "STATUS",
    "env_id": "test-env",
    "author": "test-author",
    "data": {"status": "closed"}
}
with open(f"{base}/0000000001-dddd-CREATE.json", "w") as f:
    json.dump(create_event, f)
with open(f"{base}/0000000002-dddd-STATUS.json", "w") as f:
    json.dump(status_event, f)
PYEOF
    git -C "$tracker_dir" add "$closed_bug_id/" 2>/dev/null
    git -C "$tracker_dir" commit -q -m "test: add closed bug for combined filter test" 2>/dev/null || true

    # RED: neither --type= nor --status= is recognized; ticket-list.sh will exit 1
    local list_output
    local exit_code=0
    list_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list --type=bug --status=open 2>/dev/null) || exit_code=$?

    assert_eq "--type=bug --status=open exits 0" "0" "$exit_code"

    local check_result
    check_result=$(python3 - "$list_output" "$open_bug_id" "$open_task_id" "$closed_bug_id" <<'PYEOF'
import json, sys

try:
    tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

open_bug_id = sys.argv[2]
open_task_id = sys.argv[3]
closed_bug_id = sys.argv[4]

if not isinstance(tickets, list):
    print(f"NOT_ARRAY: {type(tickets).__name__}")
    sys.exit(2)

ticket_ids = [t.get("ticket_id") for t in tickets if isinstance(t, dict)]
errors = []

if open_bug_id not in ticket_ids:
    errors.append(f"open bug {open_bug_id!r} missing from combined filter output")
if open_task_id in ticket_ids:
    errors.append(f"open task {open_task_id!r} present in --type=bug output (should be excluded)")
if closed_bug_id in ticket_ids:
    errors.append(f"closed bug {closed_bug_id!r} present in --status=open output (should be excluded)")

# Every returned ticket must be bug type AND open status
for t in tickets:
    if not isinstance(t, dict):
        continue
    if t.get("ticket_type") != "bug":
        errors.append(f"non-bug ticket in output: ticket_id={t.get('ticket_id')!r} type={t.get('ticket_type')!r}")
    if t.get("status") != "open":
        errors.append(f"non-open ticket in output: ticket_id={t.get('ticket_id')!r} status={t.get('status')!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(3)

print("OK")
PYEOF
) || true

    if [ "$check_result" = "OK" ]; then
        assert_eq "combined filter output contains only open bug tickets" "OK" "OK"
    else
        assert_eq "combined filter output contains only open bug tickets" "OK" "$check_result"
    fi

    assert_pass_if_clean "test_combined_type_and_status_filter"
}
test_combined_type_and_status_filter

# ── Test 14: --help prints usage info and exits 0 ────────────────────────────
echo "Test 14: --help prints usage info and exits 0"
test_help_flag_prints_usage_and_exits_0() {
    _snapshot_fail

    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_help_flag_prints_usage_and_exits_0"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # RED: --help is not recognized; ticket-list.sh will print an error and exit 1
    local help_output
    local exit_code=0
    help_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list --help 2>&1) || exit_code=$?

    assert_eq "--help exits 0" "0" "$exit_code"

    # Assert: output contains usage information (case-insensitive match on "usage" or "options")
    local lower_output
    lower_output=$(printf '%s' "$help_output" | tr '[:upper:]' '[:lower:]')
    local usage_found="no"
    case "$lower_output" in
        *"usage"*|*"options"*|*"--type"*|*"--status"*)
            usage_found="yes"
            ;;
    esac
    assert_eq "--help output contains usage information" "yes" "$usage_found"

    assert_pass_if_clean "test_help_flag_prints_usage_and_exits_0"
}
test_help_flag_prints_usage_and_exits_0

# ── Test 15: ticket list output includes preconditions_summary field ──────────
echo "Test 15: ticket list output includes preconditions_summary field for each ticket"
test_ticket_list_preconditions_summary_field() {
    _snapshot_fail

    if [ ! -f "$TICKET_LIST_SCRIPT" ]; then
        assert_eq "ticket-list.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_list_preconditions_summary_field"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # Create a story ticket
    local ticket_id=""
    ticket_id=$(_create_ticket "$repo" story "preconditions list test story")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for preconditions_summary field check" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_list_preconditions_summary_field"
        return
    fi

    # List tickets
    local list_output
    local exit_code=0
    list_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || exit_code=$?
    assert_eq "ticket list exits 0 for preconditions_summary check" "0" "$exit_code"

    # Assert: each ticket entry has a preconditions_summary field
    # (RED: reducer does not yet emit preconditions_summary)
    local field_check
    field_check=$(python3 -c "
import json, sys
try:
    tickets = json.loads(sys.argv[1])
    if not tickets:
        print('NO_TICKETS')
        sys.exit(0)
    missing = [t.get('ticket_id','?') for t in tickets if 'preconditions_summary' not in t]
    if missing:
        print('MISSING:' + ','.join(missing))
    else:
        print('OK')
except Exception as e:
    print(f'PARSE_ERROR:{e}')
" "$list_output" 2>/dev/null || echo "PARSE_ERROR")
    assert_eq "ticket list entries contain preconditions_summary field" "OK" "$field_check"

    assert_pass_if_clean "test_ticket_list_preconditions_summary_field"
}
test_ticket_list_preconditions_summary_field

print_summary
