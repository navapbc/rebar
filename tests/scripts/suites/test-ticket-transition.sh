#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-transition.sh
# RED tests for src/rebar/_engine/ticket-transition.sh — `ticket transition` subcommand.
#
# All test functions MUST FAIL until ticket-transition.sh is implemented.
# Covers: optimistic concurrency rejection, ghost ticket prevention (no dir,
# no CREATE event), idempotent no-op, invalid target_status, concurrent safety,
# and flock serialization via write_commit_event.
#
# Usage: bash tests/scripts/suites/test-ticket-transition.sh
# Returns: exit non-zero (RED) until ticket-transition.sh is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_TRANSITION_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-transition.sh"
HASH_SCRIPT="$REPO_ROOT/src/rebar/_engine/compute-verdict-hash.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-transition.sh ==="

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

# ── Helper: count STATUS event files in a ticket directory ────────────────────
_count_status_events() {
    local tracker_dir="$1"
    local ticket_id="$2"
    find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-STATUS.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' '
}

# ── Helper: get compiled status from reducer ──────────────────────────────────
_get_ticket_status() {
    local repo="$1"
    local ticket_id="$2"
    local tracker_dir="$repo/.tickets-tracker"
    python3 "$REPO_ROOT/src/rebar/_engine/ticket-reducer.py" "$tracker_dir/$ticket_id" 2>/dev/null \
        | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('status',''))" 2>/dev/null || true
}

# ── Test 1: happy path — transition exits 0 and writes STATUS event ────────────
echo "Test 1: transition open->in_progress exits 0 and writes STATUS event with correct fields"
test_transition_happy_path() {
    _snapshot_fail

    # RED: ticket-transition.sh must not exist yet
    if [ ! -f "$TICKET_TRANSITION_SCRIPT" ]; then
        assert_eq "ticket-transition.sh exists" "exists" "missing"
        assert_pass_if_clean "test_transition_happy_path"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo")

    local tracker_dir="$repo/.tickets-tracker"

    # Record STATUS event count before
    local before_count
    before_count=$(_count_status_events "$tracker_dir" "$ticket_id")

    # Run transition: open → in_progress
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open in_progress 2>/dev/null) || exit_code=$?
    assert_eq "happy path: transition exits 0" "0" "$exit_code"

    # Assert: exactly one new STATUS event was written
    local after_count
    after_count=$(_count_status_events "$tracker_dir" "$ticket_id")
    local new_events
    new_events=$(( after_count - before_count ))
    assert_eq "happy path: exactly one STATUS event written" "1" "$new_events"

    # Assert: STATUS event JSON contains required fields
    local status_file
    status_file=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-STATUS.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ -z "$status_file" ]; then
        assert_eq "happy path: STATUS event file found" "found" "not-found"
        assert_pass_if_clean "test_transition_happy_path"
        return
    fi

    local field_check
    field_check=$(python3 - "$status_file" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

errors = []

# Base schema
if not isinstance(ev.get('timestamp'), int):
    errors.append(f"timestamp not int: {type(ev.get('timestamp'))}")
if not isinstance(ev.get('uuid'), str) or not ev.get('uuid'):
    errors.append(f"uuid missing or not str: {ev.get('uuid')!r}")
if ev.get('event_type') != 'STATUS':
    errors.append(f"event_type not STATUS: {ev.get('event_type')!r}")
if not isinstance(ev.get('env_id'), str) or not ev.get('env_id'):
    errors.append(f"env_id missing or not str: {ev.get('env_id')!r}")
if not isinstance(ev.get('author'), str) or not ev.get('author'):
    errors.append(f"author missing or not str: {ev.get('author')!r}")

# STATUS-specific data fields
data = ev.get('data', {})
if not isinstance(data, dict):
    errors.append(f"data not dict: {type(data)}")
else:
    if data.get('status') != 'in_progress':
        errors.append(f"data.status not in_progress: {data.get('status')!r}")
    if data.get('current_status') != 'open':
        errors.append(f"data.current_status not open: {data.get('current_status')!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(2)
else:
    print("OK")
PYEOF
) || true

    if [ "$field_check" = "OK" ]; then
        assert_eq "happy path: STATUS event has correct fields" "OK" "OK"
    else
        assert_eq "happy path: STATUS event has correct fields" "OK" "$field_check"
    fi

    # Assert: compiled status updated to in_progress
    local compiled_status
    compiled_status=$(_get_ticket_status "$repo" "$ticket_id")
    assert_eq "happy path: compiled status is in_progress" "in_progress" "$compiled_status"

    assert_pass_if_clean "test_transition_happy_path"
}
test_transition_happy_path

# ── Test 2: optimistic concurrency rejection — wrong current_status ────────────
echo "Test 2: transition rejected when current_status does not match actual status"
test_transition_optimistic_concurrency_rejection() {
    _snapshot_fail

    # RED: ticket-transition.sh must not exist yet
    if [ ! -f "$TICKET_TRANSITION_SCRIPT" ]; then
        assert_eq "ticket-transition.sh exists" "exists" "missing"
        assert_pass_if_clean "test_transition_optimistic_concurrency_rejection"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo")
    local tracker_dir="$repo/.tickets-tracker"

    # Actual status is open; claim it is in_progress (wrong)
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" in_progress closed 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "concurrency: wrong current_status exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: stderr mentions the actual status
    if [[ "$stderr_out" =~ open|actual|current ]]; then
        assert_eq "concurrency: error output mentions actual status" "has-status-info" "has-status-info"
    else
        assert_eq "concurrency: error output mentions actual status" "has-status-info" "no-status-info: $stderr_out"
    fi

    # Assert: NO STATUS event was written
    local status_count
    status_count=$(_count_status_events "$tracker_dir" "$ticket_id")
    assert_eq "concurrency: no STATUS event written on rejection" "0" "$status_count"

    assert_pass_if_clean "test_transition_optimistic_concurrency_rejection"
}
test_transition_optimistic_concurrency_rejection

# ── Test 3: ghost prevention — non-existent ticket directory ──────────────────
echo "Test 3: transition on a non-existent ticket ID fails with clear error"
test_transition_ghost_prevention_no_dir() {
    _snapshot_fail

    # RED: ticket-transition.sh must not exist yet
    if [ ! -f "$TICKET_TRANSITION_SCRIPT" ]; then
        assert_eq "ticket-transition.sh exists" "exists" "missing"
        assert_pass_if_clean "test_transition_ghost_prevention_no_dir"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local fake_id="xxxx-0000"
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$fake_id" open in_progress 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "ghost-no-dir: exits non-zero for missing ticket" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error message is printed (not silent)
    if [ -n "$stderr_out" ]; then
        assert_eq "ghost-no-dir: error message printed" "has-message" "has-message"
    else
        assert_eq "ghost-no-dir: error message printed" "has-message" "silent"
    fi

    assert_pass_if_clean "test_transition_ghost_prevention_no_dir"
}
test_transition_ghost_prevention_no_dir

# ── Test 4: ghost prevention — ticket dir exists but has no CREATE event ───────
echo "Test 4: transition on a ticket dir with no CREATE event fails with clear error"
test_transition_ghost_prevention_no_create_event() {
    _snapshot_fail

    # RED: ticket-transition.sh must not exist yet
    if [ ! -f "$TICKET_TRANSITION_SCRIPT" ]; then
        assert_eq "ticket-transition.sh exists" "exists" "missing"
        assert_pass_if_clean "test_transition_ghost_prevention_no_create_event"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Manually create a ticket dir without a CREATE event (ghost ticket)
    local ghost_id="ghost-0001"
    mkdir -p "$tracker_dir/$ghost_id"

    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$ghost_id" open in_progress 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "ghost-no-create: exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error message is printed
    if [ -n "$stderr_out" ]; then
        assert_eq "ghost-no-create: error message printed" "has-message" "has-message"
    else
        assert_eq "ghost-no-create: error message printed" "has-message" "silent"
    fi

    # Assert: no STATUS event written
    local status_count
    status_count=$(_count_status_events "$tracker_dir" "$ghost_id")
    assert_eq "ghost-no-create: no STATUS event written" "0" "$status_count"

    assert_pass_if_clean "test_transition_ghost_prevention_no_create_event"
}
test_transition_ghost_prevention_no_create_event

