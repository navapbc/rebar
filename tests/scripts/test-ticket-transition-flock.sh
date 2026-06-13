#!/usr/bin/env bash
# tests/scripts/test-ticket-transition-flock.sh
# Structural assertion: verifies the flock critical section boundary in
# ticket-transition.sh is not widened by the S2 consolidation (e768-2bae).
#
# The flock section must contain ONLY:
#   - status read (reduce_ticket direct call — no subprocess)
#   - optimistic concurrency verify
#   - status event write (temp file + rename)
#   - git add + commit
#
# LLM formatting (to_llm / llm_format) must happen OUTSIDE the lock.
# The reduce_ticket import must appear in the flock-held Python block.
#
# Test 1 (GREEN after S2): assert ticket-transition.sh references reduce_ticket/ticket_reducer
#   (confirms S2 migration: direct import inside flock block, no subprocess for state read)
# Test 2 (guard): assert to_llm/llm_format does NOT appear inside the flock section
#   (protects against accidentally widening the lock to include formatting work)
#
# Usage: bash tests/scripts/test-ticket-transition-flock.sh
# Returns: exit 0 after S2 consolidation uses reduce_ticket directly inside flock block.

# NOTE: -e is intentionally omitted — test functions return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
# WS2: the flock-held critical section was extracted from ticket-transition.sh's
# heredoc into the importable ticket_txn.py module — it remains the lock-holding,
# committing entrypoint (one process: flock -> reduce/verify -> write -> commit).
# These structural guards now inspect ticket_txn.py, where that section lives.
TRANSITION_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket_txn.py"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-ticket-transition-flock.sh ==="
echo ""

# ── Helper: extract text between flock acquire and first release ──────────────
# Returns the content of ticket-transition.sh from the line containing
# fcntl.LOCK_EX (lock acquire) up to (but not including) the first
# 'os.close(fd)' line that follows the acquire — which is the lock release.
#
# This captures the critical section body: everything the script does
# while holding the lock.
_extract_flock_section() {
    local src="$1"
    python3 - "$src" <<'PYEOF'
import sys

with open(sys.argv[1], encoding='utf-8') as f:
    lines = f.readlines()

# Find the flock acquire line
acquire_idx = None
for i, line in enumerate(lines):
    if '_acquire_write_lock(' in line:
        acquire_idx = i
        break

if acquire_idx is None:
    print("FLOCK_ACQUIRE_NOT_FOUND", file=sys.stderr)
    sys.exit(1)

# Find the first os.close(fd) after the acquire — this is the lock release
# (closing the file descriptor releases the advisory lock)
release_idx = None
for i in range(acquire_idx + 1, len(lines)):
    if 'handle.release()' in lines[i]:
        release_idx = i
        break

if release_idx is None:
    print("FLOCK_RELEASE_NOT_FOUND", file=sys.stderr)
    sys.exit(1)

# Print the content between acquire and release (exclusive of both boundary lines)
for line in lines[acquire_idx + 1:release_idx]:
    sys.stdout.write(line)
PYEOF
}

# ── Test 1 (GREEN after S2): reduce_ticket call used inside flock section ─────
# After the S2 consolidation, ticket-transition.sh calls reduce_ticket() inside
# the flock critical section (between lock acquire and final lock release),
# eliminating the subprocess spawn for state reads.
# This test verifies the behavioral contract: reduce_ticket() is invoked after
# the lock is acquired and subprocess.run for reducer is no longer used.
echo "Test 1 (GREEN after S2): reduce_ticket() called in flock-held section, no subprocess reducer"
test_flock_reduce_ticket_in_critical_section() {
    _snapshot_fail

    if [ ! -f "$TRANSITION_SCRIPT" ]; then
        assert_eq "flock-ref: ticket-transition.sh exists" "exists" "missing"
        assert_pass_if_clean "test_flock_reduce_ticket_in_critical_section"
        return
    fi

    # Extract the full body between flock acquire and final lock release.
    # The extractor finds fcntl.LOCK_EX acquire and the LAST os.close(fd) in the
    # Python block (the final lock release), then checks the critical section body.
    local flock_body
    flock_body=$(python3 - "$TRANSITION_SCRIPT" <<'PYEOF'
import sys

with open(sys.argv[1], encoding='utf-8') as f:
    lines = f.readlines()

# Find the flock acquire line
acquire_idx = None
for i, line in enumerate(lines):
    if '_acquire_write_lock(' in line:
        acquire_idx = i
        break

if acquire_idx is None:
    print("FLOCK_ACQUIRE_NOT_FOUND", file=sys.stderr)
    sys.exit(1)

# Find the LAST os.close(fd) in the file after the acquire (final lock release)
# The first os.close(fd) after LOCK_EX is the failure-to-acquire path; the last
# is the actual lock release after the critical section completes.
release_idx = None
for i in range(acquire_idx + 1, len(lines)):
    if 'handle.release()' in lines[i]:
        release_idx = i  # keep updating to get the last one

if release_idx is None:
    print("FLOCK_RELEASE_NOT_FOUND", file=sys.stderr)
    sys.exit(1)

# Print the full content between acquire and last release
for line in lines[acquire_idx + 1:release_idx]:
    sys.stdout.write(line)
PYEOF
    ) || {
        local extract_err
        extract_err=$(python3 - "$TRANSITION_SCRIPT" <<'PYEOF2' 2>&1 >/dev/null
import sys
with open(sys.argv[1]) as f:
    content = f.read()
if '_acquire_write_lock(' not in content:
    print("FLOCK_ACQUIRE_NOT_FOUND")
PYEOF2
        ) || true
        assert_eq "flock-ref: flock body extractable" "extracted" "error: $extract_err"
        assert_pass_if_clean "test_flock_reduce_ticket_in_critical_section"
        return
    }

    # Assert 1: the flock body contains a reduce_ticket() call (S2 migration complete)
    if grep -qE 'reduce_ticket\(' <<< "$flock_body" 2>/dev/null; then
        assert_eq "flock-ref: reduce_ticket() called in flock body" "found" "found"
    else
        assert_eq "flock-ref: reduce_ticket() called in flock body" "found" "not-found (S2 not yet applied)"
    fi

    # Assert 2: the flock body does NOT spawn subprocess for reducer (old pattern absent)
    if grep -qE "subprocess\.run.*reducer_path|python3.*reducer_path" <<< "$flock_body" 2>/dev/null; then
        assert_eq "flock-ref: no subprocess reducer in flock body" "absent" "found (subprocess reducer still present)"
    else
        assert_eq "flock-ref: no subprocess reducer in flock body" "absent" "absent"
    fi

    assert_pass_if_clean "test_flock_reduce_ticket_in_critical_section"
}
test_flock_reduce_ticket_in_critical_section

