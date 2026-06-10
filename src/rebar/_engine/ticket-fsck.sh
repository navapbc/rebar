#!/usr/bin/env bash
# ticket-fsck.sh
# Non-destructive ticket system integrity validator.
#
# Runs five validation checks:
#   1. JSON validity of event files
#   2. CREATE event presence (via reducer)
#   3. Stale .git/index.lock cleanup
#   4. SNAPSHOT source_event_uuids consistency
#   4.5 Push-pending notice (local ahead of origin/tickets; informational)
#   5. Summary
#
# Non-destructive except for stale index.lock removal.
# Uses python3 for all JSON parsing.
#
# Usage: ticket-fsck.sh
# Exit: 0 if no issues, 1 if any issues found.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"
TRACKER_DIR="$REPO_ROOT/.tickets-tracker"

# Canonical structured-output flag (--output/-o); logic in ticket_output.py.
# shellcheck source=/dev/null
source "$SCRIPT_DIR/ticket-output.sh"
_resolve_output_format report "$@" || exit 2
_strip_output_flags "$@"
set -- ${_OUTPUT_ARGS[@]+"${_OUTPUT_ARGS[@]}"}

# --output json: re-run the (untouched) text scan, then transform its tagged
# lines into {issues:[{kind,ticket_id?,filename?,detail}], fixed[], issue_count}.
# Re-running in text mode keeps the scan logic in exactly one place.
if [ "$_OUTPUT_FMT" = "json" ]; then
    _fsck_rc=0
    _fsck_text=$(bash "$0" ${_OUTPUT_ARGS[@]+"${_OUTPUT_ARGS[@]}"} 2>/dev/null) || _fsck_rc=$?
    printf '%s' "$_fsck_text" | python3 -c '
import json, re, sys
issues, fixed = [], []
for line in sys.stdin.read().splitlines():
    if not line.strip():
        continue
    if line.startswith("FIXED:"):
        fixed.append(line[len("FIXED:"):].strip())
        continue
    if line.startswith("fsck complete"):
        continue
    m = re.match(r"^([A-Z_]+):\s*(.*)$", line)
    if not m:
        continue
    kind, rest = m.group(1).lower(), m.group(2)
    item = {"kind": kind}
    # Only the per-ticket kinds carry a "<ticket_id>[/<filename>] — <detail>"
    # head; free-form notices (warn/push_pending) keep the whole text as detail.
    structured = {"corrupt", "corrupt_create", "missing_create",
                  "snapshot_inconsistent", "orphan_event"}
    head, sep, detail = rest.partition(" — ")
    if sep and kind in structured:
        if "/" in head:
            tid, _, fn = head.partition("/")
            item["ticket_id"], item["filename"] = tid, fn
        else:
            item["ticket_id"] = head
        item["detail"] = detail
    else:
        item["detail"] = rest
    issues.append(item)
print(json.dumps({"issues": issues, "fixed": fixed, "issue_count": len(issues)}))
'
    exit "$_fsck_rc"
fi

# ── Validate ticket system ───────────────────────────────────────────────────
if [ ! -d "$TRACKER_DIR" ]; then
    echo "Error: ticket system not initialized (.tickets-tracker/ not found)." >&2
    echo "Run 'ticket init' first." >&2
    exit 1
fi

issue_count=0

