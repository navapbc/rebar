#!/usr/bin/env bash
# tests/lib/run_test.sh
# Shared test runner helper for plugin test suites.
#
# Usage:
#   source "$(dirname "${BASH_SOURCE[0]}")/../lib/run_test.sh"
#   run_test "test name" 0 "expected_pattern" command args...
#
# Provides:
#   run_test()     - Run a command and check exit code + output pattern
#   print_results  - Print PASS/FAIL summary and exit appropriately
#   PASS, FAIL     - Counters (initialized to 0, accumulate across calls)

: "${PASS:=0}"
: "${FAIL:=0}"

run_test() {
    local test_name="$1" expected_exit="$2" expected_pattern="$3"
    shift 3
    local exit_code=0
    local output
    output=$("$@" 2>&1) || exit_code=$?
    if [ "$exit_code" -ne "$expected_exit" ]; then
        echo "  FAIL: $test_name (expected exit $expected_exit, got $exit_code)" >&2
        (( FAIL++ ))
        return
    fi
    if [ -n "$expected_pattern" ] && ! { echo "$output" | grep -cE "$expected_pattern" >/dev/null 2>&1; }; then
        echo "  FAIL: $test_name (output missing pattern '$expected_pattern')" >&2
        echo "  Output was: $output" >&2
        (( FAIL++ ))
        return
    fi
    echo "  PASS: $test_name"
    (( PASS++ ))
}

print_results() {
    echo ""
    echo "PASSED: $PASS  FAILED: $FAIL"
    if [ "$FAIL" -gt 0 ]; then
        exit 1
    fi
    exit 0
}
