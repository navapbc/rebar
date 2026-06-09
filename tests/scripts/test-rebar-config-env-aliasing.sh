#!/usr/bin/env bash
# tests/scripts/test-rebar-config-env-aliasing.sh
#
# WS1: bash-surface env aliasing. rebar-config.sh exposes REBAR_* as the public
# contract and seeds the engine-internal DSO_* names from them, so a caller who
# sets only REBAR_* still drives the engine, and legacy DSO_* keeps working.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
CONFIG_SH="$REPO_ROOT/src/rebar/_engine/rebar-config.sh"

echo "=== test-rebar-config-env-aliasing.sh ==="
PASSED=0
FAILED=0

_check() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "  PASS: $label"; PASSED=$((PASSED + 1))
    else
        echo "  FAIL: $label (expected '$expected', got '$actual')"; FAILED=$((FAILED + 1))
    fi
}

# REBAR_AUTHOR (public) seeds DSO_AUTHOR (engine-internal) when DSO_AUTHOR unset.
out=$(env -u DSO_AUTHOR REBAR_AUTHOR="alice" bash -c "source '$CONFIG_SH'; printf '%s' \"\${DSO_AUTHOR:-<unset>}\"")
_check "REBAR_AUTHOR seeds DSO_AUTHOR" "alice" "$out"

# Legacy DSO_AUTHOR alone still works (deprecated path).
out=$(env -u REBAR_AUTHOR DSO_AUTHOR="bob" bash -c "source '$CONFIG_SH'; printf '%s' \"\${DSO_AUTHOR:-<unset>}\"")
_check "legacy DSO_AUTHOR still honored" "bob" "$out"

# Neither set → DSO_AUTHOR remains unset (stripped by the hub's empty-alias guard).
out=$(env -u REBAR_AUTHOR -u DSO_AUTHOR bash -c "source '$CONFIG_SH'; printf '%s' \"\${DSO_AUTHOR:-<unset>}\"")
_check "neither set leaves DSO_AUTHOR unset" "<unset>" "$out"

# REBAR_TICKET_CLI seeds DSO_TICKET_CLI similarly.
out=$(env -u DSO_TICKET_CLI REBAR_TICKET_CLI="/x/rebar" bash -c "source '$CONFIG_SH'; printf '%s' \"\${DSO_TICKET_CLI:-<unset>}\"")
_check "REBAR_TICKET_CLI seeds DSO_TICKET_CLI" "/x/rebar" "$out"

echo ""
printf "PASSED: %d  FAILED: %d\n" "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