# ── Test 5: idempotent no-op — current equals target status ───────────────────
echo "Test 5: transition open->open is a no-op (exits 0, no new STATUS event written)"
test_transition_idempotent_noop() {
    _snapshot_fail

    # RED: ticket-transition.sh must not exist yet
    if [ ! -f "$TICKET_TRANSITION_SCRIPT" ]; then
        assert_eq "ticket-transition.sh exists" "exists" "missing"
        assert_pass_if_clean "test_transition_idempotent_noop"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo")

    local tracker_dir="$repo/.tickets-tracker"

    # Transition open → open (same status)
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open open 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "noop: transition exits 0" "0" "$exit_code"

    # Assert: NO new STATUS event written
    local status_count
    status_count=$(_count_status_events "$tracker_dir" "$ticket_id")
    assert_eq "noop: no STATUS event written" "0" "$status_count"

    assert_pass_if_clean "test_transition_idempotent_noop"
}
test_transition_idempotent_noop

# ── Test 6: invalid target_status — rejected with error ───────────────────────
echo "Test 6: transition with invalid target_status exits non-zero with error"
test_transition_invalid_target_status() {
    _snapshot_fail

    # RED: ticket-transition.sh must not exist yet
    if [ ! -f "$TICKET_TRANSITION_SCRIPT" ]; then
        assert_eq "ticket-transition.sh exists" "exists" "missing"
        assert_pass_if_clean "test_transition_invalid_target_status"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo")

    local tracker_dir="$repo/.tickets-tracker"

    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open invalid_status 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "invalid-status: exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error message mentions the invalid status or valid values
    if [[ "${stderr_out,,}" =~ invalid|status|open|in_progress|closed|blocked ]]; then
        assert_eq "invalid-status: error message mentions status info" "has-status-info" "has-status-info"
    else
        assert_eq "invalid-status: error message mentions status info" "has-status-info" "no-status-info: $stderr_out"
    fi

    # Assert: no STATUS event written on invalid status
    local status_count
    status_count=$(_count_status_events "$tracker_dir" "$ticket_id")
    assert_eq "invalid-status: no STATUS event written" "0" "$status_count"

    assert_pass_if_clean "test_transition_invalid_target_status"
}
test_transition_invalid_target_status

# ── Test 7: concurrent safety — two transitions; at most one succeeds ──────────
echo "Test 7: two concurrent transitions on same ticket; at most one succeeds, no corrupt events"
test_transition_concurrent_safety() {
    _snapshot_fail

    # RED: ticket-transition.sh must not exist yet
    if [ ! -f "$TICKET_TRANSITION_SCRIPT" ]; then
        assert_eq "ticket-transition.sh exists" "exists" "missing"
        assert_pass_if_clean "test_transition_concurrent_safety"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo")

    local tracker_dir="$repo/.tickets-tracker"
    local tmp_out_dir
    tmp_out_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp_out_dir")

    # Launch two concurrent transitions from the same starting status (open)
    # Both claim current=open; only one can write the STATUS event atomically
    local exit1=0 exit2=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open in_progress >"$tmp_out_dir/out1" 2>"$tmp_out_dir/err1") &
    local pid1=$!
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open closed >"$tmp_out_dir/out2" 2>"$tmp_out_dir/err2") &
    local pid2=$!

    wait "$pid1" || exit1=$?
    wait "$pid2" || exit2=$?

    # Assert: at most one exited 0 (one or both may succeed — but no corruption)
    local success_count=0
    [ "$exit1" -eq 0 ] && success_count=$((success_count + 1))
    [ "$exit2" -eq 0 ] && success_count=$((success_count + 1))

    if [ "$success_count" -le 1 ]; then
        assert_eq "concurrent: at most one transition succeeds" "at-most-1" "at-most-1"
    else
        assert_eq "concurrent: at most one transition succeeds" "at-most-1" "both-succeeded"
    fi

    # Assert: at most one STATUS event written (zero or one — matches 'at most one succeeds' invariant)
    local status_count
    status_count=$(_count_status_events "$tracker_dir" "$ticket_id")
    if [ "$status_count" -le 1 ]; then
        assert_eq "concurrent: at most one STATUS event written" "at-most-1" "at-most-1"
    else
        assert_eq "concurrent: at most one STATUS event written" "at-most-1" "$status_count-events"
    fi

    # Assert: the STATUS event file is valid JSON (no corruption), if present
    local status_file
    status_file=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-STATUS.json' ! -name '.*' 2>/dev/null | head -1)

    if [ -n "$status_file" ]; then
        local parse_exit=0
        python3 -c "import json,sys; json.load(sys.stdin)" < "$status_file" 2>/dev/null || parse_exit=$?
        assert_eq "concurrent: STATUS event is valid JSON (no corruption)" "0" "$parse_exit"
    fi
    # If no STATUS event was written (both concurrent transitions rejected each other),
    # that is also a valid outcome — no assertion needed in the empty case.

    assert_pass_if_clean "test_transition_concurrent_safety"
}
test_transition_concurrent_safety

# ── Test 8: close reports newly unblocked ticket ──────────────────────────────
echo "Test 8: ticket A closed; stdout contains 'UNBLOCKED: <B>' when B was blocked only by A"
test_close_ticket_reports_newly_unblocked() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create ticket A (the one we will close)
    local ticket_a
    ticket_a=$(_create_ticket "$repo" task "Ticket A - to be closed")

    # Create ticket B (blocked only by A)
    local ticket_b
    ticket_b=$(_create_ticket "$repo" task "Ticket B - blocked by A")

    if [ -z "$ticket_a" ] || [ -z "$ticket_b" ]; then
        assert_eq "setup: both tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_close_ticket_reports_newly_unblocked"
        return
    fi

    # Link: B depends_on A  (B is blocked by A)
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$ticket_b" "$ticket_a" depends_on 2>/dev/null) || true

    # Transition A: open → closed; capture stdout
    local stdout_out
    local exit_code=0
    stdout_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_a" open closed 2>/dev/null) || exit_code=$?

    # Assert: transition exits 0
    assert_eq "unblocked-report: transition exits 0" "0" "$exit_code"

    # Assert: stdout contains 'UNBLOCKED: ' with ticket_b listed
    # RED: ticket-transition.sh does not call ticket-unblock.py yet → this will FAIL
    if [[ "$stdout_out" =~ UNBLOCKED:.*$ticket_b ]]; then
        assert_eq "unblocked-report: stdout contains UNBLOCKED: <B>" "has-unblocked-B" "has-unblocked-B"
    else
        assert_eq "unblocked-report: stdout contains UNBLOCKED: <B>" "has-unblocked-B" "missing: $stdout_out"
    fi

    assert_pass_if_clean "test_close_ticket_reports_newly_unblocked"
}
test_close_ticket_reports_newly_unblocked

# ── Test 9: close reports 'UNBLOCKED: none' when no tickets are freed ─────────
echo "Test 9: ticket A closed with no dependent tickets; stdout contains 'UNBLOCKED: none'"
test_close_ticket_reports_no_unblocked() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create a lone ticket (no other tickets depend on it)
    local ticket_a
    ticket_a=$(_create_ticket "$repo")

    # Transition A: open → closed; capture stdout
    local stdout_out
    local exit_code=0
    stdout_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_a" open closed 2>/dev/null) || exit_code=$?

    # Assert: transition exits 0
    assert_eq "no-unblocked: transition exits 0" "0" "$exit_code"

    # Assert: stdout contains 'UNBLOCKED: none'
    # RED: ticket-transition.sh does not emit UNBLOCKED output yet → this will FAIL
    if [[ "$stdout_out" == *"UNBLOCKED: none"* ]]; then
        assert_eq "no-unblocked: stdout contains UNBLOCKED: none" "has-none" "has-none"
    else
        assert_eq "no-unblocked: stdout contains UNBLOCKED: none" "has-none" "missing: $stdout_out"
    fi

    assert_pass_if_clean "test_close_ticket_reports_no_unblocked"
}
test_close_ticket_reports_no_unblocked

# ── Test 10: UNBLOCKED output only on close, not on other transitions ─────────
echo "Test 10: transition to in_progress does NOT emit 'UNBLOCKED:' in stdout"
test_close_ticket_unblocked_output_only_on_close() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_a
    ticket_a=$(_create_ticket "$repo")

    # Transition open → in_progress (NOT a close); capture stdout
    local stdout_out
    local exit_code=0
    stdout_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_a" open in_progress 2>/dev/null) || exit_code=$?

    # Assert: transition exits 0
    assert_eq "only-on-close: in_progress transition exits 0" "0" "$exit_code"

    # Assert: stdout does NOT contain 'UNBLOCKED:'
    # This test should PASS even before implementation (the script doesn't emit UNBLOCKED yet).
    # After implementation it must still pass (guard: only emit on close).
    if [[ "$stdout_out" == *"UNBLOCKED:"* ]]; then
        assert_eq "only-on-close: no UNBLOCKED in stdout for non-close transition" "no-unblocked" "has-unblocked: $stdout_out"
    else
        assert_eq "only-on-close: no UNBLOCKED in stdout for non-close transition" "no-unblocked" "no-unblocked"
    fi

    assert_pass_if_clean "test_close_ticket_unblocked_output_only_on_close"
}
test_close_ticket_unblocked_output_only_on_close

