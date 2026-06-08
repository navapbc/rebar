#!/usr/bin/env bash
# ticket-migrate-file-impact-v1.sh
# One-time migration: scan tickets for ## File Impact markdown sections and
# write structured FILE_IMPACT events. Idempotent via stamp file.
#
# Usage: ticket-migrate-file-impact-v1.sh [--target <host-project-root>] [--dry-run]
# Exit codes: 0 = success (including idempotent re-run), 1 = fatal error

set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Plugin root is one level above scripts/; repo root is two levels above that.
_PLUGIN_ROOT="$(cd "$_SCRIPT_DIR/.." && pwd)"

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

# Resolve target (default: git rev-parse --show-toplevel)
if [ -z "$_TARGET" ]; then
    _TARGET="$(git rev-parse --show-toplevel)"
fi
# Normalize _TARGET to a canonical absolute path so symlinks/relative paths
# don't cause the plugin-source-repo equality check to miss.
_TARGET="$(cd "$_TARGET" && pwd)"

# ── Plugin-source-repo guard ─────────────────────────────────────────────────
# Skip if _TARGET is the repo that contains this plugin (i.e., the plugin source repo).
# Derived from _PLUGIN_ROOT to avoid hardcoding a literal install path.
# Fail-open: if the plugin dir is not a git checkout (installed copy), skip the guard.
_PLUGIN_PARENT_REPO="$(git -C "$_PLUGIN_ROOT" rev-parse --show-toplevel 2>/dev/null)" || _PLUGIN_PARENT_REPO=""
if [ "$_TARGET" = "$_PLUGIN_PARENT_REPO" ] && [ -f "$_PLUGIN_ROOT/.claude-plugin/plugin.json" ]; then
    echo "NOTICE: target '$_TARGET' is the plugin source repo — skipping migration" >&2
    exit 0
fi

# ── Ticket tracker location ───────────────────────────────────────────────────
_TRACKER_DIR="$_TARGET/.tickets-tracker"
if [ ! -d "$_TRACKER_DIR" ]; then
    echo "NOTICE: no .tickets-tracker at $_TARGET — skipping migration" >&2
    exit 0
fi

# ── Stamp file — keyed by migration ID "file-impact-v1" ─────────────────────
_STAMP_FILE="$_TARGET/.claude/.file-impact-migration-v1"
if [ -f "$_STAMP_FILE" ]; then
    echo "NOTICE: file-impact-v1 migration already applied — skipping" >&2
    exit 0
fi

# ── Single-pass migration via Python inline heredoc ──────────────────────────
# stdout contract:
#   WRITE\t<ticket_id>\t<filename>\t<json>  — FILE_IMPACT event to write
#   SKIPPED\t<ticket_id>\t<reason>          — malformed section (found but no paths)
#   SUMMARY\t<processed>\t<skipped>         — final counts
_migrate_output=$(python3 - "$_TRACKER_DIR" "$_DRYRUN" <<'PYEOF'
import json, os, re, sys, uuid, datetime, pathlib

tracker_dir = pathlib.Path(sys.argv[1])
dryrun = sys.argv[2] == "1"

tickets_processed = 0
tickets_skipped = 0

for ticket_dir in sorted(tracker_dir.iterdir()):
    if not ticket_dir.is_dir() or ticket_dir.name.startswith('.'):
        continue
    ticket_id = ticket_dir.name

    # Check if FILE_IMPACT event already exists for this ticket (idempotency)
    existing_fi = list(ticket_dir.glob("*-FILE_IMPACT.json"))
    if existing_fi:
        continue  # Already has a FILE_IMPACT event — skip

    # Find ticket's event files sorted by name (timestamp-prefixed)
    event_files = sorted(ticket_dir.glob("*.json"))
    if not event_files:
        continue

    # Get created_at from first event for timestamp
    try:
        with open(event_files[0]) as f:
            first_event = json.load(f)
        ticket_ts = first_event.get("timestamp", int(datetime.datetime.utcnow().timestamp()))
    except Exception:
        ticket_ts = int(datetime.datetime.utcnow().timestamp())

    # Scan all event files for ## File Impact section
    file_impact = []
    found_section = False
    for ef in event_files:
        try:
            with open(ef) as f:
                event_data = json.load(f)
        except Exception:
            continue
        # Check data.body and data.description fields
        for field in ("body", "description"):
            text = event_data.get("data", {}).get(field, "")
            if not text:
                continue
            # Find ## File Impact section (up to next ## heading or end)
            match = re.search(r'##\s+File\s+Impact\s*\n(.*?)(?=\n##|\Z)', text, re.DOTALL | re.IGNORECASE)
            if not match:
                continue
            found_section = True
            section = match.group(1)
            # Parse lines: "- path/to/file (reason)" or "- path/to/file"
            for line in section.split('\n'):
                line = line.strip()
                if not line.startswith('-'):
                    continue
                line = line[1:].strip()
                if not line:
                    continue
                reason_match = re.match(r'^(.+?)\s+\((\w+)\)\s*$', line)
                if reason_match:
                    file_impact.append({
                        "path": reason_match.group(1).strip(),
                        "reason": reason_match.group(2).strip()
                    })
                else:
                    file_impact.append({"path": line, "reason": "modified"})

    if found_section and not file_impact:
        # Malformed: section found but no paths extracted
        print(f"SKIPPED\t{ticket_id}\tmalformed ## File Impact section", flush=True)
        tickets_skipped += 1
        continue

    if not file_impact:
        continue  # No file impact section found — nothing to migrate

    # Emit FILE_IMPACT event
    event_uuid = str(uuid.uuid4())
    event = {
        "event_type": "FILE_IMPACT",
        "timestamp": ticket_ts,
        "uuid": event_uuid,
        "env_id": "00000000-0000-4000-8000-migration001",
        "data": {"file_impact": file_impact}
    }
    filename = f"{ticket_ts}-{event_uuid}-FILE_IMPACT.json"
    print(f"WRITE\t{ticket_id}\t{filename}\t{json.dumps(event)}", flush=True)
    tickets_processed += 1

print(f"SUMMARY\t{tickets_processed}\t{tickets_skipped}", flush=True)
PYEOF
)

