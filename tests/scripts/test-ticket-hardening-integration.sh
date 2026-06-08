#!/usr/bin/env bash
# tests/scripts/test-ticket-hardening-integration.sh
# RED integration tests for full hardening roundtrip:
#   - 16-hex ticket IDs with alias in CREATE event
#   - resolve_ticket_id by alias
#   - parent_status_uuid: null on first STATUS event
#   - Fork detection (PARENT_CHAIN_FORK_RESOLVED signal)
#   - Fork resolution produces valid consistent state
#
# All 5 test functions MUST FAIL until the hardening features are implemented:
#   - ticket-create.sh: 16-hex IDs + alias in CREATE event data
#   - ticket-lib.sh: resolve_ticket_id function
#   - ticket-transition.sh: parent_status_uuid in STATUS events
#   - ticket-reducer/_processors.py: PARENT_CHAIN_FORK_RESOLVED fork detection
#
# Usage: bash tests/scripts/test-ticket-hardening-integration.sh
# Returns: exit non-zero (RED) until all features are implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_CREATE_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-create.sh"
TICKET_TRANSITION_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-transition.sh"
TICKET_REDUCER_PY="$REPO_ROOT/src/rebar/_engine/ticket-reducer.py"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-hardening-integration.sh ==="

# ── Helper: create a fresh temp git repo with ticket system initialized ────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: find the single CREATE event file for a ticket ────────────────────
_find_create_event() {
    local tracker_dir="$1" ticket_id="$2"
    find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | head -1
}

# ── Helper: find the latest STATUS event file for a ticket ────────────────────
_find_latest_status_event() {
    local tracker_dir="$1" ticket_id="$2"
    find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-STATUS.json' ! -name '.*' 2>/dev/null | sort | tail -1
}

# ── Helper: run the reducer on a ticket dir and return JSON state ──────────────
_reduce_ticket() {
    local repo="$1" ticket_id="$2"
    local tracker_dir="$repo/.tickets-tracker"
    python3 "$TICKET_REDUCER_PY" "$tracker_dir/$ticket_id" 2>/dev/null || true
}

# ── Test 1: 16-hex ID format and alias in CREATE event ────────────────────────
echo "Test 1: ticket create outputs 16-hex ID and CREATE event data.alias is present and non-empty"
test_integration_16hex_and_alias() {
    local repo
    repo=$(_make_test_repo)

    # Create a ticket and capture stdout
    local stdout_out
    stdout_out=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Hardening test ticket" 2>/dev/null) || true
    # The last line is the canonical ticket ID
    local ticket_id
    ticket_id=$(echo "$stdout_out" | tail -1)

    # Assert: stdout last line matches 16-hex canonical ID pattern
    # RED: current implementation outputs 8-hex IDs (xxxx-xxxx), not 16-hex
    # (xxxx-xxxx-xxxx-xxxx). This assertion will fail until the ID format is upgraded.
    if [[ "$ticket_id" =~ ^[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}$ ]]; then
        assert_eq "16hex: ticket ID matches 16-hex pattern" "16hex-match" "16hex-match"
    else
        assert_eq "16hex: ticket ID matches 16-hex pattern" "16hex-match" "no-match: $ticket_id"
    fi

    if [ -z "$ticket_id" ]; then
        assert_eq "16hex: ticket ID is non-empty (cannot check alias)" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "16hex: CREATE event file found" "found" "not-found"
        return
    fi

    # Assert: data.alias field exists and is non-empty
    # RED: current CREATE events do not include a data.alias field
    local alias_val
    alias_val=$(python3 - "$event_file" <<'PYEOF'
import json, sys
try:
    ev = json.load(open(sys.argv[1], encoding='utf-8'))
    alias = ev.get('data', {}).get('alias', 'MISSING')
    print(alias)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)
PYEOF
) || true

    if [ "$alias_val" = "MISSING" ] || [ -z "$alias_val" ]; then
        assert_eq "16hex: data.alias is present and non-empty in CREATE event" "has-alias" "missing-or-empty: $alias_val"
    else
        assert_eq "16hex: data.alias is present and non-empty in CREATE event" "has-alias" "has-alias"
    fi
}
test_integration_16hex_and_alias

