#!/usr/bin/env bash
# tests/scripts/test-should-create-minor-finding-tickets.sh
# Tests for src/rebar/_engine/should-create-minor-finding-tickets.sh
#
# The gate script consults `review.minor_findings_create_tickets` in
# .claude/dso-config.conf and signals via exit code whether the
# REVIEW-WORKFLOW.md post-pass block should auto-file minor findings as
# bug tickets.
#
# Default behavior (config absent OR set to false): exit 1 — DO NOT create.
# Explicit opt-in (config = true): exit 0 — create tickets.
#
# Rationale (Approach 4): minor / suggestion-class findings rarely meet the bar
# for work tracking. Auto-filing them creates the deferred-nitpick treadmill —
# tickets like 57b9, 9726, 5329 sit at pri=4 indefinitely, generating triage
# cost on every list pass. Surface as PR comments by default; opt-in for
# projects that want the tickets.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DSO_PLUGIN_DIR="$PLUGIN_ROOT/src/rebar/_engine"

echo "=== test-should-create-minor-finding-tickets.sh ==="

source "$PLUGIN_ROOT/tests/lib/assert.sh"

SCRIPT="$DSO_PLUGIN_DIR/should-create-minor-finding-tickets.sh"

# Helper: run the gate against an isolated config file
# Usage: run_gate <config_content>
run_gate() {
    local content="$1"
    local cfg
    cfg=$(mktemp "${TMPDIR:-/tmp}/dso-config.XXXXXX.conf")
    printf '%s\n' "$content" > "$cfg"
    local rc=0
    WORKFLOW_CONFIG_FILE="$cfg" bash "$SCRIPT" >/dev/null 2>&1 || rc=$?
    rm -f "$cfg"
    echo "$rc"
}

# test_script_exists
if [[ -x "$SCRIPT" ]]; then
    actual="executable"
else
    actual="not_executable"
fi
assert_eq "test_script_exists: gate script is present and executable" "executable" "$actual"

# test_default_when_key_absent_is_no_create
# Default behavior: do NOT create minor-finding tickets when the config key is absent.
DEFAULT_RC=$(run_gate "version=1.1.0")
assert_eq \
    "test_default_when_key_absent_is_no_create: missing key exits 1 (don't create)" \
    "1" \
    "$DEFAULT_RC"

# test_explicit_false_is_no_create
# Explicit false: do NOT create.
FALSE_RC=$(run_gate "version=1.1.0
review.minor_findings_create_tickets=false")
assert_eq \
    "test_explicit_false_is_no_create: explicit false exits 1 (don't create)" \
    "1" \
    "$FALSE_RC"

# test_explicit_true_is_create
# Explicit true: DO create.
TRUE_RC=$(run_gate "version=1.1.0
review.minor_findings_create_tickets=true")
assert_eq \
    "test_explicit_true_is_create: explicit true exits 0 (create tickets)" \
    "0" \
    "$TRUE_RC"

# test_unrecognized_value_is_no_create
# Unrecognized value (not 'true'): treat as false (fail-closed; don't create).
UNRECOG_RC=$(run_gate "version=1.1.0
review.minor_findings_create_tickets=yes")
assert_eq \
    "test_unrecognized_value_is_no_create: unrecognized value exits 1 (don't create)" \
    "1" \
    "$UNRECOG_RC"

# test_missing_config_file_is_no_create
# When the config file does not exist at all, default behavior applies.
MISSING_CFG=$(mktemp -u "${TMPDIR:-/tmp}/dso-missing.XXXXXX.conf")
MISSING_RC=0
WORKFLOW_CONFIG_FILE="$MISSING_CFG" bash "$SCRIPT" >/dev/null 2>&1 || MISSING_RC=$?
assert_eq \
    "test_missing_config_file_is_no_create: missing config file exits 1 (don't create)" \
    "1" \
    "$MISSING_RC"

# test_yaml_true_is_create
# YAML configs are parsed via Python; Python emits "True" (capital T) for boolean
# `true` values, which a case-sensitive string compare would treat as disabled.
# The gate must accept both "true" and "True" so behavior is consistent across
# .conf and .yaml configs.
YAML_TRUE_CFG=$(mktemp "${TMPDIR:-/tmp}/dso-config.XXXXXX.yaml")
cat > "$YAML_TRUE_CFG" <<'YAML'
review:
  minor_findings_create_tickets: true
YAML
YAML_TRUE_RC=0
WORKFLOW_CONFIG_FILE="$YAML_TRUE_CFG" bash "$SCRIPT" >/dev/null 2>&1 || YAML_TRUE_RC=$?
rm -f "$YAML_TRUE_CFG"
assert_eq \
    "test_yaml_true_is_create: YAML 'true' (parsed as Python 'True') exits 0 (create tickets)" \
    "0" \
    "$YAML_TRUE_RC"

# test_yaml_false_is_no_create
# Mirror of the YAML true case for the false path.
YAML_FALSE_CFG=$(mktemp "${TMPDIR:-/tmp}/dso-config.XXXXXX.yaml")
cat > "$YAML_FALSE_CFG" <<'YAML'
review:
  minor_findings_create_tickets: false
YAML
YAML_FALSE_RC=0
WORKFLOW_CONFIG_FILE="$YAML_FALSE_CFG" bash "$SCRIPT" >/dev/null 2>&1 || YAML_FALSE_RC=$?
rm -f "$YAML_FALSE_CFG"
assert_eq \
    "test_yaml_false_is_no_create: YAML 'false' (parsed as Python 'False') exits 1 (don't create)" \
    "1" \
    "$YAML_FALSE_RC"

# test_mixed_case_true_is_create
# Defensive: any mixed-case spelling of "true" should be accepted (e.g., TRUE,
# True, tRuE). Avoids surprising the user if some config tool upper-cases values.
MIXED_RC=$(run_gate "version=1.1.0
review.minor_findings_create_tickets=TRUE")
assert_eq \
    "test_mixed_case_true_is_create: TRUE (uppercase) exits 0 (create tickets)" \
    "0" \
    "$MIXED_RC"

print_summary
