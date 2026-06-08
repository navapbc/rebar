#!/usr/bin/env bash
# tests/scripts/test-ticket-link.sh
# RED tests for ticket-link.sh — `ticket link` and `ticket unlink` subcommands.
#
# All test functions MUST FAIL until ticket-link.sh is implemented.
# Covers:
#   1. ticket link <id1> <id2> blocks   → LINK event in id1 dir with correct fields
#   2. ticket link <id2> <id1> depends_on → LINK event in id2 dir
#   3. ticket unlink <id1> <id2>        → UNLINK event referencing original LINK uuid
#   4. Linking to nonexistent ticket exits nonzero
#   5. Duplicate link is idempotent (no duplicate LINK event on second call)
#   6. ticket link with <2 args exits nonzero with usage message
#   7. ticket link <id1> <id2> relates_to → bidirectional LINK events in both dirs
#
# NOTE: Cycle detection tests are NOT in this file — cycle detection is in
# ticket-graph.py (dso-dr38) and tested in test_ticket_graph.py (dso-zej9).
#
# Usage: bash tests/scripts/test-ticket-link.sh
# Returns: exit non-zero (RED) until ticket-link.sh is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_LINK_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-link.sh"
HASH_SCRIPT="$REPO_ROOT/src/rebar/_engine/compute-verdict-hash.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-link.sh ==="

_verdict_hash() {
    local repo="$1" ticket_id="$2"
    (cd "$repo" && PROJECT_ROOT="$repo" bash "$HASH_SCRIPT" "$ticket_id" PASS 2>/dev/null)
}

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

# ── Helper: count LINK event files in a ticket directory ─────────────────────
_count_link_events() {
    local tracker_dir="$1"
    local ticket_id="$2"
    find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-LINK.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' '
}

# ── Helper: count UNLINK event files in a ticket directory ───────────────────
_count_unlink_events() {
    local tracker_dir="$1"
    local ticket_id="$2"
    find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-UNLINK.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' '
}

