#!/usr/bin/env bash
# ticket-link.sh
# Write LINK and UNLINK events for the event-sourced ticket system.
#
# Usage:
#   ticket-link.sh link <id1> <id2> [<relation>]  — write LINK event in id1 dir
#   ticket-link.sh unlink <id1> <id2>            — write UNLINK event in id1 dir
#
# Relations supported: blocks, depends_on, relates_to, duplicates, supersedes
# For relates_to, a reciprocal LINK event is also written in id2 dir.
# duplicates and supersedes are directional — no reciprocal LINK is written.
# Idempotent: duplicate link (same target_id + relation) is a no-op (exits 0).
# Validates both ticket IDs exist; exits nonzero if not.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=${_PLUGIN_ROOT}/scripts/ticket-lib.sh
source "$SCRIPT_DIR/ticket-lib.sh"

REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"
TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
TRACKER_DIR=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$TRACKER_DIR")

# ── Usage ─────────────────────────────────────────────────────────────────────
_usage() {
    echo "Usage: ticket link <source_id> <target_id> [<relation>]" >&2
    echo "       ticket unlink <source_id> <target_id>" >&2
    echo "" >&2
    echo "  relation: blocks | depends_on | relates_to | duplicates | supersedes" >&2
    echo "  relates_to creates bidirectional LINK events in both ticket dirs" >&2
    echo "  duplicates and supersedes are directional (no reciprocal link)" >&2
    exit 1
}

# ── Validate ticket system is initialized ─────────────────────────────────────
_check_initialized() {
    if [ ! -f "$TRACKER_DIR/.env-id" ]; then
        echo "Error: ticket system not initialized. Run 'ticket init' first." >&2
        exit 1
    fi
}

# ── Validate ticket exists (has a directory in .tickets-tracker/) ─────────────
_check_ticket_exists() {
    local tid="$1"
    if [ ! -d "$TRACKER_DIR/$tid" ]; then
        echo "Error: ticket '$tid' does not exist" >&2
        exit 1
    fi
}

# ── Check if a LINK event already exists for the same (target_id, relation) ──
# Returns 0 if duplicate exists (link is net-active), 1 if no active duplicate found.
#
# A LINK/UNLINK/re-LINK sequence must allow the re-LINK to succeed: if a LINK
# was later cancelled by an UNLINK (identified by data.link_uuid matching the
# LINK event's uuid), the link is no longer active and re-linking is permitted.
# We build a net-effective link set by replaying all LINK and UNLINK events in
# filename order (lexicographic = chronological) before checking for duplicates.
_is_duplicate_link() {
    local source_id="$1"
    local target_id="$2"
    local relation="$3"

    local ticket_dir="$TRACKER_DIR/$source_id"
    if [ ! -d "$ticket_dir" ]; then
        return 1
    fi

    # Build net-effective link set: replay LINK and UNLINK events in order.
    # An UNLINK with data.link_uuid = <uuid> cancels the LINK with that uuid.
    local found
    found=$(python3 - "$ticket_dir" "$target_id" "$relation" <<'PYEOF'
import json, sys, pathlib

ticket_dir = sys.argv[1]
target_id = sys.argv[2]
relation = sys.argv[3]

p = pathlib.Path(ticket_dir)

# Collect all LINK and UNLINK event files, sorted chronologically.
all_events = []
for f in sorted(p.glob('*-LINK.json')):
    all_events.append(('LINK', f))
for f in sorted(p.glob('*-UNLINK.json')):
    all_events.append(('UNLINK', f))

# Re-sort by filename (basename) so LINK and UNLINK interleave in timestamp order.
# Sort key: (timestamp, event_type_order, full_name)
# - timestamp (first filename segment) preserves chronological order
# - event_type_order (LINK=0, UNLINK=1) guarantees LINK processes before UNLINK
#   when two events share the same second-level timestamp (different random UUIDs)
# - full name as final tiebreaker for stable ordering within same type+timestamp
_event_order = {'LINK': 0, 'UNLINK': 1}
all_events.sort(key=lambda x: (x[1].name.split('-')[0], _event_order.get(x[0], 99), x[1].name))

# Replay events to build net-active link set: maps uuid -> (target_id, relation)
active_links: dict[str, tuple[str, str]] = {}
cancelled_uuids: set[str] = set()

for event_type, f in all_events:
    try:
        with open(f, encoding='utf-8') as fh:
            ev = json.load(fh)
    except (json.JSONDecodeError, OSError):
        continue
    data = ev.get('data', {})
    uuid = ev.get('uuid', '')
    if event_type == 'LINK':
        if uuid:
            active_links[uuid] = (data.get('target_id', data.get('target', '')), data.get('relation', ''))
    elif event_type == 'UNLINK':
        link_uuid = data.get('link_uuid', '')
        if link_uuid:
            cancelled_uuids.add(link_uuid)
            active_links.pop(link_uuid, None)

# Check if (target_id, relation) pair is net-active from LINK files
for uuid, (tid, rel) in active_links.items():
    if tid == target_id and rel == relation:
        print('DUPLICATE')
        sys.exit(0)

# ── SNAPSHOT fallback (f5a8) ──────────────────────────────────────────────────
# ticket-compact.sh bakes LINK events into a SNAPSHOT compiled_state.deps[] and
# deletes the source *-LINK.json files.  Fall back to scanning SNAPSHOT deps[]
# for a matching (target_id, relation) entry whose link_uuid has not been
# cancelled by a post-compaction UNLINK event.
for snap_path in sorted(p.glob('*-SNAPSHOT.json')):
    try:
        with open(snap_path, encoding='utf-8') as fh:
            snap = json.load(fh)
    except (json.JSONDecodeError, OSError):
        continue
    compiled = snap.get('data', {}).get('compiled_state', {})
    for dep in compiled.get('deps', []):
        dep_target = dep.get('target_id', '')
        dep_uuid = dep.get('link_uuid', '')
        dep_relation = dep.get('relation', '')
        if dep_target == target_id and dep_relation == relation and dep_uuid and dep_uuid not in cancelled_uuids:
            print('DUPLICATE')
            sys.exit(0)

print('NOT_FOUND')
sys.exit(0)
PYEOF
) || return 1

    if [ "$found" = "DUPLICATE" ]; then
        return 0
    fi
    return 1
}

