#!/usr/bin/env bash
# tests/lib/suite-engine.sh
# Shared parallel test suite runner with per-test timeouts, fail-fast,
# and progress reporting.
#
# Usage (as a script — receives test file paths as arguments):
#   bash suite-engine.sh test1.sh test2.sh test3.sh ...
#
# Usage (sourced — for access to helper functions):
#   source suite-engine.sh
#   run_test_suite "Label" file1.sh file2.sh ...
#
# Environment variables:
#   TEST_TIMEOUT=30              Per-test timeout in seconds (default: 30)
#   MAX_PARALLEL=8               Max concurrent test processes (default: 8)
#   MAX_CONSECUTIVE_FAILS=5      Abort after N consecutive failures (default: 5)
#   SUITE_LABEL="Tests"          Label for progress output (default: "Tests")
#
# Output format:
#   [1/10] test-foo.sh ... PASS (3 pass, 0 fail)
#   [2/10] test-bar.sh ... FAIL (1 pass, 2 fail)
#   [3/10] test-slow.sh ... TIMEOUT (exceeded 30s)
#   ABORT: 5 consecutive failures — likely systemic issue
#
# Aggregated summary:
#   PASSED: 42  FAILED: 3
#
# Exit code: 0 if all pass, 1 if any fail or abort

set -uo pipefail

# Temp dir cleanup on exit
_CLEANUP_DIRS=()
_cleanup() { for d in "${_CLEANUP_DIRS[@]}"; do rm -rf "$d"; done; }
trap _cleanup EXIT

# --- Configuration from environment ---
: "${TEST_TIMEOUT:=30}"
# Cap parallelism to min(4, nproc/2) to prevent fork exhaustion when
# run-all.sh launches 4 concurrent suites (4 × MAX_PARALLEL × subprocess layers).
_nproc=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
_default_parallel=$(( _nproc / 2 ))
[ "$_default_parallel" -lt 2 ] && _default_parallel=2
[ "$_default_parallel" -gt 4 ] && _default_parallel=4
: "${MAX_PARALLEL:=$_default_parallel}"
unset _nproc _default_parallel
: "${MAX_CONSECUTIVE_FAILS:=5}"
: "${SUITE_LABEL:=Tests}"

# --- Repo file protection ---
# Snapshot critical repo files before tests run. After each test, verify they
# haven't been modified. This catches tests that accidentally leak git operations
# or file writes into the real working tree (e.g., via a failed git checkout that
# falls through to the real repo).
_SE_REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
declare -A _PROTECTED_FILE_HASHES=()

_snapshot_protected_files() {
    _PROTECTED_FILE_HASHES=()
    [[ -z "$_SE_REPO_ROOT" ]] && return
    local _pf
    for _pf in \
        "$_SE_REPO_ROOT/.claude/dso-config.conf" \
        "$_SE_REPO_ROOT/.test-index" \
    ; do
        if [[ -f "$_pf" ]]; then
            _PROTECTED_FILE_HASHES["$_pf"]=$(md5sum "$_pf" 2>/dev/null | cut -d' ' -f1 || echo "")
        fi
    done
}

