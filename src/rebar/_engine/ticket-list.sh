#!/usr/bin/env bash
# ticket-list.sh
# List all tickets by compiling each ticket directory via the reducer.
#
# Usage: ticket-list.sh [--output llm] [--include-archived] [--exclude-deleted] [--type=<type>] [--status=<status>] [--parent=<id>] [--has-tag=<tag>]
#   Outputs a JSON array of compiled ticket states to stdout (default).
#   --include-archived  Include archived tickets in the output (default: excluded).
#   --exclude-deleted   Exclude deleted (tombstone) tickets (default: included; opt-in).
#   --parent=<id>       Filter to direct children of <id> (matches parent_id field).
#   --has-tag=<tag>     Filter to tickets that have <tag> in their tags list.
#                       When <tag> matches ^detected_by:, automatically intersects
#                       with --type=bug (only bug-type tickets are returned).
#   --output llm  Outputs JSONL (one minified ticket per line) with shortened keys,
#                 stripped nulls/empty lists, and no verbose timestamps
#                 (created_at and env_id are omitted; comment timestamps omitted).
#                 Key mapping:
#                   ticket_id   → id
#                   ticket_type → t
#                   title       → ttl
#                   status      → st
#                   author      → au
#                   parent_id   → pid
#                   priority    → pr
#                   assignee    → asn
#                   comments    → cm (sub-keys: body→b, author→au)
#                   tags        → tg
#                   deps        → dp (sub-keys: target_id→tid, relation→r)
#   Errors go to stderr; exits 0 on success (even if some tickets have errors).
#   Empty tracker outputs [] (default) or nothing (llm format).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REDUCER="$SCRIPT_DIR/ticket-reducer.py"
# Canonical structured-output flag (--output/-o); logic in ticket_output.py.
# shellcheck source=/dev/null
source "$SCRIPT_DIR/ticket-output.sh"

# Allow tests to inject a custom tracker directory via TICKETS_TRACKER_DIR env var.
# When GIT_DIR is set (e.g., in tests), derive REPO_ROOT from its parent to avoid
# requiring an actual git repository at that path.
if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
    TRACKER_DIR="$TICKETS_TRACKER_DIR"
elif [ -n "${GIT_DIR:-}" ]; then
    REPO_ROOT="$(dirname "$GIT_DIR")"
    TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
else
    REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"
    TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
fi

# ── Parse arguments ──────────────────────────────────────────────────────────
# Resolve the canonical --output/-o flag (reader: json|llm, default json) and
# strip it; the loop below only handles list's own filters.
if ! _resolve_output_format reader "$@"; then exit 2; fi
format="default"
[ "$_OUTPUT_FMT" = "llm" ] && format="llm"
_strip_output_flags "$@"
set -- ${_OUTPUT_ARGS[@]+"${_OUTPUT_ARGS[@]}"}
include_archived=""
exclude_deleted_flag=""
filter_type=""
filter_status=""
filter_parent=""
filter_tag=""
filter_priority=""
filter_without_tag=""
for arg in "$@"; do
    case "$arg" in
        --include-archived)
            include_archived="true"
            ;;
        --exclude-deleted)
            exclude_deleted_flag="true"
            ;;
        --type=*)
            filter_type="${arg#--type=}"
            ;;
        --status=*)
            filter_status="${arg#--status=}"
            ;;
        --parent=*)
            filter_parent="${arg#--parent=}"
            ;;
        --has-tag=*)
            filter_tag="${arg#--has-tag=}"
            ;;
        --priority=*)
            filter_priority="${arg#--priority=}"
            ;;
        --without-tag=*)
            filter_without_tag="${arg#--without-tag=}"
            ;;
        --help|-h)
            echo "Usage: ticket-list.sh [--output llm] [--include-archived] [--exclude-deleted] [--type=<type>] [--status=<status>] [--priority=<n>] [--parent=<id>] [--has-tag=<tag>] [--without-tag=<tag>]" >&2
            echo "  --output llm       Output JSONL with shortened keys (-o llm)" >&2
            echo "  --include-archived  Include archived tickets" >&2
            echo "  --exclude-deleted   Exclude deleted (tombstone) tickets (default: included)" >&2
            echo "  --type=<type>      Filter by ticket type (bug, epic, story, task)" >&2
            echo "  --status=<status>  Filter by status (open, in_progress, closed; comma-separated for OR)" >&2
            echo "  --priority=<n>     Filter by priority 0-4 (comma-separated for OR; exact match;" >&2
            echo "                     tickets with no explicit priority are not matched)" >&2
            echo "  --parent=<id>      Filter to direct children of <id> (matches parent_id)" >&2
            echo "  --has-tag=<tag>    Filter to tickets with <tag> in their tags list (comma-separated for OR);" >&2
            echo "                     tags matching ^detected_by: auto-intersect with --type=bug" >&2
            echo "  --without-tag=<tag>  Exclude tickets having ANY of <tag> (comma-separated)" >&2
            exit 0
            ;;
        -*)
            echo "Error: unknown option '$arg'" >&2
            echo "Valid filters: --type --status --priority --parent --has-tag --without-tag --include-archived --exclude-deleted --output llm" >&2
            exit 1
            ;;
    esac
done

# --has-tag=detected_by:* auto-intersects with bug type (detected_by namespace is bug-only)
if [ -n "$filter_tag" ]; then
    case "$filter_tag" in
        detected_by:*)
            if [ -z "$filter_type" ]; then
                filter_type="bug"
            fi
            ;;
    esac
fi