# ── Write a LINK event file ───────────────────────────────────────────────────
_write_link_event() {
    local source_id="$1"
    local target_id="$2"
    local relation="$3"

    _check_initialized
    _check_ticket_exists "$source_id"
    _check_ticket_exists "$target_id"

    # Guard: cannot write any LINK event for a closed source ticket
    # A closed ticket is frozen — adding new dependency/relation events to it
    # bypasses the closed-ticket invariant and can introduce children after close.
    # Fail-open: if ticket_read_status fails (reducer unavailable or old format),
    # source_status will be empty and we allow the link rather than blocking valid
    # operations due to a transient read failure.
    local source_status
    source_status=$(ticket_read_status "$TRACKER_DIR" "$source_id" 2>/dev/null) || source_status=""
    if [ -n "$source_status" ] && [ "$source_status" = "closed" ]; then
        echo "Error: cannot create $relation link — source ticket '$source_id' is closed. Reopen it first with: ticket transition $source_id closed open" >&2
        exit 1
    fi

    # Guard: depends_on to a closed ticket is not allowed
    if [ "$relation" = "depends_on" ]; then
        local target_status
        # Fail-open: if ticket_read_status fails (e.g., reducer unavailable or old
        # ticket format), target_status will be empty and we allow the link rather
        # than blocking valid operations due to a transient read failure.
        target_status=$(ticket_read_status "$TRACKER_DIR" "$target_id" 2>/dev/null) || target_status=""
        if [ -n "$target_status" ] && [ "$target_status" = "closed" ]; then
            echo "Error: cannot create depends_on link — target ticket '$target_id' is closed" >&2
            exit 1
        fi
    fi

    # Idempotency: skip if same (target_id, relation) pair already exists
    if _is_duplicate_link "$source_id" "$target_id" "$relation"; then
        return 0
    fi

    local env_id
    env_id=$(cat "$TRACKER_DIR/.env-id")
    local author
    author=$(git config user.name 2>/dev/null || echo "Unknown")

    local temp_event
    temp_event=$(mktemp "$TRACKER_DIR/.tmp-link-XXXXXX")

    python3 -c "
import json, sys, time, uuid

event = {
    'event_type': 'LINK',
    'timestamp': time.time_ns(),
    'uuid': str(uuid.uuid4()),
    'env_id': sys.argv[1],
    'author': sys.argv[2],
    'data': {
        'relation': sys.argv[3],
        'target_id': sys.argv[4],
    },
}

with open(sys.argv[5], 'w', encoding='utf-8') as f:
    json.dump(event, f, ensure_ascii=False)
" "$env_id" "$author" "$relation" "$target_id" "$temp_event" || {
        rm -f "$temp_event"
        echo "Error: failed to build LINK event JSON" >&2
        exit 1
    }

    write_commit_event "$source_id" "$temp_event" || {
        rm -f "$temp_event"
        echo "Error: failed to write and commit LINK event" >&2
        exit 1
    }

    rm -f "$temp_event"
}

