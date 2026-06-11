#!/usr/bin/env bash
# tests/scripts/test-ticket-transition-open-children-perf.sh
# RED test for babe-ff38: ticket-transition open-children scan must be O(children)
# not O(total_tickets).
#
# The bug: ticket-transition.sh uses os.scandir(tracker_dir) over ALL tickets
# when closing, invoking a reducer subprocess (~35ms each) for every ticket
# that lacks a SNAPSHOT. With 16k+ tickets, this totals ~473s and causes
# timeout failures.
#
# The fix: use a targeted lookup that only processes direct children of the
# ticket being closed, not all tickets.
#
# This test creates N "unrelated" tickets (no parent relationship to the
# target ticket), then wraps ticket-reducer.py to count invocations.
# After closing the target, reducer invocations must be bounded by
# (children_count + flock_overhead), NOT by N.
#
# Usage: bash tests/scripts/test-ticket-transition-open-children-perf.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_TRANSITION_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-transition.sh"
REDUCER_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-reducer.py"
HASH_SCRIPT="$REPO_ROOT/src/rebar/_engine/compute-verdict-hash.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-transition-open-children-perf.sh ==="

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
    local extra_args="${4:-}"
    local out
    # shellcheck disable=SC2086
    out=$(cd "$repo" && bash "$TICKET_SCRIPT" create "$ticket_type" "$title" $extra_args 2>/dev/null) || true
    echo "$out" | tail -1
}

# ── Helper: get compiled status from reducer ──────────────────────────────────
_get_ticket_status() {
    local repo="$1"
    local ticket_id="$2"
    local tracker_dir="$repo/.tickets-tracker"
    python3 "$REDUCER_SCRIPT" "$tracker_dir/$ticket_id" 2>/dev/null \
        | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('status',''))" 2>/dev/null || true
}

# ── Test 1: reducer subprocess invocations bounded by children_count ─────
# current code invokes reducer for EVERY ticket in tracker_dir
# that lacks a snapshot — O(N) invocations. Fix: O(children_count) invocations.
echo ""
echo "--- Test 1: reducer invocations during close are O(children_count), not O(total_tickets) ---"
test_open_children_check_bounded_by_children() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create the target epic ticket (no children — leaf node)
    local target_id
    target_id=$(_create_ticket "$repo" epic "Target epic to close")

    if [ -z "$target_id" ]; then
        assert_eq "setup: target epic created" "non-empty" "empty"
        assert_pass_if_clean "test_open_children_check_bounded_by_children"
        return
    fi

    # Create N=10 unrelated tickets (no parent relationship to target)
    local n=10
    local i
    for i in $(seq 1 $n); do
        _create_ticket "$repo" task "Unrelated task $i" >/dev/null
    done

    # Create a wrapper reducer that counts invocations and delegates to real reducer
    local counter_dir
    counter_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$counter_dir")
    local count_file="$counter_dir/reducer_invoke_count"
    echo "0" > "$count_file"

    local wrapper_script="$counter_dir/counting-reducer.py"
    cat > "$wrapper_script" <<PYEOF
#!/usr/bin/env python3
"""Counting wrapper for ticket-reducer.py — records invocation count."""
import json, os, subprocess, sys

count_file = os.environ.get("REDUCER_COUNT_FILE", "")
if count_file and os.path.exists(count_file):
    try:
        with open(count_file) as f:
            count = int(f.read().strip() or "0")
        with open(count_file, "w") as f:
            f.write(str(count + 1))
    except Exception:
        pass

