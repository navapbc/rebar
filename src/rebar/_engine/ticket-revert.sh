#!/usr/bin/env bash
# ticket-revert.sh
# Write a REVERT event targeting an existing event in a ticket.
#
# Usage: ticket revert <ticket_id> <target_uuid> [--reason=<text>]
#   ticket_id:   the ticket directory name (e.g., w21-ablv)
#   target_uuid: UUID of the event to revert
#   --reason=    optional human-readable reason (default: "")
#
# Constraints:
#   - target_uuid must exist in the ticket's event files
#   - cannot revert a REVERT event (REVERT-of-REVERT is rejected)
#
# The script writes the event file directly and then attempts a git commit
# in the tracker worktree. The git commit is skipped if the tracker directory
# is not a valid git worktree (test environments).
#
# Exits 0 on success, 1 on validation failure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=${_PLUGIN_ROOT}/scripts/ticket-lib.sh
source "$SCRIPT_DIR/ticket-lib.sh"

# Allow tests to inject a custom tracker directory via TICKETS_TRACKER_DIR env var.
# When GIT_DIR is set (e.g., in tests), derive REPO_ROOT from its parent to avoid
# requiring an actual git repository at that path.
if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
    TRACKER_DIR="$TICKETS_TRACKER_DIR"
elif [ -n "${GIT_DIR:-}" ]; then
    REPO_ROOT="$(dirname "$GIT_DIR")"
    TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
else
    REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"
    TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
fi

# ── Usage ─────────────────────────────────────────────────────────────────────
_usage() {
    echo "Usage: ticket revert <ticket_id> <target_uuid> [--reason=<text>]" >&2
    echo "  ticket_id:   ticket directory name" >&2
    echo "  target_uuid: UUID of the event to revert" >&2
    echo "  --reason=    optional reason text" >&2
    exit 1
}

# ── Parse arguments ──────────────────────────────────────────────────────────
if [ $# -lt 2 ]; then
    _usage
fi

ticket_id="$1"
target_uuid="$2"
reason=""

shift 2
for arg in "$@"; do
    case "$arg" in
        --reason=*)
            reason="${arg#--reason=}"
            ;;
        *)
            echo "Error: unknown argument '$arg'" >&2
            _usage
            ;;
    esac
done

# ── Validate ticket system is initialized ─────────────────────────────────────
if [ ! -f "$TRACKER_DIR/.env-id" ]; then
    echo "Error: ticket system not initialized. Run 'ticket init' first." >&2
    exit 1
fi

# ── Resolve any ID form (full, short, alias, jira_key, prefix) to canonical ──
if ! ticket_id=$(TICKETS_TRACKER_DIR="$TRACKER_DIR" resolve_ticket_id "$ticket_id"); then
    exit 1
fi

# ── Validate ticket exists (ghost check) ─────────────────────────────────────
ticket_dir="$TRACKER_DIR/$ticket_id"
if [ ! -d "$ticket_dir" ]; then
    echo "Error: ticket '$ticket_id' does not exist" >&2
    exit 1
fi

if ! find "$ticket_dir" -maxdepth 1 \( -name '*-CREATE.json' -o -name '*-SNAPSHOT.json' \) ! -name '.*' 2>/dev/null | grep -q .; then
    echo "Error: ticket $ticket_id has no CREATE or SNAPSHOT event" >&2
    exit 1
fi

# ── Find the target event by UUID ─────────────────────────────────────────────
# Event filenames: <timestamp>-<uuid>-<event_type>.json
# UUID portion: everything between the first dash-separated timestamp and the
# trailing -<EVENT_TYPE>.json. Use python3 to search via JSON content for reliability.
target_event_file=""
target_event_type=""

