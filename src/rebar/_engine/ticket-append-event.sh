#!/usr/bin/env bash
# ticket-append-event.sh — the Tier B write seam (docs/bash-migration.md §4).
#
# Usage: ticket-append-event.sh <ticket_id> <staged_event_json_path>
#
# Tier B of the bash→Python strangler-fig ports each leaf-write command's
# argument parsing / validation / event composition into Python
# (rebar._commands), but deliberately does NOT port the locked write path yet
# (that is Tier D). Instead, a Python command composes the event JSON into a
# temp file and hands it to this seam, which delegates to ticket-lib.sh's
# write_commit_event — the ONE locked write path (flock + atomic rename + git
# commit + best-effort push). So invariant I5 (single locked write path) holds
# unchanged while all the bash above it is deleted.
#
# write_commit_event derives the commit message ("ticket: <TYPE> <id>") and the
# final filename from the event JSON itself, validates the event_type enum, and
# canonicalises via `jq -S -c` — so this seam stays a thin pass-through and the
# committed bytes are identical to the bash command path.
#
# When Tier D lands, the seam's INTERIOR swaps from this script to
# rebar._store.event_append under REBAR_WRITE_CORE; the Python commands that call
# the seam do not change again.
#
# Exit: 0 on success; write_commit_event's exit code otherwise (e.g. 75 =
# rebase/merge guard, 1 = lock timeout / commit failure). Same contract Rec 7a
# routes the reconciler's direct event writes through.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/ticket-lib.sh"

if [ $# -lt 2 ]; then
    echo "Usage: ticket-append-event.sh <ticket_id> <staged_event_json_path>" >&2
    exit 1
fi

ticket_id="$1"
staged_event="$2"

if [ ! -f "$staged_event" ]; then
    echo "Error: staged event file not found: $staged_event" >&2
    exit 1
fi

write_commit_event "$ticket_id" "$staged_event"
