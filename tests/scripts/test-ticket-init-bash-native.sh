#!/usr/bin/env bash
# tests/scripts/test-ticket-init-bash-native.sh
# RED tests for bash-native _ensure_initialized (task 269a-9362, epic 78fc-3858).
#
# Verifies that:
#   1. `ticket show` spawns zero python3 processes (bash-native path).
#   2. Bash-native md5 computation is byte-identical to Python's hashlib.md5.
#
# Test 1 verifies the bash-native (zero-python3) read path; Test 2 documents the
# bash md5 replacement.
#
# Usage: bash tests/scripts/test-ticket-init-bash-native.sh

# NOTE: -e intentionally omitted — test functions use return-codes to signal RED.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

# Shared git-fixtures exports _TICKET_TEST_NO_SYNC=1; unset here so individual
# tests can opt in explicitly.
unset _TICKET_TEST_NO_SYNC 2>/dev/null || true

source "$REPO_ROOT/tests/lib/git-fixtures.sh"
# git-fixtures sets _TICKET_TEST_NO_SYNC=1 globally — keep it per-test controllable.
unset _TICKET_TEST_NO_SYNC

echo "=== test-ticket-init-bash-native.sh ==="

# Track temp dirs / sentinel files for cleanup.
_CLEANUP_DIRS=()
_CLEANUP_FILES=()
_cleanup() {
    local d f
    for d in "${_CLEANUP_DIRS[@]:-}"; do
        [ -n "$d" ] && [ -d "$d" ] && rm -rf "$d"
    done
    for f in "${_CLEANUP_FILES[@]:-}"; do
        [ -n "$f" ] && [ -f "$f" ] && rm -f "$f"
    done
}
trap _cleanup EXIT

# Resolve the real python3 once so the PATH shim can exec it.
_REAL_PYTHON3="$(command -v python3 2>/dev/null || true)"
# If `command -v` returned a shim in PATH we haven't set up yet, fall back.
if [ -z "$_REAL_PYTHON3" ] || [ ! -x "$_REAL_PYTHON3" ]; then
    _REAL_PYTHON3="/usr/bin/python3"
fi

# ── Helper: create a PATH shim dir with a sentinel python3 wrapper ────────────
# The shim writes "CALLED" to the sentinel file, then execs the real python3 so
# any downstream logic still works.
_make_python3_shim() {
    local shim_dir sentinel
    shim_dir=$(mktemp -d)
    sentinel="$1"
    _CLEANUP_DIRS+=("$shim_dir")
    cat > "$shim_dir/python3" <<EOF
#!/usr/bin/env bash
echo "CALLED" >> "$sentinel"
exec "$_REAL_PYTHON3" "\$@"
EOF
    chmod +x "$shim_dir/python3"
    echo "$shim_dir"
}

# ── Helper: create an initialized ticket repo with one ticket ─────────────────
# Returns: "$repo_path $ticket_id" (space-separated).
_make_initialized_repo_with_ticket() {
    local tmp repo ticket_id
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_test_repo "$tmp/repo"
    repo="$tmp/repo"

    # Initialize ticket system and create a ticket (allow python3 here — we
    # measure python3 usage of _ensure_initialized later, not of init/create).
    (cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" init --silent) >/dev/null 2>&1 || {
        echo "setup-failed: ticket init" >&2
        return 1
    }
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "Bash-native test" 2>/dev/null | tail -1)
    if [ -z "$ticket_id" ]; then
        echo "setup-failed: ticket create" >&2
        return 1
    fi
    echo "$repo $ticket_id"
}

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

# ── Test 1: ticket show spawns zero python3 processes ─────────────────────────
test_ensure_initialized_no_python3() {
    local setup repo ticket_id sentinel shim_dir
    setup=$(_make_initialized_repo_with_ticket) || { echo "  setup failed"; return 1; }
    repo="${setup% *}"
    ticket_id="${setup##* }"

    sentinel="/tmp/python3-sentinel-$$-no"
    rm -f "$sentinel"
    _CLEANUP_FILES+=("$sentinel")

    shim_dir=$(_make_python3_shim "$sentinel")

    # Run ticket show with sentinel-shim prepended to PATH.
    # _TICKET_TEST_NO_SYNC=1 skips the fetch block (no remote in temp repo).
    local exit_code=0 err_file
    err_file=$(mktemp)
    _CLEANUP_FILES+=("$err_file")
    (
        cd "$repo"
        PATH="$shim_dir:$PATH" _TICKET_TEST_NO_SYNC=1 \
            bash "$TICKET_SCRIPT" show "$ticket_id" >/dev/null 2>"$err_file"
    ) || exit_code=$?

    # ticket show must succeed; independent failure is informative.
    if [ "$exit_code" -ne 0 ]; then
        echo "  ticket show exited $exit_code (expected 0); stderr:"
        sed 's/^/    /' "$err_file"
        return 1
    fi

    if [ -f "$sentinel" ]; then
        echo "  sentinel exists: python3 was spawned (expected zero spawns)"
        return 1
    fi
    return 0
}

# ── Test 2: bash md5-12 is byte-identical to Python's hashlib.md5 ─────────────
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
    test_ensure_initialized_no_python3 \
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
