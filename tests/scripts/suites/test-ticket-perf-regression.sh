#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-perf-regression.sh
# Unit tests for ticket CLI performance regression-detection logic (task 7d55-cb75).
#
# These tests exercise the _compare_perf comparison function with fixture inputs —
# no live hyperfine runs. All tests complete in <10 seconds.
#
#   tests/perf/a0-baseline.json is created (task 51f0-a97a).
#
# GREEN-phase tests (always runnable once _compare_perf is defined):
#   - Equal measured and baseline → PASS
#   - 9% regression → PASS (within 10% threshold)
#   - 11% regression → FAIL (exceeds 10% threshold)
#   - Improvement (faster) → PASS
#
# hyperfine guard: if hyperfine not installed, skip integration runs (not relevant
# here since no hyperfine calls are made, but guard is present for future extension).
#
# Usage: bash tests/scripts/suites/test-ticket-perf-regression.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"

source "$REPO_ROOT/tests/lib/assert.sh"

# ── _compare_perf: pure comparison logic ────────────────────────────────────
# Arguments:
#   op_name        - name of the operation (for display)
#   measured_ms    - measured median in milliseconds (integer or float)
#   baseline_ms    - baseline median in milliseconds (integer or float)
#   threshold_frac - threshold multiplier (e.g., 1.10 for 10% tolerance)
#
# Returns:
#   0 if measured_ms <= baseline_ms * threshold_frac  (PASS)
#   1 if measured_ms >  baseline_ms * threshold_frac  (FAIL / regression)
#
# Stdout: "PASS: <details>" or "FAIL: <details>"
_compare_perf() {
    local op_name="$1"
    local measured_ms="$2"
    local baseline_ms="$3"
    local threshold_frac="$4"

    python3 - "$op_name" "$measured_ms" "$baseline_ms" "$threshold_frac" <<'PYEOF'
import sys
op      = sys.argv[1]
meas    = float(sys.argv[2])
base    = float(sys.argv[3])
thresh  = float(sys.argv[4])
limit   = base * thresh
if meas > limit:
    print("FAIL [{}]: {:.4f}ms > {:.4f}ms (baseline {:.4f}ms x {})".format(
        op, meas, limit, base, thresh))
    sys.exit(1)
else:
    print("PASS [{}]: {:.4f}ms <= {:.4f}ms (baseline {:.4f}ms x {})".format(
        op, meas, limit, base, thresh))
    sys.exit(0)
PYEOF
}

# ── Test: equal measured and baseline → PASS ─────────────────────────────────
test_compare_perf_equal_values_pass() {
    local output exit_code
    output=$(_compare_perf "show" 100 100 1.10 2>&1)
    exit_code=$?
    assert_eq "equal values: exit code 0" "0" "$exit_code"
    assert_contains "equal values: output contains PASS" "PASS" "$output"
}

# ── Test: 9% regression → PASS (within 10% threshold) ───────────────────────
test_compare_perf_nine_pct_regression_pass() {
    local output exit_code
    output=$(_compare_perf "list" 109 100 1.10 2>&1)
    exit_code=$?
    assert_eq "9% regression: exit code 0" "0" "$exit_code"
    assert_contains "9% regression: output contains PASS" "PASS" "$output"
}

# ── Test: 11% regression → FAIL (exceeds 10% threshold) ─────────────────────
test_compare_perf_eleven_pct_regression_fail() {
    local output exit_code
    output=$(_compare_perf "create" 111 100 1.10 2>&1)
    exit_code=$?
    assert_eq "11% regression: exit code 1" "1" "$exit_code"
    assert_contains "11% regression: output contains FAIL" "FAIL" "$output"
}

# ── Test: improvement (faster than baseline) → PASS ──────────────────────────
test_compare_perf_improvement_pass() {
    local output exit_code
    output=$(_compare_perf "tag" 50 100 1.10 2>&1)
    exit_code=$?
    assert_eq "improvement: exit code 0" "0" "$exit_code"
    assert_contains "improvement: output contains PASS" "PASS" "$output"
}

# ── test: parse a0-baseline.json and verify a real op comparison ─────────
# Once the baseline exists, it reads the "show" median and asserts PASS for a
# measured value equal to the baseline — exercising the full comparison pipeline.
_parse_baseline_ms() {
    # Usage: _parse_baseline_ms <baseline_file> <op>
    # Prints median in ms. Exits 1 if file missing or op absent.
    local baseline_file="$1" op="$2"
    python3 -c "
import json, sys
path, op = sys.argv[1], sys.argv[2]
with open(path) as f:
    data = json.load(f)
if op not in data:
    print('MISSING_OP', file=sys.stderr); sys.exit(1)
val = data[op].get('median', data[op].get('median_ms', None))
if val is None:
    print('MISSING_FIELD', file=sys.stderr); sys.exit(1)
ms = val * 1000 if val < 10 else val
print('{:.4f}'.format(ms))
" "$baseline_file" "$op"
}

test_compare_perf_reads_baseline_file() {
    local baseline_file="$REPO_ROOT/tests/perf/a0-baseline.json"

    local baseline_ms
    baseline_ms=$(_parse_baseline_ms "$baseline_file" "show" 2>&1)
    local parse_exit=$?

    if [ $parse_exit -ne 0 ]; then
        echo "FAIL [test_compare_perf_reads_baseline_file]: could not parse $baseline_file — $baseline_ms" >&2
        (( ++FAIL ))
        return
    fi

    local output exit_code
    output=$(_compare_perf "show" "$baseline_ms" "$baseline_ms" 1.10 2>&1)
    exit_code=$?

    assert_eq "baseline file read: equal values pass" "0" "$exit_code"
    assert_contains "baseline file read: output contains PASS" "PASS" "$output"
}

# ── Run all tests ─────────────────────────────────────────────────────────────
echo "=== test-ticket-perf-regression.sh ==="
echo ""

test_compare_perf_equal_values_pass
test_compare_perf_nine_pct_regression_pass
test_compare_perf_eleven_pct_regression_fail
test_compare_perf_improvement_pass
test_compare_perf_reads_baseline_file

print_summary
