#!/usr/bin/env bash
# ticket-compact.sh
# Compact a ticket's event history into a single SNAPSHOT event.
#
# Usage: ticket-compact.sh <ticket_id> [--threshold=N] [--skip-sync] [--no-commit]
# Default threshold: COMPACT_THRESHOLD env var or 10
#
# The compaction operation:
#   1. Pre-flock rough count as optimization gate — skips if clearly below threshold
#   2. Acquires flock for all mutation operations
#   3. Re-lists event files inside flock (authoritative)
#   4. Re-checks count against threshold — skips if below (concurrent compaction)
#   5. Runs the reducer to compile current state
#   6. Writes a SNAPSHOT event with source_event_uuids
#   7. Deletes only the specific files listed inside flock
#   8. Commits all changes atomically
#
# IMPORTANT: git operations are inlined (NOT via write_commit_event)
# to avoid nested flock deadlock.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=${_PLUGIN_ROOT}/scripts/ticket-lib.sh
source "$SCRIPT_DIR/ticket-lib.sh"

REPO_ROOT="$(git rev-parse --show-toplevel)"
TRACKER_DIR="${TICKET_TRACKER_DIR:-$REPO_ROOT/.tickets-tracker}"

# ── Usage ────────────────────────────────────────────────────────────────────
_usage() {
    echo "Usage: ticket-compact.sh <ticket_id> [--threshold=N]" >&2
    echo "  Default threshold: COMPACT_THRESHOLD env var or 10" >&2
    exit 1
}

# ── Parse arguments ──────────────────────────────────────────────────────────
if [ $# -lt 1 ]; then
    _usage
fi

ticket_id=$(TICKETS_TRACKER_DIR="$TRACKER_DIR" resolve_ticket_id "$1") || exit 1
shift

threshold="${COMPACT_THRESHOLD:-10}"
skip_sync=false
no_commit=false
while [ $# -gt 0 ]; do
    case "$1" in
        --threshold=*)
            threshold="${1#--threshold=}"
            ;;
        --skip-sync)
            skip_sync=true
            ;;
        --no-commit)
            no_commit=true
            ;;
        *)
            echo "Error: unknown argument '$1'" >&2
            _usage
            ;;
    esac
    shift
done

# ── Validate ticket system ───────────────────────────────────────────────────
if [ ! -d "$TRACKER_DIR" ] || { [ ! -f "$TRACKER_DIR/.git" ] && [ ! -d "$TRACKER_DIR/.git" ]; }; then
    echo "Error: ticket system not initialized. Run 'ticket init' first." >&2
    exit 1
fi

ticket_dir="$TRACKER_DIR/$ticket_id"
if [ ! -d "$ticket_dir" ]; then
    echo "Error: ticket directory not found: $ticket_dir" >&2
    exit 1
fi

# ── Sync-before-compact precondition ─────────────────────────────────────────
if [ "$skip_sync" != "true" ]; then
    # Call ticket sync before compacting to pull the latest remote state.
    # TICKET_SYNC_CMD can be overridden in tests; default is "ticket sync" via PATH.
    _sync_cmd="${TICKET_SYNC_CMD:-ticket sync}"

    # Run sync; treat exit 127 (subcommand absent) as a graceful skip (warn + continue).
    _sync_err=$(mktemp /tmp/ticket-compact-sync-err.XXXXXX)
    _sync_exit=0
    eval "$_sync_cmd" 2>"$_sync_err" || _sync_exit=$?
    if [ "$_sync_exit" -eq 127 ]; then
        echo "warning: sync unavailable (sync subcommand absent) — skipping sync before compact" >&2
        _sync_exit=0
    elif [ "$_sync_exit" -ne 0 ]; then
        _sync_stderr=$(cat "$_sync_err" 2>/dev/null || true)
        rm -f "$_sync_err"
        echo "Error: ticket sync failed (exit $_sync_exit)${_sync_stderr:+: $_sync_stderr}" >&2
        exit "$_sync_exit"
    fi
    rm -f "$_sync_err"

    # ── Remote SNAPSHOT check ─────────────────────────────────────────────────
    # If any SNAPSHOT file already exists in the ticket dir (written by a remote
    # environment after sync), skip local compaction to avoid redundant snapshots.
    _existing_snapshot=$(find "$ticket_dir" -maxdepth 1 -name '*-SNAPSHOT.json' 2>/dev/null | head -1)
    if [ -n "$_existing_snapshot" ]; then
        echo "skipping compaction for $ticket_id — remote SNAPSHOT exists"
        exit 0
    fi
fi