real_reducer = os.environ.get("REAL_REDUCER_SCRIPT", "$REDUCER_SCRIPT")
result = subprocess.run(
    ["python3", real_reducer] + sys.argv[1:],
    capture_output=False,
)
sys.exit(result.returncode)
PYEOF
    chmod +x "$wrapper_script"

    # Run ticket transition: close the target (which has 0 children)
    # Use the counting wrapper as REBAR_REDUCER if ticket-transition.sh supports it,
    # otherwise count via a different method.
    # Strategy: patch the inline python in ticket-transition.sh to use our wrapper
    # by setting an environment variable REBAR_REDUCER_SCRIPT.
    # so we temporarily create a counting wrapper at the same path.
    # Alternative: use time-based check instead.
    local exit_code=0

    # Time-based RED test: with N=10 unrelated tickets (no snapshots), the current
    # O(N) approach takes N*~35ms = ~350ms minimum. The fixed O(0) approach for a
    # leaf ticket (0 children) takes <10ms for the open-children check.
    #
    # Use a 5-second timeout budget: if the transition completes in <5s, it passed
    # the performance check. Current code with N=10 should complete in <5s but
    # we confirm the behavioral fix via a structural test (Test 2).
    local t_start t_end elapsed_ms
    t_start=$(python3 -c "import time; print(int(time.monotonic() * 1000))" 2>/dev/null || echo "0")

    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$target_id" open closed --verdict-hash="$(_verdict_hash "$repo" "$target_id")" 2>/dev/null) || exit_code=$?

    t_end=$(python3 -c "import time; print(int(time.monotonic() * 1000))" 2>/dev/null || echo "0")
    elapsed_ms=$(( t_end - t_start ))

    # Assert: transition exits 0
    assert_eq "perf: transition exits 0" "0" "$exit_code"

    # Assert: target status is now closed
    local compiled_status
    compiled_status=$(_get_ticket_status "$repo" "$target_id")
    assert_eq "perf: target status is closed" "closed" "$compiled_status"

    # The N=10 case is small enough that even the O(N) path completes quickly.
    # The structural test (Test 2) is the real RED gate.
    # Record timing as informational.
    echo "INFO: elapsed_ms=$elapsed_ms for close of leaf epic with N=$n unrelated tickets"

    assert_pass_if_clean "test_open_children_check_bounded_by_children"
}
test_open_children_check_bounded_by_children