# ── Process output from Python ────────────────────────────────────────────────
tickets_processed=0
tickets_skipped=0
_write_count=0
while IFS=$'\t' read -r action field1 field2 field3; do
    case "$action" in
        WRITE)
            ticket_id="$field1"
            filename="$field2"
            event_json="$field3"
            if [ "$_DRYRUN" = "1" ]; then
                echo "[dryrun] Would write FILE_IMPACT event for $ticket_id: $filename" >&2
            else
                printf '%s' "$event_json" > "$_TRACKER_DIR/$ticket_id/$filename"
                echo "[migrate-file-impact-v1] Wrote FILE_IMPACT event for $ticket_id" >&2
                _write_count=$(( _write_count + 1 ))
            fi
            ;;
        SKIPPED)
            ticket_id="$field1"
            reason="$field2"
            echo "[migrate-file-impact-v1] SKIPPED $ticket_id: $reason" >&2
            ;;
        SUMMARY)
            tickets_processed="$field1"
            tickets_skipped="$field2"
            ;;
    esac
done <<< "$_migrate_output"

# ── Commit written events to the tickets git branch ──────────────────────────
# Events written above are untracked until committed; uncommitted events are
# invisible to all ticket system consumers and will be lost on git clean.
if [ "$_DRYRUN" = "0" ] && [ "$_write_count" -gt 0 ]; then
    git -C "$_TRACKER_DIR" add -A
    git -C "$_TRACKER_DIR" commit -m "migrate: add FILE_IMPACT events for $_write_count tickets (file-impact-v1)" 2>&1 || {
        echo "[migrate-file-impact-v1] ERROR: git commit failed — removing written event files to allow clean retry" >&2
        git -C "$_TRACKER_DIR" reset 2>/dev/null || true
        git -C "$_TRACKER_DIR" clean -f 2>/dev/null || true
        exit 1
    }
fi

# ── Write stamp file (skipped in dry-run) ────────────────────────────────────
if [ "$_DRYRUN" = "0" ]; then
    mkdir -p "$(dirname "$_STAMP_FILE")"
    python3 -c "
import json, time
stamp = {
    'version': 'file-impact-v1',
    'migrated_at': int(time.time()),
    'tickets_processed': $tickets_processed,
    'tickets_skipped': $tickets_skipped
}
print(json.dumps(stamp))
" > "$_STAMP_FILE"
    echo "[migrate-file-impact-v1] Migration complete: $tickets_processed processed, $tickets_skipped skipped" >&2
fi

exit 0