# ── Pre-flock optimization gate ──────────────────────────────────────────────
# Cheap count to avoid acquiring the lock when clearly below threshold.
# The authoritative count happens inside flock (another process may compact first).
_preflock_count=$(find "$ticket_dir" -maxdepth 1 -name '*.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
if [ "$_preflock_count" -le "$threshold" ]; then
    echo "below threshold ($_preflock_count <= $threshold) — skipping compaction"
    exit 0
fi

# ── Acquire flock for file listing + compile + write + delete + commit ───────
# All mutation and state-reading happens under flock to prevent races.
# Inlines git operations to avoid nested flock deadlock with write_commit_event.
lock_file="$TRACKER_DIR/.ticket-write.lock"

# Ensure gc.auto=0 in tickets worktree (idempotent guard)
git -C "$TRACKER_DIR" config gc.auto 0

max_retries=2
flock_timeout=30
attempt=0
lock_acquired=false

while [ "$attempt" -lt "$max_retries" ]; do
    attempt=$((attempt + 1))

    flock_exit=0
    python3 - "$lock_file" "$flock_timeout" "$TRACKER_DIR" "$ticket_id" "$ticket_dir" "$threshold" "$SCRIPT_DIR/ticket-reducer.py" "$no_commit" << 'PYEOF' || flock_exit=$?
import fcntl, glob, json, os, subprocess, sys, time, uuid

lock_path = sys.argv[1]
timeout = int(sys.argv[2])
tracker_dir = sys.argv[3]
ticket_id = sys.argv[4]
ticket_dir = sys.argv[5]
threshold = int(sys.argv[6])
reducer_script = sys.argv[7]
no_commit = sys.argv[8] == 'true'

# ── Acquire the unified write lock (Tier D: fcntl + mkdir dual leg, so compaction
# mutually excludes bash leaf-writes on every platform class — stiff-mop-lane) ──
_src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(reducer_script))))
if _src not in sys.path:
    sys.path.insert(0, _src)
from rebar._store import lock as _store_lock
try:
    handle = _store_lock.acquire(tracker_dir, timeout=timeout, attempts=1, dual_window=True)
except _store_lock.LockTimeout:
    sys.exit(1)

# ── Lock acquired — all operations below are under flock ─────────────────

# Re-list event files inside flock (authoritative).
# Exclude *-SYNC.json — SYNC files are bridge metadata (Jira key mapping) and
# must survive compaction so resolve_jira_key() works on post-compact events.
candidate_files = sorted([
    os.path.join(ticket_dir, f)
    for f in os.listdir(ticket_dir)
    if f.endswith('.json') and not f.startswith('.') and not f.endswith('-SYNC.json')
])

# Forward-compatibility (schema-version rule, ticket_reducer/_version.py): an event
# whose event_type is UNKNOWN to this rebar was written by a newer clone. It must be
# PRESERVED at the file level — never absorbed into the SNAPSHOT nor deleted — or an
# older clone's compaction would destroy a newer clone's data. Partition unknown-type
# files out of the compaction set; they are left on disk untouched.
sys.path.insert(0, os.path.dirname(reducer_script))
try:
    from ticket_reducer._version import KNOWN_EVENT_TYPES
except Exception:
    # Fail-safe mirror of _version.KNOWN_EVENT_TYPES (kept in sync; the import path
    # is reliable in practice, this only guards a pathological package-shadow case).
    KNOWN_EVENT_TYPES = frozenset({
        'CREATE', 'STATUS', 'COMMENT', 'LINK', 'UNLINK', 'BRIDGE_ALERT', 'REVERT',
        'EDIT', 'FILE_IMPACT', 'VERIFY_COMMANDS', 'SIGNATURE', 'ARCHIVED', 'SNAPSHOT',
    })

event_files = []
for fp in candidate_files:
    try:
        with open(fp, encoding='utf-8') as f:
            etype = json.load(f).get('event_type', '')
    except (json.JSONDecodeError, OSError):
        etype = ''  # corrupt/unreadable: treat as compactable (preserves prior behavior)
    if etype and etype not in KNOWN_EVENT_TYPES:
        continue  # unknown-type event: preserve untouched, do not snapshot or delete
    event_files.append(fp)
event_count = len(event_files)

# Re-check threshold inside flock — another process may have compacted
if event_count <= threshold:
    handle.release()
    # Exit 10 = below-threshold-inside-flock (distinct from lock-timeout exit 1)
    sys.exit(10)

# Compile current state via reducer (inside flock)
try:
    result = subprocess.run(
        ['python3', reducer_script, ticket_dir],
        capture_output=True, text=True, check=True,
    )
    compiled_state_json = result.stdout
