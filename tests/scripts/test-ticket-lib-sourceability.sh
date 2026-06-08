#!/usr/bin/env bash
# tests/scripts/test-ticket-lib-sourceability.sh
# Regression suite: locks in the sourceability contract for ticket-lib-api.sh.
#
# Contract: sourcing ticket-lib-api.sh must not leak set -euo pipefail, exit
# calls, EXIT traps, GIT_* mutations, or silent function-name collisions into
# the caller's shell.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_LIB_API="$REPO_ROOT/src/rebar/_engine/ticket-lib-api.sh"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-ticket-lib-sourceability.sh ==="

# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Strict-mode caller test
# Source ticket-lib-api.sh from a shell that has set -euo pipefail and a
# pre-set EXIT trap; assert trap and set-o state are preserved after sourcing.
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test 1: strict-mode caller — EXIT trap and set -euo pipefail survive sourcing"

test_strict_mode_caller() {
    (
        set -euo pipefail
        trap 'echo CALLER_EXIT_TRAP' EXIT

        # Capture trap and options BEFORE sourcing
        trap_before=$(trap -p EXIT)
        errexit_before=$(set -o | grep errexit  | awk '{print $2}')
        nounset_before=$(set -o | grep nounset  | awk '{print $2}')
        pipefail_before=$(set -o | grep pipefail | awk '{print $2}')

        # Source the library
        source "$TICKET_LIB_API"

        # Capture AFTER sourcing
        trap_after=$(trap -p EXIT)
        errexit_after=$(set -o | grep errexit  | awk '{print $2}')
        nounset_after=$(set -o | grep nounset  | awk '{print $2}')
        pipefail_after=$(set -o | grep pipefail | awk '{print $2}')

        # Emit results for parent subshell to assert on
        echo "TRAP_BEFORE=$trap_before"
        echo "TRAP_AFTER=$trap_after"
        echo "ERREXIT_BEFORE=$errexit_before"
        echo "ERREXIT_AFTER=$errexit_after"
        echo "NOUNSET_BEFORE=$nounset_before"
        echo "NOUNSET_AFTER=$nounset_after"
        echo "PIPEFAIL_BEFORE=$pipefail_before"
        echo "PIPEFAIL_AFTER=$pipefail_after"
    )
}

t1_output=$(test_strict_mode_caller 2>/dev/null)

# Parse the output lines into variables
trap_before=$(echo  "$t1_output" | grep '^TRAP_BEFORE='    | cut -d= -f2-)
trap_after=$(echo   "$t1_output" | grep '^TRAP_AFTER='     | cut -d= -f2-)
errexit_before=$(echo  "$t1_output" | grep '^ERREXIT_BEFORE='  | cut -d= -f2-)
errexit_after=$(echo   "$t1_output" | grep '^ERREXIT_AFTER='   | cut -d= -f2-)
nounset_before=$(echo  "$t1_output" | grep '^NOUNSET_BEFORE='  | cut -d= -f2-)
nounset_after=$(echo   "$t1_output" | grep '^NOUNSET_AFTER='   | cut -d= -f2-)
pipefail_before=$(echo "$t1_output" | grep '^PIPEFAIL_BEFORE=' | cut -d= -f2-)
pipefail_after=$(echo  "$t1_output" | grep '^PIPEFAIL_AFTER='  | cut -d= -f2-)

assert_contains "T1: EXIT trap still set after sourcing"    "CALLER_EXIT_TRAP" "$trap_after"
assert_eq       "T1: errexit unchanged after sourcing"      "$errexit_before"  "$errexit_after"
assert_eq       "T1: nounset unchanged after sourcing"      "$nounset_before"  "$nounset_after"
assert_eq       "T1: pipefail unchanged after sourcing"     "$pipefail_before" "$pipefail_after"

# ─────────────────────────────────────────────────────────────────────────────
# Test 2: No bare `exit` in function bodies
# For each ticket_* / _ticketlib_* function, assert zero occurrences of bare
# `exit` (word-boundary; `return` is fine; subshell scripts are acceptable).
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test 2: no bare 'exit' in ticket_* / _ticketlib_* function bodies"

