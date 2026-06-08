#!/usr/bin/env bash
# ticket-migrate-schema-hardening.sh
# One-time migration: rebuild the SNAPSHOT with schema hardening applied.
#
# Usage:
#   ticket-migrate-schema-hardening.sh [--dry-run | --rollback]
#
# Flags:
#   --dry-run     Show what would change without making any changes (read-only)
#   --rollback    Restore the most recent pre-migration backup to SNAPSHOT.json
#
# Exit codes:
#   0 — Success
#   1 — Fatal error (concurrent invocation, lock failure, or not yet implemented)
#
# Lock:
#   Uses an exclusive lock (/tmp/ticket-migrate-schema-hardening.lock) to
#   prevent concurrent invocations from corrupting the SNAPSHOT backup.
#   Lock is released on success, failure, and SIGINT/SIGTERM.

set -euo pipefail

# ── Lock file ────────────────────────────────────────────────────────────────
# stable across concurrent invocations (mktemp would produce different paths per
# invocation, defeating the mutex). sibling script uses the same pattern.
_LOCK_FILE="/tmp/ticket-migrate-schema-hardening.lock"
_LOCK_TIMEOUT=5
_LOCK_ACQUIRED=false

# ── Acquire exclusive lock (portable: python3 fcntl.flock or mkdir fallback) ──
_acquire_lock() {
    local lock_file="$_LOCK_FILE"
    local timeout="$_LOCK_TIMEOUT"

    # Try python3 fcntl.flock first (works on macOS + Linux).
    # This is an advisory pre-check: the fcntl lock is released when the python3
    # subprocess exits. The mkdir-based sustained lock below is the real mutex.
    if command -v python3 >/dev/null 2>&1; then
        python3 - "$lock_file" "$timeout" <<'PYEOF' 2>&1 || {
import fcntl, os, sys, time

lock_file = sys.argv[1]
timeout = int(sys.argv[2])

try:
    fd = open(lock_file, 'w')
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Lock acquired — write PID and leave fd open (but we can't return fd)
            fd.write(str(os.getpid()) + '\n')
            fd.flush()
            sys.exit(0)
        except (IOError, OSError):
            if time.time() >= deadline:
                sys.exit(1)
            time.sleep(0.1)
except Exception as e:
    print(str(e), file=sys.stderr)
    sys.exit(1)
PYEOF
            echo "migration already in progress — aborting" >&2
            exit 1
        }
        # fcntl lock was released when the python3 subprocess exited.
        # Fall through to mkdir-based lock for sustained hold across script lifetime.
    fi

    # Sustained lock: use a directory lock (atomic mkdir, POSIX-compliant).
    # This is the real mutex — it is held for the duration of this process and
    # cleaned up by _release_lock().
    local lock_dir="${lock_file}.d"
    local deadline
    deadline=$(( $(date +%s) + timeout ))
    while true; do
        if mkdir "$lock_dir" 2>/dev/null; then
            _LOCK_ACQUIRED=true
            # Write PID to lock file for diagnostics
            echo "$$" > "$lock_file" 2>/dev/null || true
            return 0
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "migration already in progress — aborting" >&2
            exit 1
        fi
        sleep 0.1
    done
}

# ── Lock cleanup (removes both the lock file and directory lock) ──────────────
# shellcheck disable=SC2329  # invoked indirectly via trap
_release_lock() {
    if [ "$_LOCK_ACQUIRED" = true ]; then
        rm -f "$_LOCK_FILE"
        rm -rf "${_LOCK_FILE}.d"
        _LOCK_ACQUIRED=false
    fi
}
trap '_release_lock' EXIT
trap '_release_lock; exit 130' INT
trap '_release_lock; exit 143' TERM

# ── Acquire lock before any operation ────────────────────────────────────────
_acquire_lock

# ── Parse arguments ──────────────────────────────────────────────────────────
_DRYRUN=0
_ROLLBACK=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)
            _DRYRUN=1
            shift
            ;;
        --rollback)
            _ROLLBACK=1
            shift
            ;;
        *)
            echo "Error: unknown argument '$1'" >&2
            exit 1
            ;;
    esac
done

# ── Resolve repo root and tracker dir ────────────────────────────────────────
_REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "Error: not inside a git repository" >&2
    exit 1
}
_TRACKER_DIR="$_REPO_ROOT/.tickets-tracker"
_SNAPSHOT_FILE="$_TRACKER_DIR/SNAPSHOT.json"
_MARKER_FILE="$_TRACKER_DIR/.migrations/schema-hardening.done"

# ── Plugin-source-repo guard ─────────────────────────────────────────────────
if [ -f "$_REPO_ROOT/plugin.json" ]; then
    echo "NOTICE: target '$_REPO_ROOT' is the plugin source repo — skipping migration (no changes made)" >&2
    exit 0
fi

# ── Dispatch ──────────────────────────────────────────────────────────────────

if [ "$_DRYRUN" -eq 1 ]; then
    # Determine current schema_version and compute target
    _current_sv=0
    if [ -f "$_SNAPSHOT_FILE" ]; then
        _current_sv=$(python3 - "$_SNAPSHOT_FILE" <<'PYEOF' 2>/dev/null || echo "0"
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get('schema_version', 0))
except Exception:
    print(0)
