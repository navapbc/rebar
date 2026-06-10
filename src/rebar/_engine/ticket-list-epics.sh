#!/usr/bin/env bash
set -euo pipefail
# ticket-list-epics.sh — List unblocked epics, sorted by priority.
#
# Reads ticket state from the v3 event-sourced tracker via ticket-reducer.py.
#
# Usage:
#   ticket-list-epics.sh              # List unblocked open epics, sorted by priority
#   ticket-list-epics.sh --all        # Include blocked epics (marked with BLOCKED)
#
# Output: One line per epic, tab-separated:
#   <alias-or-id>\tP*\t<title>\t<child_count>                     (in-progress epics, listed first — P* replaces priority)
#   <alias-or-id>\tP<priority>\t<title>\t<child_count>            (unblocked open epics)
#
#   Column 1 is the human-friendly alias when one is set; falls back to canonical
#   ticket ID when no alias exists. For machine-parseable canonical IDs use
#   `ticket show <alias>` or the ticket list command.
#
# Blocked epics (with --all) are appended after unblocked, prefixed:
#   BLOCKED\t<alias-or-id>\tP<priority>\t<title>\t<child_count>\t<blocker_ids>
#
# Exit codes:
#   0 — At least one unblocked epic found
#   1 — No open epics exist
#   2 — Open epics exist but all are blocked (details on stderr)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Canonical structured-output flag (--output/-o); logic in ticket_output.py.
# shellcheck source=/dev/null
source "$SCRIPT_DIR/ticket-output.sh"

# Resolve --output/-o (report: text|json) and strip it; exported to the python
# emit block below as _LE_FMT (json emits {p0_bugs, epics}; exit 0/1/2 preserved).
_resolve_output_format report "$@" || exit 2
export _LE_FMT="$_OUTPUT_FMT"
_strip_output_flags "$@"
set -- ${_OUTPUT_ARGS[@]+"${_OUTPUT_ARGS[@]}"}

show_all=false
# min_children and max_children are intentionally unset by default (use ${var+x} set-check)
# has_tag is intentionally unset by default (use ${has_tag+x} set-check)
# without_tag is intentionally unset by default (use ${without_tag+x} set-check)
for _arg in "$@"; do
    case "$_arg" in
        --all) show_all=true ;;
        --min-children=*) min_children="${_arg#--min-children=}" ;;
        --max-children=*) max_children="${_arg#--max-children=}" ;;
        --has-tag=*) has_tag="${_arg#--has-tag=}" ;;
        --without-tag=*) without_tag="${_arg#--without-tag=}" ;;
    esac
done
unset _arg

REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"
REDUCER="$SCRIPT_DIR/ticket-reducer.py"

# ---------------------------------------------------------------------------
# v3 event-sourced ticket system.
# Reads from .tickets-tracker/ (or TICKETS_TRACKER_DIR env override).
# ---------------------------------------------------------------------------
TRACKER_DIR="${TICKETS_TRACKER_DIR:-$REPO_ROOT/.tickets-tracker}"

# ---------------------------------------------------------------------------
# Ensure tracker is initialized (worktree startup race condition fix).
# In fresh worktrees, .tickets-tracker is a symlink created by ticket-init.sh.
# If the tracker dir doesn't exist and TICKETS_TRACKER_DIR is not set (i.e., we
# are using the default path, not a test override), call ticket-init.sh to create
# the symlink before reading. Without this, ticket-list-epics.sh silently reports
# "No open epics found" when it's the first script to run in a new worktree session.
# ---------------------------------------------------------------------------
if [ ! -d "$TRACKER_DIR" ] && [ -z "${TICKETS_TRACKER_DIR:-}" ] && [ -f "$SCRIPT_DIR/ticket-init.sh" ]; then
    _init_stderr=$(bash "$SCRIPT_DIR/ticket-init.sh" --silent 2>&1 >/dev/null) || {
        if [ -n "$_init_stderr" ]; then
            echo "Warning: ticket-init.sh failed — $_init_stderr" >&2
        fi
    }
fi

