#!/usr/bin/env bash
# tests/lib/assert.sh
# Shared bash assertion helpers for plugin/hook test files.
#
# Usage (source into test scripts):
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/assert.sh"
#
# Usage (self-test):
#   RUN_SELF_TESTS=1 bash tests/lib/assert.sh
#
# Provides:
#   assert_eq(label, expected, actual)   — PASS/FAIL with message
#   assert_ne(label, not_expected, actual) — PASS/FAIL with message
#   assert_contains(label, substring, string) — PASS/FAIL with message
#   print_summary()                      — prints 'PASSED: N  FAILED: N' and exits with FAIL count
#
# Global counters (initialized on source, reset by print_summary):
#   PASS — number of passing assertions
#   FAIL — number of failing assertions

# Initialize counters (only if not already set — allows callers to accumulate)
: "${PASS:=0}"
: "${FAIL:=0}"

# All assertion helpers below print FAIL messages with the caller's source
# file and line on a SEPARATE `at:` line (audit P5-1). The `at:` line is
# placed *after* the `FAIL: <label>` line so the existing
# `parse_failing_tests_from_output` regex (red-zone.sh) — which requires the
# FAIL line to end immediately after the label — still matches. Putting the
# location on the same line as `FAIL:` would break red-zone tolerance
# project-wide.
#
# Use `${BASH_SOURCE[1]}` and `${BASH_LINENO[0]}` — NOT `${LINENO}`, which
# would expand to this library's own line.

# assert_eq label expected actual
# Increments PASS if expected == actual, FAIL otherwise.
# Note: actual receives a NARROW normalization that triggers ONLY when it
# matches the specific BSD `grep -c "..." || echo "0"` double-output pattern
# — two identical lines of all-digits. BSD grep exits 1 for 0-count, causing
# both grep ("0\n") and the fallback echo ("0\n") to fire, producing the
# "0\n0" string. The narrow normalization recovers the correct count without
# truncating legitimately multi-line actual values (e.g. comment bodies with
# embedded newlines compared by ticket-platform-compat tests). The earlier
# broad ``actual="${actual%%$'\n'*}"`` form silently truncated every
# multi-line value and is the cause of test-ticket-platform-compat C4
# regressing on the d2f9 recovery branch.
assert_eq() {
    local label="$1" expected="$2" actual="$3"
    # Narrow normalization: only collapse when actual looks like the BSD
    # grep -c quirk (identical short digit lines). Arbitrary multi-line
    # values pass through unchanged.
    if [[ "$actual" == *$'\n'* ]]; then
        local _aeq_first="${actual%%$'\n'*}"
        local _aeq_rest="${actual#*$'\n'}"
        if [[ "$_aeq_first" == "$_aeq_rest" ]] \
            && [[ "$_aeq_first" =~ ^[0-9]+$ ]]; then
            actual="$_aeq_first"
        fi
    fi
    if [[ "$expected" == "$actual" ]]; then
        (( ++PASS ))
    else
        (( ++FAIL ))
        printf "FAIL: %s\n  at:       %s:%s\n  expected: %s\n  actual:   %s\n" "$label" "${BASH_SOURCE[1]:-?}" "${BASH_LINENO[0]:-?}" "$expected" "$actual" >&2
    fi
}

# assert_ne label not_expected actual
# Increments PASS if not_expected != actual, FAIL otherwise.
assert_ne() {
    local label="$1" not_expected="$2" actual="$3"
    if [[ "$not_expected" != "$actual" ]]; then
        (( ++PASS ))
    else
        (( ++FAIL ))
        printf "FAIL: %s\n  at:            %s:%s\n  should NOT be: %s\n  actual:        %s\n" "$label" "${BASH_SOURCE[1]:-?}" "${BASH_LINENO[0]:-?}" "$not_expected" "$actual" >&2
    fi
}

# assert_contains label substring string
# Increments PASS if substring appears in string, FAIL otherwise.
assert_contains() {
    local label="$1" substring="$2" string="$3"
    if [[ "$string" == *"$substring"* ]]; then
        (( ++PASS ))
    else
        (( ++FAIL ))
        printf "FAIL: %s\n  at:                  %s:%s\n  expected to contain: %s\n  actual:              %s\n" "$label" "${BASH_SOURCE[1]:-?}" "${BASH_LINENO[0]:-?}" "$substring" "$string" >&2
    fi
}

# assert_not_contains label substring string
# Increments PASS if substring does NOT appear in string, FAIL otherwise.
assert_not_contains() {
    local label="$1" substring="$2" string="$3"
    if [[ "$string" != *"$substring"* ]]; then
        (( ++PASS ))
    else
        (( ++FAIL ))
        printf "FAIL: %s\n  at:                      %s:%s\n  expected NOT to contain: %s\n  actual:                  %s\n" "$label" "${BASH_SOURCE[1]:-?}" "${BASH_LINENO[0]:-?}" "$substring" "$string" >&2
    fi
}

# _snapshot_fail
# Captures current FAIL count for later comparison by assert_pass_if_clean.
_snapshot_fail() { _fail_snapshot=$FAIL; }