# ── Get the relation of the most recent active LINK event for a (source, target) pair ─
# Outputs "<uuid> <relation>" or empty string if not found or link is net-inactive.
#
# Replays both LINK and UNLINK events chronologically (same pattern as
# _is_duplicate_link) so that an UNLINK cancelling a prior LINK causes this
# function to return empty output — making downstream guards in _write_unlink_event
# detect already-unlinked pairs cleanly.
_get_link_info() {
    local source_id="$1"
    local target_id="$2"

    python3 - "$TRACKER_DIR/$source_id" "$target_id" <<'PYEOF'
import json, sys, pathlib

ticket_dir = sys.argv[1]
target_id = sys.argv[2]

p = pathlib.Path(ticket_dir)

# Collect all LINK and UNLINK event files, sorted chronologically.
all_events = []
for f in sorted(p.glob('*-LINK.json')):
    all_events.append(('LINK', f))
for f in sorted(p.glob('*-UNLINK.json')):
    all_events.append(('UNLINK', f))

# Re-sort by filename (basename) so LINK and UNLINK interleave in timestamp order.
# Sort key: (timestamp, event_type_order, full_name)
# - timestamp (first filename segment) preserves chronological order
# - event_type_order (LINK=0, UNLINK=1) guarantees LINK processes before UNLINK
#   when two events share the same second-level timestamp (different random UUIDs)
# - full name as final tiebreaker for stable ordering within same type+timestamp
_event_order = {'LINK': 0, 'UNLINK': 1}
all_events.sort(key=lambda x: (x[1].name.split('-')[0], _event_order.get(x[0], 99), x[1].name))

# Replay events to build net-active link set: maps uuid -> (target_id, relation)
active_links: dict[str, tuple[str, str]] = {}
# Collect cancelled uuids (from UNLINK events) for the SNAPSHOT fallback below.
cancelled_uuids: set[str] = set()

for event_type, f in all_events:
    try:
        with open(f, encoding='utf-8') as fh:
            ev = json.load(fh)
    except (json.JSONDecodeError, OSError):
        continue
    data = ev.get('data', {})
    uuid = ev.get('uuid', '')
    if event_type == 'LINK':
        if uuid:
            active_links[uuid] = (data.get('target_id', data.get('target', '')), data.get('relation', ''))
    elif event_type == 'UNLINK':
        link_uuid = data.get('link_uuid', '')
        if link_uuid:
            cancelled_uuids.add(link_uuid)
            active_links.pop(link_uuid, None)

# Return the uuid and relation for the net-active link matching target_id, if any.
# Iterate in insertion order (Python 3.7+) to return the most recent active link.
found_uuid = ''
found_relation = ''
for uuid, (tid, rel) in active_links.items():
    if tid == target_id:
        found_uuid = uuid
        found_relation = rel

if found_uuid:
    print(found_uuid + ' ' + found_relation)
    sys.exit(0)

# ── SNAPSHOT fallback (f5a8) ──────────────────────────────────────────────────
# ticket-compact.sh bakes LINK events into a SNAPSHOT compiled_state.deps[] and
# deletes the original *-LINK.json files.  When no active LINK file was found
# above, scan any *-SNAPSHOT.json for a matching dep entry.  A link that was
# cancelled post-compaction will have an UNLINK event on disk (not compacted away
# because UNLINKs are written after the SNAPSHOT) — subtract those via
# cancelled_uuids before trusting a SNAPSHOT dep.
for snap_path in sorted(p.glob('*-SNAPSHOT.json')):
    try:
        with open(snap_path, encoding='utf-8') as fh:
            snap = json.load(fh)
    except (json.JSONDecodeError, OSError):
        continue
    compiled = snap.get('data', {}).get('compiled_state', {})
    for dep in compiled.get('deps', []):
        dep_target = dep.get('target_id', '')
        dep_uuid = dep.get('link_uuid', '')
        dep_relation = dep.get('relation', '')
        if dep_target == target_id and dep_uuid and dep_uuid not in cancelled_uuids:
            print(dep_uuid + ' ' + dep_relation)
            sys.exit(0)

sys.exit(0)
PYEOF
}