# ---------------------------------------------------------------------------
# Fetch latest ticket data from remote (throttled to avoid redundant fetches).
# Uses the same 5-minute throttle as the ticket dispatcher's _ensure_initialized.
# ---------------------------------------------------------------------------
if [ -d "$TRACKER_DIR/.git" ] || [ -f "$TRACKER_DIR/.git" ]; then
    _resolved_tracker=$(cd "$TRACKER_DIR" && pwd -P 2>/dev/null || echo "$TRACKER_DIR")
    # Use 12-char truncated hash to match the ticket dispatcher's sync marker key
    _sync_hash=$(python3 -c "import hashlib,sys; print(hashlib.md5(sys.argv[1].encode()).hexdigest()[:12])" "$_resolved_tracker" 2>/dev/null || echo "fallback")
    _sync_marker="/tmp/.ticket-sync-${_sync_hash}"
    _needs_fetch=true
    if [ -f "$_sync_marker" ]; then
        if [[ "$(uname)" == "Darwin" ]]; then
            _marker_mtime=$(stat -f %m "$_sync_marker" 2>/dev/null || echo 0)
        else
            _marker_mtime=$(stat -c %Y "$_sync_marker" 2>/dev/null || echo 0)
        fi
        _marker_age=$(( $(date +%s) - _marker_mtime ))
        if [ "$_marker_age" -lt 300 ]; then
            _needs_fetch=false
        fi
    fi
    if [ "$_needs_fetch" = true ]; then
        if git -C "$TRACKER_DIR" fetch origin tickets 2>/dev/null; then
            # Two-phase sync guard (0051-3428 + eb00-efd0):
            # Phase 1: orphan branch (no merge-base) — safe to force-reset.
            # Phase 2: related history — preserve local-ahead commits.
            if ! git -C "$TRACKER_DIR" merge-base tickets origin/tickets &>/dev/null; then
                git -C "$TRACKER_DIR" reset --hard origin/tickets >/dev/null 2>&1 || true
            else
                _local_ahead=$(git -C "$TRACKER_DIR" log --oneline origin/tickets..tickets 2>/dev/null) || true
                if [ -z "$_local_ahead" ]; then
                    git -C "$TRACKER_DIR" reset --hard origin/tickets >/dev/null 2>&1 || true
                else
                    # Local is ahead — rebase to incorporate any origin-only commits (d88e-4365)
                    git -C "$TRACKER_DIR" rebase origin/tickets >/dev/null 2>&1 || \
                        git -C "$TRACKER_DIR" rebase --abort >/dev/null 2>&1 || true
                fi
            fi
        fi
        touch "$_sync_marker" 2>/dev/null || true
    fi
fi

# ---------------------------------------------------------------------------
# Retry configuration for worktree startup race conditions.
# When the tracker dir has entries but the reducer returns an empty index,
# retry after a short wait. This handles the case where the tracker symlink
# or filesystem isn't fully ready yet (common during worktree creation).
# ---------------------------------------------------------------------------
# not the total number of attempts. Total attempts = MAX_RETRIES + 1 (initial attempt + retries).
# The retry loop condition `attempt < MAX_RETRIES` is intentional: attempt starts at 0 and
# increments after each retry, so the loop runs at most MAX_RETRIES times (additional attempts).
MAX_RETRIES="${SPRINT_MAX_RETRIES:-3}"
RETRY_WAIT="${SPRINT_RETRY_WAIT:-1}"

# ---------------------------------------------------------------------------
# Build index from v3 reducer (with retry on transient failure).
# ---------------------------------------------------------------------------
export _SPRINT_TRACKER_DIR="$TRACKER_DIR"
export _SPRINT_REDUCER="$REDUCER"