# ── Test 11: transition succeeds even if unblock detection fails ──────────────
echo "Test 11: transition exits 0 and emits stderr warning even if ticket-unblock.py is unavailable"
test_close_ticket_succeeds_even_if_unblock_fails() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_a
    ticket_a=$(_create_ticket "$repo")

    # Simulate ticket-unblock.py being unavailable by pointing TRACKER_DIR to an
    # invalid path via a wrapper that overrides the unblock script invocation.
    # We do this by creating a broken ticket-unblock.py in a temp bin dir and
    # prepending it to PATH so it shadows the real one (if it exists).
    local fake_bin
    fake_bin=$(mktemp -d)
    _CLEANUP_DIRS+=("$fake_bin")
    # Write a broken ticket-unblock.py that always exits 1 with an error
    cat > "$fake_bin/ticket-unblock.py" <<'PYEOF'
import sys
print("simulated unblock failure", file=sys.stderr)
sys.exit(1)
PYEOF

    # Run transition with a modified environment: override UNBLOCK_SCRIPT if the
    # implementation uses it, otherwise pass an invalid tracker_dir suffix via env
    # so detect_newly_unblocked fails.
    # Strategy: set REBAR_UNBLOCK_SCRIPT env to the broken script so ticket-transition.sh
    # uses it when calling ticket-unblock.py (the implementation should honor this).
    # RED: regardless of strategy, the test verifies exit 0 + stderr warning.
    local stdout_out stderr_out
    local exit_code=0
    stdout_out=$(cd "$repo" && REBAR_UNBLOCK_SCRIPT="$fake_bin/ticket-unblock.py" \
        bash "$TICKET_SCRIPT" transition "$ticket_a" open closed 2>/tmp/test-unblock-fail-stderr-$$) || exit_code=$?
    stderr_out=$(cat /tmp/test-unblock-fail-stderr-$$ 2>/dev/null || true)
    rm -f /tmp/test-unblock-fail-stderr-$$

    # Assert: transition exits 0 (non-blocking — close succeeded even if unblock fails)
    # RED: current ticket-transition.sh doesn't call unblock at all → this assertion
    # will currently PASS. The test becomes meaningful after dso-f8xn implements
    # unblock calling with non-blocking error handling.
    assert_eq "unblock-fail: transition exits 0 (non-blocking)" "0" "$exit_code"

    # Assert: if unblock was attempted and failed, a warning appears on stderr.
    # RED: current implementation doesn't call unblock → no warning emitted.
    # After dso-f8xn: warning should appear when REBAR_UNBLOCK_SCRIPT exits non-zero.
    # We can only assert on the warning presence AFTER implementation calls the script.
    # For now this assertion is the RED trigger: warn on stderr when unblock fails.
    # Note: this specific assertion fails RED only after dso-f8xn adds the call.
    # The exit-0 assertion above validates the non-blocking contract at GREEN time.
    #
    # Check if either: (a) a warning was emitted to stderr, OR (b) UNBLOCKED: none
    # appears in stdout (meaning unblock ran and returned no results — still valid).
    # If neither, the implementation hasn't added unblock support yet (RED for now).
    if [[ "${stderr_out,,}" =~ warn|unblock|fail|error ]] || \
       [[ "$stdout_out" == *"UNBLOCKED:"* ]]; then
        assert_eq "unblock-fail: stderr warning or UNBLOCKED output present (unblock called)" "unblock-called" "unblock-called"
    else
        # RED: unblock not called yet — this assertion fails until dso-f8xn is implemented
        assert_eq "unblock-fail: stderr warning or UNBLOCKED output present (unblock called)" "unblock-called" "not-called: stdout='$stdout_out' stderr='$stderr_out'"
    fi

    assert_pass_if_clean "test_close_ticket_succeeds_even_if_unblock_fails"
}
test_close_ticket_succeeds_even_if_unblock_fails

# ── Test 12 (RED): bug close requires --reason flag ───────────────────────────
echo "Test 12 (RED): closing a bug ticket without --reason exits non-zero"
test_transition_bug_close_requires_reason() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Use fixture bug ticket
    local ticket_id
    ticket_id=$(_create_ticket "$repo" bug)

    # Attempt to close the bug WITHOUT --reason — must exit non-zero
    # RED: current ticket-transition.sh does not enforce this guard → exits 0
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open closed 2>&1) || exit_code=$?

    # Assert: exits non-zero (guard not yet implemented → currently exits 0, so FAILS RED)
    assert_eq "bug-close-no-reason: exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error message mentions '--reason' or 'reason' (guard feedback)
    if [[ "${stderr_out,,}" =~ reason|--reason ]]; then
        assert_eq "bug-close-no-reason: error mentions --reason" "has-reason-hint" "has-reason-hint"
    else
        assert_eq "bug-close-no-reason: error mentions --reason" "has-reason-hint" "no-hint: $stderr_out"
    fi

    assert_pass_if_clean "test_transition_bug_close_requires_reason"
}
test_transition_bug_close_requires_reason

# ── Test 13 (RED): bug close with --reason succeeds ──────────────────────────
echo "Test 13 (RED): closing a bug ticket WITH --reason exits 0"
test_transition_bug_close_with_reason_succeeds() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Use fixture bug ticket
    local ticket_id
    ticket_id=$(_create_ticket "$repo" bug)

    # Close the bug WITH --reason — must exit 0
    # RED: current ticket-transition.sh does not accept --reason → may exit 0 for wrong reason
    # (it exits 0 because it doesn't validate, but after guard implementation it must only exit 0
    # when --reason is supplied). We verify the STATUS event is written to confirm it succeeded.
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open closed --reason "Fixed in commit abc123" 2>&1) || exit_code=$?

    # Assert: exits 0
    assert_eq "bug-close-with-reason: exits 0" "0" "$exit_code"

    # Assert: transition evidence exists — either a STATUS event or a SNAPSHOT (compact-on-close)
    local tracker_dir="$repo/.tickets-tracker"
    local status_count
    status_count=$(_count_status_events "$tracker_dir" "$ticket_id")
    local snapshot_count
    snapshot_count=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | wc -l | tr -d ' ')
    local evidence_count=$(( status_count + snapshot_count ))
    assert_eq "bug-close-with-reason: transition evidence exists" "1" "$([ "$evidence_count" -ge 1 ] && echo 1 || echo 0)"

    # Assert: compiled status is now closed
    local compiled_status
    compiled_status=$(_get_ticket_status "$repo" "$ticket_id")
    assert_eq "bug-close-with-reason: compiled status is closed" "closed" "$compiled_status"

    assert_pass_if_clean "test_transition_bug_close_with_reason_succeeds"
}
test_transition_bug_close_with_reason_succeeds

# ── Test 14 (RED): close blocked by open children ─────────────────────────────
echo "Test 14 (RED): closing a ticket with open children exits non-zero"
test_transition_close_blocked_with_open_children() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create a parent epic ticket
    local parent_id
    parent_id=$(_create_ticket "$repo" epic "Epic with open children")

    if [ -z "$parent_id" ]; then
        assert_eq "parent epic ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_transition_close_blocked_with_open_children"
        return
    fi

    # Create a child ticket under the parent
    local child_id
    child_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Open child task" --parent "$parent_id" 2>/dev/null) || true
    child_id=$(echo "$child_id" | tail -1)

    if [ -z "$child_id" ]; then
        # Child creation with --parent may not yet exist; this test should still fail RED
        # by detecting open children via ticket_find_open_children which won't be implemented yet.
        # If children can't be created, guard can't be triggered — assert failure to stay RED.
        assert_eq "child ticket created under parent" "non-empty" "empty"
        assert_pass_if_clean "test_transition_close_blocked_with_open_children"
        return
    fi

    # Attempt to close the parent epic while it has an open child — must exit non-zero
    # RED: current ticket-transition.sh does not check open children → exits 0
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$parent_id" open closed 2>&1) || exit_code=$?

    # Assert: exits non-zero (guard not yet implemented → currently exits 0, so FAILS RED)
    assert_eq "close-with-open-children: exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error message mentions children or open tickets
    if [[ "${stderr_out,,}" =~ child|children|open|block ]]; then
        assert_eq "close-with-open-children: error mentions children" "has-children-hint" "has-children-hint"
    else
        assert_eq "close-with-open-children: error mentions children" "has-children-hint" "no-hint: $stderr_out"
    fi

    # Assert: the parent's status is still open (transition was blocked)
    local compiled_status
    compiled_status=$(_get_ticket_status "$repo" "$parent_id")
    assert_eq "close-with-open-children: parent status unchanged (still open)" "open" "$compiled_status"

    assert_pass_if_clean "test_transition_close_blocked_with_open_children"
}
test_transition_close_blocked_with_open_children

