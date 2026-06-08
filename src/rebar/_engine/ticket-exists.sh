#!/usr/bin/env bash
# ticket-exists.sh — O(1) presence check for a ticket in the tracker.
# Exit 0 if ticket exists, exit 1 if not.
set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "Usage: ticket exists <ticket_id>" >&2
    exit 1
fi

ticket_id="$1"

# Resolve tracker dir without unconditional git subprocess.
if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
    TRACKER_DIR="$TICKETS_TRACKER_DIR"
else
    REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"
    TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
fi

# Resolve 8-char short ID prefix (e.g. "f61f-7e0a") to full canonical ID.
# The tracker may be a symlink (worktrees); use -L so find follows it.
if [[ "$ticket_id" =~ ^[a-z0-9]{4}-[a-z0-9]{4}$ ]]; then
    _full_id=""
    _match_count=0
    while IFS= read -r -d '' _entry; do
        # Bug 19a3-03ca: ${var##*/} param expansion — no basename subprocess per entry.
        _base="${_entry##*/}"
        if [[ "${_base:0:9}" == "$ticket_id" ]] && \
           [[ "$_base" =~ ^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$ ]]; then
            _full_id="$_base"
            _match_count=$((_match_count + 1))
        fi
    done < <(find -L "$TRACKER_DIR" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -print0 2>/dev/null)
    if [ "$_match_count" -eq 1 ]; then
        ticket_id="$_full_id"
    fi
fi

ticket_dir="$TRACKER_DIR/$ticket_id"

# Check for CREATE (normal) or SNAPSHOT (post-compaction) events.
if [ -d "$ticket_dir" ] && \
   { ls "$ticket_dir/"*-CREATE.json >/dev/null 2>&1 || ls "$ticket_dir/"*-SNAPSHOT.json >/dev/null 2>&1; }; then
    exit 0
fi
exit 1
