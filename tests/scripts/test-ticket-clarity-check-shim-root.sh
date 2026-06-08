#!/usr/bin/env bash
# tests/scripts/test-ticket-clarity-check-shim-root.sh
# Regression tests for ticket-clarity-check.sh REPO_ROOT resolution via PROJECT_ROOT.
#
# Covers the bug where SCRIPT_DIR-based git detection resolves to the plugin
# marketplace cache root when dispatched via the dso shim, causing DSO_CLI to
# be empty and the script to exit 2 with "ERROR: could not locate .claude/scripts/dso shim".
#
# The dso shim exports PROJECT_ROOT (the host project git root) before dispatch;
# the fix makes PROJECT_ROOT take priority over SCRIPT_DIR-based git detection,
# so REPO_ROOT=$PROJECT_ROOT when the script is invoked from outside the repo.
#
# Test cases (3):
#   test_project_root_locates_shim     — PROJECT_ROOT set to repo root, REPO_ROOT unset,
#                                        script copied to temp dir outside project (ticket_id mode):
#                                        pre-fix exits 2 (shim not found); post-fix finds shim
#                                        (exits 2 only for "failed to retrieve ticket", not "shim")
#   test_repo_root_direct_stdin        — REPO_ROOT set directly, --stdin mode: still works (baseline)
#   test_neither_set_outside_project   — both unset, script in temp dir, ticket_id mode: exits 2
#
# Usage: bash tests/scripts/test-ticket-clarity-check-shim-root.sh

# NOTE: -e intentionally omitted — test functions may return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
SUT="$REPO_ROOT/src/rebar/_engine/ticket-clarity-check.sh"

source "$REPO_ROOT/tests/lib/assert.sh"

# ── Temp dir cleanup on exit ──────────────────────────────────────────────────
_CLEANUP_DIRS=()
_cleanup() {
    for d in "${_CLEANUP_DIRS[@]:-}"; do
        rm -rf "$d"
    done
}
trap _cleanup EXIT

echo "=== test-ticket-clarity-check-shim-root.sh ==="

# ── test_project_root_locates_shim ────────────────────────────────────────────
# Simulates the dso shim dispatch: script is in a temp dir outside the project
# (SCRIPT_DIR-based git detection fails), REPO_ROOT is unset, but PROJECT_ROOT
# is set to the real repo root (as the dso shim exports it).
#
# Pre-fix: REPO_ROOT becomes "" → DSO_CLI is empty → exit 2 with message
#   "ERROR: could not locate .claude/scripts/dso shim"
# Post-fix: REPO_ROOT="$PROJECT_ROOT" → DSO_CLI found → exit 2 only if ticket
#   retrieval fails (different message: "failed to retrieve ticket"), not shim.
#
# We distinguish by checking the stderr message.
test_project_root_locates_shim() {
    _snapshot_fail

    # Create a temp dir outside the project to simulate plugin cache dispatch
    local tmpdir
    tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/clarity.XXXXXX")
    _CLEANUP_DIRS+=("$tmpdir")

    # Copy the SUT to tmpdir so SCRIPT_DIR resolves outside the project
    local sut_copy="$tmpdir/ticket-clarity-check.sh"
    cp "$SUT" "$sut_copy"
    chmod +x "$sut_copy"

    # Invoke in ticket_id mode (requires shim) with REPO_ROOT unset but PROJECT_ROOT set.
    # Use env -i to strip inherited environment, restoring only what we intend.
    local stderr_output
    stderr_output=$(
        env -i PATH="$PATH" HOME="$HOME" \
            PROJECT_ROOT="$REPO_ROOT" \
            bash "$sut_copy" "synthetic-test-ticket" 2>&1 >/dev/null
    ) || true

    # Pre-fix: stderr contains "could not locate .claude/scripts/dso shim"
    # Post-fix: shim is found; stderr may say "failed to retrieve ticket" (no such ticket) instead
    local shim_not_found_msg="could not locate .claude/scripts/dso shim"
    local shim_error_present
    if echo "$stderr_output" | grep -qF "$shim_not_found_msg"; then
        shim_error_present="yes"
    else
        shim_error_present="no"
    fi

    assert_eq "test_project_root_locates_shim: PROJECT_ROOT prevents shim-not-located error" \
        "no" "$shim_error_present"

    assert_pass_if_clean "test_project_root_locates_shim"
}

# ── test_repo_root_direct_stdin ───────────────────────────────────────────────
# Baseline: REPO_ROOT set directly, --stdin mode still works (no regression).
test_repo_root_direct_stdin() {
    _snapshot_fail

    local json
    json=$(python3 -c "
import json
t = {'ticket_id': 'test-0001', 'ticket_type': 'task', 'title': 'T',
     'status': 'open', 'description': 'x' * 300, 'comments': []}
print(json.dumps(t))
")

    local exit_code=0
    REPO_ROOT="$REPO_ROOT" bash "$SUT" --stdin 2>/dev/null <<< "$json" || exit_code=$?

    # Exit 0 or 1 (clarity pass/fail), NOT 2 (error)
    local not_exit2
    if [[ "$exit_code" -ne 2 ]]; then
        not_exit2="yes"
    else
        not_exit2="no"
    fi
    assert_eq "test_repo_root_direct_stdin: explicit REPO_ROOT + --stdin works (exit != 2)" \
        "yes" "$not_exit2"

    assert_pass_if_clean "test_repo_root_direct_stdin"
}

# ── test_neither_set_outside_project ─────────────────────────────────────────
# When both REPO_ROOT and PROJECT_ROOT are unset AND the script is placed in a
# temp dir outside the project (so git detection fails), ticket_id mode exits 2.
test_neither_set_outside_project() {
    _snapshot_fail

    local tmpdir
    tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/clarity.XXXXXX")
    _CLEANUP_DIRS+=("$tmpdir")

    local sut_copy="$tmpdir/ticket-clarity-check.sh"
    cp "$SUT" "$sut_copy"
    chmod +x "$sut_copy"

    local exit_code=0
    env -i PATH="$PATH" HOME="$HOME" \
        bash "$sut_copy" "fake-ticket-id" 2>/dev/null || exit_code=$?

    assert_eq "test_neither_set_outside_project: no env vars + outside project exits 2" \
        "2" "$exit_code"

    assert_pass_if_clean "test_neither_set_outside_project"
}

# ── Run all tests ─────────────────────────────────────────────────────────────
test_project_root_locates_shim
test_repo_root_direct_stdin
test_neither_set_outside_project

print_summary