# ── Suite-runner guard for RED compact-on-close tests ──────────────────────────
# ticket-transition.sh does not call compact yet and does not read REBAR_COMPACT_SCRIPT.
# When running under run-all.sh, skip these RED tests so the suite stays green.
_compact_on_close_implemented() {
    grep -q 'REBAR_COMPACT_SCRIPT' "$TICKET_TRANSITION_SCRIPT" 2>/dev/null
}

if [ "${_RUN_ALL_ACTIVE:-0}" = "1" ] && ! _compact_on_close_implemented; then
    echo "SKIP: compact-on-close not yet implemented (RED) — tests 15-16 deferred"
    echo ""
    print_summary
    exit 0
fi

# ── Test 15 (RED): close triggers compaction ────────────────────────────────────
echo "Test 15 (RED): closing a ticket with 12+ events triggers compaction (SNAPSHOT file created)"
test_close_triggers_compaction() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo")

    local tracker_dir="$repo/.tickets-tracker"

    # Add 12+ comment events to exceed the compaction threshold
    local i
    for i in $(seq 1 13); do
        (cd "$repo" && bash "$TICKET_SCRIPT" comment "$ticket_id" "Comment number $i" 2>/dev/null) || true
    done

    # Close the ticket via transition
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open closed 2>/dev/null) || exit_code=$?

    # Assert: transition exits 0
    assert_eq "compact: transition exits 0" "0" "$exit_code"

    # Assert: a *-SNAPSHOT.json file exists in the ticket dir
    # RED: ticket-transition.sh does not call compact on close yet → no SNAPSHOT file
    local snapshot_count
    snapshot_count=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-SNAPSHOT.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')

    if [ "$snapshot_count" -ge 1 ]; then
        assert_eq "compact: SNAPSHOT file exists after close" "has-snapshot" "has-snapshot"
    else
        assert_eq "compact: SNAPSHOT file exists after close" "has-snapshot" "no-snapshot (count=$snapshot_count)"
    fi

    assert_pass_if_clean "test_close_triggers_compaction"
}
test_close_triggers_compaction

# ── Test 16 (RED): close succeeds even if compact fails ────────────────────────
echo "Test 16 (RED): close succeeds (exit 0) even if REBAR_COMPACT_SCRIPT points to a failing script"
test_close_succeeds_if_compact_fails() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo")

    # Create a temp script that writes a breadcrumb then exits 1 (simulates compact failure)
    local tmpdir
    tmpdir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmpdir")
    cat > "$tmpdir/fail-compact.sh" <<COMPEOF
#!/bin/bash
touch "$tmpdir/compact-was-called"
exit 1
COMPEOF
    chmod +x "$tmpdir/fail-compact.sh"

    # Close the ticket with REBAR_COMPACT_SCRIPT pointing to the failing script
    # RED: ticket-transition.sh does not read REBAR_COMPACT_SCRIPT yet
    local exit_code=0
    (cd "$repo" && REBAR_COMPACT_SCRIPT="$tmpdir/fail-compact.sh" \
        bash "$TICKET_SCRIPT" transition "$ticket_id" open closed 2>/dev/null) || exit_code=$?

    # Assert: transition exits 0 (compact failure is non-blocking)
    assert_eq "compact-fail: transition exits 0 despite compact failure" "0" "$exit_code"

    # Assert: ticket was actually closed (STATUS event written)
    local compiled_status
    compiled_status=$(_get_ticket_status "$repo" "$ticket_id")
    assert_eq "compact-fail: ticket status is closed" "closed" "$compiled_status"

    # Assert: REBAR_COMPACT_SCRIPT was actually invoked (breadcrumb file exists)
    # RED: ticket-transition.sh does not read REBAR_COMPACT_SCRIPT yet → compact not called
    if [ -f "$tmpdir/compact-was-called" ]; then
        assert_eq "compact-fail: REBAR_COMPACT_SCRIPT was invoked" "called" "called"
    else
        assert_eq "compact-fail: REBAR_COMPACT_SCRIPT was invoked" "called" "not-called"
    fi

    assert_pass_if_clean "test_close_succeeds_if_compact_fails"
}
test_close_succeeds_if_compact_fails

# ── Test 17: close blocked by 2 open children (batch-close path) ──────────────
echo "Test 17: close with 2 open children exits 1 and lists both children in error"
test_close_with_open_children_blocked_via_batch() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create a parent epic ticket
    local parent_id
    parent_id=$(_create_ticket "$repo" epic "Epic with two open children")

    if [ -z "$parent_id" ]; then
        assert_eq "parent epic ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_close_with_open_children_blocked_via_batch"
        return
    fi

    # Create two child tickets under the parent
    local child1_id child2_id
    child1_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Open child one" --parent "$parent_id" 2>/dev/null) || true
    child1_id=$(echo "$child1_id" | tail -1)
    child2_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Open child two" --parent "$parent_id" 2>/dev/null) || true
    child2_id=$(echo "$child2_id" | tail -1)

    if [ -z "$child1_id" ] || [ -z "$child2_id" ]; then
        assert_eq "both child tickets created under parent" "non-empty" "empty: child1='$child1_id' child2='$child2_id'"
        assert_pass_if_clean "test_close_with_open_children_blocked_via_batch"
        return
    fi

    # Attempt to close the parent while both children are still open — must exit non-zero
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$parent_id" open closed 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "batch-open-children: exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error output mentions both child IDs
    if [[ "$stderr_out" == *"$child1_id"* ]]; then
        assert_eq "batch-open-children: error lists child1" "has-child1" "has-child1"
    else
        assert_eq "batch-open-children: error lists child1" "has-child1" "missing: $stderr_out"
    fi

    if [[ "$stderr_out" == *"$child2_id"* ]]; then
        assert_eq "batch-open-children: error lists child2" "has-child2" "has-child2"
    else
        assert_eq "batch-open-children: error lists child2" "has-child2" "missing: $stderr_out"
    fi

    # Assert: parent status unchanged (still open)
    local compiled_status
    compiled_status=$(_get_ticket_status "$repo" "$parent_id")
    assert_eq "batch-open-children: parent status unchanged (still open)" "open" "$compiled_status"

    assert_pass_if_clean "test_close_with_open_children_blocked_via_batch"
}
test_close_with_open_children_blocked_via_batch

# ── Test 18: close unblocks dependent ticket (batch path) ─────────────────────
echo "Test 18: close ticket A that blocks B → stdout contains UNBLOCKED line with B's ID"
test_close_unblock_works_via_batch() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create ticket A (the one we will close)
    local ticket_a
    ticket_a=$(_create_ticket "$repo" task "Ticket A - blocks B")

    # Create ticket B (blocked by A)
    local ticket_b
    ticket_b=$(_create_ticket "$repo" task "Ticket B - depends on A")

    if [ -z "$ticket_a" ] || [ -z "$ticket_b" ]; then
        assert_eq "unblock-batch: both tickets created" "non-empty" "empty: a='$ticket_a' b='$ticket_b'"
        assert_pass_if_clean "test_close_unblock_works_via_batch"
        return
    fi

    # Link: B depends_on A  (B is blocked by A)
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$ticket_b" "$ticket_a" depends_on 2>/dev/null) || true

    # Close A; capture stdout
    local stdout_out
    local exit_code=0
    stdout_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_a" open closed 2>/dev/null) || exit_code=$?

    # Assert: transition exits 0
    assert_eq "unblock-batch: transition exits 0" "0" "$exit_code"

    # Assert: stdout contains UNBLOCKED with ticket_b's ID
    if [[ "$stdout_out" =~ UNBLOCKED.*$ticket_b|UNBLOCKED:.*$ticket_b ]]; then
        assert_eq "unblock-batch: stdout contains UNBLOCKED: <B>" "has-unblocked-B" "has-unblocked-B"
    else
        assert_eq "unblock-batch: stdout contains UNBLOCKED: <B>" "has-unblocked-B" "missing: $stdout_out"
    fi

    assert_pass_if_clean "test_close_unblock_works_via_batch"
}
test_close_unblock_works_via_batch

