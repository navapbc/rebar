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
    echo "  --force                  Skip the open-children guard when closing. Open children remain open." >&2
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

def read_parent_id_from_create(ticket_dir):
    """Read parent_id from the CREATE event without running the full reducer.

    Returns the parent_id string (may be None/empty) or the module-level sentinel
    _UNKNOWN if the CREATE event cannot be read (corrupt/missing).
    """
    creates = sorted(glob.glob(os.path.join(ticket_dir, '*-CREATE.json')))
    if not creates:
        return _UNKNOWN
    try:
        with open(creates[-1]) as f:
            event = json.load(f)
        return event.get('data', {}).get('parent_id')
    except Exception:
        return _UNKNOWN

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

        # Pass 1a: try SNAPSHOT (fastest — compiled state, no subprocess)
        snapshot_state = read_state_from_snapshot(entry.path)
        if snapshot_state is not None:
            # Snapshot gives us parent_id without a subprocess.
            # If parent_id doesn't match, skip immediately.
            if snapshot_state.get('parent_id') != ticket_id:
                continue
            # Parent matches — check status from snapshot.
            # Still need reducer to account for STATUS events after the snapshot.
            state = read_state_via_reducer(entry.path)
            if state is None:
                state = snapshot_state  # fall back to snapshot status
        else:
            # Pass 1b: no SNAPSHOT — read parent_id from CREATE event (no subprocess).
            parent_id_from_create = read_parent_id_from_create(entry.path)
            if parent_id_from_create is _UNKNOWN:
                # Cannot determine parent without reducer; skip for safety.
                # (Corrupt/missing CREATE — not a valid child anyway.)
                continue
            if parent_id_from_create != ticket_id:
                # CREATE event confirms this is not a child — skip without reducer.
                continue
            # Confirmed child via CREATE event — run reducer for current status.
            state = read_state_via_reducer(entry.path)

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
            echo "Warning: closing ticket '$ticket_id' with ${open_children_count} open child ticket(s) (--force)." >&2
            echo "The following children remain open:" >&2
            echo "$open_children" >&2
        else
            echo "Error: cannot close ticket '$ticket_id' while it has ${open_children_count} open child ticket(s)." >&2
            echo "Close the following children first, or use --force to close the parent with children remaining open:" >&2
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
python3 -c "
import fcntl, json, os, subprocess, sys, time, uuid

lock_path = sys.argv[1]
tracker_dir = sys.argv[2]
ticket_id = sys.argv[3]
current_status = sys.argv[4]
target_status = sys.argv[5]
env_id_val = sys.argv[6]
author_val = sys.argv[7]
reducer_path = sys.argv[8]
close_reason = sys.argv[9] if len(sys.argv) > 9 else ''
verdict_hash_arg = sys.argv[10] if len(sys.argv) > 10 else ''
force_close_reason_arg = sys.argv[11] if len(sys.argv) > 11 else ''

# Import reduce_ticket directly (single-process: eliminates subprocess for state read)
sys.path.insert(0, os.path.dirname(os.path.abspath(reducer_path)))
from ticket_reducer import reduce_ticket

timeout = 30

# Acquire flock
fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
deadline = time.monotonic() + timeout
acquired = False
while time.monotonic() < deadline:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        break
    except (IOError, OSError):
        time.sleep(0.1)
if not acquired:
    os.close(fd)
    print('Error: could not acquire lock', file=sys.stderr)
    sys.exit(1)

