#!/usr/bin/env bash
# tests/scripts/test-ticket-comment.sh
# RED tests for src/rebar/_engine/ticket-comment.sh — `ticket comment` subcommand.
#
# All test functions MUST FAIL until ticket-comment.sh is implemented.
# Covers: COMMENT event written and committed, ghost prevention (nonexistent ticket,
# no CREATE event), empty body rejection, multiple comments accumulate,
# and COMMENT event JSON schema validation.
#
# Usage: bash tests/scripts/test-ticket-comment.sh
# Returns: exit non-zero (RED) until ticket-comment.sh is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_COMMENT_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-comment.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-comment.sh ==="

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

# ── Test 1: happy path — comment exits 0 and writes COMMENT event ──────────────
echo "Test 1: ticket comment exits 0 and writes COMMENT event with correct body"
test_ticket_comment_happy_path() {
    _snapshot_fail

    # RED: ticket-comment.sh must not exist yet
    if [ ! -f "$TICKET_COMMENT_SCRIPT" ]; then
        assert_eq "ticket-comment.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_comment_happy_path"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Comment test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for comment test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_comment_happy_path"
        return
    fi

    # Record COMMENT count before
    local before_count
    before_count=$(_count_comment_events "$tracker_dir" "$ticket_id")

    # Run ticket comment
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" comment "$ticket_id" "my test note" 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "ticket comment exits 0" "0" "$exit_code"

    # Assert: exactly one new COMMENT event file was written
    local after_count
    after_count=$(_count_comment_events "$tracker_dir" "$ticket_id")
    local new_events
    new_events=$(( after_count - before_count ))
    assert_eq "exactly one COMMENT event written" "1" "$new_events"

    # Assert: COMMENT event file contains correct body
    local comment_file
    comment_file=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-COMMENT.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ -z "$comment_file" ]; then
        assert_eq "COMMENT event file found" "found" "not-found"
        assert_pass_if_clean "test_ticket_comment_happy_path"
        return
    fi

    local body_check
    body_check=$(python3 - "$comment_file" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

body = ev.get('data', {}).get('body', '')
if body == 'my test note':
    print("OK")
else:
    print(f"WRONG_BODY:{body!r}")
PYEOF
) || true

    if [ "$body_check" = "OK" ]; then
        assert_eq "COMMENT event has correct body" "OK" "OK"
    else
        assert_eq "COMMENT event has correct body" "OK" "$body_check"
    fi

    assert_pass_if_clean "test_ticket_comment_happy_path"
}
test_ticket_comment_happy_path

# ── Test 2: ghost prevention — comment on nonexistent ticket_id fails ──────────
echo "Test 2: comment on nonexistent ticket ID fails with non-zero exit"
test_ticket_comment_nonexistent_ticket() {
    _snapshot_fail

    # RED: ticket-comment.sh must not exist yet
    if [ ! -f "$TICKET_COMMENT_SCRIPT" ]; then
        assert_eq "ticket-comment.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_comment_nonexistent_ticket"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local fake_id="xxxx-0000"
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" comment "$fake_id" "some comment" 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "nonexistent ticket exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error message is printed (not silent)
    if [ -n "$stderr_out" ]; then
        assert_eq "nonexistent ticket error message printed" "has-message" "has-message"
    else
        assert_eq "nonexistent ticket error message printed" "has-message" "silent"
    fi

    assert_pass_if_clean "test_ticket_comment_nonexistent_ticket"
}
test_ticket_comment_nonexistent_ticket

# ── Test 3: ghost prevention — ticket dir exists but has no CREATE event ────────
echo "Test 3: comment on ticket dir with no CREATE event fails with non-zero exit"
test_ticket_comment_ghost_no_create() {
    _snapshot_fail

    # RED: ticket-comment.sh must not exist yet
    if [ ! -f "$TICKET_COMMENT_SCRIPT" ]; then
        assert_eq "ticket-comment.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_comment_ghost_no_create"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Manually create a ghost ticket dir (no CREATE event)
    local ghost_id="ghost-cmmt1"
    mkdir -p "$tracker_dir/$ghost_id"

    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" comment "$ghost_id" "ghost comment" 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "ghost-no-create: exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error message is printed
    if [ -n "$stderr_out" ]; then
        assert_eq "ghost-no-create: error message printed" "has-message" "has-message"
    else
        assert_eq "ghost-no-create: error message printed" "has-message" "silent"
    fi

    # Assert: no COMMENT event written
    local comment_count
    comment_count=$(_count_comment_events "$tracker_dir" "$ghost_id")
    assert_eq "ghost-no-create: no COMMENT event written" "0" "$comment_count"

    assert_pass_if_clean "test_ticket_comment_ghost_no_create"
}
test_ticket_comment_ghost_no_create

# ── Test 4: empty body rejection — exits non-zero ──────────────────────────────
echo "Test 4: comment with empty body is rejected with non-zero exit"
test_ticket_comment_empty_body_rejected() {
    _snapshot_fail

    # RED: ticket-comment.sh must not exist yet
    if [ ! -f "$TICKET_COMMENT_SCRIPT" ]; then
        assert_eq "ticket-comment.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_comment_empty_body_rejected"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Empty body test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for empty body test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_comment_empty_body_rejected"
        return
    fi

    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" comment "$ticket_id" '' 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "empty body exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: no COMMENT event written
    local comment_count
    comment_count=$(_count_comment_events "$tracker_dir" "$ticket_id")
    assert_eq "empty body: no COMMENT event written" "0" "$comment_count"

    assert_pass_if_clean "test_ticket_comment_empty_body_rejected"
}
test_ticket_comment_empty_body_rejected

