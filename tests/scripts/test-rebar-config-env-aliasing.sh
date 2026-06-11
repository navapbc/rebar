#!/usr/bin/env bash
# tests/scripts/test-rebar-config-env-aliasing.sh
#
# rebar-config.sh exposes REBAR_* as the SOLE public env surface (DSO_* support
# removed — clean break). Verifies: REBAR_ROOT/PROJECT_ROOT stay in sync, the
# public REBAR_GC_AUTO_ZERO maps onto the engine-internal _REBAR_GC_AUTO_ZERO,
# _rebar_ticket_cli honors REBAR_TICKET_CLI, and legacy DSO_* vars are IGNORED.

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

# REBAR_ROOT <-> PROJECT_ROOT stay in sync (repo-root agreement).
out=$(env -u PROJECT_ROOT REBAR_ROOT="/tmp/x" bash -c "source '$CONFIG_SH'; printf '%s' \"\${PROJECT_ROOT:-<unset>}\"")
_check "REBAR_ROOT seeds PROJECT_ROOT" "/tmp/x" "$out"

out=$(env -u REBAR_ROOT PROJECT_ROOT="/tmp/y" bash -c "source '$CONFIG_SH'; printf '%s' \"\${REBAR_ROOT:-<unset>}\"")
_check "PROJECT_ROOT seeds REBAR_ROOT" "/tmp/y" "$out"

# Public REBAR_GC_AUTO_ZERO maps onto engine-internal _REBAR_GC_AUTO_ZERO.
out=$(env -u _REBAR_GC_AUTO_ZERO REBAR_GC_AUTO_ZERO="1" bash -c "source '$CONFIG_SH'; printf '%s' \"\${_REBAR_GC_AUTO_ZERO:-<unset>}\"")
_check "REBAR_GC_AUTO_ZERO maps to _REBAR_GC_AUTO_ZERO" "1" "$out"

# _rebar_ticket_cli honors the REBAR_TICKET_CLI override.
out=$(env -u REBAR_TICKET_CLI REBAR_TICKET_CLI="/x/rebar" bash -c "source '$CONFIG_SH'; _rebar_ticket_cli")
_check "REBAR_TICKET_CLI honored by _rebar_ticket_cli" "/x/rebar" "$out"

# Clean break: legacy DSO_* env vars are IGNORED.
out=$(env -u REBAR_TICKET_CLI DSO_TICKET_CLI="/x/legacy" bash -c "source '$CONFIG_SH'; _rebar_ticket_cli")
_check "legacy DSO_TICKET_CLI ignored (falls back to engine rebar)" "$REPO_ROOT/src/rebar/_engine/rebar" "$out"

echo ""
printf "PASSED: %d  FAILED: %d\n" "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