while IFS= read -r -d '' event_file; do
    result=$(python3 -c "
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        data = json.load(f)
    if data.get('uuid') == sys.argv[2]:
        print(data.get('event_type', ''))
except Exception:
    pass
" "$event_file" "$target_uuid")
    if [ -n "$result" ]; then
        target_event_file="$event_file"
        target_event_type="$result"
        break
    fi
done < <(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '.*' -print0 2>/dev/null)

if [ -z "$target_event_file" ]; then
    echo "Error: event not found: no event with UUID '$target_uuid' in ticket '$ticket_id'" >&2
    exit 1
fi

# ── Reject REVERT-of-REVERT ──────────────────────────────────────────────────
if [ "$target_event_type" = "REVERT" ]; then
    echo "Error: cannot revert a REVERT event (target UUID '$target_uuid' is a REVERT)" >&2
    exit 1
fi

# ── Build REVERT event JSON ───────────────────────────────────────────────────
env_id=$(cat "$TRACKER_DIR/.env-id")
author=$(git config user.name 2>/dev/null || echo "Unknown")

event_json=$(python3 -c "
import json, sys, time, uuid as _uuid_mod

env_id = sys.argv[1]
author = sys.argv[2]
target_event_uuid = sys.argv[3]
target_event_type = sys.argv[4]
reason = sys.argv[5]

timestamp = time.time_ns()
event_uuid = str(_uuid_mod.uuid4())

event = {
    'timestamp': timestamp,
    'uuid': event_uuid,
    'event_type': 'REVERT',
    'env_id': env_id,
    'author': author,
    'data': {
        'target_event_uuid': target_event_uuid,
        'target_event_type': target_event_type,
        'reason': reason,
    },
}
print(json.dumps(event))
" "$env_id" "$author" "$target_uuid" "$target_event_type" "$reason") || {
    echo "Error: failed to build REVERT event JSON" >&2
    exit 1
}

# ── Extract timestamp and uuid for filename ───────────────────────────────────
timestamp=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d['timestamp'])" "$event_json")
event_uuid=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d['uuid'])" "$event_json")

# ── Write event file ──────────────────────────────────────────────────────────
final_filename="${timestamp}-${event_uuid}-REVERT.json"
final_path="$ticket_dir/$final_filename"

python3 -c "
import json, sys
data = json.loads(sys.argv[1])
with open(sys.argv[2], 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False)
" "$event_json" "$final_path" || {
    echo "Error: failed to write REVERT event file" >&2
    exit 1
}

# ── Remove .archived marker when reverting an ARCHIVED event (best-effort) ───
if [ "$target_event_type" = "ARCHIVED" ]; then
    python3 -c 'import sys, os; sys.path.insert(0, sys.argv[1]); from ticket_reducer.marker import remove_marker; remove_marker(os.path.join(sys.argv[2], sys.argv[3]))' \
        "$SCRIPT_DIR" "$TRACKER_DIR" "$ticket_id" 2>/dev/null || true
fi

# ── Commit to tracker git worktree (if available) ────────────────────────────
# Skip git operations if tracker is not a valid git worktree (e.g., test environments).
if [ -f "$TRACKER_DIR/.git" ] && git -C "$TRACKER_DIR" rev-parse --is-inside-work-tree &>/dev/null; then
    git -C "$TRACKER_DIR" config gc.auto 0 2>/dev/null || true
    git -C "$TRACKER_DIR" add "$ticket_id/$final_filename" 2>/dev/null || true
    commit_exit=0
    git -C "$TRACKER_DIR" commit -q --no-verify -m "ticket: REVERT $ticket_id" 2>/dev/null || commit_exit=$?
    # SC3: rollback marker removal if commit failed
    if [ "$commit_exit" -ne 0 ] && [ "${target_event_type:-}" = "ARCHIVED" ]; then
        python3 -c "import sys, pathlib; pathlib.Path(sys.argv[1]).touch()" \
            "$TRACKER_DIR/$ticket_id/.archived" 2>/dev/null || true
    fi
fi

echo "Reverted event '$target_uuid' on ticket '$ticket_id'"
exit 0