test_no_bare_exit() {
    (
        source "$TICKET_LIB_API"

        bad_fns=()
        while IFS= read -r fn_line; do
            fn_name="${fn_line%% *}"
            # Only check ticket_* and _ticketlib_* namespaced functions
            case "$fn_name" in
                ticket_*|_ticketlib_*) ;;
                *) continue ;;
            esac
            fn_body=$(declare -f "$fn_name" 2>/dev/null)
            # Word-boundary match: `exit` as a standalone word (not inside a string
            # or a variable name), followed by optional code/space/newline/end.
            # We look for `exit` as a statement — preceded by whitespace/semicolon/newline
            # and followed by a space, digit, or newline. This avoids false-positives
            # from compound words or comments referencing "exit".
            if grep -qE '(^|[[:space:]|;])exit([[:space:]]|$)' <<< "$fn_body"; then
                bad_fns+=("$fn_name")
            fi
        done < <(declare -F)

        if [[ ${#bad_fns[@]} -eq 0 ]]; then
            echo "NO_BARE_EXIT=true"
        else
            echo "NO_BARE_EXIT=false"
            printf "BAD_FN:%s\n" "${bad_fns[@]}"
        fi
    )
}

t2_output=$(test_no_bare_exit 2>/dev/null)
no_bare_exit=$(echo "$t2_output" | grep '^NO_BARE_EXIT=' | cut -d= -f2)
bad_fns_list=$(echo "$t2_output" | grep '^BAD_FN:' | sed 's/^BAD_FN://' | tr '\n' ' ')

assert_eq "T2: no bare 'exit' in ticket_* / _ticketlib_* function bodies (bad fns: ${bad_fns_list:-none})" \
    "true" "$no_bare_exit"

# ─────────────────────────────────────────────────────────────────────────────
# Test 3: GIT_* env var preservation
# Export sentinel GIT_* vars before sourcing; invoke _ticketlib_dispatch
# ticket_show (expected to fail — no tracker in cwd); assert all four retain
# their sentinel values after the call.
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test 3: GIT_* env vars preserved across source and dispatch"

test_git_env_preservation() {
    (
        export GIT_DIR=/tmp/sentinel-gitdir
        export GIT_INDEX_FILE=/tmp/sentinel-index
        export GIT_WORK_TREE=/tmp/sentinel-work
        export GIT_COMMON_DIR=/tmp/sentinel-common

        source "$TICKET_LIB_API"

        # Invoke dispatch — will fail because no tracker exists in cwd; that's fine.
        _ticketlib_dispatch ticket_show "nonexistent-0000" 2>/dev/null || true

        echo "GIT_DIR_AFTER=${GIT_DIR:-}"
        echo "GIT_INDEX_FILE_AFTER=${GIT_INDEX_FILE:-}"
        echo "GIT_WORK_TREE_AFTER=${GIT_WORK_TREE:-}"
        echo "GIT_COMMON_DIR_AFTER=${GIT_COMMON_DIR:-}"
    )
}

t3_output=$(test_git_env_preservation 2>/dev/null)

git_dir_after=$(echo      "$t3_output" | grep '^GIT_DIR_AFTER='        | cut -d= -f2-)
git_index_after=$(echo    "$t3_output" | grep '^GIT_INDEX_FILE_AFTER=' | cut -d= -f2-)
git_work_after=$(echo     "$t3_output" | grep '^GIT_WORK_TREE_AFTER='  | cut -d= -f2-)
git_common_after=$(echo   "$t3_output" | grep '^GIT_COMMON_DIR_AFTER=' | cut -d= -f2-)

assert_eq "T3: GIT_DIR retained after source+dispatch"         "/tmp/sentinel-gitdir"   "$git_dir_after"
assert_eq "T3: GIT_INDEX_FILE retained after source+dispatch"  "/tmp/sentinel-index"    "$git_index_after"
assert_eq "T3: GIT_WORK_TREE retained after source+dispatch"   "/tmp/sentinel-work"     "$git_work_after"
assert_eq "T3: GIT_COMMON_DIR retained after source+dispatch"  "/tmp/sentinel-common"   "$git_common_after"

# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Function-name collision detection
# Define ticket_show before sourcing; source the library; call ticket_show and
# assert output is either "CALLER_VERSION" (caller wins) or valid JSON-like
# output (library wins) — but NOT a silent no-op / empty string.
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test 4: function-name collision — caller's ticket_show not silently clobbered to no-op"

test_function_collision() {
    (
        # Define caller's version BEFORE sourcing
        ticket_show() { echo "CALLER_VERSION"; }

        source "$TICKET_LIB_API"

        # Invoke ticket_show — pass no args so even the library version returns quickly with error.
        # Capture both stdout and stderr; at least one must be non-empty (not a silent no-op).
        stdout_out=$(ticket_show 2>/dev/null || true)
        stderr_out=$(ticket_show 2>&1 >/dev/null || true)

        echo "COLLISION_STDOUT=$stdout_out"
        echo "COLLISION_STDERR=$stderr_out"
    )
}

t4_output=$(test_function_collision 2>/dev/null)
collision_stdout=$(echo "$t4_output" | grep '^COLLISION_STDOUT=' | cut -d= -f2-)
collision_stderr=$(echo "$t4_output" | grep '^COLLISION_STDERR=' | cut -d= -f2-)

# The contract: at least one of stdout or stderr must be non-empty (not a silent no-op).
# If caller wins: stdout = "CALLER_VERSION". If library wins: stderr contains usage message.
combined_signal="${collision_stdout}${collision_stderr}"
assert_ne "T4: ticket_show produces some output after collision (not a silent no-op)" \
    "" "$combined_signal"

# Document actual behavior: check which side won
if [[ "$collision_stdout" == "CALLER_VERSION" ]]; then
    echo "  (info: caller's ticket_show was preserved — caller wins)"
else
    echo "  (info: library's ticket_show replaced caller's — library wins)"
    echo "       stdout=[$collision_stdout] stderr=[$collision_stderr]"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Test 5: EXIT trap non-leakage from dispatch
# After invoking _ticketlib_dispatch, assert the caller's EXIT trap is still
# present and has not been replaced by any internal trap set inside the subshell.
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test 5: EXIT trap not leaked / replaced by _ticketlib_dispatch"

test_exit_trap_nonleakage() {
    (
        # Establish caller's EXIT trap BEFORE sourcing
        trap 'echo CALLER_TRAP_STILL_SET' EXIT

        source "$TICKET_LIB_API"

        # Capture trap before dispatch
        trap_before_dispatch=$(trap -p EXIT)

        # Invoke dispatch (will fail — no tracker — that's fine)
        _ticketlib_dispatch ticket_show "nonexistent-0000" 2>/dev/null || true

        # Capture trap after dispatch
        trap_after_dispatch=$(trap -p EXIT)

        echo "TRAP_BEFORE_DISPATCH=$trap_before_dispatch"
        echo "TRAP_AFTER_DISPATCH=$trap_after_dispatch"
    )
}

t5_output=$(test_exit_trap_nonleakage 2>/dev/null)
trap_before_dispatch=$(echo "$t5_output" | grep '^TRAP_BEFORE_DISPATCH=' | cut -d= -f2-)
trap_after_dispatch=$(echo  "$t5_output" | grep '^TRAP_AFTER_DISPATCH='  | cut -d= -f2-)

assert_contains "T5: caller's EXIT trap still set before dispatch"  "CALLER_TRAP_STILL_SET" "$trap_before_dispatch"
assert_eq       "T5: EXIT trap unchanged after _ticketlib_dispatch" "$trap_before_dispatch" "$trap_after_dispatch"

# ─────────────────────────────────────────────────────────────────────────────
print_summary