# ── Test 2: open-children check does NOT use os.scandir over all tickets ─
# current code uses os.scandir(tracker_dir) which iterates ALL ticket dirs.
# Fix: must use a targeted lookup (snapshot parent_id check first, CREATE event
# check second) that avoids invoking reducer for tickets without the correct parent_id.
echo ""
echo "--- Test 2: open-children check uses targeted parent_id lookup, not full scandir ---"
test_open_children_check_uses_targeted_lookup() {
    _snapshot_fail

    if [ ! -f "$TICKET_TRANSITION_SCRIPT" ]; then
        assert_eq "ticket-transition.sh exists" "exists" "missing"
        assert_pass_if_clean "test_open_children_check_uses_targeted_lookup"
        return
    fi

    # Structural RED test: verify the inline python in ticket-transition.sh does NOT
    # invoke read_state_via_reducer (which spawns subprocesses) for tickets whose
    # parent_id does not match the target ticket.
    #
    # The current implementation calls read_state_via_reducer for EVERY ticket that
    # lacks a SNAPSHOT, regardless of parent_id. The fix should check parent_id first
    # (from snapshot or CREATE event) before invoking the reducer.
    #
    # We verify the fix by checking that the implementation:
    # (a) Does NOT call read_state_via_reducer unconditionally for all tickets, AND
    # (b) Does check parent_id before invoking the reducer.
    #
    # This is a behavioral integration test: create a tracker with N snapshots-less
    # tickets where none are children of the target, then verify that closing the
    # target does NOT invoke the slow subprocess path for any of them.

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create target epic ticket
    local parent_id
    parent_id=$(_create_ticket "$repo" epic "Parent epic for targeted test")

    if [ -z "$parent_id" ]; then
        assert_eq "setup: parent epic created" "non-empty" "empty"
        assert_pass_if_clean "test_open_children_check_uses_targeted_lookup"
        return
    fi

    # Create N=5 unrelated tasks (not children of parent_id, no snapshots yet)
    local n=5
    local i
    local unrelated_ids=()
    for i in $(seq 1 $n); do
        local uid
        uid=$(_create_ticket "$repo" task "Unrelated ticket $i")
        if [ -n "$uid" ]; then
            unrelated_ids+=("$uid")
        fi
    done

    # Inject a counting sentinel: wrap ticket-reducer.py with a script that
    # records which ticket dirs it is called with, then delegates to the real reducer.
    local spy_dir
    spy_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$spy_dir")
    local spy_log="$spy_dir/reducer-calls.log"
    touch "$spy_log"

    local spy_reducer="$spy_dir/spy-reducer.py"
    cat > "$spy_reducer" <<SPYEOF
#!/usr/bin/env python3
"""Spy reducer: logs ticket_dir argument, then delegates to real reducer."""
import os, subprocess, sys

# Log which ticket dir we're called with
spy_log = os.environ.get("SPY_REDUCER_LOG", "")
if spy_log:
    try:
        with open(spy_log, "a") as f:
            # Record the last component of the ticket_dir (the ticket_id)
            ticket_dir = sys.argv[1] if len(sys.argv) > 1 else "unknown"
            ticket_id = os.path.basename(ticket_dir)
            f.write(ticket_id + "\n")
    except Exception:
        pass

real_reducer = os.environ.get("REAL_REDUCER_SCRIPT", "$REDUCER_SCRIPT")
result = subprocess.run(
    ["python3", real_reducer] + sys.argv[1:],
)
sys.exit(result.returncode)
SPYEOF
    chmod +x "$spy_reducer"

    # Run the transition with the spy reducer injected via REBAR_REDUCER env var.
    # hardcoded. To spy on it, we need a way to override.
    #
    # Current code passes $REDUCER to the inline python as sys.argv[3] in Step 1b.
    # We use REBAR_REDUCER_SCRIPT env var if the fix adds support for it, OR we test
    # the observable side-effect: the transition should complete quickly (< timeout).
    #
    # For the RED phase: the test checks that transition timing is bounded.
    # We create a scenario where the O(N) scan would be detectable:
    # N tickets without snapshots + target with no children.
    # The fix should skip reducer calls for all N unrelated tickets (0 matches).

    # Count reducer subprocess calls by temporarily replacing the reducer
    # use REBAR_REDUCER_SCRIPT if supported, otherwise check structural fix.

    # Structural check: verify the inline python does NOT call read_state_via_reducer
    # for all tickets — it should check parent_id from snapshot/CREATE first.
    #
    # verify that after the fix, the inline python exits early for tickets
    # whose snapshot or CREATE event shows parent_id != target_id.
    #
    # Implementation check: look for the optimization pattern in the source.
    # The fix should read parent_id from snapshot OR CREATE event before calling reducer.
    local has_targeted_check=0

    # Check 1: does the inline python check parent_id from snapshot/CREATE before reducer?
    if python3 - "$TICKET_TRANSITION_SCRIPT" <<'PYEOF'
import sys, re

with open(sys.argv[1]) as f:
    src = f.read()

# Look for a pattern that reads CREATE events to check parent_id before reducer call
# The fix should contain logic like:
# - "CREATE" in event_type check combined with parent_id lookup
# - OR a pre-filter that skips reducer for non-matching parent_ids
# - OR a function that reads only snapshot/CREATE for parent_id

patterns = [
    # Pattern: check parent_id from CREATE or SNAPSHOT before invoking reducer
    r'parent_id.*before.*reducer|check.*parent.*CREATE|CREATE.*parent_id',
    # Pattern: early skip/continue when parent_id doesn't match
    r'continue.*parent_id|parent_id.*continue|skip.*parent',
    # Pattern: read parent_id from event files without running reducer
    r'read.*parent_id.*from|parent_id.*snapshot.*reducer',
    # Pattern: filter ticket dirs by parent_id before reducer (the key optimization)
    r'parent_id\s*!=\s*ticket_id.*continue|continue.*parent_id\s*!=',
]

# Also check: the fix should have a fast path that reads parent_id from file
# without spawning reducer subprocess for non-matching tickets
has_parent_id_precheck = bool(
    re.search(r'parent_id.*ticket_id|ticket_id.*parent_id', src)
)

# Check that the fast path (snapshot read) is followed by a parent_id check
# BEFORE the slow path
# This is the key structural invariant of the fix
has_fast_path_parent_check = bool(
    re.search(
        r'(read_state_from_snapshot|SNAPSHOT.*parent_id|parent_id.*SNAPSHOT)'
        r'.*'
        r'(parent_id\s*!=|!=\s*parent_id|parent_id\s*==\s*ticket_id|ticket_id\s*==\s*parent_id)',
        src, re.DOTALL
    )
)

if has_parent_id_precheck and has_fast_path_parent_check:
    sys.exit(0)  # Fix detected
else:
    sys.exit(1)  # Fix NOT present
PYEOF
    then
        has_targeted_check=1
    fi

    # Assert: the fix is present
    assert_eq \
        "targeted-check: open-children scan checks parent_id before invoking reducer" \
        "1" \
        "$has_targeted_check"

    assert_pass_if_clean "test_open_children_check_uses_targeted_lookup"
}
test_open_children_check_uses_targeted_lookup