# Verify protected files are unchanged. Returns 1 and restores files if tampering detected.
_verify_protected_files() {
    local _test_name="${1:-unknown}"
    [[ -z "$_SE_REPO_ROOT" ]] && return 0
    [[ ${#_PROTECTED_FILE_HASHES[@]} -eq 0 ]] && return 0
    local _tampered=false
    local _pf
    for _pf in "${!_PROTECTED_FILE_HASHES[@]}"; do
        local _expected="${_PROTECTED_FILE_HASHES[$_pf]}"
        local _actual=""
        if [[ -f "$_pf" ]]; then
            _actual=$(md5sum "$_pf" 2>/dev/null | cut -d' ' -f1 || echo "")
        fi
        if [[ "$_actual" != "$_expected" ]]; then
            _tampered=true
            echo "REPO PROTECTION: $_test_name modified $_pf — restoring from git" >&2
            git -C "$_SE_REPO_ROOT" checkout -- "$_pf" 2>/dev/null || true
        fi
    done
    [[ "$_tampered" == "true" ]] && return 1
    return 0
}

# --- RED zone tolerance (optional, enabled when SUITE_TEST_INDEX is set) ---
# Source red-zone.sh for parse_failing_tests_from_output helper
#
# CI override: RED-zone marker tolerance is a development-time aid (TDD red phase).
# It MUST NOT mask test failures in CI runs targeting main. When CI=true or
# GITHUB_ACTIONS=true (both set by GitHub Actions and most other runners),
# disable tolerance unconditionally — every test must pass on its own merits
# to merge.
_RED_ZONE_ENABLED=false
declare -A _RED_MARKER_MAP=()
if [[ "${CI:-}" == "true" ]] || [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    : # tolerance disabled in CI environments — see comment above
elif [[ -n "${SUITE_TEST_INDEX:-}" ]] && [[ -f "${SUITE_TEST_INDEX}" ]]; then
    _RED_ZONE_ENABLED=true
    # Source red-zone.sh (located next to this file or in the hooks lib dir)
    _SE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    _RED_ZONE_SH=""
    # Check sibling dirs: tests/lib -> src/rebar/_engine/hooks/lib
    _REPO_ROOT_GUESS="$(cd "$_SE_DIR/../.." && pwd)"
    if [[ -f "$_SE_DIR/red-zone.sh" ]]; then
        _RED_ZONE_SH="$_SE_DIR/red-zone.sh"
    elif [[ -f "$_REPO_ROOT_GUESS/src/rebar/_engine/hooks/lib/red-zone.sh" ]]; then
        _RED_ZONE_SH="$_REPO_ROOT_GUESS/src/rebar/_engine/hooks/lib/red-zone.sh"
    fi
    if [[ -n "$_RED_ZONE_SH" ]]; then
        # shellcheck source=../src/rebar/_engine/hooks/lib/red-zone.sh
        source "$_RED_ZONE_SH"

        # Build marker map from SUITE_TEST_INDEX file.
        # Format: source/path.ext: test/path.ext [marker_name], ...
        # We parse the file directly (can't use read_red_markers_by_test_file
        # because that function builds the path as ${REPO_ROOT}/${test_file},
        # which breaks for absolute test-file paths in fixture environments).
        while IFS= read -r _line || [[ -n "$_line" ]]; do
            [[ -z "$_line" ]] && continue
            [[ "$_line" =~ ^[[:space:]]*# ]] && continue
            _right="${_line#*:}"
            IFS=',' read -ra _parts <<< "$_right"
            for _part in "${_parts[@]}"; do
                _part="${_part#"${_part%%[![:space:]]*}"}"
                _part="${_part%"${_part##*[![:space:]]}"}"
                [[ -z "$_part" ]] && continue
                _ppath="" _pmarker=""
                if [[ "$_part" =~ ^(.*[^[:space:]])[[:space:]]+\[([^]]+)\]$ ]]; then
                    _ppath="${BASH_REMATCH[1]}"
                    _pmarker="${BASH_REMATCH[2]}"
                    _ppath="${_ppath%"${_ppath##*[![:space:]]}"}"
                else
                    _ppath="$_part"
                    _pmarker=""
                fi
                if [[ -n "$_pmarker" ]] || [[ -z "${_RED_MARKER_MAP[$_ppath]:-}" ]]; then
                    _RED_MARKER_MAP["$_ppath"]="$_pmarker"
                fi
            done
        done < "${SUITE_TEST_INDEX}"
    else
        # red-zone.sh not found — disable RED tolerance gracefully
        _RED_ZONE_ENABLED=false
    fi
fi

# --- Resolve timeout command (GNU coreutils on macOS = gtimeout) ---
_TIMEOUT_CMD=""
if command -v timeout >/dev/null 2>&1; then
    _TIMEOUT_CMD="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
    _TIMEOUT_CMD="gtimeout"
fi

# --- EAGAIN resource exhaustion detection ---
# Matches fork failures caused by transient resource pressure (EAGAIN/ENOMEM).
# Exit code 254 is used as a sentinel by _run_single_test when the test process
# itself exits with this code indicating resource exhaustion.
EAGAIN_PATTERN="fork: (retry: )?Resource temporarily unavailable|BlockingIOError.*Resource temporarily unavailable"

# _is_eagain_failure <exit_code> <output_file>
# Returns 0 when exit_code==254 AND the output file contains the EAGAIN pattern.
_is_eagain_failure() {
    local exit_code="$1"
    local output_file="$2"
    [ "$exit_code" -eq 254 ] || return 1
    [ -f "$output_file" ] || return 1
    grep -qE "$EAGAIN_PATTERN" "$output_file" || return 1
    return 0
}

# _retry_eagain_test <test_path> <results_dir>
# Re-runs the test synchronously with MAX_PARALLEL=1 exported to limit
# process concurrency and reduce resource pressure. Overwrites .out, .exit,
# and .counts files in results_dir. Returns the new exit code.
_retry_eagain_test() {
    local test_path="$1"
    local results_dir="$2"
    local test_name
    test_name=$(basename "$test_path")

    # Create per-test TMPDIR matching _run_single_test's isolation contract.
    local retry_tmpdir
    retry_tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/suite-test-${test_name}-retry-XXXXXX")

    local retry_exit=0
    if [ -n "$_TIMEOUT_CMD" ]; then
        MAX_PARALLEL=1 "$_TIMEOUT_CMD" --signal=TERM --kill-after=5 "$TEST_TIMEOUT" \
            env TMPDIR="$retry_tmpdir" \
            bash "$test_path" > "$results_dir/$test_name.out" 2>&1 || retry_exit=$?
    else
        MAX_PARALLEL=1 TMPDIR="$retry_tmpdir" \
            bash "$test_path" > "$results_dir/$test_name.out" 2>&1 || retry_exit=$?
    fi

    echo "$retry_exit" > "$results_dir/$test_name.exit"

    # Clean up per-test TMPDIR (matches _run_single_test cleanup pattern)
    rm -rf "$retry_tmpdir" 2>/dev/null || true

    # Reparse counts from retry output
    if [ "$retry_exit" -eq 124 ] || [ "$retry_exit" -eq 137 ]; then
        echo "0 0 timeout" > "$results_dir/$test_name.counts"
    else
        local counts output
        output=$(cat "$results_dir/$test_name.out")
        counts=$(_parse_test_counts "$output")
        echo "$counts" > "$results_dir/$test_name.counts"
    fi

    return "$retry_exit"
}

# --- Parse test output for PASS/FAIL counts ---
# Handles both formats:
#   "PASSED: N  FAILED: N"  (assert.sh)
#   "Results: N passed, N failed"  (custom)
# Outputs: "pass_count fail_count" (space-separated)
_parse_test_counts() {
    local output="$1"
    local clean_output
    # Strip ANSI color codes
    clean_output=$(echo "$output" | sed 's/\x1b\[[0-9;]*m//g')

    # Try "PASSED: N  FAILED: N" (assert.sh pattern)
    local summary_line
    summary_line=$(echo "$clean_output" | grep -E "^PASSED: [0-9]+  FAILED: [0-9]+" | tail -1 || true)
    if [ -n "$summary_line" ]; then
        local p f
        p=$(echo "$summary_line" | grep -oE "PASSED: [0-9]+" | grep -oE "[0-9]+" || echo 0)
        f=$(echo "$summary_line" | grep -oE "FAILED: [0-9]+" | grep -oE "[0-9]+" || echo 0)
        echo "$p $f"
        return
    fi

    # Try "Results: N passed, N failed" or bare "N passed, N failed"
    local results_line
    results_line=$(echo "$clean_output" | grep -E "[0-9]+ passed" | tail -1 || true)
    if [ -n "$results_line" ]; then
        local p f
        p=$(echo "$results_line" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+" || echo 0)
        f=$(echo "$results_line" | grep -oE "[0-9]+ failed" | grep -oE "[0-9]+" || echo 0)
        echo "$p $f"
        return
    fi

    # No recognized format
    echo "0 0"
}

# --- Run a single test file with timeout ---
# Usage: _run_single_test <test_path> <results_dir>
# Writes to <results_dir>/<basename>.{out,exit,counts}
#
# Isolation: each test gets its own TMPDIR so all mktemp calls inside the
# test are automatically scoped to a per-test directory. This prevents
# parallel tests from colliding on shared /tmp paths. After the test
# finishes, SUITE_ISOLATION_CHECK=1 (opt-in) scans for files written to
# well-known shared paths that indicate broken isolation.
_run_single_test() {
    local test_path="$1" results_dir="$2"
    local test_name
    test_name=$(basename "$test_path")

    # Create per-test TMPDIR for isolation
    local test_tmpdir
    test_tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/suite-test-${test_name}-XXXXXX")

    local exit_code=0

    # Wall-clock duration per test (integer seconds; `date +%s` is portable across
    # bash 3.2 / BSD date). Surfaced on each suite report line so slow tests are
    # identifiable directly from the suite log (incl. CI) without external tooling.
    local _t_start _t_end
    _t_start=$(date +%s 2>/dev/null || echo 0)

    # Write output directly to file — NOT via $() command substitution.
    # $() waits for ALL processes holding the pipe fd to close, so if a test
    # spawns orphan children (background git ops, credential helpers, etc.),
    # the substitution hangs even after timeout kills the main process.
    # Direct file redirection avoids this: timeout kills the child, and we
    # read the file afterward regardless of orphan process state.
    if [ -n "$_TIMEOUT_CMD" ]; then
        "$_TIMEOUT_CMD" --signal=TERM --kill-after=5 "$TEST_TIMEOUT" \
            env TMPDIR="$test_tmpdir" \
            bash "$test_path" > "$results_dir/$test_name.out" 2>&1 || exit_code=$?
    else
        TMPDIR="$test_tmpdir" \
            bash "$test_path" > "$results_dir/$test_name.out" 2>&1 || exit_code=$?
    fi

    _t_end=$(date +%s 2>/dev/null || echo 0)
    echo "$(( _t_end - _t_start ))" > "$results_dir/$test_name.duration"

    echo "$exit_code" > "$results_dir/$test_name.exit"

    # --- Isolation check (opt-in via SUITE_ISOLATION_CHECK=1) ---
    # Detect if the test wrote to well-known shared paths that should be
    # per-test isolated. This catches regressions where a new test uses a
    # fixed /tmp path instead of mktemp. Violations override the exit code
    # to ensure the test is reported as FAIL.
    if [ "${SUITE_ISOLATION_CHECK:-0}" = "1" ]; then
        local _isolation_violations=""
        for _shared_path in \
            "/tmp/pytest-rts-cache" \
            "/tmp/test_deps_escape.txt" \
            "/tmp/rts-output-fixed" \
        ; do
            if [ -e "$_shared_path" ]; then
                _isolation_violations="${_isolation_violations}${_shared_path} "
                rm -rf "$_shared_path" 2>/dev/null || true
            fi
        done
        if [ -n "$_isolation_violations" ]; then
            echo "ISOLATION VIOLATION in $test_name: shared paths written: $_isolation_violations" \
                >> "$results_dir/$test_name.out"
            # Force failure so the violation is visible in suite output
            exit_code=1
            echo "$exit_code" > "$results_dir/$test_name.exit"
        fi
    fi

    # --- Repo file protection check ---
    # Verify critical repo files weren't modified by the test.
    if ! _verify_protected_files "$test_name"; then
        echo "REPO PROTECTION VIOLATION in $test_name: critical repo files were modified" \
            >> "$results_dir/$test_name.out"
        exit_code=1
        echo "$exit_code" > "$results_dir/$test_name.exit"
    fi

    # Clean up per-test TMPDIR
    rm -rf "$test_tmpdir" 2>/dev/null || true

    # Parse counts
    if [ "$exit_code" -eq 124 ] || [ "$exit_code" -eq 137 ]; then
        # Timeout (124 = TERM, 137 = KILL)
        echo "0 0 timeout" > "$results_dir/$test_name.counts"
    else
        local counts output
        output=$(cat "$results_dir/$test_name.out")
        counts=$(_parse_test_counts "$output")
        echo "$counts" > "$results_dir/$test_name.counts"
    fi
}

# --- Main suite runner ---
# Usage: run_test_suite "Label" test1.sh test2.sh ...
# Sets SUITE_TOTAL_PASS and SUITE_TOTAL_FAIL on return.
run_test_suite() {
    local label="$1"
    shift
    local test_files=("$@")
    local total=${#test_files[@]}

    SUITE_TOTAL_PASS=0
    SUITE_TOTAL_FAIL=0
    SUITE_TOTAL_TOLERATED=0
    local consecutive_fails=0
    local aborted=false
    local failed_tests=()

    local results_dir
    results_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$results_dir")

    # Snapshot critical repo files before any tests run
    _snapshot_protected_files

    # --- Parallel execution (slot-refill via wait -n) ---
    # Instead of launching a batch of MAX_PARALLEL tests and waiting for ALL to
    # finish before launching the next batch, we use `wait -n` to detect when
    # ANY single test finishes, immediately refilling that slot. This eliminates
    # idle slots when one test in a batch is slower than the others, reducing
    # both wall time and CPU contention spikes.
    local index=0
    # Maps: PID → test metadata for in-flight tests
    declare -A _pid_to_name=()
    declare -A _pid_to_index=()
    declare -A _pid_to_path=()
    # _process_completed_test: read results for a finished test and report
    _process_completed_test() {
        local pid="$1"
        local tname="${_pid_to_name[$pid]}"
        local tidx="${_pid_to_index[$pid]}"
        local tpath="${_pid_to_path[$pid]}"
        unset "_pid_to_name[$pid]" "_pid_to_index[$pid]" "_pid_to_path[$pid]"

        # Read results
        local exit_code=0
        if [ -f "$results_dir/$tname.exit" ]; then
            exit_code=$(cat "$results_dir/$tname.exit")
        fi

        # --- EAGAIN retry (must happen BEFORE rm -f on output file) ---
        # If exit code is 254 and output contains an EAGAIN pattern, the test
        # hit transient resource exhaustion. Retry synchronously with MAX_PARALLEL=1
        # to reduce process pressure. The retry result is authoritative.
        if _is_eagain_failure "$exit_code" "$results_dir/$tname.out"; then
            _retry_eagain_test "$tpath" "$results_dir" || true
            exit_code=$(cat "$results_dir/$tname.exit")
            local retry_status="FAIL"
            [ "$exit_code" -eq 0 ] && retry_status="PASS"
            echo "EAGAIN retry for $tname: $retry_status" >&2
        fi

        local counts_line="0 0"
        local is_timeout=false
        if [ -f "$results_dir/$tname.counts" ]; then
            counts_line=$(cat "$results_dir/$tname.counts")
            _tmp="$counts_line"; if [[ "$_tmp" =~ timeout ]]; then
                is_timeout=true
                counts_line="0 0"
            fi
        fi

        local file_pass file_fail
        file_pass=$(echo "$counts_line" | awk '{print $1}')
        file_fail=$(echo "$counts_line" | awk '{print $2}')

        # Per-test wall-clock duration (seconds), surfaced on the report line.
        local _dur=0
        [ -f "$results_dir/$tname.duration" ] && _dur=$(cat "$results_dir/$tname.duration" 2>/dev/null || echo 0)

        local display_idx=$(( tidx + 1 ))
        local is_tolerated=false
        if [ "$is_timeout" = true ]; then
            printf "[%d/%d] %s ... TIMEOUT (exceeded %ss) (%ss)\n" "$display_idx" "$total" "$tname" "$TEST_TIMEOUT" "$_dur"
            (( file_fail++ ))
        elif [ "$exit_code" -ne 0 ]; then
            if [ "$file_pass" -eq 0 ] && [ "$file_fail" -eq 0 ]; then
                (( file_fail++ ))
            fi

            # --- RED zone tolerance check ---
            local _red_marker_lookup=""
            if [[ -n "${_RED_MARKER_MAP[$tpath]:-}" ]]; then
                _red_marker_lookup="${_RED_MARKER_MAP[$tpath]}"
            else
                local _mk
                for _mk in "${!_RED_MARKER_MAP[@]}"; do
                    if [[ -n "${_RED_MARKER_MAP[$_mk]}" ]] && [[ "$tpath" == *"$_mk" ]]; then
                        _red_marker_lookup="${_RED_MARKER_MAP[$_mk]}"
                        break
                    fi
                done
            fi
            if [[ "$_RED_ZONE_ENABLED" = true ]] && [[ -n "$_red_marker_lookup" ]]; then
                local _marker="$_red_marker_lookup"
                local _out_file="$results_dir/$tname.out"

                local _marker_line=-1
                if [[ -f "$tpath" ]]; then
                    local _lnum=0
                    local _mpat="(^|[^a-zA-Z0-9_-])${_marker}([^a-zA-Z0-9_-]|\$)"
                    while IFS= read -r _ml || [[ -n "$_ml" ]]; do
                        (( _lnum++ )) || true
                        [[ "$_ml" =~ ^[[:space:]]*# ]] && continue
                        if [[ "$_ml" =~ $_mpat ]]; then
                            _marker_line=$_lnum
                            break
                        fi
                    done < "$tpath"
                fi

                if [[ "$_marker_line" -gt 0 ]]; then
                    local _failing_tests
                    _failing_tests=$(parse_failing_tests_from_output "$_out_file" 2>/dev/null || true)

                    if [[ -n "$_failing_tests" ]]; then
                        local _all_in_zone=true
                        while IFS= read -r _ft; do
                            [[ -z "$_ft" ]] && continue
                            local _ft_line=-1
                            local _flnum=0
                            local _ftpat="(^|[^a-zA-Z0-9_-])${_ft}([^a-zA-Z0-9_-]|\$)"
                            while IFS= read -r _fl || [[ -n "$_fl" ]]; do
                                (( _flnum++ )) || true
                                [[ "$_fl" =~ ^[[:space:]]*# ]] && continue
                                if [[ "$_fl" =~ $_ftpat ]]; then
                                    _ft_line=$_flnum
                                    break
                                fi
                            done < "$tpath"
                            if [[ "$_ft_line" -lt "$_marker_line" ]]; then
                                _all_in_zone=false
                                break
                            fi
                        done <<< "$_failing_tests"

                        if [[ "$_all_in_zone" = true ]]; then
                            is_tolerated=true
                        fi
                    fi
                fi
            fi

            if [[ "$is_tolerated" = true ]]; then
                printf "[%d/%d] %s ... TOLERATED (%d pass, %d red-zone) (%ss)\n" \
                    "$display_idx" "$total" "$tname" "$file_pass" "$file_fail" "$_dur"
                SUITE_TOTAL_TOLERATED=$(( SUITE_TOTAL_TOLERATED + file_fail ))
                file_fail=0
            else
                printf "[%d/%d] %s ... FAIL (%d pass, %d fail) (%ss)\n" "$display_idx" "$total" "$tname" "$file_pass" "$file_fail" "$_dur"
            fi
        else
            printf "[%d/%d] %s ... PASS (%d pass, %d fail) (%ss)\n" "$display_idx" "$total" "$tname" "$file_pass" "$file_fail" "$_dur"
        fi

        SUITE_TOTAL_PASS=$(( SUITE_TOTAL_PASS + file_pass ))
        SUITE_TOTAL_FAIL=$(( SUITE_TOTAL_FAIL + file_fail ))

        # Track consecutive failures for fail-fast
        if [ "$is_timeout" = true ]; then
            failed_tests+=("$tname")
            (( consecutive_fails++ ))
        elif [ "$exit_code" -ne 0 ] && [ "$is_tolerated" = false ]; then
            failed_tests+=("$tname")
            (( consecutive_fails++ ))
        elif [ "$exit_code" -eq 0 ] || [ "$is_tolerated" = true ]; then
            consecutive_fails=0
        fi

        if [ "$consecutive_fails" -ge "$MAX_CONSECUTIVE_FAILS" ]; then
            aborted=true
        fi
    }

    # Feature-detect `wait -n -p` (bash 5.1+). If unavailable, fall back to
    # `wait -n` (bash 4.3+) which blocks until any child exits but doesn't
    # report which PID finished — we then scan with kill -0.
    local _has_wait_n_p=false
    if (sleep 0 & _tp=$!; wait -n -p _tv "$_tp" 2>/dev/null; [[ "$_tv" == "$_tp" ]]); then
        _has_wait_n_p=true
    fi

    while [ "$index" -lt "$total" ] || [ ${#_pid_to_name[@]} -gt 0 ]; do
        # Fill slots up to MAX_PARALLEL
        while [ "$index" -lt "$total" ] && [ ${#_pid_to_name[@]} -lt "$MAX_PARALLEL" ]; do
            if [ "$aborted" = true ]; then
                break
            fi
            local test_file="${test_files[$index]}"
            _run_single_test "$test_file" "$results_dir" &
            local _new_pid=$!
            _pid_to_name[$_new_pid]="$(basename "$test_file")"
            _pid_to_index[$_new_pid]=$index
            _pid_to_path[$_new_pid]="$test_file"
            (( index++ ))
        done

        if [ ${#_pid_to_name[@]} -eq 0 ]; then
            break
        fi

        if [ "$aborted" = true ]; then
            break
        fi

        # Wait for ANY single test to finish (slot-refill), then process it.
        if [ "$_has_wait_n_p" = true ]; then
            # bash 5.1+: `wait -n -p` blocks until a child exits and reports its PID.
            local _done_pid=""
            wait -n -p _done_pid "${!_pid_to_name[@]}" 2>/dev/null || true
            if [ -n "$_done_pid" ] && [ -n "${_pid_to_name[$_done_pid]:-}" ]; then
                _process_completed_test "$_done_pid"
            fi
        else
            # bash 4.3+: `wait -n` blocks until any child exits but doesn't
            # report which PID. After it returns, scan with kill -0 to find it.
            wait -n "${!_pid_to_name[@]}" 2>/dev/null || true
            for _check_pid in "${!_pid_to_name[@]}"; do
                if ! kill -0 "$_check_pid" 2>/dev/null; then
                    wait "$_check_pid" 2>/dev/null || true
                    _process_completed_test "$_check_pid"
                    break  # process one at a time, then refill
                fi
            done
        fi
    done

    # Wait for any still-running tests to finish before cleaning up results_dir.
    # On abort, background _run_single_test processes may still be writing.
    if [ ${#_pid_to_name[@]} -gt 0 ]; then
        for _remaining_pid in "${!_pid_to_name[@]}"; do
            wait "$_remaining_pid" 2>/dev/null || true
        done
    fi

    if [ "$aborted" = true ]; then
        local skipped=$(( total - index ))
        if [ "$skipped" -lt 0 ]; then skipped=0; fi
        printf "\nABORT: %d consecutive failures — likely systemic issue (%d tests skipped)\n" \
            "$MAX_CONSECUTIVE_FAILS" "$skipped" >&2
    fi

    # Dump output of failed tests for CI visibility
    if [ ${#failed_tests[@]} -gt 0 ] && [ -d "$results_dir" ]; then
        echo ""
        echo "=== Failed test output ==="
        for ftname in "${failed_tests[@]}"; do
            if [ -f "$results_dir/$ftname.out" ]; then
                echo "--- $ftname ---"
                # Show all FAIL: lines (unbounded) so no failing assertion is hidden
                # when a test file has many assertions, then tail -30 for context.
                local _fail_lines
                _fail_lines=$(grep -n '^FAIL:\|FAIL: ' "$results_dir/$ftname.out" 2>/dev/null || true)
                if [ -n "$_fail_lines" ]; then
                    echo "[all FAIL: lines]"
                    printf '%s\n' "$_fail_lines"
                    echo "[last 30 lines of output]"
                fi
                tail -30 "$results_dir/$ftname.out"
                echo "--- end $ftname ---"
            fi
        done
        echo "=== End failed test output ==="
    fi

    # Print aggregated summary
    echo ""
    if [[ "$_RED_ZONE_ENABLED" = true ]] && [[ "${SUITE_TOTAL_TOLERATED:-0}" -gt 0 ]]; then
        printf "PASSED: %d  FAILED: %d  TOLERATED: %d\n" \
            "$SUITE_TOTAL_PASS" "$SUITE_TOTAL_FAIL" "$SUITE_TOTAL_TOLERATED"
    else
        printf "PASSED: %d  FAILED: %d\n" "$SUITE_TOTAL_PASS" "$SUITE_TOTAL_FAIL"
    fi

    rm -rf "$results_dir"

    if [ "$SUITE_TOTAL_FAIL" -gt 0 ] || [ "$aborted" = true ]; then
        return 1
    fi
    return 0
}

# --- Script mode: run when invoked directly with test file args ---
if [ "${BASH_SOURCE[0]}" = "$0" ] && [ $# -gt 0 ]; then
    run_test_suite "$SUITE_LABEL" "$@"
    exit $?
fi
