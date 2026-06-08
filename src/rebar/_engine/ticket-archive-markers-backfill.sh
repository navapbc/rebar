#!/usr/bin/env bash
# ticket-archive-markers-backfill.sh
# Backfill .archived marker files for all ticket directories that are
# in a net-archived state but lack the marker file.
#
# Usage: ticket-archive-markers-backfill.sh [--dry-run] [--tracker-dir=PATH]
#
# Why this exists:
#   The .archived marker file is used by ticket-list.sh and other tooling
#   to quickly skip archived tickets without parsing full event logs.
#   Tickets archived before the marker convention was introduced lack this
#   file. This script backfills it in one pass, permanently switching those
#   tickets to the fast-path exclusion check.
#
# Net archival state:
#   A ticket is considered "net archived" when it has at least one ARCHIVED
#   event whose UUID has not been reverted by a subsequent REVERT event
#   (data.target_event_uuid == archived_event_uuid).
#
# Options:
#   --dry-run            Report which tickets would get markers; write nothing.
#   --tracker-dir=PATH   Override TICKET_TRACKER_DIR env var.
#   --help, -h           Show this help.
#
# Exit codes: 0 = success (or dry-run), 1 = fatal error
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Respect PROJECT_ROOT exported by the .claude/scripts/dso shim (bb42-1291).
REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"
TRACKER_DIR="${TICKET_TRACKER_DIR:-${REPO_ROOT}/.tickets-tracker}"

# ── Parse arguments ───────────────────────────────────────────────────────────
dry_run=false

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)          dry_run=true ;;
        --tracker-dir=*)    TRACKER_DIR="${1#--tracker-dir=}" ;;
        --help|-h)
            sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Error: unknown option '$1'" >&2; exit 1 ;;
    esac
    shift
done

# ── Validate ──────────────────────────────────────────────────────────────────
if [ ! -d "$TRACKER_DIR" ]; then
    echo "Error: tracker dir not found: $TRACKER_DIR" >&2
    exit 1
fi

# ── Discover and process tickets ──────────────────────────────────────────────
# Uses inline Python for filesystem scan and marker writing.
# write_marker() logic mirrors ticket_reducer.marker.write_marker:
#   - acquires per-ticket fcntl.flock on <ticket_dir>/.write.lock
#   - creates <ticket_dir>/.archived as an empty file
#   - releases flock
#   - on OSError: logs warning to stderr, continues
#
# Net archival computation:
#   1. Scan all *.json event files in the ticket dir.
#   2. Collect UUIDs of ARCHIVED events.
#   3. Collect target_event_uuid values from REVERT events.
#   4. Net archived = any ARCHIVED UUID is NOT in the reverted set.
python3 - "$TRACKER_DIR" "$dry_run" <<'PYEOF'
import fcntl
import glob
import json
import os
import sys

tracker_dir = sys.argv[1]
dry_run = sys.argv[2].lower() == "true"

def is_net_archived(ticket_dir: str) -> bool:
    """Return True if the ticket has net archived state.

    Handles ARCHIVED-then-REVERT sequences: a ticket is net archived
    only if at least one ARCHIVED event UUID has NOT been reverted.
    """
    archived_uuids: set[str] = set()
    reverted_uuids: set[str] = set()

    for filepath in glob.glob(os.path.join(ticket_dir, "*.json")):
        try:
            with open(filepath, encoding="utf-8") as f:
                event = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        event_type = event.get("event_type", "")
        if event_type == "ARCHIVED":
            uuid = event.get("uuid")
            if uuid:
                archived_uuids.add(uuid)
        elif event_type == "REVERT":
            target_uuid = event.get("data", {}).get("target_event_uuid")
            if target_uuid:
                reverted_uuids.add(target_uuid)

    # Net archived: any ARCHIVED event UUID that was NOT subsequently reverted
    net_archived_uuids = archived_uuids - reverted_uuids
    return bool(net_archived_uuids)


def write_marker(ticket_dir: str) -> None:
    """Create .archived marker file with per-ticket fcntl.flock.

    Mirrors ticket_reducer.marker.write_marker contract:
    - Acquires LOCK_EX on <ticket_dir>/.write.lock (create if absent).
    - Creates <ticket_dir>/.archived as an empty file.
    - Releases lock.
    - On any OSError: logs warning to stderr, returns (does NOT raise).
    """
    lock_path = os.path.join(ticket_dir, ".write.lock")
    marker_path = os.path.join(ticket_dir, ".archived")
    lock_fd = None
    try:
        lock_fd = open(lock_path, "a")
        # Acquire exclusive lock with 10s timeout via repeated trylock
        import time
        deadline = time.monotonic() + 10.0
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"flock timeout acquiring lock for {ticket_dir}"
                    )
                time.sleep(0.05)
        # Create the marker file (empty, idempotent)
        open(marker_path, "a").close()
    except OSError as exc:
        print(
            f"WARNING: failed to write .archived marker for {ticket_dir}: {exc}",
            file=sys.stderr,
        )
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                lock_fd.close()
            except OSError:
                pass


wrote = 0
skipped = 0

for entry in sorted(os.scandir(tracker_dir), key=lambda e: e.name):
    if not entry.is_dir() or entry.name.startswith("."):
        continue
    # A ticket directory must contain at least one event JSON file
    if not glob.glob(os.path.join(entry.path, "*.json")):
        continue

    marker_path = os.path.join(entry.path, ".archived")

    if os.path.exists(marker_path):
        skipped += 1
        continue

    if not is_net_archived(entry.path):
        continue

    if dry_run:
        print(f"  would write: {entry.name}/.archived")
        wrote += 1
    else:
        write_marker(entry.path)
        wrote += 1

if dry_run:
    print(f"Dry-run — would write {wrote} markers, would skip {skipped} (already present)")
else:
    print(f"Wrote {wrote} markers, skipped {skipped} (already present)")
PYEOF
