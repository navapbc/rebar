#!/usr/bin/env bash
# ticket-scratch-get.sh
# Read a scratch value for a ticket key.
#
# Usage: ticket-scratch-get.sh <ticket_id> <key>
#
# On hit  (file exists and is non-empty):
#   Emits {"status":"hit","ts":<iso8601>,"value":<value>} to stdout; exits 0.
#
# On miss (file missing or empty):
#   Emits {"status":"miss","ticket_id":<id>,"key":<key>} to stdout; exits 0.
#   NOTE: miss exits 0 — orchestrators distinguish presence via the status
#   field, not the exit code.
#
# On invalid ticket_id or key:
#   Emits {"status":"error","code":<code>,"reason":<reason>} to stdout;
#   exits non-zero.
#
# On wrong argument count:
#   Prints usage to stderr; exits 1.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=${_PLUGIN_ROOT}/scripts/ticket-lib.sh
source "$SCRIPT_DIR/ticket-lib.sh"

# ── Usage ──────────────────────────────────────────────────────────────────────
_usage() {
    echo "Usage: ticket scratch get <ticket_id> <key>" >&2
    echo "  ticket_id: ticket namespace identifier" >&2
    echo "  key:       scratch key name" >&2
    exit 1
}

if [ $# -lt 2 ]; then
    _usage
fi

ticket_id="$1"
key="$2"

# ── Resolve and validate ───────────────────────────────────────────────────────
# _scratch_resolve_and_validate prints:
#   - the absolute path to stdout and exits 0 on valid inputs
#   - a JSON error envelope to stdout and exits non-zero on invalid inputs
validate_output=""
validate_exit=0
validate_output=$(_scratch_resolve_and_validate "$ticket_id" "$key") \
    || validate_exit=$?

if [ "$validate_exit" -ne 0 ]; then
    # Propagate the JSON error envelope from the validator
    printf '%s\n' "$validate_output"
    exit "$validate_exit"
fi

abs_path="$validate_output"

# ── Read the envelope ──────────────────────────────────────────────────────────
# _scratch_read_envelope returns non-zero on missing or empty file (miss).
envelope_output=""
envelope_exit=0
envelope_output=$(_scratch_read_envelope "$abs_path") \
    || envelope_exit=$?

if [ "$envelope_exit" -ne 0 ]; then
    # Miss — emit miss JSON and exit 0 (load-bearing contract)
    python3 -c "
import json, sys
print(json.dumps({'status': 'miss', 'ticket_id': sys.argv[1], 'key': sys.argv[2]}))
" "$ticket_id" "$key"
    exit 0
fi

# ── Hit — extract ts and value from stored envelope, emit hit JSON ─────────────
python3 -c "
import json, sys

raw = sys.argv[1]
ticket_id = sys.argv[2]
key = sys.argv[3]

try:
    stored = json.loads(raw)
except json.JSONDecodeError as e:
    # Malformed envelope — treat as an internal error but still exit 0
    # with a minimal hit shape so callers can introspect value
    # (we must not emit miss here since the file exists; surface the raw content)
    err = {'status': 'error', 'code': 'malformed_envelope',
           'reason': str(e), 'ticket_id': ticket_id, 'key': key}
    print(json.dumps(err))
    sys.exit(1)

ts = stored.get('ts', '')
value = stored.get('value', '')

hit = {'status': 'hit', 'ts': ts, 'value': value}
print(json.dumps(hit))
sys.exit(0)
" "$envelope_output" "$ticket_id" "$key"
