#!/usr/bin/env bash
# ticket-transition.sh
# Transition a ticket's status with optimistic concurrency control and ghost prevention.
#
# Usage: ticket-transition.sh <ticket_id> <current_status> <target_status>
#   ticket_id: the ticket directory name (e.g., w21-ablv)
#   current_status: the status the caller believes the ticket is currently in
#   target_status: the status to transition to (open, in_progress, closed, blocked)
#
# Exits 0 on success or if current_status == target_status (no-op).
# Exits 1 on validation failure or ghost ticket; exits 10 on concurrency rejection.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=${_PLUGIN_ROOT}/scripts/ticket-lib.sh
source "$SCRIPT_DIR/ticket-lib.sh"

REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"
TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
REDUCER="$SCRIPT_DIR/ticket-reducer.py"

# ── Usage ─────────────────────────────────────────────────────────────────────
_usage() {
    echo "Usage: ticket transition <ticket_id> <current_status> <target_status> [--reason=<text>] [--force] [--verdict-hash=<hash>] [--force-close=<reason>]" >&2
    echo "       ticket transition <ticket_id> <target_status> [--reason=<text>] [--force] [--verdict-hash=<hash>] [--force-close=<reason>]  (auto-detects current status)" >&2
    echo "  current_status / target_status: open | in_progress | closed | blocked" >&2
    echo "  --reason=<text>          Required when closing bug tickets. Must start with 'Fixed:' or 'Escalated to user:'." >&2
    echo "  --force                  Skip the unresolved-children guard when closing. Non-closed children remain unresolved." >&2
    echo "  --verdict-hash=<hash>    Required when closing story/epic tickets. HMAC from compute-verdict-hash.sh." >&2
    echo "  --force-close=<reason>   Bypass verdict-hash requirement for story/epic (requires user approval via hook)." >&2
    echo "  Examples:" >&2
    echo "    ticket transition abc1 open closed --reason=\"Fixed: patched null check in foo.sh\"" >&2
    echo "    ticket transition abc1 closed --verdict-hash=abc123...  # close story with verified verdict" >&2
    echo "    ticket transition abc1 closed --force-close=\"verifier timed out\"  # bypass with reason" >&2
    exit 1
}

# ── Validate arguments ───────────────────────────────────────────────────────
if [ $# -lt 2 ]; then
    _usage
fi

ticket_id=$(TICKETS_TRACKER_DIR="$TRACKER_DIR" resolve_ticket_id "$1") || exit 1
if [ $# -eq 2 ]; then
    # 2-arg convenience form: auto-detect current status from the ticket
    current_status=$(ticket_read_status "$TRACKER_DIR" "$ticket_id" 2>/dev/null) || {
        echo "Error: could not read current status for ticket '$ticket_id'. Provide current_status explicitly." >&2
        _usage
    }
    target_status="$2"
    shift 2
else
    current_status="$2"
    target_status="$3"
    shift 3
fi

# Parse optional flags from remaining args
close_reason=""
force_close=false
verdict_hash=""
force_close_reason=""
while [ $# -gt 0 ]; do
    case "$1" in
        --reason=*)
            close_reason="${1#--reason=}"
            shift
            ;;
        --reason)
            if [ $# -lt 2 ]; then
                echo "Error: --reason requires a value" >&2
                exit 1
            fi
            close_reason="$2"
            shift 2
            ;;
        --force)
            force_close=true
            shift
            ;;
        --verdict-hash=*)
            verdict_hash="${1#--verdict-hash=}"
            shift
            ;;
        --verdict-hash)
            if [ $# -lt 2 ]; then
                echo "Error: --verdict-hash requires a value" >&2
                exit 1
            fi
            verdict_hash="$2"
            shift 2
            ;;
        --force-close=*)
            force_close_reason="${1#--force-close=}"
            shift
            ;;
        --force-close)
            if [ $# -lt 2 ]; then
                echo "Error: --force-close requires a reason" >&2
                exit 1
            fi
            force_close_reason="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

# Validate statuses are in the allowed set
_validate_status() {
    local label="$1"
    local value="$2"
    case "$value" in
        open|in_progress|closed|blocked) ;;
        --reason*|--*)
            echo "Error: invalid ${label} '${value}'. Options like --reason must come AFTER <target_status>." >&2
            echo "  Correct: ticket transition <id> [<current_status>] <target_status> --reason=\"<text>\"" >&2
            exit 1
            ;;
        *)
            echo "Error: invalid ${label} '${value}'. Must be one of: open, in_progress, closed, blocked" >&2
            exit 1
            ;;
    esac
}

