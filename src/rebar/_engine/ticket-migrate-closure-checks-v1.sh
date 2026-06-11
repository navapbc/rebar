#!/usr/bin/env bash
# ticket-migrate-closure-checks-v1.sh
# One-time migration: add ## Closure Checks section to epic and story tickets
# that lack it in their description.
#
# Schema migration sentinel: v1.2.0
#
# Usage:
#   ticket-migrate-closure-checks-v1.sh [--target <host-project-root>] [--dry-run]
#
# Flags:
#   --target <path>     Path to the host project root (default: git rev-parse --show-toplevel)
#   --dry-run           Show what would change without making any changes (read-only)
#
# Exit codes:
#   0 — Success (including idempotent re-run and plugin-source-repo guard)
#   1 — Fatal error

set -euo pipefail

# ── Self-location ────────────────────────────────────────────────────────────
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Parse arguments ──────────────────────────────────────────────────────────
_TARGET=""
_DRYRUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --target)
            _TARGET="$2"
            shift 2
            ;;
        --target=*)
            _TARGET="${1#--target=}"
            shift
            ;;
        --dry-run)
            _DRYRUN=1
            shift
            ;;
        *)
            echo "Error: unknown argument '$1'" >&2
            exit 1
            ;;
    esac
done

# Resolve target (default: git rev-parse --show-toplevel from within script context)
if [ -z "$_TARGET" ]; then
    _TARGET="$(git rev-parse --show-toplevel)"
fi

# ── Plugin-source-repo guard ─────────────────────────────────────────────────
# Tests inject a plugin.json marker to simulate the plugin source repo.
# The plugin source repo has no .tickets-tracker directory, so standalone
# invocations would exit at the tracker check below anyway — this is an
# explicit belt-and-suspenders guard for test environments.
if [ -f "$_TARGET/plugin.json" ]; then
    echo "NOTICE: target '$_TARGET' is the plugin source repo — skipping migration (no changes made)" >&2
    exit 0
fi

# ── Marker check (idempotency) ────────────────────────────────────────────────
_MARKER_FILE="$_TARGET/.rebar/.closure-checks-migration-v1"
if [ -f "$_MARKER_FILE" ]; then
    exit 0
fi

# ── Ticket tracker location ───────────────────────────────────────────────────
_TRACKER_DIR="$_TARGET/.tickets-tracker"

if [ ! -d "$_TRACKER_DIR" ]; then
    echo "NOTICE: no .tickets-tracker at '$_TARGET' — skipping migration" >&2
    exit 0
fi