_build_index() {
python3 -c "
import json, os, sys, importlib.util, collections

tracker_dir = os.environ['_SPRINT_TRACKER_DIR']
reducer_path = os.environ['_SPRINT_REDUCER']

# Load reducer module
spec = importlib.util.spec_from_file_location('ticket_reducer', reducer_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
reduce_ticket = mod.reduce_ticket

idx = {}
child_counts = collections.defaultdict(int)

for entry_name in os.listdir(tracker_dir):
    ticket_dir = os.path.join(tracker_dir, entry_name)
    if not os.path.isdir(ticket_dir) or entry_name.startswith('.'):
        continue
    try:
        state = reduce_ticket(ticket_dir)
    except Exception:
        continue
    if state is None:
        continue

    ticket_id = state.get('ticket_id', entry_name)
    status = state.get('status', 'open')
    ticket_type = state.get('ticket_type', 'task')
    title = state.get('title', '')
    priority = state.get('priority')
    parent_id = state.get('parent_id', '')
    tags = state.get('tags', [])
    alias = state.get('alias') or ''

    # Build deps: only 'depends_on' entries represent prerequisites of this ticket.
    # 'blocks' entries mean this ticket blocks the target — not that it is blocked.
    deps = [d.get('target_id', '') for d in state.get('deps', [])
            if d.get('relation') == 'depends_on']

    entry = {'title': title, 'status': status, 'type': ticket_type, 'tags': tags, 'alias': alias}
    if priority is not None:
        entry['priority'] = priority
    if deps:
        entry['deps'] = deps
    if parent_id:
        entry['parent'] = parent_id
    idx[ticket_id] = entry

    # Deleted tickets are terminal tombstones (STATUS(deleted)+ARCHIVED); they
    # must NOT count toward a parent's child_count (bug f871-9869-9775-4aa0).
    if parent_id and status != 'deleted':
        child_counts[parent_id] += 1

print(json.dumps({'index': idx, 'child_counts': dict(child_counts)}))
" 2>/dev/null || echo '{"index":{},"child_counts":{}}'
}

# Check for non-hidden subdirectories in tracker to detect "has entries but reducer failed".
# Uses find -L to follow symlinks — in worktrees, .tickets-tracker is a symlink to the
# main repo's tracker dir, and find without -L returns 0 entries through symlinks on macOS.
_tracker_has_entries() {
    local first_entry
    first_entry=$(find -L "$TRACKER_DIR" -mindepth 1 -maxdepth 1 -type d ! -name '.*' 2>/dev/null | head -1)
    [ -n "$first_entry" ]
}

# Build index with retry on transient failure
index_and_counts=$(_build_index)

attempt=0
while [ "$attempt" -lt "$MAX_RETRIES" ]; do
    # Check if the index is empty (no tickets resolved)
    index_key_count=$(echo "$index_and_counts" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('index',{})))" 2>/dev/null || echo "0")

    if [ "$index_key_count" -gt 0 ]; then
        break  # Index has entries — proceed normally
    fi

    # Index is empty — check if tracker dir has entries (indicating transient failure)
    if ! _tracker_has_entries; then
        break  # Tracker genuinely has no tickets — no point retrying
    fi

    # Tracker has entries but reducer returned empty — transient failure, retry
    attempt=$(( attempt + 1 ))
    sleep "$RETRY_WAIT"
    index_and_counts=$(_build_index)
done

# ---------------------------------------------------------------------------
# Single Python pass: pipe index_and_counts via stdin to avoid ARG_MAX.
# ---------------------------------------------------------------------------
echo "$index_and_counts" | \
SPRINT_SHOW_ALL="$show_all" \
SPRINT_MIN_CHILDREN="${min_children:-}" \
SPRINT_MAX_CHILDREN="${max_children:-}" \
SPRINT_MIN_CHILDREN_SET="${min_children+1}" \
SPRINT_MAX_CHILDREN_SET="${max_children+1}" \
SPRINT_HAS_TAG="${has_tag:-}" \
SPRINT_HAS_TAG_SET="${has_tag+1}" \
SPRINT_WITHOUT_TAG="${without_tag:-}" \
SPRINT_WITHOUT_TAG_SET="${without_tag+1}" \
python3 -c "
import json, os, sys

show_all = os.environ.get('SPRINT_SHOW_ALL') == 'true'

# Child-count filters — use _SET sentinel to distinguish \"0\" from \"unset\"
min_children = int(os.environ['SPRINT_MIN_CHILDREN']) if os.environ.get('SPRINT_MIN_CHILDREN_SET') == '1' else None
max_children = int(os.environ['SPRINT_MAX_CHILDREN']) if os.environ.get('SPRINT_MAX_CHILDREN_SET') == '1' else None