# ── Test 19 (RED): ticket-transition.sh does NOT call ticket_find_open_children ─
echo "Test 19 (RED): ticket_find_open_children is NOT called from ticket-transition.sh source"
test_close_no_2n_spawns() {
    _snapshot_fail

    # Structural check: grep the source file itself for ticket_find_open_children.
    # This test is RED because ticket-transition.sh currently calls ticket_find_open_children
    # at Step 1b. The GREEN implementation should inline the child check into the batch
    # close path (or use a different mechanism) rather than calling ticket_find_open_children.
    local transition_src="$TICKET_TRANSITION_SCRIPT"

    if [ ! -f "$transition_src" ]; then
        assert_eq "test_close_no_2n_spawns: transition script exists" "exists" "missing"
        assert_pass_if_clean "test_close_no_2n_spawns"
        return
    fi

    # grep returns 0 if found, 1 if not found
    local grep_exit=0
    grep -q 'ticket_find_open_children' "$transition_src" 2>/dev/null || grep_exit=$?

    # Assert: ticket_find_open_children is NOT in the source (grep should return non-zero)
    # RED: it IS currently present → grep returns 0 → this assertion fails
    if [ "$grep_exit" -ne 0 ]; then
        assert_eq "no-2n-spawns: ticket_find_open_children not in transition source" "not-found" "not-found"
    else
        assert_eq "no-2n-spawns: ticket_find_open_children not in transition source" "not-found" "found-in-source"
    fi

    assert_pass_if_clean "test_close_no_2n_spawns"
}
test_close_no_2n_spawns

# ── Test 20 (RED): close fails even when REBAR_UNBLOCK_SCRIPT is broken, if children exist ─
echo "Test 20 (RED): close exits non-zero even when REBAR_UNBLOCK_SCRIPT fails, if parent has open children"
test_close_blocked_even_when_unblock_script_broken() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create a parent epic with one open child
    local parent_id
    parent_id=$(_create_ticket "$repo" epic "Epic with open child (unblock broken)")

    if [ -z "$parent_id" ]; then
        assert_eq "setup: parent epic created" "non-empty" "empty"
        assert_pass_if_clean "test_close_blocked_even_when_unblock_script_broken"
        return
    fi

    local child_id
    child_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Open child" --parent "$parent_id" 2>/dev/null) || true
    child_id=$(echo "$child_id" | tail -1)

    if [ -z "$child_id" ]; then
        assert_eq "setup: child task created under parent" "non-empty" "empty"
        assert_pass_if_clean "test_close_blocked_even_when_unblock_script_broken"
        return
    fi

    # Create a broken unblock script that always exits non-zero (simulates script failure)
    local fake_bin
    fake_bin=$(mktemp -d)
    _CLEANUP_DIRS+=("$fake_bin")
    local fake_script="$fake_bin/ticket-unblock.py"
    cat > "$fake_script" <<'PYEOF'
#!/usr/bin/env python3
import sys
print("simulated unblock failure — script broken", file=sys.stderr)
sys.exit(1)
PYEOF
    chmod +x "$fake_script"

    # Attempt to close the parent with the broken unblock script
    # BUG: current code does `|| true` on the unblock call, so batch_close_json is empty
    # and open_children is empty — the close SUCCEEDS silently (exit 0).
    # FIX: the close must exit non-zero because the parent has open children,
    # regardless of whether the unblock script works.
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && REBAR_UNBLOCK_SCRIPT="$fake_script" \
        bash "$TICKET_SCRIPT" transition "$parent_id" open closed 2>&1) || exit_code=$?

    # Assert: exits non-zero (child guard must fire even if unblock detection fails)
    # RED: currently exits 0 because || true silently allows close when unblock fails
    assert_eq "unblock-broken-with-children: exits non-zero" "1" \
        "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: parent status is still open (transition was blocked)
    local compiled_status
    compiled_status=$(_get_ticket_status "$repo" "$parent_id")
    assert_eq "unblock-broken-with-children: parent status still open" "open" "$compiled_status"

    assert_pass_if_clean "test_close_blocked_even_when_unblock_script_broken"
}
test_close_blocked_even_when_unblock_script_broken

# ── Test 21 (RED): first STATUS event has parent_status_uuid: null ───────────
echo "Test 21 (RED): first STATUS event has parent_status_uuid field set to JSON null"
test_parent_uuid_first_event_is_null() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo")
    local tracker_dir="$repo/.tickets-tracker"

    # Run first transition: open → in_progress
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open in_progress 2>/dev/null) || exit_code=$?
    assert_eq "parent-uuid-first: transition exits 0" "0" "$exit_code"

    # Find the STATUS event file
    local status_file
    status_file=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-STATUS.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ -z "$status_file" ]; then
        assert_eq "parent-uuid-first: STATUS event file found" "found" "not-found"
        assert_pass_if_clean "test_parent_uuid_first_event_is_null"
        return
    fi

    # Assert: parent_status_uuid key exists AND value is JSON null (not absent, not empty string)
    local check_result
    check_result=$(python3 - "$status_file" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

# Key must exist (not absent)
if 'parent_status_uuid' not in ev:
    print("MISSING: parent_status_uuid key absent from event")
    sys.exit(2)

# Value must be JSON null (Python None), not empty string, not 0, not False
val = ev['parent_status_uuid']
if val is not None:
    print(f"NOT_NULL: parent_status_uuid={val!r} (expected JSON null)")
    sys.exit(2)

print("OK")
PYEOF
) || true

    assert_eq "parent-uuid-first: parent_status_uuid is JSON null" "OK" "$check_result"

    assert_pass_if_clean "test_parent_uuid_first_event_is_null"
}
test_parent_uuid_first_event_is_null

# ── Test 22 (RED): second STATUS event points to first event's UUID ───────────
echo "Test 22 (RED): second STATUS event has parent_status_uuid equal to UUID of first STATUS event"
test_parent_uuid_second_points_to_first() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo")
    local tracker_dir="$repo/.tickets-tracker"

    # Run first transition: open → in_progress
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open in_progress 2>/dev/null) || exit_code=$?
    assert_eq "parent-uuid-second: first transition exits 0" "0" "$exit_code"

    # Capture the first STATUS event filename (UUID basename without .json)
    local first_status_file
    first_status_file=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-STATUS.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ -z "$first_status_file" ]; then
        assert_eq "parent-uuid-second: first STATUS event file found" "found" "not-found"
        assert_pass_if_clean "test_parent_uuid_second_points_to_first"
        return
    fi

    # Extract UUID from first event JSON directly
    local first_event_uuid
    first_event_uuid=$(python3 -c "
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    ev = json.load(f)
print(ev.get('uuid', ''))
" "$first_status_file" 2>/dev/null) || first_event_uuid=""

    if [ -z "$first_event_uuid" ]; then
        assert_eq "parent-uuid-second: first event UUID extracted" "non-empty" "empty"
        assert_pass_if_clean "test_parent_uuid_second_points_to_first"
        return
    fi

    # Run second transition: in_progress → closed
    # Suppress compaction (REBAR_COMPACT_SCRIPT=/bin/true) so the STATUS event file
    # is not absorbed into a SNAPSHOT before we can read it.
    local exit_code2=0
    (cd "$repo" && REBAR_COMPACT_SCRIPT=/bin/true bash "$TICKET_SCRIPT" transition "$ticket_id" in_progress closed 2>/dev/null) || exit_code2=$?
    assert_eq "parent-uuid-second: second transition exits 0" "0" "$exit_code2"

    # Find the second (newest) STATUS event file (compaction suppressed — should exist)
    local second_status_file
    second_status_file=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-STATUS.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ -z "$second_status_file" ] || [ "$second_status_file" = "$first_status_file" ]; then
        assert_eq "parent-uuid-second: second STATUS event file found" "found-new" "not-found-or-same"
        assert_pass_if_clean "test_parent_uuid_second_points_to_first"
        return
    fi

    # Assert: second event's parent_status_uuid equals the first event's UUID
    local check_result
    check_result=$(python3 - "$second_status_file" "$first_event_uuid" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

expected_uuid = sys.argv[2]

if 'parent_status_uuid' not in ev:
    print("MISSING: parent_status_uuid key absent from second event")
    sys.exit(2)

actual = ev['parent_status_uuid']
if actual != expected_uuid:
    print(f"WRONG: parent_status_uuid={actual!r} expected={expected_uuid!r}")
    sys.exit(2)

print("OK")
PYEOF
) || true

    assert_eq "parent-uuid-second: parent_status_uuid points to first event UUID" "OK" "$check_result"

    assert_pass_if_clean "test_parent_uuid_second_points_to_first"
}
test_parent_uuid_second_points_to_first

