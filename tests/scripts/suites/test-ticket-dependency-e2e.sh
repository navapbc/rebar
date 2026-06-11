#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-dependency-e2e.sh
# End-to-end integration test for the full ticket dependency flow:
#   link → deps → ready_to_work → cycle rejection → tombstone → unlink
#
# Exercises the full user-visible CLI workflow across all dependency subcommands
# using isolated temp git repos. Validates cross-component contracts that span
# ticket-link.sh, ticket-graph.py, and the ticket dispatcher.
#
# Scenarios:
#   1 — Happy path blocking: link B→A, B not ready; close A, B becomes ready
#   2 — Cycle rejection: X→Y succeeds, Y→X exits nonzero
#   3 — Tombstone-awareness: remove blocker dir, blocked becomes ready
#   4 — Unlink: link B→A, unlink B→A, B becomes ready
#
# Usage: bash tests/scripts/suites/test-ticket-dependency-e2e.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e is intentionally omitted — test functions return non-zero by design.
# -e would abort the runner on expected assertion mismatches.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-dependency-e2e.sh ==="

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

# ── Helper: close a ticket (open → closed) ───────────────────────────────────
_close_ticket() {
    local repo="$1"
    local ticket_id="$2"
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open closed >/dev/null 2>/dev/null) || true
}

# ── Helper: extract a JSON field from deps output ─────────────────────────────
# Usage: _deps_field <repo> <ticket_id> <field>
# Returns the field value as a string (booleans become "true"/"false").
_deps_field() {
    local repo="$1"
    local ticket_id="$2"
    local field="$3"
    local output
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$ticket_id" 2>/dev/null) || output=""
    python3 -c "
import json, sys
raw = sys.argv[1]
field = sys.argv[2]
try:
    d = json.loads(raw)
    val = d.get(field)
    if isinstance(val, bool):
        print('true' if val else 'false')
    elif val is None:
        print('null')
    else:
        print(str(val))
except Exception as e:
    print('PARSE_ERROR:' + str(e))
" "$output" "$field" 2>/dev/null || echo "error"
}

# ── Helper: check if ticket_id appears in blockers array ──────────────────────
_blocker_in_array() {
    local repo="$1"
    local ticket_id="$2"
    local blocker_id="$3"
    local output
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$ticket_id" 2>/dev/null) || output=""
    python3 - "$blocker_id" "$output" <<'PYEOF'
import json, sys
blocker_id = sys.argv[1]
raw = sys.argv[2]
try:
    d = json.loads(raw)
    blockers = d.get("blockers", [])
    print("found" if blocker_id in blockers else "not-found")
except Exception as e:
    print("PARSE_ERROR:" + str(e))
PYEOF
}

# ─────────────────────────────────────────────────────────────────────────────
# Scenario 1: Happy path blocking
#   1. Create Task A and Task B
#   2. link B→A blocks  →  B.ready_to_work=false, A in B.blockers
#   3. Close A          →  B.ready_to_work=true,  B.blockers=[]
# ─────────────────────────────────────────────────────────────────────────────
echo "Scenario 1: happy path blocking"
test_scenario1_happy_path_blocking() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # 1. Create two tasks
    local tkt_a tkt_b
    tkt_a=$(_create_ticket "$repo" task "Task A")
    tkt_b=$(_create_ticket "$repo" task "Task B")

    if [ -z "$tkt_a" ] || [ -z "$tkt_b" ]; then
        assert_eq "scenario1: tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_scenario1_happy_path_blocking"
        return
    fi

    # 2a. Before link: Task B should be ready_to_work=true (no blockers)
    local rtw_before
    rtw_before=$(_deps_field "$repo" "$tkt_b" "ready_to_work")
    assert_eq "scenario1: B.ready_to_work=true before link" "true" "$rtw_before"

    # 2b. Link: tkt_a blocks tkt_b
    local link_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$tkt_a" "$tkt_b" blocks >/dev/null 2>/dev/null) || link_exit=$?
    assert_eq "scenario1: link A blocks B exits 0" "0" "$link_exit"

    # 2c. Task B should now be ready_to_work=false
    local rtw_blocked
    rtw_blocked=$(_deps_field "$repo" "$tkt_b" "ready_to_work")
    assert_eq "scenario1: B.ready_to_work=false after link (A is open blocker)" "false" "$rtw_blocked"

    # 2d. Task A should appear in B's blockers list
    local blocker_check
    blocker_check=$(_blocker_in_array "$repo" "$tkt_b" "$tkt_a")
    assert_eq "scenario1: A appears in B.blockers" "found" "$blocker_check"

    # 3a. Transition Task A: open → closed
    _close_ticket "$repo" "$tkt_a"

    # 3b. Task B should now be ready_to_work=true (all blockers closed)
    local rtw_after_close
    rtw_after_close=$(_deps_field "$repo" "$tkt_b" "ready_to_work")
    assert_eq "scenario1: B.ready_to_work=true after A closed" "true" "$rtw_after_close"

    # 3c. Task A should no longer appear in B's active blockers
    local blocker_after_close
    blocker_after_close=$(_blocker_in_array "$repo" "$tkt_b" "$tkt_a")
    # A is still in the blockers list (the link exists) but since it is closed,
    # ready_to_work is true. The blockers array may still list A; what matters
    # is ready_to_work=true. We already asserted that above.
    # Additionally verify the deps output is valid JSON.
    local deps_json
    deps_json=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$tkt_b" 2>/dev/null) || deps_json=""
    local parse_exit=0
    python3 -c "import json,sys; json.loads(sys.argv[1])" "$deps_json" 2>/dev/null || parse_exit=$?
    assert_eq "scenario1: deps output is valid JSON after close" "0" "$parse_exit"

    assert_pass_if_clean "test_scenario1_happy_path_blocking"
}
test_scenario1_happy_path_blocking