# --priority accepts only integers 0-4 (comma-separated for OR); reject non-digits
# AND out-of-range values so an obvious mistake yields a clear error, not an empty list.
if [ -n "$filter_priority" ]; then
    case "$filter_priority" in
        *[!0-9,]*)
            echo "Error: --priority expects integer values 0-4 (comma-separated for OR), got '$filter_priority'" >&2
            exit 1
            ;;
    esac
    _pri_ifs=$IFS; IFS=','
    for _pri in $filter_priority; do
        case "$_pri" in
            ''|0|1|2|3|4) ;;
            *) echo "Error: --priority value '$_pri' out of range (expected 0-4)" >&2; IFS=$_pri_ifs; exit 1 ;;
        esac
    done
    IFS=$_pri_ifs
fi

# ── Validate ticket system is initialized ─────────────────────────────────────
if [ ! -d "$TRACKER_DIR" ]; then
    echo "Error: ticket system not initialized. Run 'ticket init' first." >&2
    exit 1
fi

# ── Assemble and output ────────────────────────────────────────────────────────
if [ "$format" = "llm" ]; then
    # LLM format: JSONL — one minified ticket per line, shortened keys, stripped nulls/empty lists,
    # and no verbose timestamps (created_at, env_id, and comment timestamps omitted).
    # Single-process: reduce → filter → to_llm (no subprocess pipeline).
    _TRACKER_DIR="$TRACKER_DIR" _INCLUDE_ARCHIVED="$include_archived" \
    _EXCLUDE_DELETED="$exclude_deleted_flag" \
    _TYPE_FILTER="$filter_type" _STATUS_FILTER="$filter_status" \
    _PARENT_FILTER="$filter_parent" _TAG_FILTER="$filter_tag" \
    _PRIORITY_FILTER="$filter_priority" _WITHOUT_TAG_FILTER="$filter_without_tag" \
    _SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import sys, os, json
sys.path.insert(0, os.environ['_SCRIPT_DIR'])
from ticket_reducer import reduce_all_tickets, apply_ticket_filters
from ticket_reducer.llm_format import to_llm
from ticket_reducer._present import public_state

tracker_dir = os.environ['_TRACKER_DIR']
include_archived = os.environ.get('_INCLUDE_ARCHIVED', '') == 'true'
exclude_deleted = os.environ.get('_EXCLUDE_DELETED', '') == 'true'
type_filter = os.environ.get('_TYPE_FILTER', '')
status_filter = os.environ.get('_STATUS_FILTER', '')
parent_filter = os.environ.get('_PARENT_FILTER', '')
tag_filter = os.environ.get('_TAG_FILTER', '')
priority_filter = os.environ.get('_PRIORITY_FILTER', '')
without_tag_filter = os.environ.get('_WITHOUT_TAG_FILTER', '')

if parent_filter:
    from ticket_resolver import resolve_ticket_id as _res_parent
    parent_filter = _res_parent(parent_filter, tracker_dir) or parent_filter

results = reduce_all_tickets(tracker_dir, exclude_archived=not include_archived, exclude_deleted=exclude_deleted)
results = apply_ticket_filters(
    results,
    type_filter=type_filter, status_filter=status_filter, parent_filter=parent_filter,
    tag_filter=tag_filter, priority_filter=priority_filter, without_tag_filter=without_tag_filter,
)
for t in results:
    print(json.dumps(to_llm(public_state(t)), ensure_ascii=False, separators=(',', ':')))
"
else
    # Default: JSON array — reduce, filter, and emit in a single process.
    # Also emit a passive aggregate health warning to stderr when unresolved bridge alerts exist.
    _TRACKER_DIR="$TRACKER_DIR" _INCLUDE_ARCHIVED="$include_archived" \
    _EXCLUDE_DELETED="$exclude_deleted_flag" \
    _TYPE_FILTER="$filter_type" _STATUS_FILTER="$filter_status" \
    _PARENT_FILTER="$filter_parent" _TAG_FILTER="$filter_tag" \
    _PRIORITY_FILTER="$filter_priority" _WITHOUT_TAG_FILTER="$filter_without_tag" \
    _SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import sys, os, json
sys.path.insert(0, os.environ['_SCRIPT_DIR'])
from ticket_reducer import reduce_all_tickets, apply_ticket_filters
from ticket_reducer._present import public_state

tracker_dir = os.environ['_TRACKER_DIR']
include_archived = os.environ.get('_INCLUDE_ARCHIVED', '') == 'true'
exclude_deleted = os.environ.get('_EXCLUDE_DELETED', '') == 'true'
type_filter = os.environ.get('_TYPE_FILTER', '')
status_filter = os.environ.get('_STATUS_FILTER', '')
parent_filter = os.environ.get('_PARENT_FILTER', '')
tag_filter = os.environ.get('_TAG_FILTER', '')
priority_filter = os.environ.get('_PRIORITY_FILTER', '')
without_tag_filter = os.environ.get('_WITHOUT_TAG_FILTER', '')

if parent_filter:
    from ticket_resolver import resolve_ticket_id as _res_parent
    parent_filter = _res_parent(parent_filter, tracker_dir) or parent_filter

results = reduce_all_tickets(tracker_dir, exclude_archived=not include_archived, exclude_deleted=exclude_deleted)
results = apply_ticket_filters(
    results,
    type_filter=type_filter, status_filter=status_filter, parent_filter=parent_filter,
    tag_filter=tag_filter, priority_filter=priority_filter, without_tag_filter=without_tag_filter,
)
print(json.dumps([public_state(t) for t in results], ensure_ascii=False))

alerted_count = sum(
    1 for t in results
    if any(not a.get('resolved', False) for a in t.get('bridge_alerts', []))
)
if alerted_count > 0:
    print(
        f'WARNING: {alerted_count} ticket(s) have unresolved bridge alerts. Run: ticket bridge-status for details.',
        file=sys.stderr,
    )
"
fi