# ── Write an UNLINK event file ────────────────────────────────────────────────
_write_unlink_event() {
    local source_id="$1"
    local target_id="$2"

    _check_initialized
    _check_ticket_exists "$source_id"
    _check_ticket_exists "$target_id"

    # Find the most recent LINK event for this target in source_id dir
    local link_info link_uuid link_relation
    link_info=$(_get_link_info "$source_id" "$target_id") || link_info=""
    link_uuid="${link_info%% *}"
    link_relation="${link_info##* }"

    if [ -z "$link_uuid" ]; then
        echo "Error: no LINK event found in '$source_id' targeting '$target_id'" >&2
        exit 1
    fi

    # Guard: verify the link is currently net-active (not already cancelled by a prior UNLINK).
    # _is_duplicate_link returns 0 (true) if the (target_id, relation) pair is still active.
    if ! _is_duplicate_link "$source_id" "$target_id" "$link_relation"; then
        echo "Error: no active link found between '$source_id' and '$target_id'" >&2
        exit 1
    fi

    local env_id
    env_id=$(cat "$TRACKER_DIR/.env-id")
    local author
    author=$(git config user.name 2>/dev/null || echo "Unknown")

    local temp_event
    temp_event=$(mktemp "$TRACKER_DIR/.tmp-unlink-XXXXXX")

    python3 -c "
import json, sys, time, uuid

event = {
    'event_type': 'UNLINK',
    'timestamp': time.time_ns(),
    'uuid': str(uuid.uuid4()),
    'env_id': sys.argv[1],
    'author': sys.argv[2],
    'data': {
        'link_uuid': sys.argv[3],
        'target_id': sys.argv[4],
    },
}

with open(sys.argv[5], 'w', encoding='utf-8') as f:
    json.dump(event, f, ensure_ascii=False)
" "$env_id" "$author" "$link_uuid" "$target_id" "$temp_event" || {
        rm -f "$temp_event"
        echo "Error: failed to build UNLINK event JSON" >&2
        exit 1
    }

    write_commit_event "$source_id" "$temp_event" || {
        rm -f "$temp_event"
        echo "Error: failed to write and commit UNLINK event" >&2
        exit 1
    }

    rm -f "$temp_event"
}

# ── Dry-run preview for a link operation ─────────────────────────────────────
# Resolves hierarchy and prints what would happen without writing any events.
# Exits 0 in all non-error cases (promotion, rejection, or normal creation).
_dry_run_link() {
    local source_id="$1"
    local target_id="$2"
    local relation="$3"

    _check_initialized
    _check_ticket_exists "$source_id"
    _check_ticket_exists "$target_id"

    # Resolve hierarchy via ticket-graph.py CLI
    local result_json
    result_json=$(python3 "$SCRIPT_DIR/ticket-graph.py" resolve-hierarchy-link \
        "$source_id" "$target_id" --tickets-dir="$TRACKER_DIR" 2>/dev/null) || {
        # Fallback: hierarchy resolver unavailable; show plain preview
        echo "[DRY RUN] Would create: $source_id $relation $target_id (no event written)"
        return 0
    }

    local was_redirected is_redundant resolved_source resolved_target
    was_redirected=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('was_redirected', False))" "$result_json" 2>/dev/null) || was_redirected="False"
    is_redundant=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('is_redundant', False))" "$result_json" 2>/dev/null) || is_redundant="False"
    resolved_source=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('resolved_source', sys.argv[2]))" "$result_json" "$source_id" 2>/dev/null) || resolved_source="$source_id"
    resolved_target=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('resolved_target', sys.argv[2]))" "$result_json" "$target_id" 2>/dev/null) || resolved_target="$target_id"

    if [ "$is_redundant" = "True" ]; then
        echo "[DRY RUN] Would reject: $source_id $relation $target_id — redundant link (direct child) (no event written)"
    elif [ "$was_redirected" = "True" ]; then
        echo "[DRY RUN] Would promote: $resolved_source $relation $resolved_target (no event written)"
    else
        echo "[DRY RUN] Would create: $source_id $relation $target_id (no event written)"
    fi
}