# ── Test 5: multiple comments accumulate in order ──────────────────────────────
echo "Test 5: two comments accumulate in ticket state with both entries in order"
test_ticket_comment_multiple_accumulate() {
    _snapshot_fail

    # RED: ticket-comment.sh must not exist yet
    if [ ! -f "$TICKET_COMMENT_SCRIPT" ]; then
        assert_eq "ticket-comment.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_comment_multiple_accumulate"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Multi-comment ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for multi-comment test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_comment_multiple_accumulate"
        return
    fi

    # Add two comments
    (cd "$repo" && bash "$TICKET_SCRIPT" comment "$ticket_id" "first comment" 2>/dev/null) || true
    (cd "$repo" && bash "$TICKET_SCRIPT" comment "$ticket_id" "second comment" 2>/dev/null) || true

    # Assert: two COMMENT event files exist
    local comment_count
    comment_count=$(_count_comment_events "$tracker_dir" "$ticket_id")
    assert_eq "two COMMENT events written" "2" "$comment_count"

    # Assert: compiled state via reducer has both comments in order
    local reducer_check
    reducer_check=$(python3 - "$REPO_ROOT/src/rebar/_engine/ticket-reducer.py" \
        "$tracker_dir/$ticket_id" <<'PYEOF'
import importlib.util, json, sys

reducer_path = sys.argv[1]
ticket_dir = sys.argv[2]

spec = importlib.util.spec_from_loader(
    'ticket_reducer',
    importlib.util.find_spec('importlib').loader,
)
# Load via exec to handle hyphenated filename
import importlib.util as _ilu
spec2 = _ilu.spec_from_file_location('ticket_reducer', reducer_path)
mod = _ilu.module_from_spec(spec2)
spec2.loader.exec_module(mod)

state = mod.reduce_ticket(ticket_dir)
if state is None:
    print("NO_STATE")
    sys.exit(1)

comments = state.get('comments', [])
if len(comments) != 2:
    print(f"WRONG_COUNT:{len(comments)}")
    sys.exit(2)

if comments[0].get('body') != 'first comment':
    print(f"WRONG_FIRST:{comments[0].get('body')!r}")
    sys.exit(3)

if comments[1].get('body') != 'second comment':
    print(f"WRONG_SECOND:{comments[1].get('body')!r}")
    sys.exit(4)

print("OK")
PYEOF
) || true

    if [ "$reducer_check" = "OK" ]; then
        assert_eq "reducer shows both comments in order" "OK" "OK"
    else
        assert_eq "reducer shows both comments in order" "OK" "$reducer_check"
    fi

    assert_pass_if_clean "test_ticket_comment_multiple_accumulate"
}
test_ticket_comment_multiple_accumulate

# ── Test 6: COMMENT event JSON schema validation ───────────────────────────────
echo "Test 6: COMMENT event JSON has correct schema: event_type, data.body, env_id, author, timestamp"
test_ticket_comment_event_schema() {
    _snapshot_fail

    # RED: ticket-comment.sh must not exist yet
    if [ ! -f "$TICKET_COMMENT_SCRIPT" ]; then
        assert_eq "ticket-comment.sh exists" "exists" "missing"
        assert_pass_if_clean "test_ticket_comment_event_schema"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Schema test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for schema test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_comment_event_schema"
        return
    fi

    (cd "$repo" && bash "$TICKET_SCRIPT" comment "$ticket_id" "schema test body" 2>/dev/null) || true

    local comment_file
    comment_file=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-COMMENT.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ -z "$comment_file" ]; then
        assert_eq "COMMENT event file found for schema check" "found" "not-found"
        assert_pass_if_clean "test_ticket_comment_event_schema"
        return
    fi

    local schema_check
    schema_check=$(python3 - "$comment_file" <<'PYEOF'
import json, sys

try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

errors = []

# Base schema: event_type must equal 'COMMENT'
if ev.get('event_type') != 'COMMENT':
    errors.append(f"event_type not COMMENT: {ev.get('event_type')!r}")

# Base schema: timestamp must be an integer
if not isinstance(ev.get('timestamp'), int):
    errors.append(f"timestamp not int: {type(ev.get('timestamp'))}")

# Base schema: env_id must be a non-empty string
if not isinstance(ev.get('env_id'), str) or not ev.get('env_id'):
    errors.append(f"env_id missing or not str: {ev.get('env_id')!r}")

# Base schema: author must be a non-empty string
if not isinstance(ev.get('author'), str) or not ev.get('author'):
    errors.append(f"author missing or not str: {ev.get('author')!r}")

# COMMENT-specific data fields: data.body must be a non-empty string
data = ev.get('data', {})
if not isinstance(data, dict):
    errors.append(f"data not dict: {type(data)}")
else:
    body = data.get('body')
    if not isinstance(body, str) or not body:
        errors.append(f"data.body missing or empty: {body!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(2)
else:
    print("OK")
PYEOF
) || true

    if [ "$schema_check" = "OK" ]; then
        assert_eq "COMMENT event has correct schema" "OK" "OK"
    else
        assert_eq "COMMENT event has correct schema" "OK" "$schema_check"
    fi

    assert_pass_if_clean "test_ticket_comment_event_schema"
}
test_ticket_comment_event_schema

print_summary
