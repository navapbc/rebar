#!/usr/bin/env bash
# ticket-exists.sh — O(1)-ish presence check for a ticket in the tracker.
# Exit 0 if ticket exists, exit 1 if not.
#
# Accepts any id form (full 16-hex ID, 8-hex short ID, alias, jira_key, or unique
# prefix) — consistent with show/edit/claim — by resolving the input through the
# shared resolve_ticket_id helper before the presence check.
set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "Usage: ticket exists <ticket_id>" >&2
    exit 1
fi

raw_id="$1"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve tracker dir without unconditional git subprocess.
if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
    TRACKER_DIR="$TICKETS_TRACKER_DIR"
else
    REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"
    TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
fi

# has_ticket_events — true if $1 is a ticket dir with CREATE/SNAPSHOT events.
has_ticket_events() {
    local _dir="$1"
    [ -d "$_dir" ] && \
        { ls "$_dir/"*-CREATE.json >/dev/null 2>&1 || ls "$_dir/"*-SNAPSHOT.json >/dev/null 2>&1; }
}

# Fast path: exact directory-name match (O(1), no resolver/subprocess). Covers
# canonical ids and any literal directory name without needing id resolution.
if has_ticket_events "$TRACKER_DIR/$raw_id"; then
    exit 0
fi

# Resolve any other id form (8-hex short, alias, jira_key, unique prefix) to the
# canonical ticket directory name — consistent with show/edit/claim. resolve_ticket_id
# verifies presence and exits non-zero if the input cannot be resolved to an
# existing ticket, which is exactly the "absent" semantics we want.
declare -f resolve_ticket_id >/dev/null 2>&1 || source "$SCRIPT_DIR/ticket-lib.sh"
if ! ticket_id=$(TICKETS_TRACKER_DIR="$TRACKER_DIR" resolve_ticket_id "$raw_id" 2>/dev/null); then
    exit 1
fi

# Final presence check on the resolved canonical id.
if has_ticket_events "$TRACKER_DIR/$ticket_id"; then
    exit 0
fi
exit 1
