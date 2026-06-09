#!/usr/bin/env bash
# tests/scripts/test-purge-non-project-tickets.sh
# Tests for scripts/purge-non-project-tickets.sh
#
# Usage: bash tests/scripts/test-purge-non-project-tickets.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REBAR_PLUGIN_DIR="$PLUGIN_ROOT/src/rebar/_engine"
SCRIPT="$REBAR_PLUGIN_DIR/purge-non-project-tickets.sh"
# Canonical implementation (tested for behavioral correctness)
CANONICAL="$REBAR_PLUGIN_DIR/ticket-purge-bridge.sh"

source "$SCRIPT_DIR/../lib/run_test.sh"

echo "=== test-purge-non-project-tickets.sh ==="

# ── Test 1: Script is executable ─────────────────────────────────────────────
echo "Test 1: Script is executable"
if [ -x "$SCRIPT" ]; then
    echo "  PASS: script is executable"
    (( PASS++ ))
else
    echo "  FAIL: script is not executable" >&2
    (( FAIL++ ))
fi

# ── Test 2: No bash syntax errors ────────────────────────────────────────────
echo "Test 2: No bash syntax errors"
if bash -n "$SCRIPT" 2>/dev/null; then
    echo "  PASS: no syntax errors"
    (( PASS++ ))
else
    echo "  FAIL: syntax errors found" >&2
    (( FAIL++ ))
fi

# ── Test 3: Missing --keep flag exits with error ─────────────────────────────
echo "Test 3: Missing --keep flag exits with error"
run_test "missing --keep exits non-zero" 1 "Error.*--keep" bash "$SCRIPT"

# ── Test 4: Script initializes tracker when dir doesn't exist (worktree startup) ─
echo "Test 4: test_init_on_missing_tracker — calls ticket-init.sh when tracker missing"
test_init_on_missing_tracker() {
    # Behavioral test: verifies that ticket-purge-bridge.sh (canonical) invokes
    # ticket-init.sh when the tracker dir doesn't exist and TICKETS_TRACKER_DIR is not set.
    # Tests the canonical implementation directly — purge-non-project-tickets.sh is now
    # a thin wrapper that execs ticket-purge-bridge.sh.
    # Same anti-pattern as sprint-list-epics (3b71-e877).
    local TDIR4 STUB_CALLED
    TDIR4=$(mktemp -d)
    STUB_CALLED="$TDIR4/init-was-called"

    # Copy the canonical implementation into the temp dir
    cp "$CANONICAL" "$TDIR4/ticket-purge-bridge.sh"
    chmod +x "$TDIR4/ticket-purge-bridge.sh"

    # Create a stub ticket-init.sh that records invocation and creates
    # an empty tracker dir (so the script proceeds past the check)
    cat > "$TDIR4/ticket-init.sh" << 'STUBEOF'
#!/usr/bin/env bash
touch "$STUB_CALLED_FILE"
# Create the tracker dir so the script doesn't error after init
mkdir -p "$PROJECT_ROOT/.tickets-tracker"
exit 0
STUBEOF
    chmod +x "$TDIR4/ticket-init.sh"

    # PROJECT_ROOT has no .tickets-tracker; TICKETS_TRACKER_DIR is unset (default path)
    local fake_root="$TDIR4/fake-repo"
    mkdir -p "$fake_root"

    STUB_CALLED_FILE="$STUB_CALLED" PROJECT_ROOT="$fake_root" \
        bash "$TDIR4/ticket-purge-bridge.sh" --keep=TEST >/dev/null 2>&1 || true

    local was_called=false
    [ -f "$STUB_CALLED" ] && was_called=true

    rm -rf "$TDIR4"

    [ "$was_called" = "true" ]
}
if test_init_on_missing_tracker; then
    echo "  PASS: script calls ticket-init.sh when tracker dir missing"
    (( PASS++ ))
else
    echo "  FAIL: script did not call ticket-init.sh — fresh worktrees will fail" >&2
    (( FAIL++ ))
fi

# ── Test 5: Tracker init only runs for default path, not TICKETS_TRACKER_DIR ──
echo "Test 5: test_init_skipped_for_override — no init when TICKETS_TRACKER_DIR is set"
test_init_skipped_for_override() {
    local TDIR5 STUB_CALLED
    TDIR5=$(mktemp -d)
    STUB_CALLED="$TDIR5/init-was-called"

    # Test canonical implementation (ticket-purge-bridge.sh) — same behavior
    cp "$CANONICAL" "$TDIR5/ticket-purge-bridge.sh"
    chmod +x "$TDIR5/ticket-purge-bridge.sh"

    cat > "$TDIR5/ticket-init.sh" << 'STUBEOF'
#!/usr/bin/env bash
touch "$STUB_CALLED_FILE"
exit 0
STUBEOF
    chmod +x "$TDIR5/ticket-init.sh"

    local nonexistent_tracker="$TDIR5/no-such-tracker"

    STUB_CALLED_FILE="$STUB_CALLED" TICKETS_TRACKER_DIR="$nonexistent_tracker" \
        bash "$TDIR5/ticket-purge-bridge.sh" --keep=TEST >/dev/null 2>&1 || true

    local was_called=false
    [ -f "$STUB_CALLED" ] && was_called=true

    rm -rf "$TDIR5"

    [ "$was_called" = "false" ]
}
if test_init_skipped_for_override; then
    echo "  PASS: init is skipped when TICKETS_TRACKER_DIR is set"
    (( PASS++ ))
else
    echo "  FAIL: init was called even though TICKETS_TRACKER_DIR is set" >&2
    (( FAIL++ ))
fi

# ── Test 6: Init failure stderr is surfaced, not swallowed ──────────────────
echo "Test 6: test_init_failure_emits_stderr — diagnostic output reaches stderr when init fails"
test_init_failure_emits_stderr() {
    local TDIR6
    TDIR6=$(mktemp -d)

    # Test canonical implementation (ticket-purge-bridge.sh) — same behavior
    cp "$CANONICAL" "$TDIR6/ticket-purge-bridge.sh"
    chmod +x "$TDIR6/ticket-purge-bridge.sh"

    # Stub ticket-init.sh: emits a diagnostic on stderr and exits non-zero
    cat > "$TDIR6/ticket-init.sh" << 'STUBEOF'
#!/usr/bin/env bash
echo "ERROR: tracker mount failed" >&2
exit 1
STUBEOF
    chmod +x "$TDIR6/ticket-init.sh"

    local fake_root="$TDIR6/fake-repo"
    mkdir -p "$fake_root"

    local captured_stderr
    captured_stderr=$(TICKETS_TRACKER_DIR='' PROJECT_ROOT="$fake_root" \
        bash "$TDIR6/ticket-purge-bridge.sh" --keep=TEST 2>&1 >/dev/null) || true

    rm -rf "$TDIR6"

    # The stub's error message must appear in stderr — not be silently swallowed
    [[ "$captured_stderr" == *"tracker mount failed"* ]]
}
if test_init_failure_emits_stderr; then
    echo "  PASS: init failure diagnostic is emitted on stderr"
    (( PASS++ ))
else
    echo "  FAIL: init failure stderr was silently swallowed — diagnostic output lost" >&2
    (( FAIL++ ))
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