# ── Test 2: resolve_ticket_id by alias ────────────────────────────────────────
echo "Test 2: resolve_ticket_id resolves alias to canonical ID"
test_integration_resolve_by_alias() {
    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Alias resolution test" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "resolve-alias: ticket created" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "resolve-alias: CREATE event file found" "found" "not-found"
        return
    fi

    # Extract alias from CREATE event data.alias
    # RED: current implementation has no alias field
    local alias_val
    alias_val=$(python3 - "$event_file" <<'PYEOF'
import json, sys
try:
    ev = json.load(open(sys.argv[1], encoding='utf-8'))
    alias = ev.get('data', {}).get('alias', '')
    print(alias)
except Exception as e:
    print('')
    sys.exit(1)
PYEOF
) || true

    if [ -z "$alias_val" ]; then
        # No alias present — test fails RED (alias not implemented)
        assert_eq "resolve-alias: data.alias extracted from CREATE event" "non-empty-alias" "empty (alias not implemented)"
        return
    fi

    # Call resolve_ticket_id via ticket-lib.sh sourcing
    # RED: resolve_ticket_id function does not exist in ticket-lib.sh
    local resolved_id
    resolved_id=$(cd "$repo" && bash - <<SHELLEOF 2>/dev/null || true
source "$REPO_ROOT/src/rebar/_engine/ticket-lib.sh"
resolve_ticket_id "$alias_val"
SHELLEOF
)

    if [ -z "$resolved_id" ]; then
        assert_eq "resolve-alias: resolve_ticket_id returned a value" "non-empty" "empty (function not implemented)"
        return
    fi

    assert_eq "resolve-alias: resolved ID matches canonical ticket ID" "$ticket_id" "$resolved_id"
}
test_integration_resolve_by_alias

# ── Test 3: first STATUS event has parent_status_uuid=null ────────────────────
echo "Test 3: first STATUS event has parent_status_uuid key present with value null"
test_integration_first_status_null_parent() {
    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Status parent uuid test" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "null-parent-uuid: ticket created" "non-empty" "empty"
        return
    fi

    # Run transition: open → in_progress
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open in_progress 2>/dev/null) || true

    local tracker_dir="$repo/.tickets-tracker"
    local status_file
    status_file=$(_find_latest_status_event "$tracker_dir" "$ticket_id")

    if [ -z "$status_file" ]; then
        assert_eq "null-parent-uuid: STATUS event file found after transition" "found" "not-found"
        return
    fi

    # Assert: parent_status_uuid key exists in STATUS event and its value is JSON null
    # RED: current STATUS events do not include parent_status_uuid field
    local check_result
    check_result=$(python3 - "$status_file" <<'PYEOF'
import json, sys
try:
    ev = json.load(open(sys.argv[1], encoding='utf-8'))
    data = ev.get('data', {})
    if 'parent_status_uuid' not in data:
        print('MISSING_KEY')
    elif data['parent_status_uuid'] is None:
        print('OK_NULL')
    else:
        print(f'NOT_NULL: {data["parent_status_uuid"]!r}')
except Exception as e:
    print(f'PARSE_ERROR:{e}')
    sys.exit(1)
PYEOF
) || true

    case "$check_result" in
        OK_NULL)
            assert_eq "null-parent-uuid: data.parent_status_uuid is JSON null" "OK_NULL" "OK_NULL"
            ;;
        MISSING_KEY)
            assert_eq "null-parent-uuid: data.parent_status_uuid is JSON null" "OK_NULL" "MISSING_KEY (not implemented)"
            ;;
        *)
            assert_eq "null-parent-uuid: data.parent_status_uuid is JSON null" "OK_NULL" "$check_result"
            ;;
    esac
}
test_integration_first_status_null_parent