# ── Single-pass migration ─────────────────────────────────────────────────────
# One python invocation scans every ticket, writes EDIT events for eligible
# tickets (epics and stories lacking ## Closure Checks), and emits machine-
# readable lines on stdout. Bash then commits the written events per-ticket.
# Per-ticket python subprocesses are avoided so the first-run scan scales
# linearly with ticket count.
#
# stdout contract:
#   WROTE:<ticket_id>:<event_filename>   — EDIT event written; bash must git add+commit
#   WOULD_WRITE:<ticket_id>              — dry-run: would write EDIT event (no file created)
_migrate_output=$(python3 - "$_TRACKER_DIR" "$_DRYRUN" <<'PYEOF'
import json, os, re, sys, time, uuid

TRACKER = sys.argv[1]
DRYRUN = len(sys.argv) > 2 and sys.argv[2] == "1"
CLOSURE_HEADING = "## Closure Checks"
SUCCESS_CRITERIA_HEADING = "## Success Criteria"

def _get_current_description(tdir):
    """Return the most up-to-date description by reading all events in order."""
    description = None
    try:
        files = sorted(f for f in os.listdir(tdir)
                       if f.endswith('.json') and not f.startswith('.'))
    except OSError:
        return None
    for fn in files:
        try:
            with open(os.path.join(tdir, fn)) as f:
                ev = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        etype = ev.get('event_type', '')
        data = ev.get('data') or {}
        # CREATE events carry the initial description
        if etype == 'CREATE':
            desc = data.get('description')
            if desc is not None:
                description = desc
        # EDIT events may update the description
        elif etype == 'EDIT':
            fields = data.get('fields') or {}
            desc = fields.get('description')
            if desc is not None:
                description = desc
        # SNAPSHOT events carry compiled_state.description (compacted tickets)
        elif etype == 'SNAPSHOT':
            cs = data.get('compiled_state') or {}
            desc = cs.get('description')
            if desc is not None:
                description = desc
    return description

def _is_epic_or_story(tdir):
    """Return True if the ticket is an epic or story (by ticket_type)."""
    try:
        files = sorted(f for f in os.listdir(tdir)
                       if f.endswith('.json') and not f.startswith('.'))
    except OSError:
        return False
    for fn in files:
        try:
            with open(os.path.join(tdir, fn)) as f:
                ev = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        data = ev.get('data') or {}
        etype = ev.get('event_type', '')
        tt = data.get('ticket_type')
        if tt in ('epic', 'story'):
            return True
        if etype == 'SNAPSHOT':
            cs = data.get('compiled_state') or {}
            tt2 = cs.get('ticket_type')
            if tt2 in ('epic', 'story'):
                return True
    return False

def _insert_closure_checks(description):
    """
    Insert ## Closure Checks after ## Success Criteria (if present),
    otherwise append at the end of the description.

    Returns the new description string.
    """
    closure_section = "\n## Closure Checks\n\n"

    # Find ## Success Criteria position
    sc_idx = description.find(SUCCESS_CRITERIA_HEADING)
    if sc_idx != -1:
        # Find the end of the ## Success Criteria section
        # (next ## heading or end of string)
        rest = description[sc_idx + len(SUCCESS_CRITERIA_HEADING):]
        next_heading = re.search(r'(?m)^## ', rest)
        if next_heading:
            insert_pos = sc_idx + len(SUCCESS_CRITERIA_HEADING) + next_heading.start()
            return description[:insert_pos] + closure_section + description[insert_pos:]
        else:
            # ## Success Criteria is the last section — append after it
            return description + closure_section
    else:
        # No ## Success Criteria — append at end
        return description + closure_section

def write_edit_event(tdir, new_description):
    ts = time.time_ns()
    u = str(uuid.uuid4())
    fname = f"{ts}-{u}-EDIT.json"
    fpath = os.path.join(tdir, fname)
    event = {
        "timestamp": ts,
        "uuid": u,
        "event_type": "EDIT",
        "env_id": "00000000-0000-4000-8000-migration002",
        "author": "ticket-migrate-closure-checks-v1",
        "data": {"fields": {"description": new_description}},
    }
    with open(fpath, 'w', encoding='utf-8') as f:
        json.dump(event, f, ensure_ascii=False)
    return fname

try:
    entries = sorted(os.listdir(TRACKER))
except OSError:
    sys.exit(0)

for tid in entries:
    if tid.startswith('.'):
        continue
    tdir = os.path.join(TRACKER, tid)
    if not os.path.isdir(tdir):
        continue

    # Only process epics and stories
    if not _is_epic_or_story(tdir):
        continue

    # Get current description
    description = _get_current_description(tdir)
    if description is None:
        # No description found — skip (nothing to migrate)
        continue

    # Check if ## Closure Checks already present
    if CLOSURE_HEADING in description:
        continue

    # Insert the section
    new_description = _insert_closure_checks(description)

    if DRYRUN:
        print(f'WOULD_WRITE:{tid}')
    else:
        try:
            fname = write_edit_event(tdir, new_description)
            print(f'WROTE:{tid}:{fname}')
        except OSError as e:
            print(f'ERROR:{tid}:{e}', file=sys.stderr)
PYEOF
)

# ── Commit each written EDIT event ────────────────────────────────────────────
# Commits are per-ticket to match the existing migration script pattern and
# maintain auditability. The single-pass python scan emits WROTE: only after
# the EDIT event file has been successfully written; on OSError it emits ERROR:
# to stderr and skips that ticket, so a partial-write failure cannot cause a
# mis-committed EDIT for that ticket.
# Track commit failures so the marker file is only written after every
# WROTE: line landed a successful commit. Otherwise the marker would
# suppress future runs even though some tickets were never actually
# migrated to the tickets branch.
_commit_failures=0
while IFS= read -r _line; do
    [[ -z "$_line" ]] && continue
    case "$_line" in
        WROTE:*)
            _rest="${_line#WROTE:}"
            _ticket_id="${_rest%%:*}"
            _event_name="${_rest#*:}"
            if git -C "$_TRACKER_DIR" add "$_ticket_id/$_event_name" 2>/dev/null \
                && git -C "$_TRACKER_DIR" commit -m "migration: add ## Closure Checks section to $_ticket_id" 2>/dev/null; then
                : # success
            else
                _commit_failures=$(( _commit_failures + 1 ))
                git -C "$_TRACKER_DIR" reset 2>/dev/null || true
                # Remove the staged event file so the next run re-emits a
                # WROTE: line for this ticket; otherwise the python pass
                # would see the new EDIT event on disk and SKIP it.
                rm -f "$_TRACKER_DIR/$_ticket_id/$_event_name" 2>/dev/null || true
                echo "ERROR: $_ticket_id — git add/commit failed; will retry next run" >&2
            fi
            ;;
        WOULD_WRITE:*)
            echo "DRY-RUN: ${_line#WOULD_WRITE:}"
            ;;
    esac
done <<< "$_migrate_output"

# ── Write marker file (skipped in dry-run mode AND when any commit failed) ───
# The marker is the "all done" sentinel — only write it when all WROTE: events
# successfully committed. With partial failures, leave the marker absent so
# the next run retries the failed tickets and the audit script will still
# surface them as needing migration.
if [ "$_DRYRUN" = "0" ] && [ "$_commit_failures" -eq 0 ]; then
    mkdir -p "$(dirname "$_MARKER_FILE")"
    touch "$_MARKER_FILE"
fi

if [ "$_commit_failures" -gt 0 ]; then
    echo "ERROR: $_commit_failures ticket(s) had failed git commit; marker not written; re-run after resolving" >&2
    exit 1
fi
exit 0