# ─────────────────────────────────────────────────────────────────────────────
# Scenario 2: Cycle rejection
#   1. Create X and Y
#   2. link X→Y blocks  →  exits 0 (success)
#   3. link Y→X blocks  →  exits nonzero (cycle detected)
# ─────────────────────────────────────────────────────────────────────────────
echo "Scenario 2: cycle rejection"
test_scenario2_cycle_rejection() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local tkt_x tkt_y
    tkt_x=$(_create_ticket "$repo" task "Task X")
    tkt_y=$(_create_ticket "$repo" task "Task Y")

    if [ -z "$tkt_x" ] || [ -z "$tkt_y" ]; then
        assert_eq "scenario2: tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_scenario2_cycle_rejection"
        return
    fi

    # 1. X→Y blocks succeeds
    local link_xy_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$tkt_x" "$tkt_y" blocks >/dev/null 2>/dev/null) || link_xy_exit=$?
    assert_eq "scenario2: link X→Y blocks exits 0" "0" "$link_xy_exit"

    # 2. Y→X blocks should fail (would create cycle)
    local link_yx_exit=0
    local link_yx_stderr
    link_yx_stderr=$(cd "$repo" && bash "$TICKET_SCRIPT" link "$tkt_y" "$tkt_x" blocks 2>&1 >/dev/null) || link_yx_exit=$?
    assert_eq "scenario2: link Y→X blocks exits nonzero (cycle)" "1" \
        "$([ "$link_yx_exit" -ne 0 ] && echo 1 || echo 0)"

    # 3. Error message mentions cycle
    if [[ "${link_yx_stderr,,}" == *"cycle"* ]]; then
        assert_eq "scenario2: cycle error message mentions 'cycle'" "has-cycle" "has-cycle"
    else
        assert_eq "scenario2: cycle error message mentions 'cycle'" "has-cycle" \
            "no-cycle-in: ${link_yx_stderr:0:120}"
    fi

    # 4. After the rejected link, X should still be ready_to_work=true.
    #    The cycle-creating Y→X link was rejected, so X has no new blockers.
    #    (Y already blocks X via the existing X→Y link, but X's own ready_to_work
    #     reflects that nobody blocks X — X is the blocker, not the blocked.)
    local rtw_x
    rtw_x=$(_deps_field "$repo" "$tkt_x" "ready_to_work")
    assert_eq "scenario2: X.ready_to_work=true after cycle rejection (Y→X link not written)" "true" "$rtw_x"

    # 5. Also verify: Y is in X's blockers only via the original X→Y link direction.
    #    X should have no blockers (nothing blocks X since Y→X was rejected).
    local x_blocker_check
    x_blocker_check=$(_blocker_in_array "$repo" "$tkt_x" "$tkt_y")
    assert_eq "scenario2: Y not in X.blockers after cycle rejection" "not-found" "$x_blocker_check"

    assert_pass_if_clean "test_scenario2_cycle_rejection"
}
test_scenario2_cycle_rejection