# ── Test 2 (guard): llm_format/to_llm NOT inside flock critical section ───────
# This guard asserts the S2 implementation kept formatting work outside the lock.
# The flock section must contain only: status read, concurrency verify,
# event write, git add+commit. LLM formatting must happen outside the lock.
echo ""
echo "Test 2 (guard): to_llm/llm_format does NOT appear inside the flock section"
test_flock_boundary_not_widened() {
    _snapshot_fail

    if [ ! -f "$TRANSITION_SCRIPT" ]; then
        assert_eq "flock-boundary: ticket-transition.sh exists" "exists" "missing"
        assert_pass_if_clean "test_flock_boundary_not_widened"
        return
    fi

    # Extract the flock critical section (content between acquire and first release)
    local flock_section
    flock_section=$(_extract_flock_section "$TRANSITION_SCRIPT" 2>/dev/null) || {
        local extract_err
        extract_err=$(_extract_flock_section "$TRANSITION_SCRIPT" 2>&1 >/dev/null) || true
        assert_eq "flock-boundary: flock section extractable" "extracted" "error: $extract_err"
        assert_pass_if_clean "test_flock_boundary_not_widened"
        return
    }

    # Assert: the flock section does NOT reference llm_format or to_llm
    # GREEN on current code (not there at all), GREEN after S2 (added outside the lock),
    # RED if S2 accidentally puts the formatting call inside the lock.
    if grep -qE 'llm_format|to_llm' <<< "$flock_section" 2>/dev/null; then
        # Found inside lock — boundary was widened (regression)
        local offending_lines
        offending_lines=$(echo "$flock_section" | grep -E 'llm_format|to_llm' | head -5 | tr '\n' '|')
        assert_eq "flock-boundary: llm_format/to_llm NOT inside flock section" \
            "not-inside-lock" \
            "found-inside-lock: $offending_lines"
    else
        assert_eq "flock-boundary: llm_format/to_llm NOT inside flock section" \
            "not-inside-lock" \
            "not-inside-lock"
    fi

    assert_pass_if_clean "test_flock_boundary_not_widened"
}
test_flock_boundary_not_widened

# ── Test 3 (guard): flock section does not reference ticket-llm-format.py ─────
# The importlib dance used in ticket-list.sh and ticket-show.sh must NOT be
# pulled inside the flock section during S2 consolidation.
echo ""
echo "Test 3 (guard): importlib / ticket-llm-format path NOT inside flock section"
test_flock_no_importlib_inside_lock() {
    _snapshot_fail

    if [ ! -f "$TRANSITION_SCRIPT" ]; then
        assert_eq "flock-importlib: ticket-transition.sh exists" "exists" "missing"
        assert_pass_if_clean "test_flock_no_importlib_inside_lock"
        return
    fi

    local flock_section
    flock_section=$(_extract_flock_section "$TRANSITION_SCRIPT" 2>/dev/null) || {
        # If extraction fails (flock section not yet present), skip this guard
        assert_eq "flock-importlib: flock section extractable" "extracted" "not-extractable"
        assert_pass_if_clean "test_flock_no_importlib_inside_lock"
        return
    }

    # Assert: the flock section does NOT reference the old importlib-based llm_format path
    if grep -qE 'importlib|ticket.llm.format' <<< "$flock_section" 2>/dev/null; then
        local offending_lines
        offending_lines=$(echo "$flock_section" | grep -E 'importlib|ticket.llm.format' | head -5 | tr '\n' '|')
        assert_eq "flock-importlib: no importlib/ticket-llm-format inside flock section" \
            "not-inside-lock" \
            "found-inside-lock: $offending_lines"
    else
        assert_eq "flock-importlib: no importlib/ticket-llm-format inside flock section" \
            "not-inside-lock" \
            "not-inside-lock"
    fi

    assert_pass_if_clean "test_flock_no_importlib_inside_lock"
}
test_flock_no_importlib_inside_lock

print_summary