# ── Test 1: ticket link <id1> <id2> blocks — LINK event written in id1 dir ────
echo "Test 1: ticket link <id1> <id2> blocks creates LINK event with correct fields in id1 dir"
test_ticket_link_blocks() {
    _snapshot_fail

    # RED: ticket-link.sh must not exist yet
    if [ ! -f "$TICKET_LINK_SCRIPT" ]; then
        assert_eq "ticket-link.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_link_blocks"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1 id2
    id1=$(_create_ticket "$repo" task "Source ticket")
    id2=$(_create_ticket "$repo" task "Target ticket")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "tickets created for link test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_link_blocks"
        return
    fi

    local before_count
    before_count=$(_count_link_events "$tracker_dir" "$id1")

    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$id2" blocks 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "ticket link blocks: exits 0" "0" "$exit_code"

    # Assert: exactly one new LINK event file written in id1 dir
    local after_count
    after_count=$(_count_link_events "$tracker_dir" "$id1")
    local new_events
    new_events=$(( after_count - before_count ))
    assert_eq "ticket link blocks: one LINK event written in id1 dir" "1" "$new_events"

    # Assert: LINK event has correct schema fields
    local link_file
    link_file=$(find "$tracker_dir/$id1" -maxdepth 1 -name '*-LINK.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ -z "$link_file" ]; then
        assert_eq "LINK event file found in id1 dir" "found" "not-found"
        assert_pass_if_clean "test_ticket_link_blocks"
        return
    fi

    local field_check
    field_check=$(python3 - "$link_file" "$id2" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

target_id = sys.argv[2]
errors = []

# Base schema
if ev.get('event_type') != 'LINK':
    errors.append(f"event_type not LINK: {ev.get('event_type')!r}")
if not isinstance(ev.get('timestamp'), int):
    errors.append(f"timestamp not int: {type(ev.get('timestamp'))}")
if not isinstance(ev.get('uuid'), str) or not ev.get('uuid'):
    errors.append(f"uuid missing or not str: {ev.get('uuid')!r}")
if not isinstance(ev.get('env_id'), str) or not ev.get('env_id'):
    errors.append(f"env_id missing or not str: {ev.get('env_id')!r}")
if not isinstance(ev.get('author'), str) or not ev.get('author'):
    errors.append(f"author missing or not str: {ev.get('author')!r}")

# LINK-specific data fields
data = ev.get('data', {})
if not isinstance(data, dict):
    errors.append(f"data not dict: {type(data)}")
else:
    if data.get('relation') != 'blocks':
        errors.append(f"data.relation not 'blocks': {data.get('relation')!r}")
    if data.get('target_id') != target_id:
        errors.append(f"data.target_id wrong: {data.get('target_id')!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(2)
else:
    print("OK")
PYEOF
) || true

    if [ "$field_check" = "OK" ]; then
        assert_eq "LINK event has correct schema (event_type, data.relation, data.target_id)" "OK" "OK"
    else
        assert_eq "LINK event has correct schema (event_type, data.relation, data.target_id)" "OK" "$field_check"
    fi

    assert_pass_if_clean "test_ticket_link_blocks"
}
test_ticket_link_blocks

# ── Test 2: ticket link <id2> <id1> depends_on — LINK event in id2 dir ────────
echo "Test 2: ticket link <id2> <id1> depends_on creates LINK event in id2 dir"
test_ticket_link_depends_on() {
    _snapshot_fail

    # RED: ticket-link.sh must not exist yet
    if [ ! -f "$TICKET_LINK_SCRIPT" ]; then
        assert_eq "ticket-link.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_link_depends_on"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1 id2
    id1=$(_create_ticket "$repo" task "Upstream ticket")
    id2=$(_create_ticket "$repo" task "Dependent ticket")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "tickets created for depends_on test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_link_depends_on"
        return
    fi

    local before_count
    before_count=$(_count_link_events "$tracker_dir" "$id2")

    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id2" "$id1" depends_on 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "ticket link depends_on: exits 0" "0" "$exit_code"

    # Assert: LINK event written in id2 dir (the source of the relationship)
    local after_count
    after_count=$(_count_link_events "$tracker_dir" "$id2")
    local new_events
    new_events=$(( after_count - before_count ))
    assert_eq "ticket link depends_on: one LINK event written in id2 dir" "1" "$new_events"

    # Assert: LINK event data.relation = depends_on and data.target_id = id1
    local link_file
    link_file=$(find "$tracker_dir/$id2" -maxdepth 1 -name '*-LINK.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ -z "$link_file" ]; then
        assert_eq "LINK event file found in id2 dir" "found" "not-found"
        assert_pass_if_clean "test_ticket_link_depends_on"
        return
    fi

    local field_check
    field_check=$(python3 - "$link_file" "$id1" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

target_id = sys.argv[2]
errors = []
data = ev.get('data', {})
if not isinstance(data, dict):
    errors.append(f"data not dict: {type(data)}")
else:
    if data.get('relation') != 'depends_on':
        errors.append(f"data.relation not 'depends_on': {data.get('relation')!r}")
    if data.get('target_id') != target_id:
        errors.append(f"data.target_id wrong: expected {target_id!r}, got {data.get('target_id')!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(2)
else:
    print("OK")
PYEOF
) || true

    if [ "$field_check" = "OK" ]; then
        assert_eq "LINK event depends_on: correct relation and target_id" "OK" "OK"
    else
        assert_eq "LINK event depends_on: correct relation and target_id" "OK" "$field_check"
    fi

    assert_pass_if_clean "test_ticket_link_depends_on"
}
test_ticket_link_depends_on

# ── Test 3: ticket unlink <id1> <id2> — UNLINK event referencing LINK uuid ────
echo "Test 3: ticket unlink <id1> <id2> creates UNLINK event referencing original LINK uuid via data.link_uuid"
test_ticket_unlink_references_link_uuid() {
    _snapshot_fail

    # RED: ticket-link.sh must not exist yet
    if [ ! -f "$TICKET_LINK_SCRIPT" ]; then
        assert_eq "ticket-link.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_unlink_references_link_uuid"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1 id2
    id1=$(_create_ticket "$repo" task "Link source")
    id2=$(_create_ticket "$repo" task "Link target")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "tickets created for unlink test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_unlink_references_link_uuid"
        return
    fi

    # First create a link
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$id2" blocks 2>/dev/null) || true

    # Grab the LINK event uuid
    local link_file
    link_file=$(find "$tracker_dir/$id1" -maxdepth 1 -name '*-LINK.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ -z "$link_file" ]; then
        assert_eq "LINK event exists before unlink" "found" "not-found"
        assert_pass_if_clean "test_ticket_unlink_references_link_uuid"
        return
    fi

    local link_uuid
    link_uuid=$(python3 -c "
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    ev = json.load(f)
print(ev.get('uuid', ''))
" "$link_file" 2>/dev/null) || link_uuid=""

    local before_unlink_count
    before_unlink_count=$(_count_unlink_events "$tracker_dir" "$id1")

    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" unlink "$id1" "$id2" 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "ticket unlink: exits 0" "0" "$exit_code"

    # Assert: exactly one UNLINK event written in id1 dir
    local after_unlink_count
    after_unlink_count=$(_count_unlink_events "$tracker_dir" "$id1")
    local new_events
    new_events=$(( after_unlink_count - before_unlink_count ))
    assert_eq "ticket unlink: one UNLINK event written" "1" "$new_events"

    # Assert: UNLINK event contains data.link_uuid referencing the original LINK uuid
    local unlink_file
    unlink_file=$(find "$tracker_dir/$id1" -maxdepth 1 -name '*-UNLINK.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ -z "$unlink_file" ]; then
        assert_eq "UNLINK event file found" "found" "not-found"
        assert_pass_if_clean "test_ticket_unlink_references_link_uuid"
        return
    fi

    local unlink_check
    unlink_check=$(python3 - "$unlink_file" "$link_uuid" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

expected_link_uuid = sys.argv[2]
errors = []

if ev.get('event_type') != 'UNLINK':
    errors.append(f"event_type not UNLINK: {ev.get('event_type')!r}")

data = ev.get('data', {})
if not isinstance(data, dict):
    errors.append(f"data not dict: {type(data)}")
else:
    actual_link_uuid = data.get('link_uuid', '')
    if not actual_link_uuid:
        errors.append("data.link_uuid missing or empty")
    elif actual_link_uuid != expected_link_uuid:
        errors.append(f"data.link_uuid mismatch: expected {expected_link_uuid!r}, got {actual_link_uuid!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(2)
else:
    print("OK")
PYEOF
) || true

    if [ "$unlink_check" = "OK" ]; then
        assert_eq "UNLINK event has event_type=UNLINK and data.link_uuid matches original LINK uuid" "OK" "OK"
    else
        assert_eq "UNLINK event has event_type=UNLINK and data.link_uuid matches original LINK uuid" "OK" "$unlink_check"
    fi

    assert_pass_if_clean "test_ticket_unlink_references_link_uuid"
}
test_ticket_unlink_references_link_uuid

# ── Test 4: linking to a nonexistent ticket exits nonzero ─────────────────────
echo "Test 4: ticket link to nonexistent target ticket exits nonzero"
test_ticket_link_nonexistent_target() {
    _snapshot_fail

    # RED: ticket-link.sh must not exist yet
    if [ ! -f "$TICKET_LINK_SCRIPT" ]; then
        assert_eq "ticket-link.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_link_nonexistent_target"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1
    id1=$(_create_ticket "$repo" task "Real source ticket")

    if [ -z "$id1" ]; then
        assert_eq "ticket created for nonexistent target test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_link_nonexistent_target"
        return
    fi

    local fake_id="xxxx-0000"
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$fake_id" blocks 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "link nonexistent target: exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error message printed (not silent)
    if [ -n "$stderr_out" ]; then
        assert_eq "link nonexistent target: error message printed" "has-message" "has-message"
    else
        assert_eq "link nonexistent target: error message printed" "has-message" "silent"
    fi

    # Assert: no LINK event written for id1
    local link_count
    link_count=$(_count_link_events "$tracker_dir" "$id1")
    assert_eq "link nonexistent target: no LINK event written" "0" "$link_count"

    assert_pass_if_clean "test_ticket_link_nonexistent_target"
}
test_ticket_link_nonexistent_target

# ── Test 5: duplicate link is idempotent — no duplicate LINK event ─────────────
echo "Test 5: duplicate link (same pair, same relation) is idempotent — no second LINK event written"
test_ticket_link_idempotent() {
    _snapshot_fail

    # RED: ticket-link.sh must not exist yet
    if [ ! -f "$TICKET_LINK_SCRIPT" ]; then
        assert_eq "ticket-link.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_link_idempotent"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1 id2
    id1=$(_create_ticket "$repo" task "Idempotent source")
    id2=$(_create_ticket "$repo" task "Idempotent target")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "tickets created for idempotent test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_link_idempotent"
        return
    fi

    # First link call
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$id2" blocks 2>/dev/null) || true

    local after_first
    after_first=$(_count_link_events "$tracker_dir" "$id1")

    # Second call: identical pair and relation
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$id2" blocks 2>/dev/null) || exit_code=$?

    local after_second
    after_second=$(_count_link_events "$tracker_dir" "$id1")

    # Assert: exits 0 on second call (idempotent, not an error)
    assert_eq "duplicate link: second call exits 0" "0" "$exit_code"

    # Assert: no additional LINK event written on second call
    assert_eq "duplicate link: LINK event count unchanged after second call" "$after_first" "$after_second"

    assert_pass_if_clean "test_ticket_link_idempotent"
}
test_ticket_link_idempotent

# ── Test 6: ticket link with <2 args exits nonzero with usage message ──────────
echo "Test 6: ticket link with fewer than 2 args exits nonzero with a usage message"
test_ticket_link_too_few_args() {
    _snapshot_fail

    # RED: ticket-link.sh must not exist yet
    if [ ! -f "$TICKET_LINK_SCRIPT" ]; then
        assert_eq "ticket-link.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_link_too_few_args"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    # Call with only 1 arg (just a ticket id, no target)
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" link "some-id" 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "link too few args: exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: usage message printed (mentions "usage" or "Usage" or argument info)
    if [[ "${stderr_out,,}" =~ usage|'<id'|ticket_id|source|target ]]; then
        assert_eq "link too few args: usage message printed" "has-usage" "has-usage"
    else
        assert_eq "link too few args: usage message printed" "has-usage" "no-usage: $stderr_out"
    fi

    # Call with zero args
    local exit_code2=0
    local stderr_out2
    stderr_out2=$(cd "$repo" && bash "$TICKET_SCRIPT" link 2>&1) || exit_code2=$?

    # Assert: exits non-zero
    assert_eq "link zero args: exits non-zero" "1" "$([ "$exit_code2" -ne 0 ] && echo 1 || echo 0)"

    assert_pass_if_clean "test_ticket_link_too_few_args"
}
test_ticket_link_too_few_args

# ── Test 7: relates_to creates bidirectional LINK events in both dirs ──────────
echo "Test 7: ticket link <id1> <id2> relates_to creates LINK events in both id1 and id2 dirs (bidirectional)"
test_ticket_link_relates_to_bidirectional() {
    _snapshot_fail

    # RED: ticket-link.sh must not exist yet
    if [ ! -f "$TICKET_LINK_SCRIPT" ]; then
        assert_eq "ticket-link.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_link_relates_to_bidirectional"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1 id2
    id1=$(_create_ticket "$repo" task "Relates source")
    id2=$(_create_ticket "$repo" task "Relates target")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "tickets created for relates_to test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_link_relates_to_bidirectional"
        return
    fi

    local before_id1
    before_id1=$(_count_link_events "$tracker_dir" "$id1")
    local before_id2
    before_id2=$(_count_link_events "$tracker_dir" "$id2")

    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$id2" relates_to 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "ticket link relates_to: exits 0" "0" "$exit_code"

    # Assert: LINK event written in id1 dir
    local after_id1
    after_id1=$(_count_link_events "$tracker_dir" "$id1")
    local new_id1
    new_id1=$(( after_id1 - before_id1 ))
    assert_eq "relates_to: LINK event written in id1 dir" "1" "$new_id1"

    # Assert: LINK event written in id2 dir (bidirectional)
    local after_id2
    after_id2=$(_count_link_events "$tracker_dir" "$id2")
    local new_id2
    new_id2=$(( after_id2 - before_id2 ))
    assert_eq "relates_to: LINK event written in id2 dir (bidirectional)" "1" "$new_id2"

    # Assert: id1's LINK event has relation=relates_to and target_id=id2
    local link_file_id1
    link_file_id1=$(find "$tracker_dir/$id1" -maxdepth 1 -name '*-LINK.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ -n "$link_file_id1" ]; then
        local id1_check
        id1_check=$(python3 - "$link_file_id1" "$id2" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)
target_id = sys.argv[2]
data = ev.get('data', {})
errors = []
if data.get('relation') != 'relates_to':
    errors.append(f"relation not relates_to: {data.get('relation')!r}")
if data.get('target_id') != target_id:
    errors.append(f"target_id wrong: {data.get('target_id')!r}")
print("ERRORS:" + "; ".join(errors) if errors else "OK")
PYEOF
) || true
        if [ "$id1_check" = "OK" ]; then
            assert_eq "relates_to: id1 LINK event has correct relation and target_id" "OK" "OK"
        else
            assert_eq "relates_to: id1 LINK event has correct relation and target_id" "OK" "$id1_check"
        fi
    else
        assert_eq "relates_to: id1 LINK event file found" "found" "not-found"
    fi

    # Assert: id2's LINK event has relation=relates_to and target_id=id1 (reverse direction)
    local link_file_id2
    link_file_id2=$(find "$tracker_dir/$id2" -maxdepth 1 -name '*-LINK.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ -n "$link_file_id2" ]; then
        local id2_check
        id2_check=$(python3 - "$link_file_id2" "$id1" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)
target_id = sys.argv[2]
data = ev.get('data', {})
errors = []
if data.get('relation') != 'relates_to':
    errors.append(f"relation not relates_to: {data.get('relation')!r}")
if data.get('target_id') != target_id:
    errors.append(f"target_id wrong: expected {target_id!r}, got {data.get('target_id')!r}")
print("ERRORS:" + "; ".join(errors) if errors else "OK")
PYEOF
) || true
        if [ "$id2_check" = "OK" ]; then
            assert_eq "relates_to: id2 LINK event has correct relation and target_id (reverse)" "OK" "OK"
        else
            assert_eq "relates_to: id2 LINK event has correct relation and target_id (reverse)" "OK" "$id2_check"
        fi
    else
        assert_eq "relates_to: id2 LINK event file found (bidirectional)" "found" "not-found"
    fi

    assert_pass_if_clean "test_ticket_link_relates_to_bidirectional"
}
test_ticket_link_relates_to_bidirectional

# ── Test 8: unlinking an already-unlinked pair exits nonzero (no dangling UNLINK) ─
echo "Test 8: ticket unlink on already-unlinked pair exits nonzero without writing a dangling UNLINK event"
test_ticket_unlink_already_unlinked() {
    _snapshot_fail

    # RED: ticket-link.sh must not exist yet
    if [ ! -f "$TICKET_LINK_SCRIPT" ]; then
        assert_eq "ticket-link.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_unlink_already_unlinked"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1 id2
    id1=$(_create_ticket "$repo" task "Double-unlink source")
    id2=$(_create_ticket "$repo" task "Double-unlink target")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "tickets created for double-unlink test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_unlink_already_unlinked"
        return
    fi

    # Link then unlink once
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$id2" blocks 2>/dev/null) || true
    (cd "$repo" && bash "$TICKET_SCRIPT" unlink "$id1" "$id2" 2>/dev/null) || true

    local unlink_count_after_first
    unlink_count_after_first=$(_count_unlink_events "$tracker_dir" "$id1")

    # Second unlink call on already-unlinked pair — should exit nonzero
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" unlink "$id1" "$id2" 2>&1) || exit_code=$?

    # Assert: exits non-zero (not a silent no-op — dangling UNLINK is an error)
    assert_eq "double-unlink: second unlink exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: no additional UNLINK event written
    local unlink_count_after_second
    unlink_count_after_second=$(_count_unlink_events "$tracker_dir" "$id1")
    assert_eq "double-unlink: UNLINK event count unchanged after second unlink" "$unlink_count_after_first" "$unlink_count_after_second"

    # Assert: informative error message printed
    if [ -n "$stderr_out" ]; then
        assert_eq "double-unlink: error message printed" "has-message" "has-message"
    else
        assert_eq "double-unlink: error message printed" "has-message" "silent"
    fi

    assert_pass_if_clean "test_ticket_unlink_already_unlinked"
}
test_ticket_unlink_already_unlinked

# ── Test 9: same-second LINK+UNLINK — sort order must not allow UNLINK before LINK ─
echo "Test 9: same-second LINK+UNLINK event filenames always replay LINK before UNLINK regardless of UUID sort order"
test_ticket_link_unlink_same_second_ordering() {
    _snapshot_fail

    # RED: ticket-link.sh must not exist yet
    if [ ! -f "$TICKET_LINK_SCRIPT" ]; then
        assert_eq "ticket-link.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_link_unlink_same_second_ordering"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1 id2
    id1=$(_create_ticket "$repo" task "Same-second source")
    id2=$(_create_ticket "$repo" task "Same-second target")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "tickets created for same-second ordering test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_link_unlink_same_second_ordering"
        return
    fi

    # Write a real LINK event first to get a valid link_uuid
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$id2" blocks 2>/dev/null) || true
    (cd "$repo" && bash "$TICKET_SCRIPT" unlink "$id1" "$id2" 2>/dev/null) || true

    # Now craft a scenario where we manually rename the event files to share the same
    # timestamp second and use UUIDs where UNLINK's UUID sorts before LINK's UUID.
    # This directly exercises the filename sort-order bug.
    local result
    result=$(python3 - "$tracker_dir" "$id1" "$id2" <<'PYEOF'
import json, sys, pathlib, shutil

tracker_dir = pathlib.Path(sys.argv[1])
source_id = sys.argv[2]
target_id = sys.argv[3]

ticket_dir = tracker_dir / source_id

# Find existing LINK and UNLINK event files
link_files = sorted(ticket_dir.glob('*-LINK.json'))
unlink_files = sorted(ticket_dir.glob('*-UNLINK.json'))

if not link_files or not unlink_files:
    print("SETUP_ERROR: no LINK or UNLINK events found after link+unlink")
    sys.exit(1)

link_file = link_files[-1]
unlink_file = unlink_files[-1]

# Craft filenames where UNLINK sorts before LINK (same timestamp, unlink_uuid < link_uuid alpha)
# Use a fixed timestamp and controlled UUIDs to guarantee the bad ordering
same_ts = 1000000000
# '00000000-...' < 'ffffffff-...' alphabetically, so UNLINK sorts before LINK
crafted_link_name   = f"{same_ts}-ffffffff-0000-0000-0000-000000000000-LINK.json"
crafted_unlink_name = f"{same_ts}-00000000-0000-0000-0000-000000000000-UNLINK.json"

crafted_link_path   = ticket_dir / crafted_link_name
crafted_unlink_path = ticket_dir / crafted_unlink_name

shutil.copy2(link_file, crafted_link_path)
shutil.copy2(unlink_file, crafted_unlink_path)

# Remove the original files to make crafted files the only events
link_file.unlink()
unlink_file.unlink()

# Verify: sorted order puts UNLINK before LINK
all_files = sorted(f.name for f in ticket_dir.glob('*-*.json') if not f.name.startswith('.'))
if not all_files:
    print("SETUP_ERROR: no event files after crafting")
    sys.exit(1)

first = all_files[0]
if first.endswith('-UNLINK.json'):
    print(f"ORDERING_CONFIRMED: UNLINK sorts before LINK — {all_files}")
else:
    print(f"ORDERING_REVERSED: LINK sorts before UNLINK — {all_files}")
sys.exit(0)
PYEOF
)

    # Confirm the crafted files produce the bad sort order (prerequisite for the test)
    if [[ "$result" == *"ORDERING_CONFIRMED"* ]]; then
        assert_eq "same-second ordering: crafted files produce UNLINK-before-LINK sort order" "ORDERING_CONFIRMED" "ORDERING_CONFIRMED"
    elif [[ "$result" == *"ORDERING_REVERSED"* ]]; then
        # Crafted UUIDs don't produce the bad order — skip (precondition invalid)
        assert_eq "same-second ordering: crafted files produce UNLINK-before-LINK sort order (precondition)" "ORDERING_CONFIRMED" "ORDERING_REVERSED_PRECONDITION_INVALID"
        assert_pass_if_clean "test_ticket_link_unlink_same_second_ordering"
        return
    else
        assert_eq "same-second ordering: setup succeeded" "ORDERING_CONFIRMED" "$result"
        assert_pass_if_clean "test_ticket_link_unlink_same_second_ordering"
        return
    fi

    # Now attempt a second unlink — with the bad sort order, the link appears active again,
    # so the unlink would incorrectly SUCCEED (exit 0) and write a second UNLINK event.
    # After the fix, this must exit non-zero (link is net-inactive).
    local exit_code=0
    local unlink_count_before
    unlink_count_before=$(_count_unlink_events "$tracker_dir" "$id1")
    (cd "$repo" && bash "$TICKET_SCRIPT" unlink "$id1" "$id2" 2>/dev/null) || exit_code=$?

    # With the bug present: exits 0 (falsely treats link as active due to bad sort order)
    # With the fix present: exits non-zero (correctly detects link is net-inactive)
    assert_eq "same-second ordering: second unlink exits non-zero (link is net-inactive)" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: no additional UNLINK event written
    local unlink_count_after
    unlink_count_after=$(_count_unlink_events "$tracker_dir" "$id1")
    assert_eq "same-second ordering: UNLINK count unchanged after failed second unlink" "$unlink_count_before" "$unlink_count_after"

    assert_pass_if_clean "test_ticket_link_unlink_same_second_ordering"
}
test_ticket_link_unlink_same_second_ordering

# ── Test 10 (RED): depends_on link to closed target is blocked ─────────────────
echo "Test 10 (RED): ticket link depends_on to a closed target ticket exits non-zero"
test_link_depends_on_closed_target_blocked() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create source ticket (will depend on target)
    local source_id
    source_id=$(_create_ticket "$repo" task "Source ticket for depends_on guard")

    # Create and close target ticket
    local target_id
    target_id=$(_create_ticket "$repo" task "Target ticket that will be closed")

    if [ -z "$source_id" ] || [ -z "$target_id" ]; then
        assert_eq "tickets created for closed-target depends_on test" "non-empty" "empty"
        assert_pass_if_clean "test_link_depends_on_closed_target_blocked"
        return
    fi

    # Close the target ticket
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$target_id" open closed 2>/dev/null) || true

    # Verify target is actually closed
    local target_status
    target_status=$(python3 "$REPO_ROOT/src/rebar/_engine/ticket-reducer.py" \
        "$tracker_dir/$target_id" 2>/dev/null \
        | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('status',''))" 2>/dev/null) || true

    if [ "$target_status" != "closed" ]; then
        assert_eq "link-closed-target: target is closed before test" "closed" "$target_status"
        assert_pass_if_clean "test_link_depends_on_closed_target_blocked"
        return
    fi

    # Attempt to link source depends_on closed target — must exit non-zero
    # RED: current ticket-link.sh does not enforce this guard → exits 0
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" link "$source_id" "$target_id" depends_on 2>&1) || exit_code=$?

    # Assert: exits non-zero (guard not yet implemented → currently exits 0, so FAILS RED)
    assert_eq "link-depends_on-closed-target: exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error message mentions closed or not allowed
    if [[ "${stderr_out,,}" =~ closed|not\ allowed|cannot|target ]]; then
        assert_eq "link-depends_on-closed-target: error mentions closed target" "has-closed-hint" "has-closed-hint"
    else
        assert_eq "link-depends_on-closed-target: error mentions closed target" "has-closed-hint" "no-hint: $stderr_out"
    fi

    # Assert: no LINK event was written in source_id dir
    local link_count
    link_count=$(_count_link_events "$tracker_dir" "$source_id")
    assert_eq "link-depends_on-closed-target: no LINK event written" "0" "$link_count"

    assert_pass_if_clean "test_link_depends_on_closed_target_blocked"
}
test_link_depends_on_closed_target_blocked

# ── Test 11 (RED): relates_to link to closed target is allowed ────────────────
echo "Test 11 (RED): ticket link relates_to to a closed target ticket exits 0 (allowed)"
test_link_relates_to_closed_target_allowed() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create source and target tickets
    local source_id
    source_id=$(_create_ticket "$repo" task "Source for relates_to closed-target test")

    local target_id
    target_id=$(_create_ticket "$repo" task "Closed target for relates_to test")

    if [ -z "$source_id" ] || [ -z "$target_id" ]; then
        assert_eq "tickets created for relates_to closed-target test" "non-empty" "empty"
        assert_pass_if_clean "test_link_relates_to_closed_target_allowed"
        return
    fi

    # Close the target ticket
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$target_id" open closed 2>/dev/null) || true

    # Verify target is closed
    local target_status
    target_status=$(python3 "$REPO_ROOT/src/rebar/_engine/ticket-reducer.py" \
        "$tracker_dir/$target_id" 2>/dev/null \
        | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('status',''))" 2>/dev/null) || true

    if [ "$target_status" != "closed" ]; then
        assert_eq "relates_to-closed-target: target is closed before test" "closed" "$target_status"
        assert_pass_if_clean "test_link_relates_to_closed_target_allowed"
        return
    fi

    # Link source relates_to closed target — must exit 0 (relates_to is not blocked)
    # This is the ALLOW path: relates_to should work even when target is closed.
    # RED: this test may currently pass (ticket-link.sh has no guards at all → exits 0).
    # After guard implementation, it must STILL exit 0 (relates_to bypasses the guard).
    local before_count
    before_count=$(_count_link_events "$tracker_dir" "$source_id")

    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" link "$source_id" "$target_id" relates_to 2>&1) || exit_code=$?

    # Assert: exits 0 (relates_to to closed ticket is explicitly allowed)
    assert_eq "link-relates_to-closed-target: exits 0" "0" "$exit_code"

    # Assert: a LINK event was written (link was actually created)
    local after_count
    after_count=$(_count_link_events "$tracker_dir" "$source_id")
    local new_events
    new_events=$(( after_count - before_count ))
    assert_eq "link-relates_to-closed-target: LINK event written" "1" "$new_events"

    assert_pass_if_clean "test_link_relates_to_closed_target_allowed"
}
test_link_relates_to_closed_target_allowed

# ── Test 12 (RED): cannot create LINK from a closed source ticket ──────────────
echo "Test 12 (RED): ticket link <closed_source> <target> blocks exits non-zero"
test_link_blocks_from_closed_source_blocked() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create source (to be closed) and target tickets
    local source_id
    source_id=$(_create_ticket "$repo" story "Story to be closed (blocks source)")

    local target_id
    target_id=$(_create_ticket "$repo" task "Open task (blocks target)")

    if [ -z "$source_id" ] || [ -z "$target_id" ]; then
        assert_eq "tickets created for blocks-from-closed-source test" "non-empty" "empty"
        assert_pass_if_clean "test_link_blocks_from_closed_source_blocked"
        return
    fi

    # Close the source ticket (story — requires verdict hash)
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$source_id" open closed --verdict-hash="$(_verdict_hash "$repo" "$source_id")" 2>/dev/null) || true

    # Verify source is actually closed
    local source_status
    source_status=$(python3 "$REPO_ROOT/src/rebar/_engine/ticket-reducer.py" \
        "$tracker_dir/$source_id" 2>/dev/null \
        | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('status',''))" 2>/dev/null) || true

    if [ "$source_status" != "closed" ]; then
        assert_eq "link-blocks-from-closed-source: source is closed before test" "closed" "$source_status"
        assert_pass_if_clean "test_link_blocks_from_closed_source_blocked"
        return
    fi

    # Attempt to link closed source blocks open target — must exit non-zero
    # BUG: current ticket-link.sh only checks depends_on target status, not
    # the source ticket status. Writing LINK events from a closed source ticket
    # is allowed and bypasses the closed-ticket guard.
    # FIX: any LINK event written to a closed source ticket must be rejected.
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" link "$source_id" "$target_id" blocks 2>&1) || exit_code=$?

    # Assert: exits non-zero (guard not yet implemented → currently exits 0, so FAILS RED)
    assert_eq "link-blocks-from-closed-source: exits non-zero" "1" \
        "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error message mentions closed or source
    if [[ "${stderr_out,,}" =~ closed|cannot|not\ allowed|source ]]; then
        assert_eq "link-blocks-from-closed-source: error mentions closed source" "has-closed-hint" "has-closed-hint"
    else
        assert_eq "link-blocks-from-closed-source: error mentions closed source" "has-closed-hint" "no-hint: $stderr_out"
    fi

    # Assert: no LINK event was written in source_id dir
    local link_count
    link_count=$(_count_link_events "$tracker_dir" "$source_id")
    assert_eq "link-blocks-from-closed-source: no LINK event written" "0" "$link_count"

    assert_pass_if_clean "test_link_blocks_from_closed_source_blocked"
}
test_link_blocks_from_closed_source_blocked

# ── Test 13: --dry-run prints preview without writing events ──────────────────
test_link_dry_run_no_event_written() {
    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1 id2
    id1=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Source ticket" 2>/dev/null | grep -o '[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}' | head -1)
    id2=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Target ticket" 2>/dev/null | grep -o '[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}' | head -1)

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "dry-run: tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_link_dry_run_no_event_written"
        return
    fi

    # Run link --dry-run — must exit 0 and print [DRY RUN] message
    local exit_code=0
    local stdout_out
    stdout_out=$(cd "$repo" && bash "$TICKET_LINK_SCRIPT" link "$id1" "$id2" depends_on --dry-run 2>/dev/null) || exit_code=$?

    assert_eq "dry-run: exits 0" "0" "$exit_code"

    # Assert: output contains [DRY RUN]
    if [[ "$stdout_out" =~ \[DRY\ RUN\] ]]; then
        assert_eq "dry-run: output contains [DRY RUN]" "has-dry-run" "has-dry-run"
    else
        assert_eq "dry-run: output contains [DRY RUN]" "has-dry-run" "missing: $stdout_out"
    fi

    # Assert: no LINK event was written
    local link_count
    link_count=$(_count_link_events "$tracker_dir" "$id1")
    assert_eq "dry-run: no LINK event written" "0" "$link_count"

    assert_pass_if_clean "test_link_dry_run_no_event_written"
}
test_link_dry_run_no_event_written

# ── Test 14 (RED): LINK event is committed to the tickets branch ──────────────
echo "Test 14 (RED): ticket link event is committed to the tickets git branch (not just written to disk)"
test_ticket_link_event_is_committed() {
    _snapshot_fail

    # RED: ticket-link.sh must exist (this test depends on ticket-link.sh working)
    if [ ! -f "$TICKET_LINK_SCRIPT" ]; then
        assert_eq "ticket-link.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_link_event_is_committed"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1 id2
    id1=$(_create_ticket "$repo" task "Commit-check source ticket")
    id2=$(_create_ticket "$repo" task "Commit-check target ticket")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "tickets created for commit-check test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_link_event_is_committed"
        return
    fi

    # Record git log length before link
    local log_before
    log_before=$(git -C "$tracker_dir" log --oneline 2>/dev/null | wc -l | tr -d ' ')

    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$id2" blocks 2>/dev/null) || exit_code=$?

    # Assert: link exits 0
    assert_eq "commit-check: ticket link exits 0" "0" "$exit_code"

    # Assert: LINK event file exists on disk
    local link_file
    link_file=$(find "$tracker_dir/$id1" -maxdepth 1 -name '*-LINK.json' ! -name '.*' 2>/dev/null | sort | tail -1)
    if [ -n "$link_file" ]; then
        assert_eq "commit-check: LINK event file written to disk" "found" "found"
    else
        assert_eq "commit-check: LINK event file written to disk" "found" "not-found"
        assert_pass_if_clean "test_ticket_link_event_is_committed"
        return
    fi

    # Assert: git log has a new commit (LINK event was committed, not just written)
    local log_after
    log_after=$(git -C "$tracker_dir" log --oneline 2>/dev/null | wc -l | tr -d ' ')
    local new_commits
    new_commits=$(( log_after - log_before ))
    assert_eq "commit-check: LINK event is committed to tickets branch (new commit count)" "1" "$new_commits"

    # Assert: the new commit message references the ticket IDs (link commit message pattern)
    local last_commit_msg
    last_commit_msg=$(git -C "$tracker_dir" log --oneline -1 2>/dev/null)
    if [[ "$last_commit_msg" =~ link|LINK|$id1|$id2 ]]; then
        assert_eq "commit-check: commit message references link/ticket IDs" "has-link-ref" "has-link-ref"
    else
        assert_eq "commit-check: commit message references link/ticket IDs" "has-link-ref" "no-ref: $last_commit_msg"
    fi

    # Assert: the LINK event file is NOT in the untracked/modified state
    local untracked
    untracked=$(git -C "$tracker_dir" status --porcelain 2>/dev/null | grep -c "LINK.json" || true)
    assert_eq "commit-check: LINK event file not untracked/uncommitted" "0" "$untracked"

    assert_pass_if_clean "test_ticket_link_event_is_committed"
}
test_ticket_link_event_is_committed

# ── Test 15: relates_to creates two committed LINK events (bidirectional) ──────
echo "Test 15: ticket link relates_to produces two git commits (one per direction)"
test_ticket_link_relates_to_two_commits() {
    _snapshot_fail

    if [ ! -f "$TICKET_LINK_SCRIPT" ]; then
        assert_eq "ticket-link.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_link_relates_to_two_commits"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1 id2
    id1=$(_create_ticket "$repo" task "Relates-to source")
    id2=$(_create_ticket "$repo" task "Relates-to target")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "tickets created for relates_to commit test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_link_relates_to_two_commits"
        return
    fi

    local log_before
    log_before=$(git -C "$tracker_dir" log --oneline 2>/dev/null | wc -l | tr -d ' ')

    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$id2" relates_to 2>/dev/null) || exit_code=$?

    assert_eq "relates_to-commit: exits 0" "0" "$exit_code"

    local log_after
    log_after=$(git -C "$tracker_dir" log --oneline 2>/dev/null | wc -l | tr -d ' ')
    local new_commits
    new_commits=$(( log_after - log_before ))
    # relates_to writes _write_link_event twice (forward + reciprocal), each commits
    assert_eq "relates_to-commit: two LINK commits produced (forward + reciprocal)" "2" "$new_commits"

    # Both LINK event files must not be untracked/uncommitted
    local untracked
    untracked=$(git -C "$tracker_dir" status --porcelain 2>/dev/null | grep -c "LINK.json" || true)
    assert_eq "relates_to-commit: both LINK files committed (none untracked)" "0" "$untracked"

    assert_pass_if_clean "test_ticket_link_relates_to_two_commits"
}
test_ticket_link_relates_to_two_commits

# ── Test: orphan relates_to unlink succeeds (bug 2184-bae4 Symptom 1) ─────────
# When a relates_to link has only one side (orphan state — id2 has no reciprocal
# LINK event back to id1), `ticket unlink id1 id2` must succeed (exit 0) and
# remove id1's side of the link. The missing reciprocal should emit a warning
# but NOT fail the operation.
echo ""
echo "--- test_ticket_unlink_orphan_relates_to_succeeds ---"
test_ticket_unlink_orphan_relates_to_succeeds() {
    _snapshot_fail

    if [ ! -f "$TICKET_LINK_SCRIPT" ]; then
        assert_eq "ticket-link.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_unlink_orphan_relates_to_succeeds"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create two tickets
    local id1 id2
    id1=$(_create_ticket "$repo" "epic" "Orphan source epic")
    id2=$(_create_ticket "$repo" "epic" "Orphan target epic")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "tickets created for orphan unlink test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_unlink_orphan_relates_to_succeeds"
        return
    fi

    # Set up an orphan: write a LINK event in id1 dir only, not in id2 dir
    # We use ticket link then manually delete the reciprocal to simulate orphan state
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$id2" relates_to 2>/dev/null) || {
        assert_eq "initial relates_to link exits 0" "0" "1"
        assert_pass_if_clean "test_ticket_unlink_orphan_relates_to_succeeds"
        return
    }

    # Delete id2's LINK event file to create orphan state (only id1 has the LINK)
    local id2_link_file
    id2_link_file=$(find "$tracker_dir/$id2" -maxdepth 1 -name '*-LINK.json' 2>/dev/null | head -1)
    # Hard assertion: if find returned empty, the setup failed — the orphan path is not exercised
    if [ -z "$id2_link_file" ]; then
        assert_eq "test_ticket_unlink_orphan_relates_to_succeeds: id2 LINK file found for orphan setup" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_unlink_orphan_relates_to_succeeds"
        return
    fi
    rm -f "$id2_link_file"
    # Re-commit to tracker to reflect the deletion
    (git -C "$tracker_dir" add -A && git -C "$tracker_dir" commit -m "test: simulate orphan by removing id2 LINK" --allow-empty 2>/dev/null) || true

    # When: ticket unlink id1 id2 — one-sided orphan link
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" unlink "$id1" "$id2" 2>&1 >/dev/null) || exit_code=$?

    # Then: exits 0 (primary unlink succeeds; orphan reciprocal does not block)
    assert_eq "test_ticket_unlink_orphan_relates_to_succeeds: exit 0 for orphan unlink" "0" "$exit_code"

    # And: the orphan warning must appear in stderr (verifies orphan code path was reached)
    local warn_found=0
    echo "$stderr_out" | grep -q "orphaned link" && warn_found=1
    assert_eq "test_ticket_unlink_orphan_relates_to_succeeds: orphan warning emitted on stderr" "1" "$warn_found"

    # And: id1's LINK should now be cancelled (UNLINK event exists)
    local unlink_count
    unlink_count=$(_count_unlink_events "$tracker_dir" "$id1")
    assert_eq "test_ticket_unlink_orphan_relates_to_succeeds: UNLINK event written for id1" "1" "$unlink_count"

    assert_pass_if_clean "test_ticket_unlink_orphan_relates_to_succeeds"
}
test_ticket_unlink_orphan_relates_to_succeeds

# ── Test: non-orphan relates_to unlink removes both sides ─────────────────────
# When both tickets have reciprocal LINK events, `ticket unlink id1 id2` must
# write UNLINK events for BOTH sides (happy path — no orphan case).
echo ""
echo "--- test_ticket_unlink_relates_to_removes_both_sides ---"
test_ticket_unlink_relates_to_removes_both_sides() {
    _snapshot_fail

    if [ ! -f "$TICKET_LINK_SCRIPT" ]; then
        assert_eq "ticket-link.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_unlink_relates_to_removes_both_sides"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create two tickets and link them (creates reciprocal LINKs on both sides)
    local id1 id2
    id1=$(_create_ticket "$repo" "epic" "Source epic for both-sides unlink")
    id2=$(_create_ticket "$repo" "epic" "Target epic for both-sides unlink")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "tickets created for both-sides unlink test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_unlink_relates_to_removes_both_sides"
        return
    fi

    # Link id1 → id2 with relates_to (should create LINK on both sides)
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$id2" relates_to 2>/dev/null) || {
        assert_eq "initial relates_to link exits 0" "0" "1"
        assert_pass_if_clean "test_ticket_unlink_relates_to_removes_both_sides"
        return
    }

    # When: ticket unlink id1 id2 — both sides have LINK events
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" unlink "$id1" "$id2" 2>/dev/null) || exit_code=$?

    # Then: exits 0
    assert_eq "test_ticket_unlink_relates_to_removes_both_sides: exit 0" "0" "$exit_code"

    # And: id1 has an UNLINK event
    local unlink_count1
    unlink_count1=$(_count_unlink_events "$tracker_dir" "$id1")
    assert_eq "test_ticket_unlink_relates_to_removes_both_sides: UNLINK event written for id1" "1" "$unlink_count1"

    # And: id2 ALSO has an UNLINK event (reciprocal removal)
    local unlink_count2
    unlink_count2=$(_count_unlink_events "$tracker_dir" "$id2")
    assert_eq "test_ticket_unlink_relates_to_removes_both_sides: UNLINK event written for id2" "1" "$unlink_count2"

    assert_pass_if_clean "test_ticket_unlink_relates_to_removes_both_sides"
}
test_ticket_unlink_relates_to_removes_both_sides

# ── Test 16 (RED): canonical dispatcher --dry-run must NOT write a LINK event ──
# Covers the CANONICAL dispatcher path ($TICKET_SCRIPT link ... --dry-run),
# i.e. ticket_link() in ticket-lib-api.sh, NOT the legacy ticket-link.sh path.
# Bug 3796-ccd3-863f-4d63: ticket_link() ignores --dry-run and writes a LINK event.
echo ""
echo "--- test_canonical_dispatcher_dry_run_no_link_event ---"
test_canonical_dispatcher_dry_run_no_link_event() {
    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1 id2
    id1=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "DryRun canonical source" 2>/dev/null | grep -o '[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}' | head -1)
    id2=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "DryRun canonical target" 2>/dev/null | grep -o '[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}' | head -1)

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "canonical dry-run: tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_canonical_dispatcher_dry_run_no_link_event"
        return
    fi

    # Run via the CANONICAL dispatcher ($TICKET_SCRIPT, NOT $TICKET_LINK_SCRIPT)
    local exit_code=0
    local stdout_out
    stdout_out=$(cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$id2" relates_to --dry-run 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "canonical dry-run: exits 0" "0" "$exit_code"

    # Assert: output contains [DRY RUN] preview
    if [[ "$stdout_out" =~ \[DRY\ RUN\] ]]; then
        assert_eq "canonical dry-run: output contains [DRY RUN] preview" "has-dry-run" "has-dry-run"
    else
        assert_eq "canonical dry-run: output contains [DRY RUN] preview" "has-dry-run" "missing: $stdout_out"
    fi

    # Assert: NO LINK event written to disk (this is the failing assertion before the fix)
    local link_count
    link_count=$(_count_link_events "$tracker_dir" "$id1")
    assert_eq "canonical dry-run: no LINK event written for id1" "0" "$link_count"

    assert_pass_if_clean "test_canonical_dispatcher_dry_run_no_link_event"
}
test_canonical_dispatcher_dry_run_no_link_event

# ── Test (RED): non-canonical relation 'blocked_by' must be REJECTED (Bug 61b8) ──
# The bash dispatcher (ticket-link.sh) has a case guard, but the ticket-graph.py
# --link path (add_dependency in _links.py) accepted any string verbatim.
# Expected (after fix):
#   - ticket link <id1> <id2> blocked_by exits NONZERO
#   - No LINK event is written to disk for id1
echo ""
echo "--- test_ticket_link_rejects_non_canonical_relation ---"
test_ticket_link_rejects_non_canonical_relation() {
    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local id1 id2
    id1=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Relation-grammar source" 2>/dev/null | grep -o '[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}' | head -1)
    id2=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Relation-grammar target" 2>/dev/null | grep -o '[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}' | head -1)

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "relation-grammar: tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_link_rejects_non_canonical_relation"
        return
    fi

    local before_count
    before_count=$(_count_link_events "$tracker_dir" "$id1")

    # Run ticket link with non-canonical relation 'blocked_by'
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$id2" blocked_by 2>/dev/null) || exit_code=$?

    # Assert: exits NONZERO (relation is rejected)
    assert_ne "ticket link blocked_by: exits nonzero" "0" "$exit_code"

    # Assert: no LINK event written to id1 dir
    local after_count
    after_count=$(_count_link_events "$tracker_dir" "$id1")
    local new_events=$(( after_count - before_count ))
    assert_eq "ticket link blocked_by: no LINK event written" "0" "$new_events"

    assert_pass_if_clean "test_ticket_link_rejects_non_canonical_relation"
}
test_ticket_link_rejects_non_canonical_relation

# ── Test (RED f5a8): unlink succeeds after compaction bakes LINK into SNAPSHOT ──
# After ticket-compact.sh bakes a LINK event into a SNAPSHOT (compiled_state.deps[])
# and deletes the original *-LINK.json file, `ticket unlink A B` must still succeed.
# Before the fix: exits 1 with "no LINK event found" because _get_link_info only
# globs *-LINK.json and never reads the SNAPSHOT deps[].
# After the fix: reads the SNAPSHOT deps[], finds the link_uuid, and writes UNLINK.
echo ""
echo "--- test_ticket_unlink_after_compaction_succeeds ---"
test_ticket_unlink_after_compaction_succeeds() {
    if [ ! -f "$TICKET_LINK_SCRIPT" ]; then
        assert_eq "ticket-link.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_unlink_after_compaction_succeeds"
        return
    fi

    local compact_script
    compact_script="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)/src/rebar/_engine/ticket-compact.sh"
    if [ ! -f "$compact_script" ]; then
        assert_eq "ticket-compact.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_unlink_after_compaction_succeeds"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create two tickets
    local id1 id2
    id1=$(_create_ticket "$repo" "task" "Compaction unlink source")
    id2=$(_create_ticket "$repo" "task" "Compaction unlink target")

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "test_ticket_unlink_after_compaction_succeeds: tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_unlink_after_compaction_succeeds"
        return
    fi

    # Link id1 → id2 with depends_on
    local link_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id1" "$id2" depends_on 2>/dev/null) || link_exit=$?
    if [ "$link_exit" -ne 0 ]; then
        assert_eq "test_ticket_unlink_after_compaction_succeeds: initial link exits 0" "0" "$link_exit"
        assert_pass_if_clean "test_ticket_unlink_after_compaction_succeeds"
        return
    fi

    # Confirm LINK event file exists before compaction
    local link_count_before
    link_count_before=$(_count_link_events "$tracker_dir" "$id1")
    assert_eq "test_ticket_unlink_after_compaction_succeeds: LINK event exists pre-compact" "1" "$link_count_before"

    # Force compaction with threshold=1 so the LINK event is baked into SNAPSHOT
    # and the *-LINK.json file is deleted. Use --skip-sync and --no-commit to avoid
    # remote I/O and keep the test self-contained.
    (cd "$repo" && PROJECT_ROOT="$repo" _TICKET_TEST_NO_SYNC=1 \
        bash "$compact_script" "$id1" --threshold=1 --skip-sync --no-commit 2>/dev/null) || true

    # Confirm LINK event file is now gone (compaction deleted it)
    local link_count_after
    link_count_after=$(_count_link_events "$tracker_dir" "$id1")
    if [ "$link_count_after" -ne 0 ]; then
        # Compaction didn't remove LINK files (unexpected) — skip gracefully.
        assert_eq "test_ticket_unlink_after_compaction_succeeds: LINK file deleted by compact" "0" "$link_count_after"
        assert_pass_if_clean "test_ticket_unlink_after_compaction_succeeds"
        return
    fi

    # Confirm SNAPSHOT exists (compaction wrote it)
    local snapshot_count
    snapshot_count=$(find "$tracker_dir/$id1" -maxdepth 1 -name '*-SNAPSHOT.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "test_ticket_unlink_after_compaction_succeeds: SNAPSHOT exists post-compact" "1" "$snapshot_count"

    # When: ticket unlink id1 id2 (LINK file is gone, link_uuid only in SNAPSHOT deps[])
    local unlink_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" unlink "$id1" "$id2" 2>/dev/null) || unlink_exit=$?

    # Then: exits 0 (snapshot fallback finds the link_uuid)
    assert_eq "test_ticket_unlink_after_compaction_succeeds: unlink exits 0" "0" "$unlink_exit"

    # And: an UNLINK event was written
    local unlink_count
    unlink_count=$(_count_unlink_events "$tracker_dir" "$id1")
    assert_eq "test_ticket_unlink_after_compaction_succeeds: UNLINK event written" "1" "$unlink_count"

    # And: ticket show id1 no longer lists id2 as a dep
    local show_out
    show_out=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$id1" 2>/dev/null) || show_out=""
    if echo "$show_out" | grep -q "$id2"; then
        assert_eq "test_ticket_unlink_after_compaction_succeeds: dep removed from ticket show" "not-present" "present"
    else
        assert_eq "test_ticket_unlink_after_compaction_succeeds: dep removed from ticket show" "not-present" "not-present"
    fi

    assert_pass_if_clean "test_ticket_unlink_after_compaction_succeeds"
}
test_ticket_unlink_after_compaction_succeeds

print_summary
