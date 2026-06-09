#!/usr/bin/env bash
# tests/scripts/test-jira-credential-helper.sh
# Behavioral tests for src/rebar/_engine/jira-credential-helper.sh
#
# Tests cover:
#   1. test_detect_all_vars_set      — all 3 Jira vars in env → DETECTED lists all 3
#   2. test_detect_partial_vars      — only JIRA_URL set → DETECTED=JIRA_URL, MISSING lists others
#   3. test_missing_vars_show_guidance — no vars set → output includes description text and token URL
#   4. test_token_confirm_signal     — JIRA_API_TOKEN set → output includes CONFIRM_BEFORE_COPY marker
#   5. test_jira_project_output      — --project=DIG flag → output includes JIRA_PROJECT=DIG
#   6. test_no_env_vars              — clean env → all-missing output
#
# NOTE: These tests do NOT call the GitHub API or make network requests.
# Integration exemption: gh API calls unavailable in CI; script is tested via
# env-variable stubbing only. No live Jira connection required.
#
# Usage: bash tests/scripts/test-jira-credential-helper.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# non-zero exit codes from _run_helper via || assignment. With '-e', expected
# non-zero exits would abort the script before assertions run.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REBAR_PLUGIN_DIR="$PLUGIN_ROOT/src/rebar/_engine"
REPO_ROOT="$(cd "$SCRIPT_DIR" && git rev-parse --show-toplevel)"
HELPER_SCRIPT="$REBAR_PLUGIN_DIR/jira-credential-helper.sh"

source "$PLUGIN_ROOT/tests/lib/assert.sh"

echo "=== test-jira-credential-helper.sh ==="

_TMP=$(mktemp -d)
trap 'rm -rf "$_TMP"' EXIT

# Helper: run jira-credential-helper.sh with a clean Jira-variable environment.
# Caller sets only the env vars they want present; all Jira vars are unset by default.
# Additional args are passed through to the script.
_run_helper() {
    (
        unset JIRA_URL JIRA_USER JIRA_API_TOKEN 2>/dev/null || true
        # Caller may export vars before calling this subshell
        bash "$HELPER_SCRIPT" "$@" 2>&1
    )
}

# ── test_detect_all_vars_set ──────────────────────────────────────────────────
# When all three Jira vars are present in the environment, the helper should
# report DETECTED containing all three variable names.
_snapshot_fail
all_vars_output=""
all_vars_exit=0
all_vars_output=$(
    JIRA_URL="https://jira.example.com" \
    JIRA_USER="user@example.com" \
    JIRA_API_TOKEN="secret-token" \
    bash "$HELPER_SCRIPT" 2>&1
) || all_vars_exit=$?

assert_contains "test_detect_all_vars_set: JIRA_URL detected" "JIRA_URL" "$all_vars_output"
assert_contains "test_detect_all_vars_set: JIRA_USER detected" "JIRA_USER" "$all_vars_output"
assert_contains "test_detect_all_vars_set: JIRA_API_TOKEN detected" "JIRA_API_TOKEN" "$all_vars_output"
assert_pass_if_clean "test_detect_all_vars_set"

# ── test_detect_partial_vars ──────────────────────────────────────────────────
# When only JIRA_URL is set, the output should list JIRA_URL as detected and
# include JIRA_USER and JIRA_API_TOKEN in the missing/not-set section.
_snapshot_fail
partial_output=""
partial_exit=0
partial_output=$(
    unset JIRA_USER JIRA_API_TOKEN 2>/dev/null || true
    JIRA_URL="https://jira.example.com" \
    bash "$HELPER_SCRIPT" 2>&1
) || partial_exit=$?

assert_contains "test_detect_partial_vars: JIRA_URL in output" "JIRA_URL" "$partial_output"
assert_contains "test_detect_partial_vars: JIRA_USER missing" "JIRA_USER" "$partial_output"
assert_contains "test_detect_partial_vars: JIRA_API_TOKEN missing" "JIRA_API_TOKEN" "$partial_output"
assert_pass_if_clean "test_detect_partial_vars"

# ── test_missing_vars_show_guidance ──────────────────────────────────────────
# When no Jira vars are set, the output should include guidance text describing
# what the variables are for and where to obtain an API token.
_snapshot_fail
guidance_output=""
guidance_exit=0
guidance_output=$(
    unset JIRA_URL JIRA_USER JIRA_API_TOKEN 2>/dev/null || true
    bash "$HELPER_SCRIPT" 2>&1
) || guidance_exit=$?

# Should include descriptive guidance text (URL or instructions)
_has_guidance=0
[[ "${guidance_output,,}" =~ token|api|atlassian|account|https:// ]] && _has_guidance=1
assert_eq "test_missing_vars_show_guidance: guidance text present" "1" "$_has_guidance"
assert_pass_if_clean "test_missing_vars_show_guidance"

# ── test_token_confirm_signal ─────────────────────────────────────────────────
# When JIRA_API_TOKEN is set, the output should include a CONFIRM_BEFORE_COPY
# marker so that tooling can prompt before copying sensitive values.
_snapshot_fail
confirm_output=""
confirm_exit=0
confirm_output=$(
    unset JIRA_URL JIRA_USER 2>/dev/null || true
    JIRA_API_TOKEN="my-secret-token" \
    bash "$HELPER_SCRIPT" 2>&1
) || confirm_exit=$?

assert_contains "test_token_confirm_signal: CONFIRM_BEFORE_COPY marker" "CONFIRM_BEFORE_COPY" "$confirm_output"
assert_pass_if_clean "test_token_confirm_signal"

# ── test_jira_project_output ──────────────────────────────────────────────────
# When --project=DIG is passed on the command line, the output should include
# JIRA_PROJECT=DIG so callers can read the configured project key.
_snapshot_fail
project_output=""
project_exit=0
project_output=$(
    unset JIRA_URL JIRA_USER JIRA_API_TOKEN 2>/dev/null || true
    bash "$HELPER_SCRIPT" --project=DIG 2>&1
) || project_exit=$?

assert_contains "test_jira_project_output: JIRA_PROJECT=DIG" "JIRA_PROJECT=DIG" "$project_output"
assert_pass_if_clean "test_jira_project_output"

# ── test_no_env_vars ──────────────────────────────────────────────────────────
# With a completely clean environment (all Jira vars unset), the output should
# indicate that credentials are missing/not configured.
_snapshot_fail
no_env_output=""
no_env_exit=0
no_env_output=$(
    unset JIRA_URL JIRA_USER JIRA_API_TOKEN 2>/dev/null || true
    bash "$HELPER_SCRIPT" 2>&1
) || no_env_exit=$?

# Output should mention that one or more variables are missing/not set
_has_missing=0
[[ "${no_env_output,,}" =~ missing|not\ set|unset|required|not\ configured ]] && _has_missing=1
assert_eq "test_no_env_vars: missing-vars indicator present" "1" "$_has_missing"
assert_pass_if_clean "test_no_env_vars"

# ── Summary ───────────────────────────────────────────────────────────────────
print_summary