_validate_status "current_status" "$current_status"

# Blocked transition targets
if [ "$target_status" = "deleted" ]; then
    echo "Error: deleted is not a valid transition target -- use ticket delete $ticket_id to delete a ticket" >&2
    exit 1
fi

_validate_status "target_status" "$target_status"

# ── Idempotent no-op ─────────────────────────────────────────────────────────
if [ "$current_status" = "$target_status" ]; then
    echo "No transition needed"
    exit 0
fi

# ── Step 1: Ghost check (before acquiring flock) ─────────────────────────────
if [ ! -d "$TRACKER_DIR/$ticket_id" ]; then
    echo "Error: ticket '$ticket_id' does not exist" >&2
    exit 1
fi

if ! find "$TRACKER_DIR/$ticket_id" -maxdepth 1 \( -name '*-CREATE.json' -o -name '*-SNAPSHOT.json' \) ! -name '.*' 2>/dev/null | grep -q .; then
    echo "Error: ticket $ticket_id has no CREATE or SNAPSHOT event" >&2
    exit 1
fi

# ── Validate ticket system is initialized ────────────────────────────────────
if [ ! -f "$TRACKER_DIR/.env-id" ]; then
    echo "Error: ticket system not initialized. Run 'ticket init' first." >&2
    exit 1
fi

