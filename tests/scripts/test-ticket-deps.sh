#!/usr/bin/env bash
# tests/scripts/test-ticket-deps.sh
# RED tests for the `ticket deps` subcommand CLI.
#
# All tests MUST FAIL until `ticket deps` is wired into the dispatcher
# (src/rebar/_engine/ticket). These are the RED phase tests for dso-si1e.
#
# Covers:
#   1. ticket deps <id>  prints JSON with keys ticket_id, deps, blockers, ready_to_work
#   2. ticket deps <id>  with no blockers returns ready_to_work=true
#   3. ticket deps <id>  with an open blocker returns ready_to_work=false
#   4. ticket deps <id>  with all blockers closed returns ready_to_work=true
#   5. ticket deps <nonexistent>  exits nonzero with error message
#   6. ticket deps  with no args exits nonzero with usage message
#   7. ticket deps <id>  output includes the blocker's ticket_id in blockers array
#
# TDD Requirement (RED): Run: bash tests/scripts/test-ticket-deps.sh
# Expect: non-zero exit (unknown subcommand error from the dispatcher).
#
# Usage: bash tests/scripts/test-ticket-deps.sh

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_DEPS_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-deps.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-deps.sh ==="

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

# ── Helper: transition a ticket to closed ─────────────────────────────────────
_close_ticket() {
    local repo="$1"
    local ticket_id="$2"
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open closed >/dev/null 2>/dev/null) || true
}

