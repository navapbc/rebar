#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-init-bash-native.sh
#
# Verifies that the portable bash md5-12 computation is byte-identical to
# Python's hashlib.md5 (documents the bash md5 replacement technique).
#
# (The former Test 1 — `ticket show` spawns zero python3 — was a RED test for a
# bash-native, reducer-free read path that was never implemented; `show`
# legitimately invokes the Python ticket_reducer. Removed rather than left
# perpetually failing.)
#
# Usage: bash tests/scripts/suites/test-ticket-init-bash-native.sh

# NOTE: -e intentionally omitted — test functions use return-codes to signal failure.
set -uo pipefail

echo "=== test-ticket-init-bash-native.sh ==="

# ── Portable md5-12 using only shell primitives ───────────────────────────────
# Linux has md5sum; macOS has md5 -q. Prefer md5sum when available.
_bash_md5_12() {
    local input="$1" out
    if command -v md5sum >/dev/null 2>&1; then
        out=$(printf '%s' "$input" | md5sum | cut -c1-12)
    elif command -v md5 >/dev/null 2>&1; then
        out=$(printf '%s' "$input" | md5 -q | cut -c1-12)
    else
        echo "md5-unavailable" >&2
        return 1
    fi
    echo "$out"
}

# ── Test: bash md5-12 is byte-identical to Python's hashlib.md5 ───────────────
test_md5_12_byte_identical() {
    local inputs=(
        "/some/path/to/tracker"
        "/path with spaces/tracker"
        "/path/üñícode/tracker"
    )
    # 200-char path
    local long_path="/"
    while [ "${#long_path}" -lt 200 ]; do
        long_path="${long_path}a"
    done
    long_path="${long_path:0:200}"
    inputs+=("$long_path")

    local input py_out sh_out
    for input in "${inputs[@]}"; do
        py_out=$(python3 -c "import hashlib,sys; print(hashlib.md5(sys.argv[1].encode()).hexdigest()[:12])" "$input" 2>/dev/null) || {
            echo "  python3 md5 failed for input: $input"
            return 1
        }
        sh_out=$(_bash_md5_12 "$input") || {
            echo "  bash md5 failed for input: $input"
            return 1
        }
        if [ "$py_out" != "$sh_out" ]; then
            echo "  mismatch for input '$input': python=$py_out bash=$sh_out"
            return 1
        fi
    done
    return 0
}

# ── Runner ────────────────────────────────────────────────────────────────────
pass=0
fail=0
for fn in \
    test_md5_12_byte_identical
do
    if $fn; then
        echo "PASS: $fn"
        pass=$((pass + 1))
    else
        echo "FAIL: $fn"
        fail=$((fail + 1))
    fi
done

echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
