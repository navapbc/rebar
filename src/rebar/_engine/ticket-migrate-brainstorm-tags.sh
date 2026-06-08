#!/usr/bin/env bash
# ticket-migrate-brainstorm-tags.sh
# One-time migration: tag epics that have a "### Planning Intelligence Log"
# heading with brainstorm:complete, and remove scrutiny:pending if present.
#
# Usage:
#   ticket-migrate-brainstorm-tags.sh [--target <host-project-root>] [--dry-run]
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
# synthetic test marker used in tests (the actual plugin sentinel is at
# .claude-plugin/marketplace.json inside the plugin root directory). Three layers
# protect against accidental execution in the plugin source repo:
#   1. update-artifacts.sh (the primary caller) gates the entire Phase 5 migration
#      block on [[ -z "$_DRYRUN" ]], so --dryrun invocations never reach this script.
#      That fix removes the compound risk that triggered the critical upgrade.
#   2. The _TRACKER_DIR check below (lines ~66-71) is the actual production guard for
#      standalone invocations: the plugin source repo has no .tickets-tracker/ directory,
#      so the script exits with error code 1 before any ticket state is mutated.
#   3. This plugin.json guard is belt-and-suspenders for test environments that inject
#      a plugin.json marker to simulate the plugin source repo. Even if both upper layers
#      somehow fail, the migration is idempotent via the marker file.
if [ -f "$_TARGET/plugin.json" ]; then
    echo "NOTICE: target '$_TARGET' is the plugin source repo — skipping migration (no changes made)" >&2
    exit 0
fi

# ── Ticket tracker location ───────────────────────────────────────────────────
_TRACKER_DIR="$_TARGET/.tickets-tracker"

# ── Marker check (idempotency) ────────────────────────────────────────────────
# PRIMARY marker: stored in the shared tickets tracker branch (.migrations/)
# so it persists across all worktrees (the old per-worktree path was gitignored
# and absent in fresh worktrees, causing re-migration bug 01b9-3359-7df3-45cf).
_MARKER_FILE="$_TRACKER_DIR/.migrations/brainstorm-tag-migration-v2"
# COMPAT marker: honor the old per-worktree path as a secondary "already done"
# signal so existing worktrees that have the old marker skip cleanly (one
# no-op re-run on already-migrated stores at most, since the new shared marker
# is written on the first run after the upgrade).
_MARKER_FILE_COMPAT="$_TARGET/.claude/.brainstorm-tag-migration-v2"
if [ -f "$_MARKER_FILE" ] || [ -f "$_MARKER_FILE_COMPAT" ]; then
    exit 0
fi

if [ ! -d "$_TRACKER_DIR" ]; then
    echo "Error: ticket tracker not found at '$_TRACKER_DIR'" >&2
    exit 1
fi