# ── Step 1b: Open-children guard (before flock — read-only check) ────────────
# window (a child ticket being created after this check but before the STATUS
# event is committed inside the flock) is an acceptable trade-off: the worst case
# is that a close succeeds while a sibling create is racing — which is already
# possible through direct event writes. The flock serializes STATUS event writes,
# not reads. Tightening this would require a separate lock on child creation, which
# adds complexity disproportionate to the risk.
#
# The open-children check runs via ticket-reducer.py directly (mandatory, fail-loud).
# batch_close_json is captured separately via the unblock script (non-critical, || true)
# and reused in Step 4 for unblock detection only — NOT for open-children detection.
batch_close_json=""
if [ "$target_status" = "closed" ]; then
    # ── Open-children guard: mandatory check via reducer (must NOT use || true) ──
    # Use ticket-reducer.py directly to find open children. This path is independent
    # of ticket-unblock.py so a broken/absent unblock script cannot mask children.
    open_children_check_exit=0
    open_children=$(python3 - "$TRACKER_DIR" "$ticket_id" "$REDUCER" <<'PYEOF' 2>&1
import glob, json, os, subprocess, sys

tracker_dir = sys.argv[1]
ticket_id = sys.argv[2]
reducer_path = sys.argv[3]

CLOSED_STATUSES = frozenset(('closed', 'done', 'resolved', 'cancelled', 'wont_fix', 'archived', 'deleted'))

def read_state_from_snapshot(ticket_dir):
    """Read compiled state from the newest *-SNAPSHOT.json event file. Returns dict or None."""
    snapshots = sorted(glob.glob(os.path.join(ticket_dir, '*-SNAPSHOT.json')))
    if not snapshots:
        return None
    try:
        with open(snapshots[-1]) as f:
            event = json.load(f)
        return event.get('data', {}).get('compiled_state')
    except Exception:
        return None

_UNKNOWN = object()  # Sentinel for corrupt/missing CREATE events (shared across functions)

def effective_parent_id(ticket_dir):
    """Current parent_id from the FULL event history, not just CREATE.

    The membership pre-filter must honor reparents made via `edit --parent`
    (an EDIT event) and `edit --parent=null` (detach). Reading parent_id from the
    CREATE event alone made reparented children invisible to the open-children
    guard — an epic could be closed while a child reparented onto it (via edit)
    was still open/in_progress, because that child's CREATE event recorded no
    parent. (Compacted tickets fold pre-SNAPSHOT events into compiled_state and
    retire the originals as *.retired, which the *.json glob excludes; present
    EDITs are therefore always post-baseline, so last-by-timestamp wins.)

    Pure file reads (no reducer subprocess), so this stays cheap at scale — the
    expensive reducer is still only invoked for confirmed children (Pass 2).

    Returns the parent_id string, '' (detached / no parent), or the sentinel
    _UNKNOWN if no readable CREATE/SNAPSHOT baseline exists.
    """
    setters = []          # (filename, parent_value)
    have_baseline = False
    for path in glob.glob(os.path.join(ticket_dir, '*.json')):
        name = os.path.basename(path)
        if name.startswith('.'):
            continue
        try:
            with open(path) as f:
                event = json.load(f)
        except Exception:
            continue
        data = event.get('data', {}) or {}
        if name.endswith('-CREATE.json'):
            setters.append((name, data.get('parent_id')))
            have_baseline = True
        elif name.endswith('-SNAPSHOT.json'):
            compiled = data.get('compiled_state') or {}
            setters.append((name, compiled.get('parent_id')))
            have_baseline = True
        elif name.endswith('-EDIT.json'):
            fields = data.get('fields', {}) or {}
            if 'parent_id' in fields:
                setters.append((name, fields.get('parent_id')))
    if not have_baseline or not setters:
        return _UNKNOWN
    # filename == ${timestamp_ns}-${uuid}-${TYPE}.json, so lexical == chronological
    setters.sort(key=lambda t: t[0])
    value = setters[-1][1]
    return value if value else ''

def read_state_via_reducer(ticket_dir):
    """Fallback: invoke reducer subprocess for tickets without a SNAPSHOT.

    Only called for tickets confirmed (via snapshot or CREATE) to have
    parent_id == ticket_id — keeps invocations O(children_count).
    """
    try:
        result = subprocess.run(
            ['python3', reducer_path, ticket_dir],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception:
        return None

# Scan all ticket directories for open children.
#
# Performance fix (babe-ff38): O(N) -> O(children_count)
# Previous approach: invoke reducer subprocess for ALL tickets lacking a SNAPSHOT
# (13,526 of 16,636 in production — ~473s total at ~35ms each).
#
# New approach (two-pass, targeted):
#   Pass 1 (fast): for each ticket dir, read parent_id from SNAPSHOT compiled_state
#     (no subprocess) or from CREATE event (also no subprocess). Skip immediately if
#     parent_id != ticket_id.
#   Pass 2 (targeted): only invoke reducer subprocess for confirmed children to get
#     authoritative current status (handles STATUS events after the last SNAPSHOT).
#
# This bounds reducer invocations to O(children_count) — typically 0-10 for leaf
# nodes — regardless of total ticket count.
open_children = []
try:
    for entry in os.scandir(tracker_dir):
        if not entry.is_dir():
            continue
        tid = entry.name

        # Pass 1 (fast, no subprocess): determine the ticket's CURRENT parent_id
        # from its full event history (CREATE + EDITs + SNAPSHOT). Skip immediately
        # unless this ticket is currently parented to the ticket being closed.
        eff_parent = effective_parent_id(entry.path)
        if eff_parent is _UNKNOWN or eff_parent != ticket_id:
            continue

        # Pass 2 (targeted): confirmed current child — run the reducer for its
        # authoritative status (accounts for STATUS events after any SNAPSHOT),
        # falling back to the SNAPSHOT compiled state if the reducer is unavailable.
        state = read_state_via_reducer(entry.path)
        if state is None:
            state = read_state_from_snapshot(entry.path)
        if state is None:
            continue
        # Tombstone-aware: .tombstone.json is written by ticket delete and carries
        # the terminal status; the reducer does not read it.
        tombstone_path = os.path.join(entry.path, '.tombstone.json')
        if os.path.isfile(tombstone_path):
            try:
                with open(tombstone_path) as _tf:
                    _ts = json.load(_tf)
                eff_status = str(_ts.get('status', 'deleted'))
            except Exception:
                eff_status = 'deleted'
        else:
            eff_status = state.get('status', 'open')
        if eff_status not in CLOSED_STATUSES:
            open_children.append(tid)
except Exception as e:
    print(f'Error: open-children scan failed: {e}', file=sys.stderr)
    sys.exit(2)

if open_children:
    print('\n'.join(open_children))
PYEOF
) || open_children_check_exit=$?

    if [ "$open_children_check_exit" -ne 0 ]; then
        echo "Error: open-children check failed — cannot safely close ticket '$ticket_id'" >&2
        echo "$open_children" >&2
        exit 1
    fi

    if [ -n "$open_children" ]; then
        open_children_count=$(echo "$open_children" | wc -l | tr -d ' ')
        if [ "$force_close" = "true" ]; then
            echo "Warning: closing ticket '$ticket_id' with ${open_children_count} unresolved (non-closed) child ticket(s) (--force)." >&2
            echo "The following children are not yet closed:" >&2
            echo "$open_children" >&2
        else
            echo "Error: cannot close ticket '$ticket_id' while it has ${open_children_count} unresolved (non-closed) child ticket(s)." >&2
            echo "Close the following children first, or use --force to close the parent with children unresolved:" >&2
            echo "$open_children" >&2
            exit 1
        fi
    fi

    # ── Unblock detection: run ticket-unblock.py for newly_unblocked computation ─
    # This is non-critical — if the unblock script fails, the close still proceeds
    # (children were already verified absent above). batch_close_json is only used
    # in Step 4 for UNBLOCKED output, not for open-children detection.
    unblock_script="${DSO_UNBLOCK_SCRIPT:-$SCRIPT_DIR/ticket-unblock.py}"
    batch_close_json=$(python3 "$unblock_script" --batch-close "$TRACKER_DIR" "$ticket_id" 2>/dev/null) || true
fi

# ── Step 2-3: Acquire flock, read-verify-write inside lock ───────────────────
# All concurrency-critical operations (read current state, verify, build event,
# write event) happen inside a single flock to prevent TOCTOU races.
env_id=$(cat "$TRACKER_DIR/.env-id")
author=$(git config user.name 2>/dev/null || echo "Unknown")
lock_file="$TRACKER_DIR/.ticket-write.lock"

# The entire read-verify-write is done inside python3 holding fcntl.flock.
# If concurrency check fails, python exits 10 (propagated as exit 10).
# If lock timeout, python exits 1.
flock_exit=0
# The critical section lives in ticket_txn.py (extracted from this heredoc in
# WS2). It remains the lock-holding, committing entrypoint: one python3
# process holds fcntl.flock while it re-reads+verifies, writes, and commits
# (exit 10 on optimistic-concurrency mismatch). Positional argv is unchanged.
python3 "$SCRIPT_DIR/ticket_txn.py" transition \
    "$lock_file" "$TRACKER_DIR" "$ticket_id" "$current_status" "$target_status" \
    "$env_id" "$author" "$REDUCER" "$close_reason" "$verdict_hash" "$force_close_reason" \
    || flock_exit=$?

if [ "$flock_exit" -eq 10 ]; then
    # Optimistic concurrency rejection — preserve exit 10 so callers (the rebar
    # library) can distinguish it from generic validation/ghost failures (exit 1).
    exit 10
elif [ "$flock_exit" -ne 0 ]; then
    exit 1
fi

# ── Step 3b: Write force-close audit comment (if applicable) ─────────────────
if [ "$target_status" = "closed" ] && [ -n "$force_close_reason" ]; then
    _FC_COMMENT="FORCE_CLOSE: verdict hash bypassed by user approval. Reason: \"$force_close_reason\". Session: ${SESSION_ID:-$(git rev-parse --short HEAD 2>/dev/null || echo unknown)}."
    bash "$SCRIPT_DIR/ticket-comment.sh" "$ticket_id" "$_FC_COMMENT" 2>/dev/null || true
fi

# ── Step 4: Detect newly unblocked tickets (only on close) ───────────────────
if [ "$target_status" = "closed" ]; then
    # Use the batch_close_json captured in Step 1b (already computed open_children
    # and newly_unblocked in a single Python process — no second spawn needed).
    if [ -n "$batch_close_json" ]; then
        unblocked_ids=$(echo "$batch_close_json" | python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
ids = d.get('newly_unblocked', [])
print(','.join(ids)) if ids else None
" 2>/dev/null) || unblocked_ids=""

        if [ -n "$unblocked_ids" ]; then
            echo "UNBLOCKED: $unblocked_ids"
        else
            echo "UNBLOCKED: none"
        fi
    else
        # batch_close_json was empty (e.g., unblock script failed) — warn but don't fail
        echo "Warning: batch-close JSON unavailable; unblock detection skipped" >&2
        echo "UNBLOCKED: none"
    fi

    # Compact-on-close: squash event log into SNAPSHOT (non-blocking)
    compact_script="${DSO_COMPACT_SCRIPT:-$SCRIPT_DIR/ticket-compact.sh}"
    bash "$compact_script" "$ticket_id" --threshold=0 --skip-sync 2>/dev/null || true

    # Scratch cleanup: remove per-ticket scratch dir (non-blocking; always returns 0)
    _scratch_cleanup_for_ticket "$ticket_id" 2>/dev/null || true
fi

exit 0
