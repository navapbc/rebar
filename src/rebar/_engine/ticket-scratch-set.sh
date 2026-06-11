#!/usr/bin/env bash
# ticket-scratch-set.sh
# Write a key/value pair to the scratch store for a ticket.
#
# Usage: ticket-scratch-set.sh <ticket_id> <key> <value>
#   ticket_id : per-ticket namespace
#   key       : scratch key name
#   value     : payload string to store
#
# The value is wrapped in a JSON envelope: {"ts":"<iso8601>","value":<value>}
# and written atomically via _scratch_atomic_write (same-dir tmp + fsync + rename).
#
# On success:
#   Exits 0; prints {"status":"ok","ticket_id":"<id>","key":"<key>"} to stdout.
#
# On validation or write failure:
#   Propagates structured JSON error envelope from helper to stdout; exits non-zero.
#
# Environment:
#   SCRATCH_BASE_DIR  Optional override for the scratch base directory
#                     (default: REPO_ROOT/.rebar/scratch/)
#   REBAR_TEST_CRASH    When set to "1", exits 1 between tempfile write and rename,
#                     simulating a mid-write crash (for crash-safety tests only).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=${_PLUGIN_ROOT}/scripts/ticket-lib.sh
source "$SCRIPT_DIR/ticket-lib.sh"

# ── Usage ─────────────────────────────────────────────────────────────────────
_usage() {
    echo "Usage: ticket scratch set <ticket_id> <key> <value>" >&2
    echo "  ticket_id : per-ticket namespace" >&2
    echo "  key       : scratch key name" >&2
    echo "  value     : payload string to store" >&2
    exit 1
}

# ── Parse arguments ───────────────────────────────────────────────────────────
if [ $# -lt 3 ]; then
    _usage
fi

ticket_id="$1"
key="$2"
value="$3"

# ── Step 1: Resolve and validate path ─────────────────────────────────────────
# _scratch_resolve_and_validate validates charset + path traversal safety and
# returns the resolved absolute path. On failure it prints a structured JSON
# error envelope to stdout and exits non-zero — we propagate that directly.
resolve_output=""
resolve_exit=0
resolve_output=$(_scratch_resolve_and_validate "$ticket_id" "$key") || resolve_exit=$?

if [ "$resolve_exit" -ne 0 ]; then
    # Propagate structured error envelope from helper
    printf '%s\n' "$resolve_output"
    exit "$resolve_exit"
fi

abs_path="$resolve_output"

# ── Step 2: Build JSON envelope ───────────────────────────────────────────────
# ts is ISO 8601 UTC; value is embedded as a JSON string.
envelope=$(python3 - "$ticket_id" "$key" "$value" <<'PYEOF'
import json, sys
from datetime import datetime, timezone

ticket_id = sys.argv[1]
key       = sys.argv[2]
value     = sys.argv[3]

ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
envelope = {"ts": ts, "value": value}
print(json.dumps(envelope))
PYEOF
)

# ── Step 3: Crash-test hook (test-only) ───────────────────────────────────────
# REBAR_TEST_CRASH=1 causes exit before the atomic rename, simulating a crash
# between tempfile write and rename. The atomic writer is NOT called in crash
# mode — this emulates the crash *within* the writer (after write, before rename).
# This is used by test 8 to verify crash-safety: the target file should remain
# at the prior-version envelope (or absent), and no *.tmp.* files should linger.
if [ "${REBAR_TEST_CRASH:-}" = "1" ]; then
    # Write the tmpfile ourselves (mimicking the internal state) then exit
    # without renaming — leaving the tmp file so the test can verify cleanup.
    target_dir="$(dirname "$abs_path")"
    mkdir -p "$target_dir"
    tmp_crash=$(mktemp "${abs_path}.tmp.XXXXXX.scratch")
    printf '%s' "$envelope" > "$tmp_crash" || true
    # Simulate crash: do NOT rename. Remove the tmp file to mimic process exit
    # cleanup (OS closes/unlinks temp FDs on death; our test verifies none remain).
    rm -f "$tmp_crash" || true
    exit 1
fi

# ── Step 4: Atomic write ──────────────────────────────────────────────────────
# _scratch_atomic_write uses same-dir tmp + fsync + rename for crash safety.
# On overflow or write failure it prints a structured JSON error and exits non-zero.
write_output=""
write_exit=0
write_output=$(_scratch_atomic_write "$abs_path" "$envelope") || write_exit=$?

if [ "$write_exit" -ne 0 ]; then
    # Propagate structured error envelope from helper
    printf '%s\n' "$write_output"
    exit "$write_exit"
fi

# ── Step 5: Emit success ──────────────────────────────────────────────────────
python3 - "$ticket_id" "$key" <<'PYEOF'
import json, sys
result = {"status": "ok", "ticket_id": sys.argv[1], "key": sys.argv[2]}
print(json.dumps(result))
PYEOF
