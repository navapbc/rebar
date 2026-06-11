#!/usr/bin/env bash
# ticket-purge-bridge.sh — Canonical implementation — invoked by dispatcher (ticket purge-bridge).
# Remove Jira-sourced tickets (jira-* prefix, materialized by the reconciler's
# inbound applier) from non-target Jira projects.
#
# Usage: ticket-purge-bridge.sh --keep=<PROJECT_KEY> [--dry-run]
#
# Scans .tickets-tracker/ for ticket directories prefixed with "jira-".
# For each, reads the CREATE event to extract the Jira project key.
# Deletes all jira-* ticket directories whose project key does NOT match --keep.
# Does NOT touch non-jira-* tickets (migrated, native rebar IDs, etc.).
#
# After deletion, commits the removal on the tickets branch.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Parse args
KEEP_PROJECT=""
DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --keep=*) KEEP_PROJECT="${arg#--keep=}" ;;
        --dry-run) DRY_RUN=true ;;
        *) echo "Usage: ticket-purge-bridge.sh --keep=<PROJECT_KEY> [--dry-run]" >&2; exit 1 ;;
    esac
done

if [ -z "$KEEP_PROJECT" ]; then
    echo "Error: --keep=<PROJECT_KEY> is required" >&2
    exit 1
fi

REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"
TRACKER_DIR="${TICKETS_TRACKER_DIR:-$REPO_ROOT/.tickets-tracker}"

# Ensure tracker is initialized (worktree startup race condition fix).
# In fresh worktrees, .tickets-tracker is a symlink created by ticket-init.sh.
# If the tracker dir doesn't exist and TICKETS_TRACKER_DIR is not set (i.e., we
# are using the default path, not a test override), call ticket-init.sh to create
# the symlink before reading.
if [ ! -d "$TRACKER_DIR" ] && [ -z "${TICKETS_TRACKER_DIR:-}" ] && [ -f "$SCRIPT_DIR/ticket-init.sh" ]; then
    _init_stderr=$(bash "$SCRIPT_DIR/ticket-init.sh" --silent 2>&1 >/dev/null) || {
        if [ -n "$_init_stderr" ]; then
            echo "Warning: ticket-init.sh failed — $_init_stderr" >&2
        fi
    }
fi

if [ ! -d "$TRACKER_DIR" ]; then
    echo "Error: tracker directory not found at $TRACKER_DIR" >&2
    exit 1
fi

echo "Scanning for non-$KEEP_PROJECT Jira-sourced tickets (jira-* prefix)..."

# Find all jira-* directories and check their project key
DELETE_LIST=""
DELETE_COUNT=0
KEEP_COUNT=0
SKIP_COUNT=0

for ticket_dir in "$TRACKER_DIR"/jira-*/; do
    [ -d "$ticket_dir" ] || continue
    ticket_id=$(basename "$ticket_dir")

    # Extract project key from CREATE event
    project_key=""
    for event_file in "$ticket_dir"/*-CREATE.json; do
        [ -f "$event_file" ] || continue
        project_key=$(python3 -c "
import json, sys
try:
    ev = json.load(open(sys.argv[1]))
    jira_key = ev.get('data', {}).get('jira_key', '')
    print(jira_key.split('-')[0] if '-' in jira_key else '')
except Exception:
    print('')
" "$event_file" 2>/dev/null)
        break
    done

    if [ -z "$project_key" ]; then
        SKIP_COUNT=$(( SKIP_COUNT + 1 ))
        continue
    fi

    if [ "$project_key" = "$KEEP_PROJECT" ]; then
        KEEP_COUNT=$(( KEEP_COUNT + 1 ))
    else
        DELETE_COUNT=$(( DELETE_COUNT + 1 ))
        DELETE_LIST="${DELETE_LIST}${ticket_dir}"$'\n'
    fi
done

echo "Results:"
echo "  Keep ($KEEP_PROJECT): $KEEP_COUNT"
echo "  Delete (non-$KEEP_PROJECT): $DELETE_COUNT"
echo "  Skip (no project key): $SKIP_COUNT"

if [ "$DELETE_COUNT" -eq 0 ]; then
    echo "Nothing to delete."
    exit 0
fi

if [ "$DRY_RUN" = true ]; then
    echo "[DRY RUN] Would delete $DELETE_COUNT ticket directories."
    exit 0
fi

echo "Deleting $DELETE_COUNT ticket directories..."

deleted=0
while IFS= read -r ticket_dir; do
    [ -z "$ticket_dir" ] && continue
    rm -rf "$ticket_dir"
    deleted=$(( deleted + 1 ))
    if (( deleted % 500 == 0 )); then
        echo "  Deleted $deleted / $DELETE_COUNT..."
    fi
done <<< "$DELETE_LIST"

echo "Deleted $deleted ticket directories."

# Commit the deletion on the tickets branch (if in a git worktree)
if git -C "$TRACKER_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    echo "Committing deletion on tickets branch..."
    git -C "$TRACKER_DIR" add -A
    git -C "$TRACKER_DIR" commit --no-verify -m "purge: remove $deleted non-$KEEP_PROJECT Jira-sourced (jira-*) tickets" || echo "Nothing to commit"
fi

echo "Done."