# ── Test 4: fork detection emits PARENT_CHAIN_FORK_RESOLVED to stderr ─────────
echo "Test 4: reducer emits PARENT_CHAIN_FORK_RESOLVED to stderr when concurrent STATUS fork detected"
test_integration_fork_detected() {
    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Fork detection test" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "fork-detected: ticket created" "non-empty" "empty"
        return
    fi

    # Run first transition: open → in_progress (writes STATUS event 1)
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open in_progress 2>/dev/null) || true

    local tracker_dir="$repo/.tickets-tracker"
    local status_file_1
    status_file_1=$(_find_latest_status_event "$tracker_dir" "$ticket_id")

    if [ -z "$status_file_1" ]; then
        assert_eq "fork-detected: first STATUS event written" "found" "not-found"
        return
    fi

    # Extract parent_status_uuid from the first STATUS event
    # RED: if parent_status_uuid doesn't exist, test fails here
    local parent_uuid_1
    parent_uuid_1=$(python3 - "$status_file_1" <<'PYEOF'
import json, sys
try:
    ev = json.load(open(sys.argv[1], encoding='utf-8'))
    val = ev.get('data', {}).get('parent_status_uuid', 'MISSING')
    print('' if val is None else (val if val != 'MISSING' else 'MISSING'))
except Exception:
    print('MISSING')
PYEOF
) || true

    # Simulate a concurrent write: create a SECOND STATUS event with the SAME
    # parent_status_uuid as the first (this is what a concurrent write would produce).
    # We write it directly to the store, bypassing the normal transition path.
    local ts
    ts=$(python3 -c "import time; print(int(time.time_ns()))")
    local fork_uuid
    fork_uuid=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
    local env_id
    env_id=$(cat "$tracker_dir/.env-id" 2>/dev/null || echo "test-env")

    local fork_event_file="$tracker_dir/$ticket_id/${ts}-STATUS.json"
    python3 - "$fork_event_file" "$fork_uuid" "$ts" "$env_id" "$parent_uuid_1" <<'PYEOF'
import json, sys
path, event_uuid, timestamp, env_id, parent_uuid = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
# parent_uuid may be empty string (null parent)
event = {
    "event_type": "STATUS",
    "uuid": event_uuid,
    "timestamp": timestamp,
    "env_id": env_id,
    "author": "test-fork-agent",
    "data": {
        "status": "blocked",
        "current_status": "open",
        "parent_status_uuid": parent_uuid if parent_uuid else None
    }
}
with open(path, 'w', encoding='utf-8') as f:
    json.dump(event, f, ensure_ascii=False)
print("written")
PYEOF

    # Commit the forked event to the tickets branch
    (cd "$tracker_dir" && \
        git add "$ticket_id/${ts}-STATUS.json" 2>/dev/null && \
        git -c user.email="test@test.com" -c user.name="Test" \
            commit -q -m "test: inject fork STATUS event for $ticket_id" 2>/dev/null) || true

    # Run the reducer; fork detection should emit PARENT_CHAIN_FORK_RESOLVED to stderr
    # RED: current reducer has no parent_chain fork detection
    local reducer_stderr reducer_stderr_file
    reducer_stderr_file=$(mktemp "${TMPDIR:-/tmp}/reducer-stderr.XXXXXX")
    python3 "$TICKET_REDUCER_PY" "$tracker_dir/$ticket_id" 2>"$reducer_stderr_file" >/dev/null || true
    reducer_stderr=$(cat "$reducer_stderr_file" 2>/dev/null || true)
    rm -f "$reducer_stderr_file"

    if [[ "$reducer_stderr" == *"PARENT_CHAIN_FORK_RESOLVED"* ]]; then
        assert_eq "fork-detected: reducer stderr contains PARENT_CHAIN_FORK_RESOLVED" "has-signal" "has-signal"
    else
        assert_eq "fork-detected: reducer stderr contains PARENT_CHAIN_FORK_RESOLVED" "has-signal" "missing: stderr='$reducer_stderr'"
    fi
}
test_integration_fork_detected

