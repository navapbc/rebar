#!/usr/bin/env bash
# jira-credential-helper.sh
# Detect Jira credential environment variables and output structured status.
#
# Output format:
#   DETECTED=VAR1,VAR2,...   — comma-separated list of vars that are set
#   MISSING=VAR1,VAR2,...    — comma-separated list of vars that are not set
#   GUIDANCE_DESC: <text>    — human-readable description of a missing var (one line per missing var)
#   GUIDANCE_URL: <url>      — URL with instructions for obtaining the value
#   CONFIRM_BEFORE_COPY      — emitted when JIRA_API_TOKEN is present (sensitive value warning)
#   JIRA_PROJECT=KEY         — emitted when --project=KEY is passed
#
# Usage: jira-credential-helper.sh [--project=KEY]
# Exit: 0 always (informational output only)

set -euo pipefail

# ── Argument parsing ──────────────────────────────────────────────────────────
_project_key=""
for _arg in "$@"; do
    case "$_arg" in
        --project=*)
            _project_key="${_arg#--project=}"
            ;;
        *)
            ;;
    esac
done

# ── Credential detection ──────────────────────────────────────────────────────
declare -a _detected=()
declare -a _missing=()

_check_var() {
    local var_name="$1"
    if [ -n "${!var_name:-}" ]; then
        _detected+=("$var_name")
    else
        _missing+=("$var_name")
    fi
}

_check_var JIRA_URL
_check_var JIRA_USER
_check_var JIRA_API_TOKEN

# ── Output DETECTED line ──────────────────────────────────────────────────────
if [ "${#_detected[@]}" -gt 0 ]; then
    _detected_str=$(IFS=,; echo "${_detected[*]}")
    echo "DETECTED=$_detected_str"
fi

# ── Output MISSING line ───────────────────────────────────────────────────────
if [ "${#_missing[@]}" -gt 0 ]; then
    _missing_str=$(IFS=,; echo "${_missing[*]}")
    echo "MISSING=$_missing_str"
fi

# ── Guidance for missing vars ─────────────────────────────────────────────────
for _var in "${_missing[@]}"; do
    case "$_var" in
        JIRA_URL)
            echo "GUIDANCE_DESC: JIRA_URL — Base URL of your Jira instance (e.g. https://your-org.atlassian.net)"
            echo "GUIDANCE_URL: https://support.atlassian.com/jira-software-cloud/docs/what-is-jira-software-cloud/"
            ;;
        JIRA_USER)
            echo "GUIDANCE_DESC: JIRA_USER — Your Atlassian account email address used to authenticate with the Jira API"
            echo "GUIDANCE_URL: https://id.atlassian.com/manage-profile/account-preferences"
            ;;
        JIRA_API_TOKEN)
            echo "GUIDANCE_DESC: JIRA_API_TOKEN — Atlassian API token; required to authenticate REST API calls (do not use your account password)"
            echo "GUIDANCE_URL: https://id.atlassian.com/manage-profile/security/api-tokens"
            ;;
    esac
done

# ── Sensitive-value confirmation marker ───────────────────────────────────────
# Emitted when JIRA_API_TOKEN is present so callers can prompt before copying.
if [ -n "${JIRA_API_TOKEN:-}" ]; then
    echo "CONFIRM_BEFORE_COPY"
fi

# ── Project key output ────────────────────────────────────────────────────────
if [ -n "$_project_key" ]; then
    echo "JIRA_PROJECT=$_project_key"
fi