# ── Main dispatch ─────────────────────────────────────────────────────────────
if [ $# -lt 1 ]; then
    _usage
fi

subcommand="$1"
shift

# Parse --dry-run flag from remaining args (position-independent)
DRY_RUN=0
remaining_args=()
for arg in "$@"; do
    if [ "$arg" = "--dry-run" ]; then
        DRY_RUN=1
    else
        remaining_args+=("$arg")
    fi
done
set -- "${remaining_args[@]+"${remaining_args[@]}"}"

case "$subcommand" in
    link)
        if [ $# -lt 2 ]; then
            _usage
        fi
        id1=$(TICKETS_TRACKER_DIR="$TRACKER_DIR" resolve_ticket_id "$1") || exit 1
        id2=$(TICKETS_TRACKER_DIR="$TRACKER_DIR" resolve_ticket_id "$2") || exit 1
        relation="${3:-relates_to}"

        # Validate relation
        case "$relation" in
            blocks|depends_on|relates_to|duplicates|supersedes) ;;
            child|parent)
                echo "Error: '$relation' is not a link relation. Parent-child relationships are" >&2
                echo "  established at creation time via: ticket create <type> <title> --parent=<id>" >&2
                echo "  'ticket link' is for dependency links: blocks, depends_on, relates_to, duplicates, supersedes" >&2
                exit 1
                ;;
            *)
                echo "Error: invalid relation '$relation'. Must be one of: blocks, depends_on, relates_to, duplicates, supersedes" >&2
                exit 1
                ;;
        esac

        if [ "$DRY_RUN" = "1" ]; then
            _dry_run_link "$id1" "$id2" "$relation"
            exit 0
        fi

        _write_link_event "$id1" "$id2" "$relation"

        # For relates_to: also write reciprocal LINK in id2 dir
        if [ "$relation" = "relates_to" ]; then
            _write_link_event "$id2" "$id1" "$relation"
        fi
        ;;

    unlink)
        if [ $# -lt 2 ]; then
            _usage
        fi
        id1=$(TICKETS_TRACKER_DIR="$TRACKER_DIR" resolve_ticket_id "$1") || exit 1
        id2=$(TICKETS_TRACKER_DIR="$TRACKER_DIR" resolve_ticket_id "$2") || exit 1

        # Look up the relation before unlinking to detect relates_to (bidirectional)
        link_info=$(_get_link_info "$id1" "$id2") || link_info=""
        link_relation="${link_info##* }"

        _write_unlink_event "$id1" "$id2"

        # For relates_to: also write reciprocal UNLINK in id2 dir.
        # Best-effort: if no reciprocal LINK exists (orphan state — one side was
        # compacted away or was never written), warn and continue. The primary
        # unlink on id1 already succeeded; failing for a missing reciprocal would
        # block the user from clearing an already-broken link state.
        if [ "$link_relation" = "relates_to" ]; then
            reciprocal_info=$(_get_link_info "$id2" "$id1") || reciprocal_info=""
            if [ -n "$reciprocal_info" ]; then
                _write_unlink_event "$id2" "$id1"
            else
                echo "Warning: no reciprocal LINK found in '$id2' targeting '$id1' — orphaned link, removed from '$id1' only" >&2
            fi
        fi
        ;;

    *)
        echo "Error: unknown subcommand '$subcommand'. Use 'link' or 'unlink'." >&2
        _usage
        ;;
esac

exit 0