# assert_pass_if_clean label
# Prints "label ... PASS" if no new failures occurred since last _snapshot_fail.
assert_pass_if_clean() {
    local label="$1"
    if [[ -z "${_fail_snapshot+x}" ]]; then
        echo "ERROR: assert_pass_if_clean called without _snapshot_fail for: $label" >&2
        (( ++FAIL ))
        return
    fi
    if [[ "$FAIL" -eq "$_fail_snapshot" ]]; then
        echo "$label ... PASS"
    else
        # Keep "FAIL: <label>" first-line shape parseable by red-zone.sh.
        printf "FAIL: %s\n  at: %s:%s\n" "$label" "${BASH_SOURCE[1]:-?}" "${BASH_LINENO[0]:-?}" >&2
    fi
}

# print_summary
# Prints 'PASSED: N  FAILED: N' and exits with 1 if FAIL > 0, else 0.
print_summary() {
    echo ""
    printf "PASSED: %d  FAILED: %d\n" "$PASS" "$FAIL"
    if [[ "$FAIL" -gt 0 ]]; then
        exit 1
    fi
    exit 0
}

# ============================================================
# Self-tests (only run when RUN_SELF_TESTS=1)
# ============================================================
if [[ "${RUN_SELF_TESTS:-0}" == "1" ]]; then
    # Use isolated counters for self-tests so sourcing callers are not affected
    PASS=0
    FAIL=0

    echo "=== assert.sh self-tests ==="

    # --- assert_eq ---
    echo ""
    echo "--- assert_eq ---"

    # test_assert_eq_passes_on_match: matching values should increment PASS
    _pass_before=$PASS
    assert_eq "match: equal strings" "hello" "hello"
    if [[ $PASS -eq $(( _pass_before + 1 )) ]]; then
        echo "PASS: test_assert_eq_passes_on_match"
    else
        echo "FAIL: test_assert_eq_passes_on_match — PASS counter not incremented"
        (( FAIL++ ))
    fi

    # test_assert_eq_fails_on_mismatch: mismatched values should increment FAIL
    _fail_before=$FAIL
    _pass_before=$PASS
    # Temporarily capture stderr to avoid polluting self-test output
    assert_eq "mismatch: different strings" "expected_value" "actual_value" 2>/dev/null
    if [[ $FAIL -eq $(( _fail_before + 1 )) && $PASS -eq $_pass_before ]]; then
        echo "PASS: test_assert_eq_fails_on_mismatch"
        # Correct the FAIL counter — we expected the failure and already counted it above
        (( FAIL-- ))
    else
        echo "FAIL: test_assert_eq_fails_on_mismatch — FAIL counter not incremented"
        (( FAIL++ ))
    fi

    # Additional assert_eq cases
    assert_eq "empty strings equal" "" ""
    assert_eq "numeric strings equal" "42" "42"

    # --- assert_ne ---
    echo ""
    echo "--- assert_ne ---"

    _pass_before=$PASS
    assert_ne "ne: different strings" "foo" "bar"
    if [[ $PASS -eq $(( _pass_before + 1 )) ]]; then
        echo "PASS: assert_ne passes when values differ"
    else
        echo "FAIL: assert_ne should pass when values differ"
        (( FAIL++ ))
    fi

    _fail_before=$FAIL
    _pass_before=$PASS
    assert_ne "ne: same strings" "same" "same" 2>/dev/null
    if [[ $FAIL -eq $(( _fail_before + 1 )) && $PASS -eq $_pass_before ]]; then
        echo "PASS: assert_ne fails when values are equal"
        (( FAIL-- ))  # correct — we expected this failure
    else
        echo "FAIL: assert_ne should fail when values are equal"
        (( FAIL++ ))
    fi

    assert_ne "ne: empty vs non-empty" "" "non-empty"

    # --- assert_contains ---
    echo ""
    echo "--- assert_contains ---"

    _pass_before=$PASS
    assert_contains "contains: substring present" "world" "hello world"
    if [[ $PASS -eq $(( _pass_before + 1 )) ]]; then
        echo "PASS: assert_contains passes when substring present"
    else
        echo "FAIL: assert_contains should pass when substring is present"
        (( FAIL++ ))
    fi

    _fail_before=$FAIL
    _pass_before=$PASS
    assert_contains "contains: substring absent" "missing" "hello world" 2>/dev/null
    if [[ $FAIL -eq $(( _fail_before + 1 )) && $PASS -eq $_pass_before ]]; then
        echo "PASS: assert_contains fails when substring absent"
        (( FAIL-- ))  # correct — we expected this failure
    else
        echo "FAIL: assert_contains should fail when substring is absent"
        (( FAIL++ ))
    fi

    assert_contains "contains: exact match" "hello" "hello"
    assert_contains "contains: multi-word substring" "foo bar" "prefix foo bar suffix"

    # --- print_summary exits; call only at end ---
    echo ""
    echo "=== Self-test results ==="
    print_summary
fi