PYEOF
)
    fi
    _target_sv=$(( _current_sv + 1 ))
    echo "DRY_RUN: would bump schema_version to $_target_sv"
    exit 0
fi

if [ "$_ROLLBACK" -eq 1 ]; then
    # Find the most recent backup file
    _backup_file=""
    _latest_ts=0
    while IFS= read -r _f; do
        # Extract timestamp from filename (SNAPSHOT.backup.<ts>)
        _bn="$(basename "$_f")"
        _ts="${_bn#SNAPSHOT.backup.}"
        if [[ "$_ts" =~ ^[0-9]+$ ]] && [ "$_ts" -gt "$_latest_ts" ]; then
            _latest_ts="$_ts"
            _backup_file="$_f"
        fi
    done < <(find "$_TRACKER_DIR" -maxdepth 1 -name 'SNAPSHOT.backup.*' 2>/dev/null)

    if [ -z "$_backup_file" ]; then
        echo "Error: no backup file found in '$_TRACKER_DIR'" >&2
        exit 1
    fi

    cp "$_backup_file" "$_SNAPSHOT_FILE" || { echo "Error: rollback cp failed" >&2; exit 1; }
    rm -f "$_MARKER_FILE" 2>/dev/null || true
    exit 0
fi

# ── Default: full migration ───────────────────────────────────────────────────

# Idempotency check — if marker exists, skip silently
if [ -f "$_MARKER_FILE" ]; then
    exit 0
fi

# Ensure SNAPSHOT.json exists
if [ ! -f "$_SNAPSHOT_FILE" ]; then
    echo "Error: SNAPSHOT.json not found at '$_SNAPSHOT_FILE'" >&2
    exit 1
fi

# Step 1: Create pre-migration backup
_BACKUP_TS="$(date +%s)"
_BACKUP_FILE="$_TRACKER_DIR/SNAPSHOT.backup.$_BACKUP_TS"
cp "$_SNAPSHOT_FILE" "$_BACKUP_FILE" || { echo "Error: backup failed for '$_SNAPSHOT_FILE'" >&2; exit 1; }

# Step 2: Bump schema_version in SNAPSHOT.json
python3 - "$_SNAPSHOT_FILE" <<'PYEOF'
import json, sys

snap_path = sys.argv[1]

try:
    with open(snap_path) as f:
        snapshot = json.load(f)
except Exception as e:
    print("Error: failed to read {}: {}".format(snap_path, e), file=sys.stderr)
    sys.exit(1)

current_sv = snapshot.get("schema_version", 0)
if not isinstance(current_sv, int):
    current_sv = 0
snapshot["schema_version"] = current_sv + 1

try:
    with open(snap_path, "w") as f:
        json.dump(snapshot, f, indent=2)
except Exception as e:
    print("Error: failed to write {}: {}".format(snap_path, e), file=sys.stderr)
    sys.exit(1)
PYEOF
if [ $? -ne 0 ]; then
    echo "Error: failed to bump schema_version in SNAPSHOT.json" >&2
    exit 1
fi

# Step 2.5: Backfill parent_status_uuid into existing STATUS events
# Sort STATUS events per-ticket by filename timestamp prefix and chain them so
# that each event's data.parent_status_uuid points to the uuid of the prior
# STATUS event (null for the first in the chain).
python3 - "$_TRACKER_DIR" <<'PYEOF'
import json
import os
import sys

tracker_dir = sys.argv[1]

for ticket_name in sorted(os.listdir(tracker_dir)):
    ticket_dir = os.path.join(tracker_dir, ticket_name)
    if not os.path.isdir(ticket_dir):
        continue
    if ticket_name.startswith('.'):
        continue

    # Collect STATUS event files for this ticket, sorted by filename (timestamp prefix).
    status_files = sorted(
        f for f in os.listdir(ticket_dir) if f.endswith('-STATUS.json')
    )

    prev_uuid = None
    for fname in status_files:
        fpath = os.path.join(ticket_dir, fname)
        try:
            with open(fpath, encoding='utf-8') as fh:
                event = json.load(fh)
        except (json.JSONDecodeError, OSError):
            # Break the chain at a corrupt file: subsequent events get parent=null
            # rather than a stale pointer into a chain whose middle is missing.
            prev_uuid = None
            continue

        data = event.get('data', {})
        # Only backfill if parent_status_uuid is absent from data (not merely null).
        if 'parent_status_uuid' not in data:
            data['parent_status_uuid'] = prev_uuid
            event['data'] = data
            tmp_path = fpath + '.tmp'
            try:
                with open(tmp_path, 'w', encoding='utf-8') as fh:
                    json.dump(event, fh, ensure_ascii=False)
                os.rename(tmp_path, fpath)
            except OSError as e:
                print(f'Warning: could not update {fpath}: {e}', file=sys.stderr)
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        # Advance chain: next event's parent = this event's uuid.
        prev_uuid = event.get('uuid') or None
PYEOF
if [ $? -ne 0 ]; then
    echo "Error: failed to backfill parent_status_uuid in STATUS events" >&2
    exit 1
fi

# Step 3: Write migration marker (idempotency guard)
mkdir -p "$(dirname "$_MARKER_FILE")" || { echo "Error: could not create migrations dir" >&2; exit 1; }
touch "$_MARKER_FILE" || { echo "Error: could not write migration marker" >&2; exit 1; }

exit 0