# ── Resolve git dir for the tracker worktree ─────────────────────────────────
_resolve_tracker_git_dir() {
    local tracker_git="$TRACKER_DIR/.git"
    if [ -f "$tracker_git" ]; then
        local gitdir
        gitdir=$(sed 's/^gitdir: //' "$tracker_git")
        if [[ "$gitdir" != /* ]]; then
            gitdir="$TRACKER_DIR/$gitdir"
        fi
        echo "$gitdir"
    elif [ -d "$tracker_git" ]; then
        echo "$tracker_git"
    else
        echo ""
    fi
}

# ── Check 1: JSON validity ──────────────────────────────────────────────────
for ticket_dir in "$TRACKER_DIR"/*/; do
    [ -d "$ticket_dir" ] || continue
    ticket_id="$(basename "$ticket_dir")"

    for event_file in "$ticket_dir"*.json; do
        [ -f "$event_file" ] || continue
        filename="$(basename "$event_file")"
        # Skip dotfiles (.cache.json)
        [[ "$filename" == .* ]] && continue

        if ! python3 -c "
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        json.load(f)
except (json.JSONDecodeError, ValueError):
    sys.exit(1)
" "$event_file" 2>/dev/null; then
            echo "CORRUPT: $ticket_id/$filename — invalid JSON"
            issue_count=$((issue_count + 1))
        fi
    done
done

# ── Check 2: CREATE event presence ──────────────────────────────────────────
for ticket_dir in "$TRACKER_DIR"/*/; do
    [ -d "$ticket_dir" ] || continue
    ticket_id="$(basename "$ticket_dir")"

    # Use reducer to check for CREATE event
    reducer_output=""
    reducer_exit=0
    reducer_output=$(python3 "$SCRIPT_DIR/ticket-reducer.py" "$ticket_dir" 2>/dev/null) || reducer_exit=$?

    if [ "$reducer_exit" -ne 0 ]; then
        if [ -n "$reducer_output" ]; then
            status_check=$(python3 -c "
import json, sys
try:
    state = json.loads(sys.argv[1])
    print(state.get('status', 'unknown'))
except (json.JSONDecodeError, ValueError):
    print('parse_error')
" "$reducer_output" 2>/dev/null) || status_check="parse_error"

            if [ "$status_check" = "fsck_needed" ]; then
                echo "CORRUPT_CREATE: $ticket_id — CREATE event present but missing required fields (ticket_type or title)"
                issue_count=$((issue_count + 1))
            else
                echo "MISSING_CREATE: $ticket_id — no CREATE event found"
                issue_count=$((issue_count + 1))
            fi
        else
            echo "MISSING_CREATE: $ticket_id — no CREATE event found"
            issue_count=$((issue_count + 1))
        fi
    fi
done

# ── Check 3: Stale .git/index.lock cleanup ──────────────────────────────────
# git index.lock files are empty exclusive-access lock files; they do not
# contain PIDs. Age-based check only: older than 5 minutes → stale → remove.
tracker_git_dir=$(_resolve_tracker_git_dir)
if [ -n "$tracker_git_dir" ] && [ -f "$tracker_git_dir/index.lock" ]; then
    lock_file="$tracker_git_dir/index.lock"

    lock_age_stale=false
    if python3 -c "
import os, sys, time
try:
    mtime = os.path.getmtime(sys.argv[1])
    age = time.time() - mtime
    sys.exit(0 if age > 300 else 1)
except OSError:
    sys.exit(1)
" "$lock_file" 2>/dev/null; then
        lock_age_stale=true
    fi

    if [ "$lock_age_stale" = true ]; then
        if [ "${REBAR_FSCK_NO_MUTATE:-}" = "1" ]; then
            # Read-only invocation (e.g. an MCP server with REBAR_MCP_READONLY):
            # report the stale lock but do NOT remove it. Removing index.lock is
            # a git-state write and must not happen on a read-only surface.
            echo "WARN: stale .git/index.lock present (older than 5 minutes) — not removed (read-only)"
        else
            rm -f "$lock_file"
            echo "FIXED: removed stale .git/index.lock (older than 5 minutes)"
        fi
    else
        echo "WARN: .git/index.lock exists (younger than 5 minutes) — not removed"
    fi
fi

# ── Check 4: SNAPSHOT source_event_uuids consistency ────────────────────────
for ticket_dir in "$TRACKER_DIR"/*/; do
    [ -d "$ticket_dir" ] || continue
    ticket_id="$(basename "$ticket_dir")"

    for snapshot_file in "$ticket_dir"*-SNAPSHOT.json; do
        [ -f "$snapshot_file" ] || continue
        snapshot_filename="$(basename "$snapshot_file")"

        # Use python3 to check consistency; output issues to stdout, count to fd 3
        check4_count=0
        while IFS= read -r line; do
            echo "$line"
            check4_count=$((check4_count + 1))
        done < <(python3 -c "
import json, os, sys

snapshot_path = sys.argv[1]
ticket_dir = sys.argv[2]
ticket_id = sys.argv[3]
snapshot_filename = sys.argv[4]

try:
    with open(snapshot_path, encoding='utf-8') as f:
        snapshot = json.load(f)
except (json.JSONDecodeError, OSError):
    sys.exit(0)

source_uuids = snapshot.get('data', {}).get('source_event_uuids', [])
if not source_uuids:
    sys.exit(0)

# List all non-dotfile event files in the ticket dir (excluding this snapshot)
event_files = {}
for name in sorted(os.listdir(ticket_dir)):
    if not name.endswith('.json') or name.startswith('.'):
        continue
    if name == snapshot_filename:
        continue
    # Extract UUID from filename: <timestamp>-<uuid>-<TYPE>.json
    # The timestamp is digits only, followed by a dash, then the UUID
    parts = name.split('-', 1)
    if len(parts) < 2:
        continue
    rest = parts[1]  # <uuid>-<TYPE>.json
    # Remove the -<TYPE>.json suffix to get the UUID
    # TYPE is the last segment before .json
    rest_no_ext = rest.rsplit('.json', 1)[0]  # <uuid>-<TYPE>
    type_split = rest_no_ext.rsplit('-', 1)   # [<uuid>, <TYPE>]
    if len(type_split) < 2:
        continue
    file_uuid = type_split[0]
    event_files[file_uuid] = name

# Check 4a: source UUIDs that still exist as event files on disk
source_uuid_set = set(source_uuids)
for uuid in source_uuids:
    if uuid in event_files:
        print(f'SNAPSHOT_INCONSISTENT: {ticket_id}/{snapshot_filename} — source UUID {uuid} still exists as {event_files[uuid]}')

# Check 4b: orphan pre-snapshot events (sort before SNAPSHOT, not in source_event_uuids)
for file_uuid, name in event_files.items():
    if name < snapshot_filename and '-SNAPSHOT.json' not in name:
        if file_uuid not in source_uuid_set:
            print(f'ORPHAN_EVENT: {ticket_id}/{name} — pre-snapshot event not captured in source_event_uuids')
" "$snapshot_file" "$ticket_dir" "$ticket_id" "$snapshot_filename" 2>/dev/null || true)
        issue_count=$((issue_count + check4_count))
    done
done

# ── Check 4.5: push-pending (local ahead of origin/tickets) ──────────────────
# WS3 observability: push is best-effort, so a local commit with a failed/absent
# push silently diverges from origin. Surface that instead of staying silent.
# Informational only — does NOT increment issue_count (un-pushed work is not an
# integrity defect), so the exit code is unaffected. Compares against the LOCAL
# origin/tickets ref (no network fetch — keeps fsck side-effect-free).
if git -C "$TRACKER_DIR" remote get-url origin >/dev/null 2>&1 \
   && git -C "$TRACKER_DIR" rev-parse --verify origin/tickets >/dev/null 2>&1; then
    _push_ahead=$(git -C "$TRACKER_DIR" rev-list origin/tickets..HEAD --count 2>/dev/null || echo 0)
    if [ "${_push_ahead:-0}" -gt 0 ]; then
        echo "PUSH_PENDING: local tickets branch is ahead of origin/tickets by ${_push_ahead} commit(s) — push pending (run a ticket write to retry the push, or check connectivity to origin)"
    fi
fi

# ── Check 5: Summary ────────────────────────────────────────────────────────
if [ "$issue_count" -eq 0 ]; then
    echo "fsck complete: no issues found"
    exit 0
else
    echo "fsck complete: $issue_count issues found"
    exit 1
fi