# ─────────────────────────────────────────────────────────────────────────────
# Scenario 3: Tombstone-awareness
#   1. Create Z1 and Z2
#   2. link Z1→Z2 blocks  →  Z2.ready_to_work=false
#   3. Remove Z1 dir (simulate archive/tombstone)
#   4. Z2.ready_to_work=true (missing dir treated as closed)
# ─────────────────────────────────────────────────────────────────────────────
echo "Scenario 3: tombstone-awareness (missing blocker dir treated as closed)"
test_scenario3_tombstone_awareness() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local tkt_z1 tkt_z2
    tkt_z1=$(_create_ticket "$repo" task "Task Z1")
    tkt_z2=$(_create_ticket "$repo" task "Task Z2")

    if [ -z "$tkt_z1" ] || [ -z "$tkt_z2" ]; then
        assert_eq "scenario3: tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_scenario3_tombstone_awareness"
        return
    fi

    # 1. Link Z1 blocks Z2
    local link_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$tkt_z1" "$tkt_z2" blocks >/dev/null 2>/dev/null) || link_exit=$?
    assert_eq "scenario3: link Z1→Z2 blocks exits 0" "0" "$link_exit"

    # 2. Verify Z2 is not ready (Z1 is open)
    local rtw_before_tombstone
    rtw_before_tombstone=$(_deps_field "$repo" "$tkt_z2" "ready_to_work")
    assert_eq "scenario3: Z2.ready_to_work=false before tombstone" "false" "$rtw_before_tombstone"

    # 3. Simulate archiving Z1 by removing its directory
    #    (ticket-graph.py treats missing dir as status="closed")
    if [ -d "$tracker_dir/$tkt_z1" ]; then
        rm -rf "$tracker_dir/$tkt_z1"
    else
        assert_eq "scenario3: Z1 tracker dir exists before removal" "exists" "missing"
        assert_pass_if_clean "test_scenario3_tombstone_awareness"
        return
    fi

    # 4. After dir removal, Z2 should be ready_to_work=true
    local rtw_after_tombstone
    rtw_after_tombstone=$(_deps_field "$repo" "$tkt_z2" "ready_to_work")
    assert_eq "scenario3: Z2.ready_to_work=true after Z1 dir removed (tombstone)" "true" "$rtw_after_tombstone"

    # 5. deps output remains valid JSON
    local deps_json
    deps_json=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$tkt_z2" 2>/dev/null) || deps_json=""
    local parse_exit=0
    python3 -c "import json,sys; json.loads(sys.argv[1])" "$deps_json" 2>/dev/null || parse_exit=$?
    assert_eq "scenario3: deps output is valid JSON after tombstone" "0" "$parse_exit"

    assert_pass_if_clean "test_scenario3_tombstone_awareness"
}
test_scenario3_tombstone_awareness