# ── Test 1: ticket deps <id> prints JSON with required keys ──────────────────
echo "Test 1: ticket deps <id> prints JSON with keys ticket_id, deps, blockers, ready_to_work"
test_deps_prints_json_with_required_keys() {
    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" "task" "Deps JSON test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for deps JSON test" "non-empty" "empty"
        return
    fi

    local exit_code=0
    local output
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$ticket_id" 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "ticket deps exits 0" "0" "$exit_code"

    # Assert: output has all required JSON keys
    local key_check
    key_check=$(python3 - "$ticket_id" "$output" <<'PYEOF'
import json, sys

ticket_id = sys.argv[1]
raw = sys.argv[2]

try:
    data = json.loads(raw)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

errors = []
for key in ("ticket_id", "deps", "blockers", "ready_to_work"):
    if key not in data:
        errors.append(f"missing key: {key!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
else:
    print("OK")
PYEOF
) || true

    assert_eq "ticket deps output has required keys (ticket_id, deps, blockers, ready_to_work)" "OK" "$key_check"
}
test_deps_prints_json_with_required_keys

# ── Test 2: ticket deps <id> with no blockers returns ready_to_work=true ─────
echo "Test 2: ticket deps <id> with no blockers returns ready_to_work=true"
test_deps_no_blockers_ready_to_work_true() {
    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" "task" "No-blocker ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for no-blocker test" "non-empty" "empty"
        return
    fi

    local exit_code=0
    local output
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$ticket_id" 2>/dev/null) || exit_code=$?

    assert_eq "ticket deps exits 0 (no blockers)" "0" "$exit_code"

    local rtw
    rtw=$(python3 -c "
import json, sys
try:
    d = json.loads('''$output''')
    print('true' if d.get('ready_to_work') is True else 'not-true:' + str(d.get('ready_to_work')))
except Exception as e:
    print('PARSE_ERROR:' + str(e))
" 2>/dev/null) || true

    assert_eq "ready_to_work is true when no blockers" "true" "$rtw"
}
test_deps_no_blockers_ready_to_work_true

# ── Test 3: ticket deps <id> with an open blocker returns ready_to_work=false ─
echo "Test 3: ticket deps <id> with an open blocker returns ready_to_work=false"
test_deps_open_blocker_ready_to_work_false() {
    local repo
    repo=$(_make_test_repo)

    local blocker_id
    blocker_id=$(_create_ticket "$repo" "task" "Blocker ticket (open)")

    local blocked_id
    blocked_id=$(_create_ticket "$repo" "task" "Blocked ticket")

    if [ -z "$blocker_id" ] || [ -z "$blocked_id" ]; then
        assert_eq "tickets created for blocker test" "non-empty" "empty"
        return
    fi

    # Link: blocker_id blocks blocked_id
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$blocker_id" "$blocked_id" blocks >/dev/null 2>/dev/null) || true

    local exit_code=0
    local output
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$blocked_id" 2>/dev/null) || exit_code=$?

    assert_eq "ticket deps exits 0 (with open blocker)" "0" "$exit_code"

    local rtw
    rtw=$(python3 -c "
import json, sys
try:
    d = json.loads('''$output''')
    print('false' if d.get('ready_to_work') is False else 'not-false:' + str(d.get('ready_to_work')))
except Exception as e:
    print('PARSE_ERROR:' + str(e))
" 2>/dev/null) || true

    assert_eq "ready_to_work is false when blocker is open" "false" "$rtw"
}
test_deps_open_blocker_ready_to_work_false

# ── Test 4: ticket deps <id> with all blockers closed returns ready_to_work=true
echo "Test 4: ticket deps <id> with all blockers closed returns ready_to_work=true"
test_deps_closed_blocker_ready_to_work_true() {
    local repo
    repo=$(_make_test_repo)

    local blocker_id
    blocker_id=$(_create_ticket "$repo" "task" "Blocker ticket (will close)")

    local blocked_id
    blocked_id=$(_create_ticket "$repo" "task" "Was-blocked ticket")

    if [ -z "$blocker_id" ] || [ -z "$blocked_id" ]; then
        assert_eq "tickets created for closed-blocker test" "non-empty" "empty"
        return
    fi

    # Link: blocker_id blocks blocked_id
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$blocker_id" "$blocked_id" blocks >/dev/null 2>/dev/null) || true

    # Close the blocker
    _close_ticket "$repo" "$blocker_id"

    local exit_code=0
    local output
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$blocked_id" 2>/dev/null) || exit_code=$?

    assert_eq "ticket deps exits 0 (all blockers closed)" "0" "$exit_code"

    local rtw
    rtw=$(python3 -c "
import json, sys
try:
    d = json.loads('''$output''')
    print('true' if d.get('ready_to_work') is True else 'not-true:' + str(d.get('ready_to_work')))
except Exception as e:
    print('PARSE_ERROR:' + str(e))
" 2>/dev/null) || true

    assert_eq "ready_to_work is true when all blockers are closed" "true" "$rtw"
}
test_deps_closed_blocker_ready_to_work_true

# ── Test 5: ticket deps <nonexistent> exits nonzero with error message ────────
echo "Test 5: ticket deps <nonexistent> exits nonzero with error message"
test_deps_nonexistent_id_exits_nonzero() {
    local repo
    repo=$(_make_test_repo)

    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "nonexistent-ticket-xyz" 2>&1 >/dev/null) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "ticket deps nonexistent exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error message is present (any non-empty stderr)
    assert_ne "ticket deps nonexistent prints error message" "" "$stderr_out"
}
test_deps_nonexistent_id_exits_nonzero

# ── Test 6: ticket deps with no args exits nonzero with usage message ─────────
echo "Test 6: ticket deps with no args exits nonzero with usage message"
test_deps_no_args_exits_nonzero_with_usage() {
    local repo
    repo=$(_make_test_repo)

    local exit_code=0
    local combined_out
    combined_out=$(cd "$repo" && bash "$TICKET_SCRIPT" deps 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "ticket deps (no args) exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: output contains usage information
    if echo "$combined_out" | grep -iq "usage\|ticket_id\|<id>"; then
        assert_eq "ticket deps (no args) prints usage" "found" "found"
    else
        assert_eq "ticket deps (no args) prints usage" "found" "missing: $combined_out"
    fi
}
test_deps_no_args_exits_nonzero_with_usage

# ── Test 7: ticket deps <id> output includes blocker ticket_id in blockers array
echo "Test 7: ticket deps <id> output includes blocker's ticket_id in blockers array"
test_deps_output_includes_blocker_in_blockers_array() {
    local repo
    repo=$(_make_test_repo)

    local blocker_id
    blocker_id=$(_create_ticket "$repo" "task" "Blocker to check in array")

    local blocked_id
    blocked_id=$(_create_ticket "$repo" "task" "Blocked ticket for blockers-array test")

    if [ -z "$blocker_id" ] || [ -z "$blocked_id" ]; then
        assert_eq "tickets created for blockers-array test" "non-empty" "empty"
        return
    fi

    # Link: blocker_id blocks blocked_id
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$blocker_id" "$blocked_id" blocks >/dev/null 2>/dev/null) || true

    local exit_code=0
    local output
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$blocked_id" 2>/dev/null) || exit_code=$?

    assert_eq "ticket deps exits 0 (blockers-array test)" "0" "$exit_code"

    # Assert: blockers array includes blocker_id
    local blocker_check
    blocker_check=$(python3 - "$blocker_id" "$output" <<'PYEOF'
import json, sys

blocker_id = sys.argv[1]
raw = sys.argv[2]

try:
    data = json.loads(raw)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

blockers = data.get("blockers", [])
if blocker_id in blockers:
    print("OK")
else:
    print(f"NOT_FOUND: expected {blocker_id!r} in blockers={blockers!r}")
PYEOF
) || true

    assert_eq "blockers array includes the open blocker's ticket_id" "OK" "$blocker_check"
}
test_deps_output_includes_blocker_in_blockers_array

# ── Summary ───────────────────────────────────────────────────────────────────

print_summary