# Tag filter — use _SET sentinel to distinguish empty string from unset
has_tag = os.environ['SPRINT_HAS_TAG'] if os.environ.get('SPRINT_HAS_TAG_SET') == '1' else None

# Without-tag filter — use _SET sentinel to distinguish empty string from unset
without_tag = os.environ['SPRINT_WITHOUT_TAG'] if os.environ.get('SPRINT_WITHOUT_TAG_SET') == '1' else None

# Load index and child counts from stdin (avoids ARG_MAX for large ticket systems)
try:
    _data = json.load(sys.stdin)
    index = _data.get('index', {})
    child_counts = _data.get('child_counts', {})
except Exception:
    index = {}
    child_counts = {}

# Build lookup for dep status and parent resolution
dep_status = {tid: entry.get('status', 'open') for tid, entry in index.items()}
dep_parent = {tid: entry.get('parent', '') for tid, entry in index.items()}

p0_bugs = []
in_progress = []
open_unblocked = []
open_blocked = []

for tid, entry in index.items():
    # Collect open P0 bugs for top-of-output display
    if entry.get('type') == 'bug':
        status = entry.get('status', 'open')
        priority = entry.get('priority')
        if priority is None:
            priority = 4
        # Bug 5eb9-355b-fb39-4e0b: exclude archived/deleted (terminal
        # statuses) in addition to closed — otherwise list-epics shows
        # tombstoned bugs in the P0 banner.
        if status not in ('closed', 'archived', 'deleted') and priority == 0:
            p0_bugs.append({'id': tid, 'title': entry.get('title', '')})
        continue

    if entry.get('type') != 'epic':
        continue
    status = entry.get('status', 'open')
    # Bug 5eb9-355b-fb39-4e0b: archived/deleted are terminal statuses
    # equivalent to closed for list-epics. Including them produced rows
    # whose alias columns resolved to the tombstoned ticket, presenting
    # them as if they were live.
    if status in ('closed', 'archived', 'deleted'):
        continue

    deps = entry.get('deps', [])
    # An epic is blocked only by external deps — exclude deps that are its own children.
    # Preplanning may mistakenly add child story IDs to the epic's deps field (bug w21-3w8y).
    # Children are identified by having parent == this epic's ID.
    external_deps = [dep for dep in deps if dep_parent.get(dep, '') != tid]
    open_blockers = [dep for dep in external_deps if dep_status.get(dep, 'open') != 'closed']
    is_blocked = bool(open_blockers)

    priority = entry.get('priority', 4)
    if priority is None:
        priority = 4
    title = entry.get('title', '')
    tags = entry.get('tags', [])

    children = child_counts.get(tid, 0)

    alias = entry.get('alias', '')
    if status == 'in_progress':
        in_progress.append({'id': tid, 'priority': priority, 'title': title, 'children': children, 'tags': tags, 'alias': alias})
    elif is_blocked:
        open_blocked.append({'id': tid, 'priority': priority, 'title': title, 'children': children, 'blockers': open_blockers, 'tags': tags, 'alias': alias})
    else:
        open_unblocked.append({'id': tid, 'priority': priority, 'title': title, 'children': children, 'tags': tags, 'alias': alias})

# Sort each list by priority
in_progress.sort(key=lambda x: x['priority'])
open_unblocked.sort(key=lambda x: x['priority'])
open_blocked.sort(key=lambda x: x['priority'])

# Apply child-count filters (after classification, before output)
def _passes_child_filter(e):
    c = e['children']
    if min_children is not None and c < min_children:
        return False
    if max_children is not None and c > max_children:
        return False
    return True

if min_children is not None or max_children is not None:
    in_progress    = [e for e in in_progress    if _passes_child_filter(e)]
    open_unblocked = [e for e in open_unblocked if _passes_child_filter(e)]
    open_blocked   = [e for e in open_blocked   if _passes_child_filter(e)]

# Apply tag filter (after child-count filter, before output)
if has_tag is not None:
    def _passes_tag_filter(e):
        return has_tag in e.get('tags', [])
    in_progress    = [e for e in in_progress    if _passes_tag_filter(e)]
    open_unblocked = [e for e in open_unblocked if _passes_tag_filter(e)]
    open_blocked   = [e for e in open_blocked   if _passes_tag_filter(e)]