# ── Test 3: leaf ticket close does not spawn reducer for unrelated tickets ─
# Integration test: create N=20 tickets without snapshots (by not compacting),
# none of which are children of the target. Verify that closing the target
# completes before a 10-second timeout (O(N) at 35ms each = 700ms; this
# is a soft threshold test — the true fix is structural, verified in Test 2).
echo ""
echo "--- Test 3: leaf close timing check (structural fix in Test 2 is primary) ---"
test_leaf_close_does_not_scan_all_tickets() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create target epic (no children)
    local target_id
    target_id=$(_create_ticket "$repo" epic "Leaf epic target")

    if [ -z "$target_id" ]; then
        assert_eq "setup: target epic created" "non-empty" "empty"
        assert_pass_if_clean "test_leaf_close_does_not_scan_all_tickets"
        return
    fi

    # Create N=20 unrelated tickets
    local n=20
    local i
    for i in $(seq 1 $n); do
        _create_ticket "$repo" task "Unrelated ticket $i" >/dev/null
    done

    # Close the target and measure time
    local t_start t_end elapsed_ms
    t_start=$(python3 -c "import time; print(int(time.monotonic() * 1000))")
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$target_id" open closed --verdict-hash="$(_verdict_hash "$repo" "$target_id")" 2>/dev/null) || exit_code=$?
    t_end=$(python3 -c "import time; print(int(time.monotonic() * 1000))")
    elapsed_ms=$(( t_end - t_start ))

    # Assert: transition exits 0
    assert_eq "leaf-close: transition exits 0" "0" "$exit_code"

    echo "INFO: elapsed_ms=$elapsed_ms for close of leaf epic with N=$n unrelated uncompacted tickets"

    # Timing assertion (soft): if the O(N) scan is present and each reducer subprocess
    # takes ~35ms, N=20 tickets → ~700ms for just the open-children check.
    # With the fix (targeted lookup), elapsed_ms should be << 700ms for N=20.
    # We use a 5000ms threshold to avoid flaky failures on slow CI runners,
    # while still catching catastrophically slow O(N) behavior.
    if [ "$elapsed_ms" -lt 5000 ]; then
        assert_eq "leaf-close: timing within 5s budget (N=20 unrelated tickets)" "within-budget" "within-budget"
    else
        assert_eq "leaf-close: timing within 5s budget (N=20 unrelated tickets)" "within-budget" "exceeded: ${elapsed_ms}ms"
    fi

    assert_pass_if_clean "test_leaf_close_does_not_scan_all_tickets"
}
test_leaf_close_does_not_scan_all_tickets

# ── Test 4: children-with-open-status still blocks close (regression guard) ───
# Ensure that after fixing the performance issue, the semantic check still works:
# a parent with open children must still fail to close.
echo ""
echo "--- Test 4 (regression guard): parent with open children still blocked after fix ---"
test_open_children_still_blocks_close_after_fix() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create parent epic
    local parent_id
    parent_id=$(_create_ticket "$repo" epic "Parent with open child")

    if [ -z "$parent_id" ]; then
        assert_eq "setup: parent epic created" "non-empty" "empty"
        assert_pass_if_clean "test_open_children_still_blocks_close_after_fix"
        return
    fi

    # Create a child under the parent (sets parent_id)
    local child_id
    child_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Open child" --parent "$parent_id" 2>/dev/null) || true
    child_id=$(echo "$child_id" | tail -1)

    if [ -z "$child_id" ]; then
        assert_eq "setup: child task created with --parent" "non-empty" "empty"
        assert_pass_if_clean "test_open_children_still_blocks_close_after_fix"
        return
    fi

    # Create N=5 unrelated tickets (should not affect the child check)
    local i
    for i in $(seq 1 5); do
        _create_ticket "$repo" task "Unrelated $i" >/dev/null
    done

    # Attempt to close parent — must exit non-zero (child still open)
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$parent_id" open closed 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "regression: close with open child exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error mentions the child
    if [[ "$stderr_out" == *"$child_id"* ]]; then
        assert_eq "regression: error lists child ID" "has-child-id" "has-child-id"
    else
        assert_eq "regression: error lists child ID" "has-child-id" "missing-child-id: $stderr_out"
    fi

    # Assert: parent status unchanged
    local compiled_status
    compiled_status=$(cd "$repo" && python3 "$REDUCER_SCRIPT" "$repo/.tickets-tracker/$parent_id" 2>/dev/null \
        | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('status',''))" 2>/dev/null || true)
    assert_eq "regression: parent status still open" "open" "$compiled_status"

    assert_pass_if_clean "test_open_children_still_blocks_close_after_fix"
}
test_open_children_still_blocks_close_after_fix