# ── Test 5: fork resolution produces consistent valid state ────────────────────
echo "Test 5: after fork resolution, ticket state.status is a valid non-empty status (not empty or 'conflicts')"
test_integration_fork_consistent_state() {
    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Fork consistency test" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "fork-consistent: ticket created" "non-empty" "empty"
        return
    fi

    # Run first transition: open → in_progress
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open in_progress 2>/dev/null) || true

    local tracker_dir="$repo/.tickets-tracker"
    local status_file_1
    status_file_1=$(_find_latest_status_event "$tracker_dir" "$ticket_id")

    if [ -z "$status_file_1" ]; then
        assert_eq "fork-consistent: first STATUS event written" "found" "not-found"
        return
    fi

    # Extract parent_status_uuid from first STATUS event (same as test 4)
    local parent_uuid_1
    parent_uuid_1=$(python3 - "$status_file_1" <<'PYEOF'
import json, sys
try:
    ev = json.load(open(sys.argv[1], encoding='utf-8'))
    val = ev.get('data', {}).get('parent_status_uuid', 'MISSING')
    print('' if val is None else (val if val != 'MISSING' else 'MISSING'))
except Exception:
    print('MISSING')
PYEOF
) || true

    # Inject a forked STATUS event with the SAME parent_status_uuid (concurrent write simulation)
    local ts
    ts=$(python3 -c "import time; print(int(time.time_ns()) + 1)")
    local fork_uuid
    fork_uuid=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
    local env_id
    env_id=$(cat "$tracker_dir/.env-id" 2>/dev/null || echo "test-env")

    local fork_event_file="$tracker_dir/$ticket_id/${ts}-STATUS.json"
    python3 - "$fork_event_file" "$fork_uuid" "$ts" "$env_id" "$parent_uuid_1" <<'PYEOF'
import json, sys
path, event_uuid, timestamp, env_id, parent_uuid = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
event = {
    "event_type": "STATUS",
    "uuid": event_uuid,
    "timestamp": timestamp,
    "env_id": env_id,
    "author": "test-fork-agent-2",
    "data": {
        "status": "blocked",
        "current_status": "open",
        "parent_status_uuid": parent_uuid if parent_uuid else None
    }
}
with open(path, 'w', encoding='utf-8') as f:
    json.dump(event, f, ensure_ascii=False)
print("written")
PYEOF

    # Commit the forked event
    (cd "$tracker_dir" && \
        git add "$ticket_id/${ts}-STATUS.json" 2>/dev/null && \
        git -c user.email="test@test.com" -c user.name="Test" \
            commit -q -m "test: inject fork STATUS event for consistency test $ticket_id" 2>/dev/null) || true

    # Run the reducer and capture the resulting state JSON
    local state_json
    state_json=$(python3 "$TICKET_REDUCER_PY" "$tracker_dir/$ticket_id" 2>/dev/null) || true

    if [ -z "$state_json" ]; then
        assert_eq "fork-consistent: reducer produced output" "non-empty" "empty"
        return
    fi

    # Assert: state.status is a valid non-empty string AND state has no unresolved conflicts.
    # After proper fork resolution the conflicts array must be absent or empty —
    # the reducer should deterministically pick a winner and clear the fork record.
    # RED: current reducer records the fork in state["conflicts"] and leaves it there
    # rather than resolving it, so conflicts will be non-empty.
    local status_check
    status_check=$(python3 - "$state_json" <<'PYEOF'
import json, sys
VALID_STATUSES = {'open', 'in_progress', 'blocked', 'closed'}
try:
    state = json.loads(sys.argv[1])
    status = state.get('status', '')
    conflicts = state.get('conflicts', [])
    if not status:
        print('EMPTY_STATUS')
    elif status not in VALID_STATUSES:
        print(f'INVALID_STATUS:{status!r}')
    elif conflicts:
        # Fork was NOT resolved — still present in conflicts array
        print(f'UNRESOLVED_CONFLICTS:{len(conflicts)} conflict(s) remaining')
    else:
        print(f'OK:{status}')
except Exception as e:
    print(f'PARSE_ERROR:{e}')
    sys.exit(1)
PYEOF
) || true

    case "$status_check" in
        OK:*)
            assert_eq "fork-consistent: state.status valid and no unresolved conflicts after fork resolution" "valid-no-conflicts" "valid-no-conflicts"
            ;;
        UNRESOLVED_CONFLICTS:*)
            # RED: reducer recorded the conflict but did not resolve it
            assert_eq "fork-consistent: state.status valid and no unresolved conflicts after fork resolution" "valid-no-conflicts" "$status_check (fork detection not implemented)"
            ;;
        EMPTY_STATUS)
            assert_eq "fork-consistent: state.status valid and no unresolved conflicts after fork resolution" "valid-no-conflicts" "empty (fork resolution not implemented)"
            ;;
        *)
            assert_eq "fork-consistent: state.status valid and no unresolved conflicts after fork resolution" "valid-no-conflicts" "$status_check"
            ;;
    esac
}
test_integration_fork_consistent_state

print_summary