# Apply without-tag filter (after tag filter, before output)
if without_tag is not None:
    def _passes_without_tag_filter(e):
        return without_tag not in e.get('tags', [])
    in_progress    = [e for e in in_progress    if _passes_without_tag_filter(e)]
    open_unblocked = [e for e in open_unblocked if _passes_without_tag_filter(e)]
    open_blocked   = [e for e in open_blocked   if _passes_without_tag_filter(e)]

p0_bugs.sort(key=lambda x: x['id'])

_fmt = os.environ.get('_LE_FMT', 'text')

# Build set of IDs that are blocking other epics (needed by both formats).
blocking_ids = set()
for e in open_blocked:
    for blocker_id in e.get('blockers', []):
        # Only mark as BLOCKING if the blocker is itself an epic
        blocker_entry = index.get(blocker_id, {})
        if blocker_entry.get('type') == 'epic':
            blocking_ids.add(blocker_id)

# Exit code logic (shared by both formats):
#   0 — at least one unblocked epic (in-progress or ready)
#   2 — open epics exist but all are blocked
#   1 — no open epics at all
def _exit_code():
    if in_progress or open_unblocked:
        return 0
    if open_blocked:
        return 2
    return 1

if _fmt == 'json':
    def _epic(e, blocked):
        return {
            'id': e['id'],
            'alias': e.get('alias'),
            'priority': e['priority'],
            'title': e['title'],
            'children_count': e['children'],
            'blocking': e['id'] in blocking_ids,
            'blocked': blocked,
            'blockers': e.get('blockers', []),
        }
    epics = ([_epic(e, False) for e in in_progress]
             + [_epic(e, False) for e in open_unblocked]
             + [_epic(e, True) for e in open_blocked])
    print(json.dumps({'p0_bugs': p0_bugs, 'epics': epics}))
    sys.exit(_exit_code())

# ── text format (unchanged) ───────────────────────────────────────────────────
# Display P0 bugs above the epic list (if any exist) -- must come BEFORE the
# 'no open epics' early exit so P0 bugs are always visible.
if p0_bugs:
    print('P0 bugs requiring attention:')
    for bug in p0_bugs:
        print(f'  - [{bug[\"id\"]}] {bug[\"title\"]} (P0)')

if not in_progress and not open_unblocked and not open_blocked:
    print('No open epics found.', file=sys.stderr)
    sys.exit(1)

# In-progress epics first (P* signals already claimed work)
for e in in_progress:
    marker = '\tBLOCKING' if e['id'] in blocking_ids else ''
    display_id = e.get('alias') or e['id']
    print(f'{display_id}\tP*\t{e[\"title\"]}\t{e[\"children\"]}{marker}')

# Then unblocked open epics
for e in open_unblocked:
    marker = '\tBLOCKING' if e['id'] in blocking_ids else ''
    display_id = e.get('alias') or e['id']
    print(f'{display_id}\tP{e[\"priority\"]}\t{e[\"title\"]}\t{e[\"children\"]}{marker}')

# Blocked epics appended last when --all
if show_all:
    in_progress_ids = {e['id'] for e in in_progress}
    open_unblocked_ids = {e['id'] for e in open_unblocked}
    selectable_ids = in_progress_ids | open_unblocked_ids
    for e in open_blocked:
        if e['id'] not in selectable_ids:
            blocker_ids = ','.join(e['blockers'])
            display_id = e.get('alias') or e['id']
            print(f'BLOCKED\t{display_id}\tP{e[\"priority\"]}\t{e[\"title\"]}\t{e[\"children\"]}\t{blocker_ids}')

# Exit code logic:
#   0 — at least one unblocked epic (in-progress or ready)
#   2 — open epics exist but all are blocked
#   1 — no open epics at all
if in_progress or open_unblocked:
    sys.exit(0)
elif open_blocked:
    total = len(open_blocked)
    print(f'All {total} open epics are blocked.', file=sys.stderr)
    sys.exit(2)
else:
    sys.exit(1)
"