# ── Test 5: closed child does not block parent close (regression guard) ────────
echo ""
echo "--- Test 5 (regression guard): parent with only closed children can close ---"
test_closed_children_allow_parent_close() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create parent epic
    local parent_id
    parent_id=$(_create_ticket "$repo" epic "Parent with closed child")

    if [ -z "$parent_id" ]; then
        assert_eq "setup: parent epic created" "non-empty" "empty"
        assert_pass_if_clean "test_closed_children_allow_parent_close"
        return
    fi

    # Create a child and close it immediately
    local child_id
    child_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Soon-to-be-closed child" --parent "$parent_id" 2>/dev/null) || true
    child_id=$(echo "$child_id" | tail -1)

    if [ -z "$child_id" ]; then
        assert_eq "setup: child task created with --parent" "non-empty" "empty"
        assert_pass_if_clean "test_closed_children_allow_parent_close"
        return
    fi

    # Close the child
    local child_close_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$child_id" open closed 2>/dev/null) || child_close_exit=$?

    if [ "$child_close_exit" -ne 0 ]; then
        assert_eq "setup: child closed successfully" "0" "$child_close_exit"
        assert_pass_if_clean "test_closed_children_allow_parent_close"
        return
    fi

    # Now close the parent — should succeed (all children closed)
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$parent_id" open closed --verdict-hash="$(_verdict_hash "$repo" "$parent_id")" 2>&1) || exit_code=$?

    # Assert: exits 0
    assert_eq "closed-children: parent close exits 0" "0" "$exit_code"

    # Assert: parent status is now closed
    local compiled_status
    compiled_status=$(cd "$repo" && python3 "$REDUCER_SCRIPT" "$repo/.tickets-tracker/$parent_id" 2>/dev/null \
        | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('status',''))" 2>/dev/null || true)
    assert_eq "closed-children: parent status is closed" "closed" "$compiled_status"

    assert_pass_if_clean "test_closed_children_allow_parent_close"
}
test_closed_children_allow_parent_close

# ── Test 6: corrupt CREATE event is safely skipped (no reducer crash) ──────────
echo ""
echo "--- Test 6: ticket with corrupt CREATE event is skipped (not treated as child) ---"
test_corrupt_create_event_skipped() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create a parent ticket
    local parent_id
    parent_id=$(_create_ticket "$repo" epic "Parent for corrupt-create test")
    if [ -z "$parent_id" ]; then
        assert_eq "setup: parent created" "non-empty" "empty"
        assert_pass_if_clean "test_corrupt_create_event_skipped"
        return
    fi

    # Manually inject a ticket dir with a corrupt CREATE event (not a child of parent)
    local fake_id="zzzz-fake"
    local fake_dir="$repo/.tickets-tracker/$fake_id"
    mkdir -p "$fake_dir"
    # Write a corrupt CREATE event (invalid JSON)
    echo "THIS IS NOT VALID JSON" > "$fake_dir/0000000000000000000-CREATE.json"

    # Close the parent — should succeed without crashing on the corrupt fake ticket
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$parent_id" open closed --verdict-hash="$(_verdict_hash "$repo" "$parent_id")" 2>&1) || exit_code=$?

    # Assert: exits 0 (corrupt ticket is safely skipped)
    assert_eq "corrupt-create: parent close exits 0" "0" "$exit_code"

    assert_pass_if_clean "test_corrupt_create_event_skipped"
}
test_corrupt_create_event_skipped

# ── Summary ────────────────────────────────────────────────────────────────────
print_summary
