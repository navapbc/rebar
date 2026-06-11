#!/usr/bin/env bash
# ticket-scratch-clear.sh — Remove one scratch key or an entire ticket scratch directory.
#
# Usage:
#   ticket-scratch-clear.sh <ticket_id> [<key>]
#
# With key:    validates <ticket_id> and <key>, then removes the single scratch
#              file at .rebar/scratch/<ticket_id>/<key>. Missing target is OK.
#              Emits: {"status":"ok","ticket_id":"...","key":"...","removed":<0|1>}
#
# Without key: validates <ticket_id> only, then removes the entire per-ticket
#              scratch directory at .rebar/scratch/<ticket_id>/. Missing target
#              is OK.
#              Emits: {"status":"ok","ticket_id":"...","removed":<count>}
#
# Exit codes:
#   0  — success (including idempotent no-op)
#   1  — invalid argument (structured JSON error envelope on stdout)
#
# Environment:
#   SCRATCH_BASE_DIR — override base directory (default: REPO_ROOT/.rebar/scratch)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Source ticket-lib.sh for _scratch_resolve_and_validate ───────────────────
TICKET_LIB="$SCRIPT_DIR/ticket-lib.sh"
if [ ! -f "$TICKET_LIB" ]; then
    printf '{"status":"error","code":"internal","reason":"ticket-lib.sh not found at %s"}\n' "$TICKET_LIB"
    exit 1
fi
# shellcheck source=./ticket-lib.sh
source "$TICKET_LIB"

# ── Argument handling ─────────────────────────────────────────────────────────
TICKET_ID="${1:-}"
KEY="${2:-}"

if [ -z "$TICKET_ID" ]; then
    printf '{"status":"error","code":"missing_args","reason":"Usage: ticket-scratch-clear.sh <ticket_id> [<key>]"}\n'
    exit 1
fi

# ── Resolve base dir ──────────────────────────────────────────────────────────
if [ -n "${SCRATCH_BASE_DIR:-}" ]; then
    BASE_DIR="$SCRATCH_BASE_DIR"
else
    REPO_ROOT="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)" || REPO_ROOT=""
    BASE_DIR="${REPO_ROOT}/.rebar/scratch"
fi

# ══════════════════════════════════════════════════════════════════════════════
# Single-key mode: ticket-scratch-clear.sh <ticket_id> <key>
# ══════════════════════════════════════════════════════════════════════════════
if [ -n "$KEY" ]; then
    # Validate both ticket_id and key via the shared helper
    resolved_path=""
    resolved_path=$(_scratch_resolve_and_validate "$TICKET_ID" "$KEY" "$BASE_DIR")
    rc=$?
    if [ $rc -ne 0 ]; then
        # _scratch_resolve_and_validate already printed the JSON error envelope
        printf '%s\n' "$resolved_path"
        exit 1
    fi

    removed=0
    if [ -f "$resolved_path" ]; then
        rm -f "$resolved_path"
        removed=1
    fi

    python3 -c "
import json, sys
print(json.dumps({'status': 'ok', 'ticket_id': sys.argv[1], 'key': sys.argv[2], 'removed': int(sys.argv[3])}))
" "$TICKET_ID" "$KEY" "$removed"
    exit 0
fi

# ══════════════════════════════════════════════════════════════════════════════
# Whole-ticket mode: ticket-scratch-clear.sh <ticket_id>
# ══════════════════════════════════════════════════════════════════════════════
# Validate ticket_id only — reuse the helper with a dummy key, but we only
# check that ticket_id passes; we discard the resolved path entirely.
# To avoid requiring a real key, we validate ticket_id directly via Python.
validation_output=""
validation_output=$(python3 - "$TICKET_ID" <<'PYEOF'
import json, re, sys

ticket_id = sys.argv[1]

def _validate_component(value, field_name, code):
    if not value:
        print(json.dumps({"status": "error", "code": code,
                          "reason": f"{field_name} must not be empty"}))
        sys.exit(1)
    if value.startswith('.'):
        print(json.dumps({"status": "error", "code": code,
                          "reason": f"{field_name} must not start with a dot: {value!r}"}))
        sys.exit(1)
    if '..' in value:
        print(json.dumps({"status": "error", "code": code,
                          "reason": f"{field_name} must not contain '..': {value!r}"}))
        sys.exit(1)
    if '/' in value:
        print(json.dumps({"status": "error", "code": code,
                          "reason": f"{field_name} must not contain '/': {value!r}"}))
        sys.exit(1)
    if re.search(r'[\x00-\x1f]', value):
        print(json.dumps({"status": "error", "code": code,
                          "reason": f"{field_name} must not contain control characters: {value!r}"}))
        sys.exit(1)

_validate_component(ticket_id, "ticket_id", "invalid_id")
print("ok")
sys.exit(0)
PYEOF
)
val_rc=$?
if [ $val_rc -ne 0 ]; then
    printf '%s\n' "$validation_output"
    exit 1
fi

ticket_dir="$BASE_DIR/$TICKET_ID"
removed=0

if [ -d "$ticket_dir" ]; then
    # Count regular files before removal
    removed=$(find "$ticket_dir" -maxdepth 1 -type f 2>/dev/null | wc -l | tr -d ' ')
    rm -rf "$ticket_dir"
fi

python3 -c "
import json, sys
print(json.dumps({'status': 'ok', 'ticket_id': sys.argv[1], 'removed': int(sys.argv[2])}))
" "$TICKET_ID" "$removed"
exit 0