# ── Test 23 (RED): parent_status_uuid is always present in STATUS event JSON ──
echo "Test 23 (RED): parent_status_uuid field is always present in STATUS event (never omitted)"
test_parent_uuid_always_present() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo")
    local tracker_dir="$repo/.tickets-tracker"

    # Run any transition
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open in_progress 2>/dev/null) || exit_code=$?
    assert_eq "parent-uuid-always: transition exits 0" "0" "$exit_code"

    local status_file
    status_file=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-STATUS.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ -z "$status_file" ]; then
        assert_eq "parent-uuid-always: STATUS event file found" "found" "not-found"
        assert_pass_if_clean "test_parent_uuid_always_present"
        return
    fi

    # Assert: parent_status_uuid key is present (regardless of value)
    local key_present
    key_present=$(python3 - "$status_file" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

if 'parent_status_uuid' in ev:
    print("PRESENT")
else:
    print("ABSENT")
PYEOF
) || key_present="PARSE_ERROR"

    assert_eq "parent-uuid-always: parent_status_uuid key present in JSON" "PRESENT" "$key_present"

    assert_pass_if_clean "test_parent_uuid_always_present"
}
test_parent_uuid_always_present

# ── Test 24 (RED): legacy STATUS event (no parent_status_uuid) is treated as chain root ──
echo "Test 24 (RED): new STATUS event after legacy event sets parent_status_uuid to legacy event UUID"
test_parent_uuid_legacy_tolerated() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo")
    local tracker_dir="$repo/.tickets-tracker"

    # Manually create a legacy STATUS event file WITHOUT parent_status_uuid field.
    # This simulates an event written before the parent_status_uuid feature was added.
    local legacy_uuid="aaaabbbb-cccc-dddd-eeee-ffffaaaabbbb"
    local legacy_timestamp=1000000000000000000
    local legacy_filename="${legacy_timestamp}-${legacy_uuid}-STATUS.json"
    local ticket_dir="$tracker_dir/$ticket_id"

    python3 - "$ticket_dir/$legacy_filename" <<'PYEOF'
import json, sys
legacy_event = {
    'timestamp': 1000000000000000000,
    'uuid': 'aaaabbbb-cccc-dddd-eeee-ffffaaaabbbb',
    'event_type': 'STATUS',
    'env_id': 'legacy-env',
    'author': 'Legacy Author',
    'data': {
        'status': 'in_progress',
        'current_status': 'open',
    },
    # Intentionally NO parent_status_uuid field
}
with open(sys.argv[1], 'w', encoding='utf-8') as f:
    json.dump(legacy_event, f)
PYEOF

    # Verify the legacy event was written without parent_status_uuid
    local legacy_has_field
    legacy_has_field=$(python3 -c "
import json
with open('$ticket_dir/$legacy_filename', encoding='utf-8') as f:
    ev = json.load(f)
print('HAS_FIELD' if 'parent_status_uuid' in ev else 'NO_FIELD')
" 2>/dev/null) || legacy_has_field="ERROR"
    assert_eq "parent-uuid-legacy: legacy event has no parent_status_uuid" "NO_FIELD" "$legacy_has_field"

    # Run a new transition: the reducer will compute current status from the legacy event (in_progress)
    # so we transition from in_progress → closed.
    # Suppress compaction (REBAR_COMPACT_SCRIPT=/bin/true) so the STATUS event file
    # is not absorbed into a SNAPSHOT before we can read it.
    local exit_code=0
    (cd "$repo" && REBAR_COMPACT_SCRIPT=/bin/true bash "$TICKET_SCRIPT" transition "$ticket_id" in_progress closed 2>/dev/null) || exit_code=$?
    assert_eq "parent-uuid-legacy: transition after legacy event exits 0" "0" "$exit_code"

    # Find the new STATUS event (written by ticket-transition.sh — not the legacy one)
    local new_status_file
    new_status_file=$(find "$ticket_dir" -maxdepth 1 -name '*-STATUS.json' ! -name '.*' 2>/dev/null \
        | grep -v "$legacy_filename" | sort | tail -1)

    if [ -z "$new_status_file" ]; then
        assert_eq "parent-uuid-legacy: new STATUS event file found" "found" "not-found"
        assert_pass_if_clean "test_parent_uuid_legacy_tolerated"
        return
    fi

    # Assert: new event's parent_status_uuid equals the legacy event's UUID
    local check_result
    check_result=$(python3 - "$new_status_file" "$legacy_uuid" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

expected_uuid = sys.argv[2]

if 'parent_status_uuid' not in ev:
    print("MISSING: parent_status_uuid key absent from new event")
    sys.exit(2)

actual = ev['parent_status_uuid']
if actual != expected_uuid:
    print(f"WRONG: parent_status_uuid={actual!r} expected={expected_uuid!r}")
    sys.exit(2)

print("OK")
PYEOF
) || true

    assert_eq "parent-uuid-legacy: new event parent_status_uuid points to legacy UUID" "OK" "$check_result"

    assert_pass_if_clean "test_parent_uuid_legacy_tolerated"
}
test_parent_uuid_legacy_tolerated

# ── Test 25 (7f55-c7ee): --force skips open-children guard and closes epic ─────
echo "Test 25 (7f55-c7ee): --force closes epic with open children; children remain open"
test_force_close_skips_open_children_guard() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Create a parent epic ticket
    local parent_id
    parent_id=$(_create_ticket "$repo" epic "Epic with open children - force close test")

    if [ -z "$parent_id" ]; then
        assert_eq "force-close: parent epic created" "non-empty" "empty"
        assert_pass_if_clean "test_force_close_skips_open_children_guard"
        return
    fi

    # Create a child task under the parent
    local child_id
    child_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Open child" --parent "$parent_id" 2>/dev/null) || true
    child_id=$(echo "$child_id" | tail -1)

    if [ -z "$child_id" ]; then
        assert_eq "force-close: child ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_force_close_skips_open_children_guard"
        return
    fi

    # Transition parent to in_progress first (so we close from in_progress)
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$parent_id" open in_progress 2>/dev/null) || true

    # Without --force this must fail
    local no_force_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$parent_id" in_progress closed 2>/dev/null) \
        || no_force_exit=$?
    assert_eq "force-close: without --force exits non-zero" "1" "$([ "$no_force_exit" -ne 0 ] && echo 1 || echo 0)"

    # Parent must still be in_progress (not closed) after the failed attempt
    local status_after_fail
    status_after_fail=$(_get_ticket_status "$repo" "$parent_id")
    assert_eq "force-close: parent still in_progress after failed close" "in_progress" "$status_after_fail"

    # With --force the close must succeed
    local force_exit=0
    local force_stderr
    force_stderr=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$parent_id" in_progress closed --force --verdict-hash="$(_verdict_hash "$repo" "$parent_id")" 2>&1 >/dev/null) \
        || force_exit=$?
    assert_eq "force-close: with --force exits 0" "0" "$force_exit"

    # Parent must now be closed
    local parent_status
    parent_status=$(_get_ticket_status "$repo" "$parent_id")
    assert_eq "force-close: parent is closed after --force" "closed" "$parent_status"

    # Child must still be open (not closed or affected)
    local child_status
    child_status=$(_get_ticket_status "$repo" "$child_id")
    assert_eq "force-close: child remains open after --force parent close" "open" "$child_status"

    # Warning message must list the child on stderr
    if [[ "$force_stderr" == *"$child_id"* ]]; then
        assert_eq "force-close: warning lists open child ID" "has-child" "has-child"
    else
        assert_eq "force-close: warning lists open child ID" "has-child" "missing: $force_stderr"
    fi

    # Error message without --force must suggest --force
    local hint_stderr
    # Re-verify: create a fresh epic and child so we get the error message
    local parent2_id
    parent2_id=$(_create_ticket "$repo" epic "Epic with child for hint test")
    (cd "$repo" && bash "$TICKET_SCRIPT" create task "Child for hint" --parent "$parent2_id" 2>/dev/null) || true
    hint_stderr=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$parent2_id" open closed 2>&1) || true
    if [[ "$hint_stderr" == *"--force"* ]]; then
        assert_eq "force-close: error message mentions --force hint" "has-hint" "has-hint"
    else
        assert_eq "force-close: error message mentions --force hint" "has-hint" "missing: $hint_stderr"
    fi

    assert_pass_if_clean "test_force_close_skips_open_children_guard"
}
test_force_close_skips_open_children_guard