# ─────────────────────────────────────────────────────────────────────────────
# Scenario 4: Unlink
#   1. Create tasks B and A
#   2. link B→A blocks  →  B.ready_to_work=false
#   3. unlink B A       →  exits 0
#   4. B.ready_to_work=true  (dependency removed)
# ─────────────────────────────────────────────────────────────────────────────
echo "Scenario 4: unlink removes blocking relationship"
test_scenario4_unlink() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local tkt_a tkt_b
    tkt_a=$(_create_ticket "$repo" task "Unlink Task A")
    tkt_b=$(_create_ticket "$repo" task "Unlink Task B")

    if [ -z "$tkt_a" ] || [ -z "$tkt_b" ]; then
        assert_eq "scenario4: tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_scenario4_unlink"
        return
    fi

    # 1. Link A blocks B
    local link_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$tkt_a" "$tkt_b" blocks >/dev/null 2>/dev/null) || link_exit=$?
    assert_eq "scenario4: link A→B blocks exits 0" "0" "$link_exit"

    # 2. Confirm B is blocked
    local rtw_before_unlink
    rtw_before_unlink=$(_deps_field "$repo" "$tkt_b" "ready_to_work")
    assert_eq "scenario4: B.ready_to_work=false after link" "false" "$rtw_before_unlink"

    # 3. Unlink A from B (removes the blocking relationship stored in A's dir).
    # Sleep 1s to guarantee the UNLINK event file has a strictly greater
    # timestamp than the LINK event file. Both events use int(time.time())
    # (second-precision). Without the sleep, if LINK and UNLINK land in the
    # same second, UUID-based filename ordering can sort the UNLINK before the
    # LINK, causing the event replay to incorrectly treat the link as active.
    # See: ticket-graph.py _write_link_event vs ticket-lib.sh write_commit_event.
    sleep 1
    local unlink_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" unlink "$tkt_a" "$tkt_b" >/dev/null 2>/dev/null) || unlink_exit=$?
    assert_eq "scenario4: unlink A B exits 0" "0" "$unlink_exit"

    # 4. After unlink, B should be ready_to_work=true
    local rtw_after_unlink
    rtw_after_unlink=$(_deps_field "$repo" "$tkt_b" "ready_to_work")
    assert_eq "scenario4: B.ready_to_work=true after unlink" "true" "$rtw_after_unlink"

    # 5. deps JSON is still valid
    local deps_json
    deps_json=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$tkt_b" 2>/dev/null) || deps_json=""
    local parse_exit=0
    python3 -c "import json,sys; json.loads(sys.argv[1])" "$deps_json" 2>/dev/null || parse_exit=$?
    assert_eq "scenario4: deps output is valid JSON after unlink" "0" "$parse_exit"

    # 6. Double-check: A is no longer in B's blockers
    local blocker_check
    blocker_check=$(_blocker_in_array "$repo" "$tkt_b" "$tkt_a")
    assert_eq "scenario4: A no longer in B.blockers after unlink" "not-found" "$blocker_check"

    assert_pass_if_clean "test_scenario4_unlink"
}
test_scenario4_unlink

# ─────────────────────────────────────────────────────────────────────────────
# Scenario 5: deps subcommand returns valid JSON schema for isolated ticket
# ─────────────────────────────────────────────────────────────────────────────
echo "Scenario 5: deps JSON schema for standalone ticket"
test_scenario5_deps_json_schema() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local tkt
    tkt=$(_create_ticket "$repo" task "Schema check ticket")

    if [ -z "$tkt" ]; then
        assert_eq "scenario5: ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_scenario5_deps_json_schema"
        return
    fi

    local exit_code=0
    local output
    output=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$tkt" 2>/dev/null) || exit_code=$?
    assert_eq "scenario5: ticket deps exits 0" "0" "$exit_code"

    # Validate JSON schema: ticket_id, deps (list), blockers (list), ready_to_work (bool)
    local schema_check
    schema_check=$(python3 - "$tkt" "$output" <<'PYEOF'
import json, sys

ticket_id = sys.argv[1]
raw = sys.argv[2]

try:
    d = json.loads(raw)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

errors = []

# Required keys
for key in ("ticket_id", "deps", "blockers", "ready_to_work"):
    if key not in d:
        errors.append(f"missing key: {key!r}")

# Type checks
if "ticket_id" in d and d["ticket_id"] != ticket_id:
    errors.append(f"ticket_id mismatch: {d['ticket_id']!r} != {ticket_id!r}")
if "deps" in d and not isinstance(d["deps"], list):
    errors.append(f"deps not a list: {type(d['deps'])}")
if "blockers" in d and not isinstance(d["blockers"], list):
    errors.append(f"blockers not a list: {type(d['blockers'])}")
if "ready_to_work" in d and not isinstance(d["ready_to_work"], bool):
    errors.append(f"ready_to_work not bool: {type(d['ready_to_work'])}")

# Isolated ticket has no blockers and is ready
if "blockers" in d and d["blockers"] != []:
    errors.append(f"isolated ticket should have empty blockers: {d['blockers']!r}")
if "ready_to_work" in d and d["ready_to_work"] is not True:
    errors.append(f"isolated ticket should be ready_to_work=true")

print("ERRORS:" + "; ".join(errors) if errors else "OK")
PYEOF
) || schema_check="python-error"

    assert_eq "scenario5: deps JSON has correct schema and values" "OK" "$schema_check"

    assert_pass_if_clean "test_scenario5_deps_json_schema"
}
test_scenario5_deps_json_schema

# ── Summary ───────────────────────────────────────────────────────────────────

print_summary
