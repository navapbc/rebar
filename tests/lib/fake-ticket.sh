#!/usr/bin/env bash
# tests/lib/fake-ticket.sh
# Reusable v3 ticket command stub for test suites.
#
# USAGE PATTERN
# ─────────────
# 1. Point TICKET_CMD at this script (or a copy of it) in your test setup:
#
#      export TICKET_CMD="$PLUGIN_ROOT/tests/lib/fake-ticket.sh"
#      export TICKET_LOG_FILE="$tmpdir/ticket.log"
#
# 2. Call the code under test normally; it will invoke $TICKET_CMD instead of
#    the real ticket CLI.
#
# 3. Assert on TICKET_LOG_FILE to verify which subcommands were called:
#
#      grep -q "^create " "$TICKET_LOG_FILE"  # ticket was created
#      grep -q "^comment " "$TICKET_LOG_FILE" # comment was posted
#
# ENVIRONMENT VARIABLES
# ──────────────────────
#   TICKET_LOG_FILE  Path to a file where each invocation is appended.
#                    One line per call: "<subcommand> <args...>"
#                    Optional — logging is silently skipped when unset.
#
#   TICKET_CMD       Set this to the path of this script so callers pick it up.
#
# SUBCOMMAND BEHAVIOUR
# ─────────────────────
#   create <type> <title> [flags…]  → prints "mock-xxxx" (fake ticket ID); exits 0
#   show   <id>   [flags…]          → prints a minimal JSON ticket object; exits 0
#   list   [flags…]                 → prints "[]" (empty JSON array); exits 0
#   comment <id> <body> [flags…]    → prints nothing; exits 0
#   *      (any other subcommand)   → prints nothing; exits 0
#
# CUSTOMISATION
# ─────────────
# If a test needs richer responses (e.g., list returning a specific ticket),
# write a one-off inline script rather than extending this shared stub.  Keep
# this file minimal and predictable — it is the baseline, not a framework.

set -uo pipefail

subcommand="${1:-}"

# Log the call when TICKET_LOG_FILE is set.
if [[ -n "${TICKET_LOG_FILE:-}" ]]; then
    echo "$*" >> "$TICKET_LOG_FILE"
fi

case "$subcommand" in
    create)
        # Return a deterministic fake ticket ID so callers can pattern-match it.
        echo "mock-xxxx"
        ;;
    show)
        # Return a minimal v3 ticket JSON object.
        # Fields match what sprint-next-batch.sh and classify-task.sh expect.
        ticket_id="${2:-mock-xxxx}"
        printf '{"ticket_id":"%s","ticket_type":"task","status":"open","title":"Mock ticket %s","priority":2}\n' \
            "$ticket_id" "$ticket_id"
        ;;
    list)
        # Return an empty JSON array — no tickets exist by default.
        echo "[]"
        ;;
    comment)
        # Accept the comment silently; nothing to return.
        ;;
    *)
        # Unknown subcommand — succeed silently so scripts don't abort.
        ;;
esac

exit 0
