#!/usr/bin/env bash
# tests/scripts/test-ticket-update-rav.sh
# RED test suite for ticket-update-rav.sh (read-after-write verification wrapper).
#
# Tests validate the script's interface behavior using REBAR_TICKET_RAV_TEST=1
# test-double mode — no real ticket mutations required.
#
# Test cases:
#   1. Script existence check → exits 1 (RED before impl)
#   2. create operation: mock write + verify → exits 0
#   3. tag operation: mock tag, verify tag present → exits 0
#   4. transition operation: mock transition, verify status matches → exits 0
#   5. mismatch: wrong expected value → exits 1 with structured JSON error
#   6. comment operation: mock comment, verify comment added → exits 0
#
# Usage: bash tests/scripts/test-ticket-update-rav.sh
# Returns: exit non-zero when any assertion fails.

# NOTE: -e is intentionally omitted — test functions return non-zero to assert failures.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
RAV_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-update-rav.sh"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-ticket-update-rav.sh ==="

# ── Suite-runner guard: skip GREEN tests until impl exists ──────────────────
# Tests 2-6 require the script to exist and run.
# In _RUN_ALL_ACTIVE mode, skip gracefully if script is absent (RED phase).
_script_exists() {
    [[ -f "$RAV_SCRIPT" && -x "$RAV_SCRIPT" ]]
}

# ── Test 1: Script existence check (RED before impl) ─────────────────────────
echo ""
echo "Test 1: script existence check"
test_script_exists() {
    _snapshot_fail
    if _script_exists; then
        assert_eq "script is executable" "yes" "yes"
    else
        # RED: script does not exist yet — this test passes only when RED, fails when GREEN
        # (this assertion correctly fails when the script is absent, making the suite RED)
        assert_eq "script exists at src/rebar/_engine/ticket-update-rav.sh" "found" "not_found"
    fi
    assert_pass_if_clean "Test 1: script exists"
}
test_script_exists

# ── Early exit if script absent (tests 2–6 require the impl) ─────────────────
if ! _script_exists; then
    echo ""
    echo "SKIP: Tests 2-6 require ticket-update-rav.sh to exist (currently RED)"
    echo ""
    printf "PASSED: %d  FAILED: %d\n" "$PASS" "$FAIL"
    if [[ "$FAIL" -gt 0 ]]; then
        exit 1
    fi
    exit 0
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

# Run the RAV script in test-double mode and capture stdout+stderr
_run_rav() {
    REBAR_TICKET_RAV_TEST=1 bash "$RAV_SCRIPT" "$@"
}

# Run and capture stderr separately
_run_rav_stderr() {
    local tmp_err
    tmp_err=$(mktemp "${TMPDIR:-/tmp}/rav-test-stderr.XXXXXX")
    local exit_code=0
    REBAR_TICKET_RAV_TEST=1 bash "$RAV_SCRIPT" "$@" 2>"$tmp_err" || exit_code=$?
    cat "$tmp_err" >&2
    local stderr_content
    stderr_content=$(cat "$tmp_err")
    rm -f "$tmp_err"
    echo "$stderr_content"
    return "$exit_code"
}

# Run and return stderr content via a named variable (avoids subshell exit code loss)
_run_rav_capture() {
    local _out_var="$1"
    shift
    local tmp_err
    tmp_err=$(mktemp "${TMPDIR:-/tmp}/rav-test-stderr.XXXXXX")
    local exit_code=0
    REBAR_TICKET_RAV_TEST=1 bash "$RAV_SCRIPT" "$@" 2>"$tmp_err" || exit_code=$?
    printf -v "$_out_var" '%s' "$(cat "$tmp_err")"
    rm -f "$tmp_err"
    return "$exit_code"
}

# ── Test 2: create operation — mock write + verify exits 0 ───────────────────
echo ""
echo "Test 2: create operation exits 0"
test_create_operation() {
    _snapshot_fail
    local exit_code=0
    _run_rav \
        --operation=create \
        --ticket-id=test-1234 \
        --assert-field=status \
        --assert-value=open \
        -- task "Test ticket" \
        >/dev/null 2>&1 || exit_code=$?
    assert_eq "create: exits 0" "0" "$exit_code"
    assert_pass_if_clean "Test 2: create exits 0"
}
test_create_operation

# ── Test 3: tag operation — mock tag, verify tag present exits 0 ─────────────
echo ""
echo "Test 3: tag operation exits 0"
test_tag_operation() {
    _snapshot_fail
    local exit_code=0
    _run_rav \
        --operation=tag \
        --ticket-id=test-1234 \
        --assert-field=tags \
        --assert-value=my-test-tag \
        -- test-1234 my-test-tag \
        >/dev/null 2>&1 || exit_code=$?
    assert_eq "tag: exits 0" "0" "$exit_code"
    assert_pass_if_clean "Test 3: tag exits 0"
}
test_tag_operation

# ── Test 4: transition operation — mock status, verify status matches exits 0 ──
echo ""
echo "Test 4: transition operation exits 0"
test_transition_operation() {
    _snapshot_fail
    local exit_code=0
    _run_rav \
        --operation=transition \
        --ticket-id=test-1234 \
        --assert-field=status \
        --assert-value=in_progress \
        -- test-1234 open in_progress \
        >/dev/null 2>&1 || exit_code=$?
    assert_eq "transition: exits 0" "0" "$exit_code"
    assert_pass_if_clean "Test 4: transition exits 0"
}
test_transition_operation

# ── Test 5: mismatch — wrong expected value → exits 1 with JSON error ─────────
echo ""
echo "Test 5: mismatch exits 1 with structured JSON"
test_mismatch_exits_1_with_json() {
    _snapshot_fail
    local stderr_out=""
    local exit_code=0
    _run_rav_capture stderr_out \
        --operation=tag \
        --ticket-id=test-1234 \
        --assert-field=tags \
        --assert-value=__wrong_tag_that_will_not_match__ \
        -- test-1234 my-test-tag \
        >/dev/null || exit_code=$?

    assert_eq "mismatch: exits 1" "1" "$exit_code"
    assert_contains "mismatch: stderr has rav_mismatch" '"rav_mismatch"' "$stderr_out"
    assert_contains "mismatch: stderr has operation field" '"operation"' "$stderr_out"
    assert_contains "mismatch: stderr has ticket_id field" '"ticket_id"' "$stderr_out"
    assert_contains "mismatch: stderr has intended_value field" '"intended_value"' "$stderr_out"
    assert_contains "mismatch: stderr has actual_value field" '"actual_value"' "$stderr_out"
    assert_pass_if_clean "Test 5: mismatch exits 1 with JSON"
}
test_mismatch_exits_1_with_json

# ── Test 6: comment operation — mock comment, verify comment added exits 0 ────
echo ""
echo "Test 6: comment operation exits 0"
test_comment_operation() {
    _snapshot_fail
    local exit_code=0
    _run_rav \
        --operation=comment \
        --ticket-id=test-1234 \
        --assert-field=comments \
        --assert-value=present \
        -- test-1234 "Test comment body" \
        >/dev/null 2>&1 || exit_code=$?
    assert_eq "comment: exits 0" "0" "$exit_code"
    assert_pass_if_clean "Test 6: comment exits 0"
}
test_comment_operation

# ── Summary ───────────────────────────────────────────────────────────────────
print_summary