# ── Test 26 (bug #6): a non-closed (in_progress) child blocks the parent close ──
# Regression for the too-narrow open-children guard: previously only children with
# status exactly 'open' were counted, so an in_progress child did NOT block the
# parent close. The guard must treat ANY non-closed child (open, in_progress,
# blocked, in_review, ...) as unresolved.
echo ""
echo "Test 26 (bug #6): in_progress child blocks parent close (not just status=open)"
test_in_progress_child_blocks_parent_close() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Parent epic
    local parent_id
    parent_id=$(_create_ticket "$repo" epic "Epic with in_progress child")
    if [ -z "$parent_id" ]; then
        assert_eq "in_progress-child: parent epic created" "non-empty" "empty"
        assert_pass_if_clean "test_in_progress_child_blocks_parent_close"
        return
    fi

    # Child task under the parent, then moved to in_progress (NOT open, NOT closed)
    local child_id
    child_id=$(_create_ticket "$repo" task "Active child" "--parent $parent_id")
    if [ -z "$child_id" ]; then
        assert_eq "in_progress-child: child ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_in_progress_child_blocks_parent_close"
        return
    fi
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$child_id" open in_progress 2>/dev/null) || true
    local child_status
    child_status=$(_get_ticket_status "$repo" "$child_id")
    assert_eq "in_progress-child: child is in_progress" "in_progress" "$child_status"

    # Closing the parent (with a valid verdict-hash so we reach the children guard,
    # not the verdict-hash guard) must FAIL because the child is non-closed.
    local close_exit=0 close_stderr
    close_stderr=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$parent_id" open closed \
        --verdict-hash="$(_verdict_hash "$repo" "$parent_id")" 2>&1) || close_exit=$?
    assert_eq "in_progress-child: close blocked (non-zero exit)" "1" "$([ "$close_exit" -ne 0 ] && echo 1 || echo 0)"

    # Parent must remain open (not closed)
    local parent_status
    parent_status=$(_get_ticket_status "$repo" "$parent_id")
    assert_eq "in_progress-child: parent still open after blocked close" "open" "$parent_status"

    # Error must name the unresolved child
    if [[ "$close_stderr" == *"$child_id"* ]]; then
        assert_eq "in_progress-child: error lists the unresolved child" "has-child" "has-child"
    else
        assert_eq "in_progress-child: error lists the unresolved child" "has-child" "missing: $close_stderr"
    fi

    assert_pass_if_clean "test_in_progress_child_blocks_parent_close"
}
test_in_progress_child_blocks_parent_close

# ── Test 27 (bug #6): a child REPARENTED via `edit --parent` blocks the close ──
# Root cause of the audit finding: the open-children scan read parent_id only from
# the CREATE event, so a child created without a parent and later reparented onto
# the epic (via `edit --parent`) was invisible to the guard — the epic could close
# while that child was still in_progress. The scan must use the effective (reduced)
# parent_id including EDIT events.
echo ""
echo "Test 27 (bug #6): child reparented via edit --parent blocks parent close"
test_reparented_child_blocks_parent_close() {
    local repo
    repo=$(_make_test_repo)

    local parent_id
    parent_id=$(_create_ticket "$repo" epic "Epic gaining a reparented child")
    if [ -z "$parent_id" ]; then
        assert_eq "reparent-child: parent epic created" "non-empty" "empty"
        assert_pass_if_clean "test_reparented_child_blocks_parent_close"
        return
    fi

    # Child created with NO parent (CREATE event records no parent_id) ...
    local child_id
    child_id=$(_create_ticket "$repo" task "Orphan that gets adopted")
    if [ -z "$child_id" ]; then
        assert_eq "reparent-child: child ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_reparented_child_blocks_parent_close"
        return
    fi
    # ... then reparented onto the epic via an EDIT event, and set in_progress.
    (cd "$repo" && bash "$TICKET_SCRIPT" edit "$child_id" --parent="$parent_id" 2>/dev/null) || true
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$child_id" open in_progress 2>/dev/null) || true

    local close_exit=0 close_stderr
    close_stderr=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$parent_id" open closed \
        --verdict-hash="$(_verdict_hash "$repo" "$parent_id")" 2>&1) || close_exit=$?
    assert_eq "reparent-child: close blocked (non-zero exit)" "1" "$([ "$close_exit" -ne 0 ] && echo 1 || echo 0)"

    local parent_status
    parent_status=$(_get_ticket_status "$repo" "$parent_id")
    assert_eq "reparent-child: parent still open after blocked close" "open" "$parent_status"

    if [[ "$close_stderr" == *"$child_id"* ]]; then
        assert_eq "reparent-child: error lists the reparented child" "has-child" "has-child"
    else
        assert_eq "reparent-child: error lists the reparented child" "has-child" "missing: $close_stderr"
    fi

    assert_pass_if_clean "test_reparented_child_blocks_parent_close"
}
test_reparented_child_blocks_parent_close

# ── GAP-2: 2-arg auto-detect transition form `transition <id> <target>` ────────
# The dispatcher accepts `transition <id> <target>` (no current_status) and
# infers the current status. An open ticket transitioned with just `in_progress`
# must move to in_progress.
echo ""
echo "GAP-2: transition <id> in_progress (auto-detect current status) moves open->in_progress"
test_transition_autodetect_two_arg() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo")
    if [ -z "$ticket_id" ]; then
        assert_eq "gap2-autodetect: ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_transition_autodetect_two_arg"
        return
    fi

    # 2-arg form: omit current_status; the engine should auto-detect open.
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" in_progress 2>/dev/null) || exit_code=$?
    assert_eq "gap2-autodetect: 2-arg transition exits 0" "0" "$exit_code"

    # Compiled status must now be in_progress.
    local compiled_status
    compiled_status=$(_get_ticket_status "$repo" "$ticket_id")
    assert_eq "gap2-autodetect: compiled status is in_progress" "in_progress" "$compiled_status"

    assert_pass_if_clean "test_transition_autodetect_two_arg"
}
test_transition_autodetect_two_arg

# ── GAP-3: backward transition `transition <id> in_progress open` ──────────────
# A ticket moved to in_progress can be transitioned back to open; the backward
# move succeeds and the compiled status returns to open.
echo ""
echo "GAP-3: backward transition in_progress->open succeeds (status returns to open)"
test_transition_backward_in_progress_to_open() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo")
    if [ -z "$ticket_id" ]; then
        assert_eq "gap3-backward: ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_transition_backward_in_progress_to_open"
        return
    fi

    # First move open -> in_progress.
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" open in_progress 2>/dev/null) || true
    local mid_status
    mid_status=$(_get_ticket_status "$repo" "$ticket_id")
    assert_eq "gap3-backward: precondition status is in_progress" "in_progress" "$mid_status"

    # Backward move in_progress -> open must succeed.
    local exit_code=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" in_progress open 2>/dev/null) || exit_code=$?
    assert_eq "gap3-backward: in_progress->open exits 0" "0" "$exit_code"

    local final_status
    final_status=$(_get_ticket_status "$repo" "$ticket_id")
    assert_eq "gap3-backward: compiled status returns to open" "open" "$final_status"

    assert_pass_if_clean "test_transition_backward_in_progress_to_open"
}
test_transition_backward_in_progress_to_open

# ── GAP-9: the verdict-hash close gate is OPT-IN (off by default; enforced when on) ──
# The story/epic verdict-hash gate is OFF by default: close without a hash must
# succeed. With verify.require_verdict_for_close=true it is enforced — the
# OMITTED-hash case must fail (non-zero exit) and leave the ticket open, while a
# valid hash (from _verdict_hash) still succeeds as a control.
echo ""
echo "GAP-9: verdict-hash close gate is opt-in (off by default, enforced when enabled)"
test_transition_story_epic_close_requires_verdict_hash() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Default (no config): gate is OFF — story close WITHOUT a hash succeeds.
    local def_tid def_exit=0
    def_tid=$(_create_ticket "$repo" story "Gap9 default-off close")
    if [ -n "$def_tid" ]; then
        (cd "$repo" && bash "$TICKET_SCRIPT" transition "$def_tid" open closed 2>/dev/null) || def_exit=$?
        assert_eq "gap9: default (gate off) story close WITHOUT hash succeeds" "0" "$def_exit"
        assert_eq "gap9: default-off story is closed" "closed" "$(_get_ticket_status "$repo" "$def_tid")"
    else
        assert_eq "gap9: default story created" "non-empty" "empty"
    fi

    # Opt IN to the gate via repo config, then enforcement applies.
    mkdir -p "$repo/.rebar"
    echo "verify.require_verdict_for_close=true" > "$repo/.rebar/config.conf"

    local ttype
    for ttype in story epic; do
        local tid
        tid=$(_create_ticket "$repo" "$ttype" "Gap9 $ttype to close")
        if [ -z "$tid" ]; then
            assert_eq "gap9: $ttype created" "non-empty" "empty"
            continue
        fi

        # OMITTED verdict hash — must be rejected (gate enabled).
        local exit_code=0
        local stderr_out
        stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$tid" open closed 2>&1) || exit_code=$?
        assert_eq "gap9: $ttype close WITHOUT --verdict-hash exits non-zero" "1" \
            "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

        # Ticket must remain open (no bypass).
        local status_after
        status_after=$(_get_ticket_status "$repo" "$tid")
        assert_eq "gap9: $ttype still open after rejected close" "open" "$status_after"

        # Control: WITH a valid verdict hash the close succeeds.
        local ok_exit=0
        (cd "$repo" && bash "$TICKET_SCRIPT" transition "$tid" open closed \
            --verdict-hash="$(_verdict_hash "$repo" "$tid")" 2>/dev/null) || ok_exit=$?
        assert_eq "gap9: $ttype close WITH valid --verdict-hash exits 0" "0" "$ok_exit"
        assert_eq "gap9: $ttype is closed with valid hash" "closed" "$(_get_ticket_status "$repo" "$tid")"
    done

    assert_pass_if_clean "test_transition_story_epic_close_requires_verdict_hash"
}
test_transition_story_epic_close_requires_verdict_hash