except subprocess.CalledProcessError:
    print(f'Error: reducer failed for ticket {ticket_id} (corrupt or ghost ticket)', file=sys.stderr)
    handle.release()
    sys.exit(3)

# Validate compiled state is not an error state
compiled_state = json.loads(compiled_state_json)
status = compiled_state.get('status', '')
if status in ('error', 'fsck_needed'):
    print(f"Error: ticket {ticket_id} has status '{status}' — cannot compact", file=sys.stderr)
    handle.release()
    sys.exit(3)

# Extract UUIDs from each event file
source_uuids = []
for filepath in event_files:
    try:
        with open(filepath, encoding='utf-8') as f:
            event = json.load(f)
        source_uuids.append(event.get('uuid', os.path.basename(filepath)))
    except (json.JSONDecodeError, OSError):
        source_uuids.append(os.path.basename(filepath))

# Read env_id from .env-id file
env_id_path = os.path.join(tracker_dir, '.env-id')
try:
    with open(env_id_path, encoding='utf-8') as f:
        env_id = f.read().strip()
except OSError:
    env_id = '00000000-0000-4000-8000-000000000000'

# Get author from git config
try:
    result = subprocess.run(
        ['git', 'config', 'user.name'],
        capture_output=True, text=True, check=True,
    )
    author = result.stdout.strip()
except subprocess.CalledProcessError:
    author = 'system'

# Build SNAPSHOT event
snapshot_uuid = str(uuid.uuid4())
snapshot_timestamp = time.time_ns()

snapshot_event = {
    'event_type': 'SNAPSHOT',
    'timestamp': snapshot_timestamp,
    'uuid': snapshot_uuid,
    'env_id': env_id,
    'author': author,
    'data': {
        'compiled_state': compiled_state,
        'source_event_uuids': source_uuids,
        'compacted_at': snapshot_timestamp,
    },
}

# Write SNAPSHOT to temp file, then atomic rename
snapshot_filename = f'{snapshot_timestamp}-{snapshot_uuid}-SNAPSHOT.json'
final_path = os.path.join(ticket_dir, snapshot_filename)
staging_temp = final_path + '.tmp'

with open(staging_temp, 'w', encoding='utf-8') as f:
    json.dump(snapshot_event, f, ensure_ascii=False)
os.rename(staging_temp, final_path)

# Delete original event files (only the specific files listed inside flock)
for filepath in event_files:
    try:
        os.remove(filepath)
    except OSError:
        pass  # Already removed by concurrent process

# Invalidate cache (snapshot changed the state representation)
cache_path = os.path.join(ticket_dir, '.cache.json')
try:
    os.remove(cache_path)
except OSError:
    pass

# Stage all changes in the ticket dir and commit atomically (unless --no-commit)
# Using git add -A on the ticket subdir handles both additions and deletions
if not no_commit:
    try:
        subprocess.run(
            ['git', '-C', tracker_dir, 'add', '-A', f'{ticket_id}/'],
            check=True, capture_output=True, text=True,
        )
        # Only commit if there are staged changes
        status_result = subprocess.run(
            ['git', '-C', tracker_dir, 'diff', '--cached', '--quiet'],
            capture_output=True, text=True,
        )
        if status_result.returncode != 0:
            subprocess.run(
                ['git', '-C', tracker_dir, 'commit', '-q', '--no-verify', '-m',
                 f'ticket: COMPACT {ticket_id}'],
                check=True, capture_output=True, text=True,
            )
    except subprocess.CalledProcessError as e:
        print(f'Error: git compact commit failed: {e.stderr}', file=sys.stderr)
        handle.release()
        sys.exit(2)

# Emit event count for the shell wrapper's summary message
print(f'EVENT_COUNT={event_count}')

# Release lock
handle.release()
sys.exit(0)
PYEOF

    if [ "$flock_exit" -eq 0 ]; then
        lock_acquired=true
        break
    elif [ "$flock_exit" -eq 10 ]; then
        # Below threshold after re-check inside flock (concurrent compaction)
        echo "below threshold (re-checked inside flock) — skipping compaction"
        exit 0
    elif [ "$flock_exit" -eq 2 ]; then
        echo "Error: git operation failed while holding lock" >&2
        exit 1
    elif [ "$flock_exit" -eq 3 ]; then
        # Reducer or state validation failure — already printed error
        exit 1
    fi
done

if [ "$lock_acquired" = false ]; then
    total_wait=$((flock_timeout * max_retries))
    echo "Error: flock: could not acquire lock after ${total_wait}s" >&2
    exit 1
fi

echo "compacted events into SNAPSHOT for $ticket_id"
exit 0