# ── Single-pass migration ─────────────────────────────────────────────────────
# One python invocation scans every ticket, writes EDIT events for eligible epics,
# and emits machine-readable lines on stdout. Bash then commits the written events
# per-ticket. Per-ticket python subprocesses are avoided so the first-run scan
# scales linearly with ticket count rather than (ticket_count × python startup) —
# critical for large trackers (~15k tickets) where the old pattern took ~30 min
# and made skill-entry invocation unusable.
#
# stdout contract:
#   WROTE:<ticket_id>:<event_filename>   — EDIT event written; bash must git add+commit
#   WOULD_WRITE:<ticket_id>              — dry-run: would write EDIT event (no file created)
#   UNMATCHED:<ticket_id>                — epic with no brainstorm:complete tag and no PIL
_migrate_output=$(python3 - "$_TRACKER_DIR" "$_DRYRUN" <<'PYEOF'
import json, os, re, sys, time, uuid

TRACKER = sys.argv[1]
DRYRUN = len(sys.argv) > 2 and sys.argv[2] == "1"
PIL = "### Planning Intelligence Log"

# Mandatory field markers — every canonical PIL written by
# epic-scrutiny-pipeline.md populates these three. Absence of any one
# of them indicates a stub PIL that bypassed the pipeline (a307-0f58).
REQUIRED_PIL_FIELDS = (
    "**Web research (Step 2.6)**:",
    "**Scenario analysis (Step 2.75)**:",
    "**LLM-instruction signal (Step 5)**:",
)

def _pil_body_passes(text):
    """True iff text contains a PIL heading whose body has all required fields."""
    if not text or PIL not in text:
        return False
    idx = text.find(PIL)
    rest = text[idx + len(PIL):]
    end = re.search(r'(?m)^#{1,3} ', rest)
    section = rest if end is None else rest[:end.start()]
    for field in REQUIRED_PIL_FIELDS:
        if field not in section:
            return False
    scenario = re.search(
        r'\*\*Scenario analysis \(Step 2\.75\)\*\*:\s*([^\n]+)', section)
    if scenario and 'triggered' in scenario.group(1) \
       and 'not triggered' not in scenario.group(1) \
       and 'skipped' not in scenario.group(1):
        if 'Red Team' not in section or 'Blue Team' not in section:
            return False
    return True

def scan_ticket(tdir):
    """Return (is_epic, latest_tags, pil_found) by reading every event file once."""
    is_epic = False
    tags = []
    pil = False
    try:
        files = sorted(f for f in os.listdir(tdir)
                       if f.endswith('.json') and not f.startswith('.'))
    except OSError:
        return (False, [], False)
    for fn in files:
        try:
            with open(os.path.join(tdir, fn)) as f:
                ev = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        etype = ev.get('event_type', '')
        data = ev.get('data') or {}
        # Ticket type: CREATE carries data.ticket_type; SNAPSHOT carries
        # data.compiled_state.ticket_type (compacted tickets may be SNAPSHOT-only
        # with no CREATE event surviving).
        if data.get('ticket_type') == 'epic':
            is_epic = True
        if etype == 'SNAPSHOT':
            cs = data.get('compiled_state') or {}
            if cs.get('ticket_type') == 'epic':
                is_epic = True
            # Tags snapshotted under compiled_state.tags
            snap_tags = cs.get('tags', None)
            if snap_tags is not None:
                if isinstance(snap_tags, list):
                    tags = snap_tags
                elif isinstance(snap_tags, str) and snap_tags:
                    tags = snap_tags.split(',')
                else:
                    tags = []
        # Latest tags from raw events (CREATE: data.tags; EDIT: data.fields.tags).
        # Processed in timestamp order so a later EDIT overrides a SNAPSHOT's tags.
        if etype == 'EDIT':
            raw = (data.get('fields') or {}).get('tags', None)
        elif etype != 'SNAPSHOT':
            raw = data.get('tags', None)
        else:
            raw = None
        if raw is not None:
            if isinstance(raw, list):
                tags = raw
            elif isinstance(raw, str) and raw:
                tags = raw.split(',')
            else:
                tags = []
        # PIL detection: heading present AND mandatory pipeline fields populated
        # in the same body. A stub PIL (heading only) is rejected (a307-0f58).
        if _pil_body_passes(data.get('description') or ''):
            pil = True
        if _pil_body_passes(data.get('body') or ''):
            pil = True
        if etype == 'EDIT':
            fields = data.get('fields') or {}
            if _pil_body_passes(fields.get('description') or ''):
                pil = True
        if etype == 'SNAPSHOT':
            cs = data.get('compiled_state') or {}
            if _pil_body_passes(cs.get('description') or ''):
                pil = True
            for c in (cs.get('comments') or []):
                if isinstance(c, dict) and _pil_body_passes(c.get('body') or ''):
                    pil = True
    return (is_epic, tags, pil)

def write_edit_event(tdir, new_tags):
    ts = time.time_ns()
    u = str(uuid.uuid4())
    fname = f"{ts}-{u}-EDIT.json"
    fpath = os.path.join(tdir, fname)
    event = {
        "timestamp": ts,
        "uuid": u,
        "event_type": "EDIT",
        "env_id": "00000000-0000-4000-8000-migration001",
        "author": "ticket-migrate-brainstorm-tags",
        "data": {"fields": {"tags": new_tags}},
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
    is_epic, tags, pil = scan_ticket(tdir)
    if not is_epic:
        continue
    if 'brainstorm:complete' in tags:
        continue
    if not pil:
        print(f'UNMATCHED:{tid}')
        continue
    new_tags = [t for t in tags if t and t != 'scrutiny:pending']
    if 'brainstorm:complete' not in new_tags:
        new_tags.append('brainstorm:complete')
    if DRYRUN:
        removed = [t for t in tags if t == 'scrutiny:pending']
        note = f" (removes: {removed})" if removed else ""
        print(f'WOULD_WRITE:{tid}{note}')
    else:
        try:
            fname = write_edit_event(tdir, new_tags)
            print(f'WROTE:{tid}:{fname}')
        except OSError as e:
            print(f'ERROR:{tid}:{e}', file=sys.stderr)
PYEOF
)

# ── Commit each written EDIT event ────────────────────────────────────────────
# are intentional for the same four reasons as before the refactor:
#   (a) one-time sequential migration — no parallelism within the loop;
#   (b) _flock_stage_commit exists to serialize concurrent multi-agent writers;
#   (c) update-artifacts.sh's Phase 5 and the skill-entry migration calls are
#       interactive, not concurrent with other ticket operations in the same session;
#   (d) ticket-edit.sh uses `git config user.name` and the session env_id — it cannot
#       emit the migration-specific author ("ticket-migrate-brainstorm-tags") and
#       env_id ("00000000-0000-4000-8000-migration001") needed for auditability.
#
# Failure containment: the single-pass python scan emits `WROTE:` only after the
# EDIT event file has been successfully written; on OSError it emits `ERROR:` to
# stderr and skips to the next ticket. The bash loop only commits `WROTE:` lines,
# so a partial-write failure on one ticket cannot cause a mis-committed EDIT for
# that ticket (subsumes main's 3cb1-429e guard for the old per-ticket python3 call).
while IFS= read -r _line; do
    [[ -z "$_line" ]] && continue
    case "$_line" in
        WROTE:*)
            _rest="${_line#WROTE:}"
            _ticket_id="${_rest%%:*}"
            _event_name="${_rest#*:}"
            git -C "$_TRACKER_DIR" add "$_ticket_id/$_event_name" 2>/dev/null && \
                git -C "$_TRACKER_DIR" commit -m "migration: add brainstorm:complete tag to $_ticket_id" 2>/dev/null || \
                git -C "$_TRACKER_DIR" reset 2>/dev/null || true
            ;;
        WOULD_WRITE:*)
            echo "DRY-RUN: ${_line#WOULD_WRITE:}"
            ;;
        UNMATCHED:*)
            echo "UNMATCHED: ${_line#UNMATCHED:}"
            ;;
    esac
done <<< "$_migrate_output"

# ── Write shared marker file and commit to tracker branch (skipped in dry-run) ─
# Writing to the tickets tracker branch (not .claude/, which is gitignored)
# ensures the marker persists across all worktrees (bug 01b9-3359-7df3-45cf fix).
if [ "$_DRYRUN" = "0" ]; then
    mkdir -p "$(dirname "$_MARKER_FILE")"
    touch "$_MARKER_FILE"
    git -C "$_TRACKER_DIR" add ".migrations/brainstorm-tag-migration-v2" 2>/dev/null && \
        git -C "$_TRACKER_DIR" commit -m "migration: write brainstorm-tag-migration-v2 marker" 2>/dev/null || \
        git -C "$_TRACKER_DIR" reset 2>/dev/null || true
fi

exit 0