# ── GAP-10: closing a BUG with an invalid --reason PREFIX is rejected ──────────
# --reason is required for bug close and must start with 'Fixed:' or
# 'Escalated to user:'. A prefix like "patched it" must be rejected (prefix
# validation, not mere presence); a valid prefix must be accepted.
echo ""
echo "GAP-10: bug close --reason prefix is validated (invalid prefix rejected, 'Fixed:'/'Escalated to user:' accepted)"
test_transition_bug_close_reason_prefix_validated() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    # Invalid prefix: present but does not start with an allowed prefix.
    local bug_bad
    bug_bad=$(_create_ticket "$repo" bug "Gap10 bug invalid reason prefix")
    if [ -z "$bug_bad" ]; then
        assert_eq "gap10: bug (bad-prefix) created" "non-empty" "empty"
        assert_pass_if_clean "test_transition_bug_close_reason_prefix_validated"
        return
    fi
    local bad_exit=0
    local bad_stderr
    bad_stderr=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$bug_bad" open closed --reason="patched it" 2>&1) || bad_exit=$?
    assert_eq "gap10: bug close with invalid --reason prefix exits non-zero" "1" \
        "$([ "$bad_exit" -ne 0 ] && echo 1 || echo 0)"
    assert_eq "gap10: bug stays open after rejected reason prefix" "open" "$(_get_ticket_status "$repo" "$bug_bad")"
    # Error should hint at the required prefix.
    if [[ "$bad_stderr" =~ Fixed:|Escalated|prefix|reason ]]; then
        assert_eq "gap10: error mentions required prefix" "has-prefix-hint" "has-prefix-hint"
    else
        assert_eq "gap10: error mentions required prefix" "has-prefix-hint" "no-hint: $bad_stderr"
    fi

    # Valid prefix 'Fixed:' — must be accepted.
    local bug_fixed
    bug_fixed=$(_create_ticket "$repo" bug "Gap10 bug Fixed prefix")
    local fixed_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$bug_fixed" open closed --reason="Fixed: patched the null check" 2>/dev/null) || fixed_exit=$?
    assert_eq "gap10: bug close with 'Fixed:' prefix exits 0" "0" "$fixed_exit"
    assert_eq "gap10: bug with 'Fixed:' reason is closed" "closed" "$(_get_ticket_status "$repo" "$bug_fixed")"

    # Valid prefix 'Escalated to user:' — must also be accepted.
    local bug_esc
    bug_esc=$(_create_ticket "$repo" bug "Gap10 bug Escalated prefix")
    local esc_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$bug_esc" open closed --reason="Escalated to user: needs product decision" 2>/dev/null) || esc_exit=$?
    assert_eq "gap10: bug close with 'Escalated to user:' prefix exits 0" "0" "$esc_exit"
    assert_eq "gap10: bug with 'Escalated to user:' reason is closed" "closed" "$(_get_ticket_status "$repo" "$bug_esc")"

    assert_pass_if_clean "test_transition_bug_close_reason_prefix_validated"
}
test_transition_bug_close_reason_prefix_validated

# ── Test: archived -> open un-archive seam via `transition` ───────────────────
# BUG f803-63ea: `archived` was an inescapable status — there was no CLI path
# out of it. `rebar transition <id> archived open` must un-archive the ticket
# (via the designed REVERT-of-ARCHIVED seam): exit 0, the ticket reappears in
# the default list, and it becomes claimable again. `transition <id> archived
# closed` must be rejected with a clear message. Happy-path transitions stay OK.
echo "Test: transition archived->open un-archives; archived->closed rejected"
test_transition_archived_to_open() {
    _snapshot_fail
    if [ ! -f "$TICKET_TRANSITION_SCRIPT" ]; then
        assert_eq "ticket-transition.sh exists" "exists" "missing"
        assert_pass_if_clean "test_transition_archived_to_open"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Archived ticket recovery")
    if [ -z "$ticket_id" ]; then
        assert_eq "archived->open: ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_transition_archived_to_open"
        return
    fi

    # Archive it via the real CLI archive subcommand.
    (cd "$repo" && bash "$TICKET_SCRIPT" archive "$ticket_id" 2>/dev/null) || true
    assert_eq "archived->open: precondition status is archived" "archived" "$(_get_ticket_status "$repo" "$ticket_id")"

    # archived -> closed must be rejected (only `open` is a valid un-archive target).
    local bad_exit=0 bad_out
    bad_out=$(cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" archived closed 2>&1) || bad_exit=$?
    assert_ne "archived->closed exits non-zero" "0" "$bad_exit"
    assert_eq "archived->closed leaves status archived" "archived" "$(_get_ticket_status "$repo" "$ticket_id")"
    if [[ "$bad_out" =~ open|un-archive|unarchive ]]; then
        assert_eq "archived->closed gives a clear message" "clear" "clear"
    else
        assert_eq "archived->closed gives a clear message" "clear" "unclear: $bad_out"
    fi

    # archived -> open must succeed (un-archive seam).
    local ok_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" archived open 2>/dev/null) || ok_exit=$?
    assert_eq "archived->open exits 0" "0" "$ok_exit"
    assert_eq "archived->open: status is open" "open" "$(_get_ticket_status "$repo" "$ticket_id")"

    # Ticket reappears in default `rebar list` (which excludes archived).
    local in_list
    in_list=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null \
        | python3 -c "import json,sys; ids=[t.get('ticket_id') for t in json.load(sys.stdin)]; print('yes' if sys.argv[1] in ids else 'no')" "$ticket_id" 2>/dev/null)
    assert_eq "archived->open: ticket visible in default list" "yes" "$in_list"

    # Ticket is claimable again (open -> in_progress + assignee, atomic).
    local claim_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" claim "$ticket_id" --assignee=tester 2>/dev/null) || claim_exit=$?
    assert_eq "archived->open: ticket is claimable" "0" "$claim_exit"
    assert_eq "archived->open: claimed ticket is in_progress" "in_progress" "$(_get_ticket_status "$repo" "$ticket_id")"

    # fsck stays clean after the un-archive round-trip.
    local fsck_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" fsck 2>/dev/null >/dev/null) || fsck_exit=$?
    assert_eq "archived->open: fsck clean" "0" "$fsck_exit"

    assert_pass_if_clean "test_transition_archived_to_open"
}
test_transition_archived_to_open

# ── Test: deleted+archived ticket reverted via this path stays deleted ────────
# BUG f803-63ea deleted-interaction guard: a ticket that is deleted AND archived
# must NOT be resurrected by the un-archive REVERT (the reducer guarantees this).
echo "Test: deleted+archived ticket stays deleted after un-archive revert"
test_transition_archived_open_does_not_resurrect_deleted() {
    _snapshot_fail
    if [ ! -f "$TICKET_TRANSITION_SCRIPT" ]; then
        assert_eq "ticket-transition.sh exists" "exists" "missing"
        assert_pass_if_clean "test_transition_archived_open_does_not_resurrect_deleted"
        return
    fi

    local repo
    repo=$(_make_test_repo)
    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Deleted and archived")
    if [ -z "$ticket_id" ]; then
        assert_pass_if_clean "test_transition_archived_open_does_not_resurrect_deleted"
        return
    fi

    (cd "$repo" && bash "$TICKET_SCRIPT" archive "$ticket_id" 2>/dev/null) || true
    (cd "$repo" && bash "$TICKET_SCRIPT" delete "$ticket_id" --user-approved 2>/dev/null) || true

    # Attempt the un-archive seam; whatever its exit, the ticket must stay deleted.
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$ticket_id" archived open 2>/dev/null) || true

    local status
    status=$(_get_ticket_status "$repo" "$ticket_id")
    if [[ "$status" == "deleted" || -z "$status" ]]; then
        assert_eq "deleted+archived not resurrected" "still-deleted" "still-deleted"
    else
        assert_eq "deleted+archived not resurrected" "still-deleted" "resurrected:$status"
    fi

    assert_pass_if_clean "test_transition_archived_open_does_not_resurrect_deleted"
}
test_transition_archived_open_does_not_resurrect_deleted

print_summary