# Lock acquired — read current state via direct reduce_ticket import (no subprocess)
try:
    state = reduce_ticket(os.path.join(tracker_dir, ticket_id))
    if state is None:
        print('Error: reducer returned no state (ticket may be corrupt or missing events)', file=sys.stderr)
        os.close(fd)
        sys.exit(1)

    actual_status = state.get('status', '')

    # Optimistic concurrency check
    if actual_status != current_status:
        print(f'Error: current status is \"{actual_status}\", not \"{current_status}\". Re-run: ticket transition {ticket_id} {actual_status} {target_status}', file=sys.stderr)
        os.close(fd)
        sys.exit(10)

    # Bug-close-reason guard
    if target_status == 'closed':
        ticket_type = state.get('ticket_type', '')
        # If ticket_type is empty (old tickets predating the type field), treat as
        # non-bug: don't require --reason. This ensures backward compatibility.
        if ticket_type == 'bug':
            if not close_reason:
                print('Error: closing a bug ticket requires --reason with prefix \"Fixed:\" or \"Escalated to user:\"', file=sys.stderr)
                os.close(fd)
                sys.exit(1)
            # Validate required prefix: accept Fixed (covers Fixed:, Fixed in, etc.)
            # and case-insensitive escalat prefix (covers Escalated to user: variants).
            if not (close_reason.startswith('Fixed') or close_reason.lower().startswith('escalat')):
                print('Error: --reason must start with \"Fixed:\" or \"Escalated to user:\"', file=sys.stderr)
                os.close(fd)
                sys.exit(1)

    # ── Verdict hash gate (story/epic closure) ────────────────────────────
    # Stories and epics require a verified completion verdict to close.
    # The verdict hash is an HMAC that encodes: this ticket received PASS at this git state.
    # compute-verdict-hash.sh and this gate compute the same HMAC independently.
    if target_status == 'closed' and ticket_type in ('story', 'epic'):
        # Check config: verify.require_verdict_for_close (default: true)
        require_verdict = True
        try:
            _cfg_root = os.environ.get('REBAR_ROOT') or os.environ.get('PROJECT_ROOT') or tracker_dir.rsplit('/', 1)[0]
            config_path = os.environ.get('REBAR_CONFIG') or os.path.join(_cfg_root, '.rebar', 'config.conf')
            if os.path.isfile(config_path):
                with open(config_path) as _cf:
                    for _line in _cf:
                        if _line.strip().startswith('verify.require_verdict_for_close='):
                            val = _line.strip().split('=', 1)[1].strip().lower()
                            if val in ('false', '0', 'no'):
                                require_verdict = False
        except Exception:
            pass

        if require_verdict:
            if force_close_reason_arg:
                # Force-close with reason — write audit comment
                print(f'Warning: closing {ticket_type} {ticket_id} via --force-close (verdict hash bypassed)', file=sys.stderr)
                print(f'  Reason: {force_close_reason_arg}', file=sys.stderr)
                # The STATUS event data will include the force_close_reason for audit
            elif verdict_hash_arg:
                # Verify the hash by independently computing the expected HMAC
                import hmac, hashlib
                key_file = os.path.join(tracker_dir, '.closure-key')
                if not os.path.isfile(key_file):
                    print(f'Error: .closure-key not found. Run ticket init to generate it.', file=sys.stderr)
                    os.close(fd)
                    sys.exit(1)
                with open(key_file, 'r') as _kf:
                    key = _kf.read().strip().encode()
                try:
                    head_sha = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=5).stdout.strip()
                except Exception:
                    head_sha = 'unknown'
                expected_data = f'{ticket_id}|PASS|{head_sha}'.encode()
                expected_hash = hmac.new(key, expected_data, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(verdict_hash_arg, expected_hash):
                    print(f'Error: verdict hash mismatch for {ticket_type} {ticket_id}.', file=sys.stderr)
                    print(f'  This means the completion verifier did not produce a PASS verdict at the current HEAD.', file=sys.stderr)
                    print(f'  Recovery: dispatch dso:completion-verifier, then run compute-verdict-hash.sh.', file=sys.stderr)
                    print(f'  Override: use --force-close=\"<reason>\" to bypass (requires user approval).', file=sys.stderr)
                    os.close(fd)
                    sys.exit(1)
            else:
                print(f'Error: closing a {ticket_type} requires --verdict-hash (from compute-verdict-hash.sh after completion verifier PASS).', file=sys.stderr)
                print(f'  Recovery: dispatch dso:completion-verifier, then:', file=sys.stderr)
                print(f'    bash compute-verdict-hash.sh {ticket_id} PASS  # produces the hash', file=sys.stderr)
                print(f'    ticket transition {ticket_id} closed --verdict-hash=<hash-from-above>', file=sys.stderr)
                print(f'  Override: use --force-close=\"<reason>\" to bypass (requires user approval).', file=sys.stderr)
                os.close(fd)
                sys.exit(1)

    # Compute parent_status_uuid: UUID of the most recent prior STATUS event for this ticket,
    # or null if this is the first STATUS event.
    # Sort STATUS event files by filename (timestamp prefix ensures chronological order).
    ticket_dir_path = os.path.join(tracker_dir, ticket_id)
    parent_status_uuid = None
    try:
        status_files = sorted(
            f for f in os.listdir(ticket_dir_path)
            if f.endswith('-STATUS.json') and not f.startswith('.')
        )
        if status_files:
            most_recent = os.path.join(ticket_dir_path, status_files[-1])
            with open(most_recent, encoding='utf-8') as _sf:
                _prev = json.load(_sf)
            parent_status_uuid = _prev.get('uuid') or None
    except Exception:
        parent_status_uuid = None

    # Build STATUS event JSON
    timestamp = time.time_ns()
    event_uuid = str(uuid.uuid4())
    event = {
        'timestamp': timestamp,
        'uuid': event_uuid,
        'event_type': 'STATUS',
        'env_id': env_id_val,
        'author': author_val,
        'parent_status_uuid': parent_status_uuid,
        'data': {
            'status': target_status,
            'current_status': current_status,
            'parent_status_uuid': parent_status_uuid,
        },
    }

    # Write to temp file
    temp_path = os.path.join(tracker_dir, f'.tmp-transition-{event_uuid}')
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(event, f, ensure_ascii=False)

    # Compute final filename and path
    final_filename = f'{timestamp}-{event_uuid}-STATUS.json'
    ticket_dir = os.path.join(tracker_dir, ticket_id)
    final_path = os.path.join(ticket_dir, final_filename)

    # Atomic rename
    os.rename(temp_path, final_path)

    # Ensure gc.auto=0
    subprocess.run(
        ['git', '-C', tracker_dir, 'config', 'gc.auto', '0'],
        check=True, capture_output=True, text=True,
    )

    # git add + commit
    subprocess.run(
        ['git', '-C', tracker_dir, 'add', f'{ticket_id}/{final_filename}'],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ['git', '-C', tracker_dir, 'commit', '-q', '--no-verify', '-m', f'ticket: STATUS {ticket_id}'],
        check=True, capture_output=True, text=True,
    )

except subprocess.CalledProcessError as e:
    print(f'Error: git operation failed: {e.stderr}', file=sys.stderr)
    # Clean up event file if it was written
    try:
        os.remove(final_path)
    except (OSError, NameError):
        pass
    os.close(fd)
    sys.exit(2)
except Exception as e:
    print(f'Error: {e}', file=sys.stderr)
    os.close(fd)
    sys.exit(1)

# Release lock
os.close(fd)
sys.exit(0)
" "$lock_file" "$TRACKER_DIR" "$ticket_id" "$current_status" "$target_status" "$env_id" "$author" "$REDUCER" "$close_reason" "$verdict_hash" "$force_close_reason" || flock_exit=$?

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

    # Epic-close reminder: emit /dso:end-session prompt when an epic is closed
    ticket_type_on_close=$(_TRACKER="$TRACKER_DIR" _TID="$ticket_id" _SDIR="$SCRIPT_DIR" python3 -c "
import sys, os
sys.path.insert(0, os.environ['_SDIR'])
from ticket_reducer import reduce_ticket
state = reduce_ticket(os.path.join(os.environ['_TRACKER'], os.environ['_TID']))
print((state or {}).get('ticket_type', ''))
" 2>/dev/null) || ticket_type_on_close=""
    if [ "$ticket_type_on_close" = "epic" ]; then
        echo "REMINDER: Epic closed — run /dso:end-session to complete the sprint cleanly."
    fi

    # Scratch cleanup: remove per-ticket scratch dir (non-blocking; always returns 0)
    _scratch_cleanup_for_ticket "$ticket_id" 2>/dev/null || true
fi

exit 0
