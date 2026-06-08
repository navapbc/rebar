#!/usr/bin/env bash
# compute-verdict-hash.sh — Compute an HMAC that proves a verifier verdict for a ticket.
#
# The hash encodes: this ticket received this verdict at this git state.
# Both this script and ticket-transition.sh compute the same HMAC independently,
# so the hash cannot be reused across tickets, verdicts, or git states.
#
# Usage:
#   compute-verdict-hash.sh <ticket-id> <verdict>
#
# Output: HMAC-SHA256 hex string to stdout
#
# Inputs to HMAC:
#   key:  contents of <tracker-dir>/.closure-key
#   data: "<ticket-id>|<verdict>|<head-sha>"
#
# The .closure-key is generated at ticket system init (alongside .env-id).
# It is gitignored and local to each environment.
set -euo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: compute-verdict-hash.sh <ticket-id> <verdict>" >&2
    exit 1
fi

TICKET_ID="$1"
VERDICT="$2"

case "$VERDICT" in
    PASS|FAIL|BLOCKED|INCONCLUSIVE|EVIDENCE_PENDING) ;;
    *)
        echo "Error: invalid verdict '$VERDICT'. Must be one of: PASS, FAIL, BLOCKED, INCONCLUSIVE, EVIDENCE_PENDING" >&2
        exit 1
        ;;
esac

REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null)}"
TRACKER_DIR="$REPO_ROOT/.tickets-tracker"  # tickets-boundary-ok: reads .closure-key for HMAC

KEY_FILE="$TRACKER_DIR/.closure-key"
if [ ! -f "$KEY_FILE" ]; then
    python3 -c "import uuid; print(uuid.uuid4())" > "$KEY_FILE"
fi

HEAD_SHA=$(git rev-parse HEAD 2>/dev/null || echo "unknown")

python3 -c "
import hmac, hashlib, sys

with open(sys.argv[1], 'r') as f:
    key = f.read().strip().encode()

data = f'{sys.argv[2]}|{sys.argv[3]}|{sys.argv[4]}'.encode()
print(hmac.new(key, data, hashlib.sha256).hexdigest())
" "$KEY_FILE" "$TICKET_ID" "$VERDICT" "$HEAD_SHA"
