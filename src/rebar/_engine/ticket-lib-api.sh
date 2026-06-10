#!/usr/bin/env bash
# ticket-lib-api.sh
# Sourceable library exposing in-process implementations of ticket subcommands.
# Replaces per-call `exec bash ticket-<cmd>.sh` subprocesses from the dispatcher.
#
# SOURCEABILITY CONTRACT (strict):
#   - No file-scope `set -euo pipefail` (would leak into caller).
#   - No file-scope `exit` (would kill caller).
#   - No file-scope `trap` (would clobber caller traps).
#   - No file-scope mutation of GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE / GIT_COMMON_DIR.
#   - Functions use `ticket_` / `_ticketlib_` namespace.
#   - Idempotent source guard (re-sourcing is a no-op).

# ── Source guard ─────────────────────────────────────────────────────────────
if declare -f _ticketlib_dispatch >/dev/null 2>&1; then
    return 0 2>/dev/null
fi

# Resolve library directory (used to find sibling scripts + python package).
_TICKETLIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Canonical structured-output flag helpers (--output/-o). The format logic lives
# once in ticket_output.py; this shim is sourced for _resolve_output_format /
# _strip_output_flags used by ticket_show / ticket_list below.
# shellcheck source=/dev/null
source "$_TICKETLIB_DIR/ticket-output.sh"

# ── Platform capability detection ────────────────────────────────────────────
# Detect flock(1) availability at source-time so callers and internal functions
# can branch without repeated command -v calls.
command -v flock >/dev/null 2>&1 && _ticketlib_has_flock=1 || _ticketlib_has_flock=0

# ── Short ID resolver ────────────────────────────────────────────────────────
# _ticketlib_resolve_short_id <input> <tracker_dir>
# If <input> is an 8-hex short ID (xxxx-xxxx), scan <tracker_dir> for a
# unique matching full 16-hex directory and echo it. Otherwise echo <input>.
# Callers must reassign: ticket_id="$(_ticketlib_resolve_short_id "$ticket_id" "$TRACKER_DIR")"
_ticketlib_resolve_short_id() {
    local _input="$1" _tracker="$2"
    if [[ "$_input" =~ ^[a-z0-9]{4}-[a-z0-9]{4}$ ]]; then
        local _matches=()
        # Bug 19a3-03ca: delegate the scan to ticket-alias-resolve.py --mode=8hex
        # (one Python process instead of ~20K basename subprocesses). Best-effort:
        # bash fallback runs on either helper unavailable OR helper exit !=0;
        # in both cases a stderr warning is emitted on failure so the cause
        # is observable. Unified error semantics with resolve_ticket_id in
        # ticket-lib.sh (both fall back on helper failure).
        local _resolver_short _used_helper=0
        _resolver_short="$_TICKETLIB_DIR/ticket-alias-resolve.py"
        if [ -n "${_TICKETLIB_DIR:-}" ] && [ -f "$_resolver_short" ] && command -v python3 >/dev/null 2>&1; then
            local _short_out _short_rc=0
            _short_out=$(python3 "$_resolver_short" --mode=8hex "$_input" "$_tracker" 2>/dev/null) || _short_rc=$?
            if [ "$_short_rc" -eq 0 ]; then
                _used_helper=1
                if [ -n "$_short_out" ]; then
                    local _short_line
                    while IFS= read -r _short_line; do
                        [ -z "$_short_line" ] && continue
                        if [[ "$_short_line" =~ ^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$ ]]; then
                            _matches+=("$_short_line")
                        fi
                    done <<< "$_short_out"
                fi
            else
                echo "Warning: 8-hex resolver helper exited $_short_rc for '$_input' — falling back to bash scan" >&2
            fi
        fi
        if [ "$_used_helper" -eq 0 ]; then
            # Fallback bash scan (param expansion — no fork per entry)
            local _entry _base
            while IFS= read -r -d '' _entry; do
                _base="${_entry##*/}"
                if [[ "${_base:0:9}" == "$_input" ]] && \
                   [[ "$_base" =~ ^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$ ]]; then
                    _matches+=("$_base")
                fi
            done < <(find -L "$_tracker" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -print0 2>/dev/null)
        fi
        if [ "${#_matches[@]}" -eq 1 ]; then
            echo "${_matches[0]}"
            return 0
        fi
    fi
    echo "$_input"
}

# ── Full ID resolver ─────────────────────────────────────────────────────────
# _ticketlib_resolve_id <input> <tracker_dir>
# Resolves any supported ticket-ID form (16-hex full, 8-hex short, jira_key,
# alias, or unique prefix >= 4 chars) to the canonical ticket directory name.
# Prints the canonical ID to stdout; returns 0 on success, 1 on miss/ambiguous.
#
# Resolution order (cheapest first):
#   1. 16-hex full ID: passthrough if directory exists.
#   2. 8-hex short ID: delegate to _ticketlib_resolve_short_id (scans tracker).
#   3. All other forms (alias, jira_key, prefix): delegate to resolve_ticket_id
#      from ticket-lib.sh, which handles the full pipeline.
#
# Callers must reassign and check rc:
#     if ! ticket_id="$(_ticketlib_resolve_id "$ticket_id" "$TRACKER_DIR")"; then
#         return 1
#     fi
_ticketlib_resolve_id() {
    local _input="$1" _tracker="$2"

    if [ -z "$_input" ]; then
        echo "Error: ticket id must be non-empty" >&2
        return 1
    fi

    # Step 1: 16-hex full ID passthrough
    if [[ "$_input" =~ ^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$ ]]; then
        if [ -d "$_tracker/$_input" ]; then
            echo "$_input"
            return 0
        fi
        echo "Error: ticket '$_input' not found" >&2
        return 1
    fi

    # Step 2: 8-hex short ID — fast scan via existing helper
    if [[ "$_input" =~ ^[a-z0-9]{4}-[a-z0-9]{4}$ ]]; then
        local _resolved
        _resolved="$(_ticketlib_resolve_short_id "$_input" "$_tracker")"
        if [ "$_resolved" != "$_input" ] && [ -d "$_tracker/$_resolved" ]; then
            echo "$_resolved"
            return 0
        fi
        # Direct dir match (legacy / planted short IDs)
        if [ -d "$_tracker/$_input" ]; then
            echo "$_input"
            return 0
        fi
        echo "Error: ticket '$_input' not found" >&2
        return 1
    fi

    # Step 3: alias, jira_key, or prefix — delegate to ticket-lib.sh
    if [ -z "${_TICKETLIB_DIR:-}" ]; then
        echo "Error: _TICKETLIB_DIR is not set; cannot resolve ticket alias" >&2
        return 1
    fi
    declare -f resolve_ticket_id >/dev/null 2>&1 || source "$_TICKETLIB_DIR/ticket-lib.sh"

    local _resolved _rc=0
    _resolved="$(TICKETS_TRACKER_DIR="$_tracker" resolve_ticket_id "$_input")" || _rc=$?
    if [ "$_rc" -ne 0 ] || [ -z "$_resolved" ]; then
        # resolve_ticket_id already emitted a specific error to stderr.
        return 1
    fi
    echo "$_resolved"
    return 0
}

# ── Dispatch helper ──────────────────────────────────────────────────────────
# Wraps each call in a subshell so per-call set -e / traps / var mutations
# cannot leak back into the caller's shell state.
_ticketlib_dispatch() {
    local op="$1"
    shift
    ( "$op" "$@" )
}

# ── ticket_show ──────────────────────────────────────────────────────────────
# In-process replacement for ticket-show.sh.
# Uses bash+jq event reduction — zero python3 subprocess spawns on the default path.
ticket_show() {

    # Resolve the canonical --output/-o flag ONCE (reader profile: json|llm,
    # default json) via the single source of truth (ticket_output.py), then strip
    # it so neither the multi-ID scan nor the single-ID body re-parses format.
    if ! _resolve_output_format reader "$@"; then return 2; fi
    local _show_fmt="$_OUTPUT_FMT"
    _strip_output_flags "$@"
    set -- ${_OUTPUT_ARGS[@]+"${_OUTPUT_ARGS[@]}"}

    # Multi-ID support (bug jira-dig-2565): if more than one positional ID is
    # supplied, iterate and recurse single-ID for each, threading the resolved
    # format (re-injected as `--output=<fmt>`) and any other flags. Default/json
    # output is separated by a blank line between tickets; llm output is one
    # self-delimiting JSON object per line (NDJSON) and needs no separator.
    # The function returns 1 if any single-ID call failed, after processing
    # all tickets so callers can scan the full output.
    local _ms_flag_args=()
    local _ms_ids=()
    local _ms_arg
    for _ms_arg in "$@"; do
        case "$_ms_arg" in
            -*) _ms_flag_args+=("$_ms_arg") ;;
            *)  _ms_ids+=("$_ms_arg") ;;
        esac
    done
    if [ "${#_ms_ids[@]}" -gt 1 ]; then
        local _ms_idx=0 _ms_rc=0 _ms_id
        for _ms_id in "${_ms_ids[@]}"; do
            _ms_idx=$((_ms_idx + 1))
            if [ "$_ms_idx" -gt 1 ] && [ "$_show_fmt" != "llm" ]; then
                echo
            fi
            ticket_show --output="$_show_fmt" \
                ${_ms_flag_args[@]+"${_ms_flag_args[@]}"} "$_ms_id" || _ms_rc=1
        done
        return "$_ms_rc"
    fi

    # Run the body with strict mode scoped to this function via a subshell.
    (
        set -euo pipefail

        # Unset git hook env vars so git commands target the correct repo.
        # Scoped to this subshell — does not leak to caller.
        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            # (empty REPO_ROOT → TRACKER_DIR="/.tickets-tracker" → file ops fail downstream with clear errors).
            # A hard return 1 here breaks callers (e.g., test setups using isolated $tmp repos) that supply
            # GIT_DIR directly or rely on subshell fall-through. GIT_DISCOVERY_ACROSS_FILESYSTEM=1 is the
            # real fix for alpine volume-mount git discovery; the empty-guard adds no safety value.
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi

        _usage() {
            echo "Usage: ticket show [--output llm] <ticket_id> [<ticket_id> ...]" >&2
            return 1
        }

        # Format already resolved + stripped by the caller (reader: json|llm).
        # json is the default pretty rendering; llm is the minified short-key form.
        local format="$_show_fmt"
        local ticket_id=""
        local arg
        for arg in "$@"; do
            case "$arg" in
                -*)
                    echo "Error: unknown option '$arg'" >&2
                    _usage
                    return 1
                    ;;
                *)
                    if [ -z "$ticket_id" ]; then
                        ticket_id="$arg"
                    fi
                    ;;
            esac
        done

        if [ -z "$ticket_id" ]; then
            _usage
            return 1
        fi

        # Resolve any supported ID form (16-hex, 8-hex, jira_key, alias, prefix)
        # to a canonical directory name. On miss, emit JSON to stdout for the
        # orchestrator's `json.load` pattern + a free-form line to stderr.
        local _raw_input="$ticket_id"
        local _resolved
        if ! _resolved="$(_ticketlib_resolve_id "$ticket_id" "$TRACKER_DIR" 2>/dev/null)"; then
            # JSON to stdout (parseable by callers).
            local _esc_input
            _esc_input="$(printf '%s' "$_raw_input" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null)"
            if [ -z "$_esc_input" ]; then
                _esc_input="\"$_raw_input\""
            fi
            printf '{"error": "ticket_not_found", "input": %s, "message": "Ticket %s not found"}\n' \
                "$_esc_input" "'$_raw_input'"
            # Free-form line to stderr (preserves existing test compatibility).
            echo "Error: Ticket '$_raw_input' not found" >&2
            return 1
        fi
        ticket_id="$_resolved"

        if [ ! -d "$TRACKER_DIR/$ticket_id" ]; then
            # Defensive: should not happen since _ticketlib_resolve_id verifies dir.
            local _esc_input
            _esc_input="$(printf '%s' "$_raw_input" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null)"
            if [ -z "$_esc_input" ]; then
                _esc_input="\"$_raw_input\""
            fi
            printf '{"error": "ticket_not_found", "input": %s, "message": "Ticket %s not found"}\n' \
                "$_esc_input" "'$_raw_input'"
            echo "Error: Ticket '$_raw_input' not found" >&2
            return 1
        fi

        local TICKET_DIR="$TRACKER_DIR/$ticket_id"

        # ── Reduce via the single source of truth: the Python ticket_reducer ──
        # Replaces the former in-bash jq reducer + separate preconditions scan,
        # which had drifted from the Python reducer (bug f026). reduce_ticket
        # computes the FULL compiled state (including preconditions_summary) and
        # uses the .cache.json fast-path; public_state strips internal-only keys
        # (parent_status_uuid, last_status_env_id, preconditions_summary.source_count)
        # so the interface output matches list/search exactly.
        local state
        state=$(_SCRIPT_DIR="$_TICKETLIB_DIR" python3 -c '
import sys, os, json
sys.path.insert(0, os.environ["_SCRIPT_DIR"])
from ticket_reducer import reduce_ticket
from ticket_reducer._present import public_state
print(json.dumps(public_state(reduce_ticket(sys.argv[1])), ensure_ascii=False))
' "$TICKET_DIR" 2>/dev/null) || true
        if [ -z "$state" ]; then
            echo "Error: failed to reduce ticket \"$ticket_id\"" >&2
            return 1
        fi

        # Verify CREATE/SNAPSHOT was present (ticket_type non-null after reduction)
        local _ttype
        _ttype=$(printf '%s' "$state" | jq -r '.ticket_type // empty' 2>/dev/null)
        if [ -z "$_ttype" ]; then
            echo "Error: ticket \"$ticket_id\" has no CREATE or SNAPSHOT event" >&2
            return 1
        fi

        # ── Output ────────────────────────────────────────────────────────────
        if [ "$format" = "llm" ]; then
            # LLM format via the single shared formatter ticket_reducer.llm_format
            # to_llm (same one `list`/`ready --output llm` use), so show and list
            # emit identical LLM shapes (bug f026 — the old inline jq mapping had
            # drifted from to_llm). `state` is already public_state-filtered.
            printf '%s' "$state" | _SCRIPT_DIR="$_TICKETLIB_DIR" python3 -c '
import sys, os, json
sys.path.insert(0, os.environ["_SCRIPT_DIR"])
from ticket_reducer.llm_format import to_llm
print(json.dumps(to_llm(json.load(sys.stdin)), ensure_ascii=False, separators=(",", ":")))
' 2>/dev/null
        else
            # Default format: pretty-printed JSON
            printf '%s' "$state" | jq '.' 2>/dev/null
            # Emit bridge-alert warning to stderr (mirrors ticket-show.sh behaviour)
            local _unresolved
            _unresolved=$(printf '%s' "$state" | \
                          jq '[.bridge_alerts[] | select(.resolved == false)] | length' \
                          2>/dev/null) || _unresolved=0
            if [ "${_unresolved:-0}" -gt 0 ]; then
                printf 'WARNING: ticket %s has %s unresolved bridge alert(s). Run: ticket bridge-status for details.\n' \
                    "$ticket_id" "$_unresolved" >&2
            fi
        fi
    )
}

# ── ticket_list ──────────────────────────────────────────────────────────────
# In-process replacement for ticket-list.sh.
ticket_list() {

    # Resolve the canonical --output/-o flag (reader profile: json|llm, default
    # json) via the single source of truth, then strip it; the option loop below
    # only handles list's own filters. json emits a JSON array; llm emits JSONL.
    if ! _resolve_output_format reader "$@"; then return 2; fi
    local _list_fmt="$_OUTPUT_FMT"
    _strip_output_flags "$@"
    set -- ${_OUTPUT_ARGS[@]+"${_OUTPUT_ARGS[@]}"}

    # Run the body with strict mode scoped to this function via a subshell.
    (
        set -euo pipefail

        # Unset git hook env vars so git commands target the correct repo.
        # Scoped to this subshell — does not leak to caller.
        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        elif [ -n "${GIT_DIR:-}" ]; then
            local REPO_ROOT
            REPO_ROOT="$(dirname "$GIT_DIR")"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        else
            local REPO_ROOT
            # (empty REPO_ROOT → TRACKER_DIR="/.tickets-tracker" → file ops fail downstream with clear errors).
            # A hard return 1 here breaks callers (e.g., test setups using isolated $tmp repos) that supply
            # GIT_DIR directly or rely on subshell fall-through. GIT_DISCOVERY_ACROSS_FILESYSTEM=1 is the
            # real fix for alpine volume-mount git discovery; the empty-guard adds no safety value.
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi

        local format="$_list_fmt"
        local include_archived=""
        local exclude_deleted_flag=""
        local filter_type=""
        local filter_status=""
        local filter_parent=""
        local filter_tag=""
        local filter_priority=""
        local filter_without_tag=""
        local arg
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
                    echo "Usage: ticket list [--output llm] [--include-archived] [--exclude-deleted] [--type=<type>] [--status=<status>] [--priority=<n>] [--parent=<id>] [--has-tag=<tag>] [--without-tag=<tag>]" >&2
                    echo "  --type=<type>      Filter by ticket type (bug, epic, story, task)" >&2
                    echo "  --status=<status>  Filter by status (comma-separated for OR)" >&2
                    echo "  --priority=<n>     Filter by priority 0-4 (comma-separated for OR; exact match; unset priority not matched)" >&2
                    echo "  --parent=<id>      Filter to direct children of <id>" >&2
                    echo "  --has-tag=<tag>    Filter to tickets having <tag> (comma-separated for OR);" >&2
                    echo "                     tags matching ^detected_by: auto-intersect with --type=bug" >&2
                    echo "  --without-tag=<tag>  Exclude tickets having ANY of <tag> (comma-separated)" >&2
                    return 0
                    ;;
                -*)
                    echo "Error: unknown option '$arg'" >&2
                    echo "Valid filters: --type --status --priority --parent --has-tag --without-tag --include-archived --exclude-deleted --output llm" >&2
                    return 1
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
                    return 1
                    ;;
            esac
            local _pri_ifs=$IFS _pri
            IFS=','
            for _pri in $filter_priority; do
                case "$_pri" in
                    ''|0|1|2|3|4) ;;
                    *) echo "Error: --priority value '$_pri' out of range (expected 0-4)" >&2; IFS=$_pri_ifs; return 1 ;;
                esac
            done
            IFS=$_pri_ifs
        fi

        if [ ! -d "$TRACKER_DIR" ]; then
            echo "Error: ticket system not initialized. Run 'ticket init' first." >&2
            return 1
        fi

        if [ "$format" = "llm" ]; then
            _TRACKER_DIR="$TRACKER_DIR" _INCLUDE_ARCHIVED="$include_archived" \
            _EXCLUDE_DELETED="$exclude_deleted_flag" \
            _TYPE_FILTER="$filter_type" _STATUS_FILTER="$filter_status" \
            _PARENT_FILTER="$filter_parent" _TAG_FILTER="$filter_tag" \
            _PRIORITY_FILTER="$filter_priority" _WITHOUT_TAG_FILTER="$filter_without_tag" \
            _SCRIPT_DIR="$_TICKETLIB_DIR" python3 -c "
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
            _TRACKER_DIR="$TRACKER_DIR" _INCLUDE_ARCHIVED="$include_archived" \
            _EXCLUDE_DELETED="$exclude_deleted_flag" \
            _TYPE_FILTER="$filter_type" _STATUS_FILTER="$filter_status" \
            _PARENT_FILTER="$filter_parent" _TAG_FILTER="$filter_tag" \
            _PRIORITY_FILTER="$filter_priority" _WITHOUT_TAG_FILTER="$filter_without_tag" \
            _SCRIPT_DIR="$_TICKETLIB_DIR" python3 -c "
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
    )
}

# ── ticket_create ─────────────────────────────────────────────────────────────
# In-process replacement for ticket-create.sh.
ticket_create() {

    # Run the body with strict mode scoped to this subshell.
    (
        set -euo pipefail

        # Unset git hook env vars so git commands target the correct repo.
        # PROJECT_ROOT is unset here because it is exported by the rebar CLI to
        # point at the host project root — ticket_create must resolve the tracker
        # from CWD (the repo the CLI was invoked in) rather than the shim's root.
        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR PROJECT_ROOT 2>/dev/null || true

        # Source ticket-lib.sh for write_commit_event and ticket_read_status.
        # shellcheck source=/dev/null
        source "$_TICKETLIB_DIR/ticket-lib.sh"

        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            # (empty REPO_ROOT → TRACKER_DIR="/.tickets-tracker" → file ops fail downstream with clear errors).
            # A hard return 1 here breaks callers (e.g., test setups using isolated $tmp repos) that supply
            # GIT_DIR directly or rely on subshell fall-through. GIT_DISCOVERY_ACROSS_FILESYSTEM=1 is the
            # real fix for alpine volume-mount git discovery; the empty-guard adds no safety value.
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi

        _usage() {
            echo "Usage: ticket create <ticket_type> <title> [--parent <id>] [--priority <n>] [--assignee <name>] [--description <text>] [--tags <tag1,tag2>]" >&2
            echo "  ticket_type: bug | epic | story | task" >&2
            echo "  --priority, -p: 0-4 (0=critical, 4=backlog; default: 2)" >&2
            return 1
        }

        # Resolve --output/-o (report: text|json) and strip it before the
        # positional <type> <title> + flag parsing (ticket-output.sh is sourced
        # at the top of this lib).
        _resolve_output_format report "$@" || return 2
        local _create_fmt="$_OUTPUT_FMT"
        _strip_output_flags "$@"
        set -- ${_OUTPUT_ARGS[@]+"${_OUTPUT_ARGS[@]}"}

        if [ $# -lt 2 ]; then
            _usage
            return 1
        fi

        local ticket_type="$1"
        # shellcheck disable=SC2030  # local to this subshell; intentional scope
        local title="$2"
        shift 2

        local parent_id=""
        local priority="2"
        local assignee=""
        local description=""
        local tags=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --parent)
                    parent_id="$2"
                    shift 2
                    ;;
                --parent=*)
                    parent_id="${1#--parent=}"
                    shift
                    ;;
                --priority)
                    priority="$2"
                    shift 2
                    ;;
                --priority=*)
                    priority="${1#--priority=}"
                    shift
                    ;;
                -p)
                    priority="$2"
                    shift 2
                    ;;
                --assignee)
                    assignee="$2"
                    shift 2
                    ;;
                --assignee=*)
                    assignee="${1#--assignee=}"
                    shift
                    ;;
                --description)
                    description="$2"
                    shift 2
                    ;;
                --description=*)
                    description="${1#--description=}"
                    shift
                    ;;
                -d)
                    description="$2"
                    shift 2
                    ;;
                --tags)
                    if [ -n "$tags" ]; then
                        tags="$tags,$2"
                    else
                        tags="$2"
                    fi
                    shift 2
                    ;;
                --tags=*)
                    _tag_val="${1#--tags=}"
                    if [ -n "$tags" ]; then
                        tags="$tags,$_tag_val"
                    else
                        tags="$_tag_val"
                    fi
                    shift
                    ;;
                *)
                    # Positional: treat as parent_id (backward-compatible)
                    parent_id="$1"
                    shift
                    ;;
            esac
        done

        # Assignee defaults to empty (unassigned) when not provided. The
        # `author` field already records the creator (from `git config
        # user.name`); the `assignee` field is for designated ownership,
        # which is rarely the creator. Defaulting to git user.name
        # conflated the two and caused bridge-side ACLI rejections when
        # the local git user.name doesn't match a valid Jira user.

        # Validate ticket_type
        case "$ticket_type" in
            bug|epic|story|task) ;;
            *)
                echo "Error: invalid ticket type '$ticket_type'. Must be one of: bug, epic, story, task" >&2
                return 1
                ;;
        esac

        # Validate title is non-empty
        if [ -z "$title" ]; then
            echo "Error: title must be non-empty" >&2
            return 1
        fi

        # Validate title length <= 255 chars
        if [ "${#title}" -gt 255 ]; then
            echo "Error: title exceeds 255 characters (${#title} chars)" >&2
            return 1
        fi

        # Validate priority is 0-4
        case "$priority" in
            0|1|2|3|4) ;;
            *)
                echo "Error: invalid priority '$priority'. Must be 0-4" >&2
                return 1
                ;;
        esac

        # Unicode arrow conversion (U+2192 -> ASCII ->)
        title=$(python3 -c "import sys; print(sys.argv[1].replace('\u2192', '->'))" "$title")

        # Validate ticket system is initialized
        if [ ! -f "$TRACKER_DIR/.env-id" ]; then
            echo "Error: ticket system not initialized. Run 'ticket init' first." >&2
            return 1
        fi

        # Validate parent_id exists if provided
        if [ -n "$parent_id" ]; then
            # Resolve alias or jira-* ID to canonical ticket_id (mirrors ticket-edit.sh:145)
            if ! parent_id=$(TICKETS_TRACKER_DIR="$TRACKER_DIR" resolve_ticket_id "$parent_id"); then
                return 1
            fi
            if [ ! -d "$TRACKER_DIR/$parent_id" ]; then
                echo "Error: parent ticket '$parent_id' does not exist" >&2
                return 1
            fi
            if ! find "$TRACKER_DIR/$parent_id" -maxdepth 1 \( -name '*-CREATE.json' -o -name '*-SNAPSHOT.json' \) ! -name '.*' 2>/dev/null | grep -q .; then
                echo "Error: parent ticket '$parent_id' has no CREATE or SNAPSHOT event" >&2
                return 1
            fi
            local parent_status
            parent_status=$(ticket_read_status "$TRACKER_DIR" "$parent_id") || {
                echo "Error: could not read status for parent ticket '$parent_id'" >&2
                return 1
            }
            if [ "$parent_status" = "closed" ]; then
                echo "Error: cannot create child of closed ticket '$parent_id'. Reopen the parent first with: ticket transition $parent_id closed open" >&2
                return 1
            fi
        fi

        # Generate ticket ID and event metadata
        local env_id
        env_id=$(cat "$TRACKER_DIR/.env-id")
        local author
        author=$(git config user.name 2>/dev/null || echo "Unknown")

        local event_meta ticket_id event_uuid timestamp
        event_meta=$(python3 -c "
import uuid, time
u = str(uuid.uuid4()).replace('-', '')
ticket_id = u[:4] + '-' + u[4:8] + '-' + u[8:12] + '-' + u[12:16]
event_uuid = str(uuid.uuid4())
timestamp = time.time_ns()
print(ticket_id)
print(event_uuid)
print(timestamp)
")
        ticket_id=$(echo "$event_meta" | sed -n '1p')
        event_uuid=$(echo "$event_meta" | sed -n '2p')
        timestamp=$(echo "$event_meta" | sed -n '3p')

        # Compute human-readable alias from ticket ID.
        # Honour TICKET_WORDLIST_PATH env override (for testing); fall back to the
        # wordlist bundled with the plugin.
        # ${_TICKETLIB_DIR%/scripts} strips trailing "/scripts" — assumes this file lives
        # in a directory named "scripts" with "resources" as a sibling (plugin layout).
        local _wordlist _alias_stderr ticket_alias
        _wordlist="${TICKET_WORDLIST_PATH:-${_TICKETLIB_DIR%/scripts}/resources/ticket-wordlist.txt}"
        _alias_stderr=$(mktemp /tmp/ticket-alias-stderr.XXXXXX)
        ticket_alias=$(python3 "$_TICKETLIB_DIR/ticket-alias-compute.py" "$ticket_id" "$_wordlist" 2>"$_alias_stderr")
        if grep -q "^FALLBACK$" "$_alias_stderr" 2>/dev/null; then
            echo "WARN: ticket-wordlist.txt not found — using hex fallback alias" >&2
        fi
        rm -f "$_alias_stderr"

        # Build CREATE event JSON via python3
        local temp_event desc_file
        temp_event=$(mktemp "$TRACKER_DIR/.tmp-create-XXXXXX")
        desc_file=$(mktemp "$TRACKER_DIR/.tmp-desc-XXXXXX")
        # shellcheck disable=SC2064
        trap "rm -f '$temp_event' '$desc_file' '$_alias_stderr'" EXIT
        printf '%s' "$description" > "$desc_file"

        python3 -c "
import json, sys

tags_str = sys.argv[10]
tags_list = [t.strip() for t in tags_str.split(',') if t.strip()] if tags_str else []

with open(sys.argv[9], 'r', encoding='utf-8') as df:
    description = df.read()

data = {
    'ticket_type': sys.argv[5],
    'title': sys.argv[6],
    'parent_id': sys.argv[7] if sys.argv[7] else '',
    'description': description,
    'tags': tags_list
}
if sys.argv[8]:
    data['priority'] = int(sys.argv[8])

assignee_arg = sys.argv[11] if len(sys.argv) > 11 else ''
if assignee_arg:
    data['assignee'] = assignee_arg

alias_arg = sys.argv[13] if len(sys.argv) > 13 else ''
if alias_arg:
    data['alias'] = alias_arg

id_arg = sys.argv[14] if len(sys.argv) > 14 else ''
if id_arg:
    data['id'] = id_arg

event = {
    'timestamp': int(sys.argv[1]),
    'uuid': sys.argv[2],
    'event_type': 'CREATE',
    'env_id': sys.argv[3],
    'author': sys.argv[4],
    'data': data
}

with open(sys.argv[12], 'w', encoding='utf-8') as f:
    json.dump(event, f, ensure_ascii=False)
" "$timestamp" "$event_uuid" "$env_id" "$author" "$ticket_type" "$title" "$parent_id" "$priority" "$desc_file" "$tags" "$assignee" "$temp_event" "$ticket_alias" "$ticket_id" || {
            rm -f "$temp_event" "$desc_file"
            echo "Error: failed to build CREATE event JSON" >&2
            return 1
        }
        rm -f "$desc_file"

        # Write and commit via ticket-lib.sh
        write_commit_event "$ticket_id" "$temp_event" || {
            rm -f "$temp_event"
            echo "Error: failed to write and commit CREATE event" >&2
            return 1
        }

        rm -f "$temp_event"

        # --output json: one structured object {id, alias, title}. Default (text):
        # human summary first, canonical ID last (| tail -1 id-scrape).
        if [ "$_create_fmt" = "json" ]; then
            python3 -c 'import json,sys; print(json.dumps({"id": sys.argv[1], "alias": (sys.argv[2] or None), "title": sys.argv[3]}))' \
                "$ticket_id" "$ticket_alias" "$title"
        else
            # Lead with the human-readable alias when available; canonical ID
            # is parenthetical. Matches the parity output in ticket-create.sh.
            if [ -n "$ticket_alias" ] && [ "$ticket_alias" != "$ticket_id" ]; then
                echo "Created ticket $ticket_alias ($ticket_id): $title"
            else
                echo "Created ticket $ticket_id: $title"
            fi
            echo "$ticket_id"
        fi
    )
}

# ── ticket_comment ────────────────────────────────────────────────────────────
# In-process replacement for ticket-comment.sh.
ticket_comment() {

    # Run the body with strict mode scoped to this function via a subshell.
    (
        set -euo pipefail

        # Unset git hook env vars so git commands target the correct repo.
        # Scoped to this subshell — does not leak to caller.
        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        # Source ticket-lib.sh to get write_commit_event.
        # shellcheck source=/dev/null
        source "$_TICKETLIB_DIR/ticket-lib.sh"

        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            # (empty REPO_ROOT → TRACKER_DIR="/.tickets-tracker" → file ops fail downstream with clear errors).
            # A hard return 1 here breaks callers (e.g., test setups using isolated $tmp repos) that supply
            # GIT_DIR directly or rely on subshell fall-through. GIT_DISCOVERY_ACROSS_FILESYSTEM=1 is the
            # real fix for alpine volume-mount git discovery; the empty-guard adds no safety value.
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi

        if [ $# -lt 2 ]; then
            echo "Usage: ticket comment <ticket_id> <body>" >&2
            return 1
        fi

        local ticket_id="$1"
        local body="$2"

        if [ -z "$ticket_id" ]; then
            echo "Error: ticket_id must be non-empty" >&2
            return 1
        fi

        if [ -z "$body" ]; then
            echo "Error: comment body must be non-empty" >&2
            return 1
        fi

        if ! ticket_id="$(_ticketlib_resolve_id "$ticket_id" "$TRACKER_DIR")"; then
            return 1
        fi

        # Ghost check: ticket must have a CREATE or SNAPSHOT event.
        if ! find "$TRACKER_DIR/$ticket_id" -maxdepth 1 \( -name '*-CREATE.json' -o -name '*-SNAPSHOT.json' \) ! -name '.*' 2>/dev/null | grep -q .; then
            echo "Error: ticket $ticket_id has no CREATE or SNAPSHOT event" >&2
            return 1
        fi

        local env_id
        env_id=$(cat "$TRACKER_DIR/.env-id")
        local author
        author=$(git config user.name 2>/dev/null || echo "Unknown")

        local temp_event body_file
        temp_event=$(mktemp "$TRACKER_DIR/.tmp-comment-XXXXXX")
        # Write body to temp file to avoid ARG_MAX limits on large payloads.
        body_file=$(mktemp "$TRACKER_DIR/.tmp-body-XXXXXX")
        # shellcheck disable=SC2064
        trap "rm -f '$temp_event' '$body_file'" EXIT
        printf '%s' "$body" > "$body_file"

        python3 -c "
import json, sys, time, uuid

with open(sys.argv[3], 'r', encoding='utf-8') as bf:
    body = bf.read()

event = {
    'timestamp': time.time_ns(),
    'uuid': str(uuid.uuid4()),
    'event_type': 'COMMENT',
    'env_id': sys.argv[1],
    'author': sys.argv[2],
    'data': {
        'body': body
    }
}

with open(sys.argv[4], 'w', encoding='utf-8') as f:
    json.dump(event, f, ensure_ascii=False)
" "$env_id" "$author" "$body_file" "$temp_event" || {
            rm -f "$temp_event" "$body_file"
            echo "Error: failed to build COMMENT event JSON" >&2
            return 1
        }
        rm -f "$body_file"

        write_commit_event "$ticket_id" "$temp_event" || {
            rm -f "$temp_event"
            echo "Error: failed to write and commit COMMENT event" >&2
            return 1
        }

        rm -f "$temp_event"
    )
}

# ── ticket_set_file_impact ────────────────────────────────────────────────────
# Write a FILE_IMPACT event recording which files are affected by a ticket.
ticket_set_file_impact() {

    (
        set -euo pipefail

        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        # shellcheck source=/dev/null
        source "$_TICKETLIB_DIR/ticket-lib.sh"

        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            # (empty REPO_ROOT → TRACKER_DIR="/.tickets-tracker" → file ops fail downstream with clear errors).
            # A hard return 1 here breaks callers (e.g., test setups using isolated $tmp repos) that supply
            # GIT_DIR directly or rely on subshell fall-through. GIT_DISCOVERY_ACROSS_FILESYSTEM=1 is the
            # real fix for alpine volume-mount git discovery; the empty-guard adds no safety value.
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi

        if [ $# -lt 2 ]; then
            echo "Usage: ticket set-file-impact <ticket_id> <json_array>" >&2
            return 1
        fi

        local ticket_id="$1"
        local json_array="$2"

        if [ -z "$ticket_id" ]; then
            echo "Error: ticket_id must be non-empty" >&2
            return 1
        fi

        # exception as ticket_show (epic 78fc-3858, story 564c-e391). python3 would
        # require a subshell + subprocess; jq is already the project-standard JSON
        # processor on the bash-native path and is available wherever ticket-lib-api.sh runs.
        # Validate: must be valid JSON
        if ! printf '%s' "$json_array" | jq -e . >/dev/null 2>&1; then
            echo "Error: file_impact argument is not valid JSON" >&2
            return 1
        fi

        # Validate: must be a JSON array (not object, not scalar)
        local json_type
        json_type=$(printf '%s' "$json_array" | jq -r 'type' 2>/dev/null) || json_type="unknown"
        if [ "$json_type" != "array" ]; then
            echo "Error: file_impact argument must be a JSON array, got '$json_type'" >&2
            return 1
        fi

        if ! ticket_id="$(_ticketlib_resolve_id "$ticket_id" "$TRACKER_DIR")"; then
            return 1
        fi

        # Ghost check: ticket must have a CREATE or SNAPSHOT event.
        if ! find "$TRACKER_DIR/$ticket_id" -maxdepth 1 \( -name '*-CREATE.json' -o -name '*-SNAPSHOT.json' \) ! -name '.*' 2>/dev/null | grep -q .; then
            echo "Error: ticket $ticket_id has no CREATE or SNAPSHOT event" >&2
            return 1
        fi

        local env_id
        env_id=$(cat "$TRACKER_DIR/.env-id")
        local author
        author=$(git config user.name 2>/dev/null || echo "Unknown")

        local temp_event array_file
        temp_event=$(mktemp "$TRACKER_DIR/.tmp-file-impact-XXXXXX")
        array_file=$(mktemp "$TRACKER_DIR/.tmp-fi-array-XXXXXX")
        # shellcheck disable=SC2064
        trap "rm -f '$temp_event' '$array_file'" EXIT
        printf '%s' "$json_array" > "$array_file"

        python3 -c "
import json, sys, time, uuid

with open(sys.argv[3], 'r', encoding='utf-8') as af:
    file_impact = json.load(af)

event = {
    'timestamp': time.time_ns(),
    'uuid': str(uuid.uuid4()),
    'event_type': 'FILE_IMPACT',
    'env_id': sys.argv[1],
    'author': sys.argv[2],
    'data': {
        'file_impact': file_impact
    }
}

with open(sys.argv[4], 'w', encoding='utf-8') as f:
    json.dump(event, f, ensure_ascii=False)
" "$env_id" "$author" "$array_file" "$temp_event" || {
            rm -f "$temp_event" "$array_file"
            echo "Error: failed to build FILE_IMPACT event JSON" >&2
            return 1
        }
        rm -f "$array_file"

        write_commit_event "$ticket_id" "$temp_event" || {
            rm -f "$temp_event"
            echo "Error: failed to write and commit FILE_IMPACT event" >&2
            return 1
        }

        rm -f "$temp_event"
    )
}

# ── ticket_get_file_impact ────────────────────────────────────────────────────
# Read the compiled file_impact array for a ticket.
ticket_get_file_impact() {

    (
        set -euo pipefail

        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            # (empty REPO_ROOT → TRACKER_DIR="/.tickets-tracker" → file ops fail downstream with clear errors).
            # A hard return 1 here breaks callers (e.g., test setups using isolated $tmp repos) that supply
            # GIT_DIR directly or rely on subshell fall-through. GIT_DISCOVERY_ACROSS_FILESYSTEM=1 is the
            # real fix for alpine volume-mount git discovery; the empty-guard adds no safety value.
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi

        if [ $# -lt 1 ]; then
            echo "Usage: ticket get-file-impact <ticket_id>" >&2
            return 1
        fi

        local ticket_id="$1"

        if [ -z "$ticket_id" ]; then
            echo "Error: ticket_id must be non-empty" >&2
            return 1
        fi

        if ! ticket_id="$(_ticketlib_resolve_id "$ticket_id" "$TRACKER_DIR" 2>/dev/null)"; then
            # Preserve legacy behavior: silently return [] on lookup miss.
            echo "[]"
            return 0
        fi

        if [ ! -d "$TRACKER_DIR/$ticket_id" ]; then
            echo "[]"
            return 0
        fi

        # Reduce via the single source of truth (Python ticket_reducer) and emit
        # the file_impact array — same reducer as show/list (bug f026), replacing
        # the former duplicated in-bash jq reducer. reduce_ticket always yields a
        # file_impact key (default []), preserving the legacy []-on-miss contract.
        local fi_out
        fi_out=$(_SCRIPT_DIR="$_TICKETLIB_DIR" python3 -c '
import sys, os, json
sys.path.insert(0, os.environ["_SCRIPT_DIR"])
from ticket_reducer import reduce_ticket
print(json.dumps(reduce_ticket(sys.argv[1]).get("file_impact") or [], ensure_ascii=False))
' "$TRACKER_DIR/$ticket_id" 2>/dev/null) || fi_out="[]"

        echo "$fi_out"
    )
}

# ── ticket_set_verify_commands ─────────────────────────────────────────────────
# Write a VERIFY_COMMANDS event recording DD-level verify commands for a ticket.
# Follows the set-file-impact pattern: last-write-wins, compiled into verify_commands field.
ticket_set_verify_commands() {

    (
        set -euo pipefail

        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        # shellcheck source=/dev/null
        source "$_TICKETLIB_DIR/ticket-lib.sh"

        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi

        if [ $# -lt 2 ]; then
            echo "Usage: ticket set-verify-commands <ticket_id> <json_array>" >&2
            return 1
        fi

        local ticket_id="$1"
        local json_array="$2"

        if [ -z "$ticket_id" ]; then
            echo "Error: ticket_id must be non-empty" >&2
            return 1
        fi

        if ! printf '%s' "$json_array" | jq -e . >/dev/null 2>&1; then
            echo "Error: verify_commands argument is not valid JSON" >&2
            return 1
        fi

        local json_type
        json_type=$(printf '%s' "$json_array" | jq -r 'type' 2>/dev/null) || json_type="unknown"
        if [ "$json_type" != "array" ]; then
            echo "Error: verify_commands argument must be a JSON array, got '$json_type'" >&2
            return 1
        fi

        if ! ticket_id="$(_ticketlib_resolve_id "$ticket_id" "$TRACKER_DIR")"; then
            return 1
        fi

        if ! find "$TRACKER_DIR/$ticket_id" -maxdepth 1 \( -name '*-CREATE.json' -o -name '*-SNAPSHOT.json' \) ! -name '.*' 2>/dev/null | grep -q .; then
            echo "Error: ticket $ticket_id has no CREATE or SNAPSHOT event" >&2
            return 1
        fi

        local env_id
        env_id=$(cat "$TRACKER_DIR/.env-id")
        local author
        author=$(git config user.name 2>/dev/null || echo "Unknown")

        local temp_event array_file
        temp_event=$(mktemp "$TRACKER_DIR/.tmp-verify-cmds-XXXXXX")
        array_file=$(mktemp "$TRACKER_DIR/.tmp-vc-array-XXXXXX")
        # shellcheck disable=SC2064
        trap "rm -f '$temp_event' '$array_file'" EXIT
        printf '%s' "$json_array" > "$array_file"

        python3 -c "
import json, sys, time, uuid

with open(sys.argv[3], 'r', encoding='utf-8') as af:
    verify_commands = json.load(af)

event = {
    'timestamp': time.time_ns(),
    'uuid': str(uuid.uuid4()),
    'event_type': 'VERIFY_COMMANDS',
    'env_id': sys.argv[1],
    'author': sys.argv[2],
    'data': {
        'verify_commands': verify_commands
    }
}

with open(sys.argv[4], 'w', encoding='utf-8') as f:
    json.dump(event, f, ensure_ascii=False)
" "$env_id" "$author" "$array_file" "$temp_event" || {
            rm -f "$temp_event" "$array_file"
            echo "Error: failed to build VERIFY_COMMANDS event JSON" >&2
            return 1
        }
        rm -f "$array_file"

        write_commit_event "$ticket_id" "$temp_event" || {
            rm -f "$temp_event"
            echo "Error: failed to write and commit VERIFY_COMMANDS event" >&2
            return 1
        }

        rm -f "$temp_event"
    )
}

# ── ticket_get_verify_commands ────────────────────────────────────────────────
# Read the compiled verify_commands array for a ticket.
ticket_get_verify_commands() {

    (
        set -euo pipefail

        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        # shellcheck source=/dev/null
        source "$_TICKETLIB_DIR/ticket-lib.sh"

        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi

        if [ $# -lt 1 ]; then
            echo "Usage: ticket get-verify-commands <ticket_id>" >&2
            return 1
        fi

        local ticket_id="$1"

        if [ -z "$ticket_id" ]; then
            echo "Error: ticket_id must be non-empty" >&2
            return 1
        fi

        if ! ticket_id="$(_ticketlib_resolve_id "$ticket_id" "$TRACKER_DIR")"; then
            return 1
        fi

        local vc_out
        vc_out=$(ticket_show "$ticket_id" | jq -c '.verify_commands // []')
        echo "$vc_out"
    )
}

# ── ticket_tag ────────────────────────────────────────────────────────────────
# In-process replacement for ticket-tag.sh.
ticket_tag() {

    (
        set -euo pipefail
        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        # shellcheck source=/dev/null
        source "$_TICKETLIB_DIR/ticket-lib.sh"

        if [ $# -lt 2 ]; then
            echo "Usage: ticket tag <ticket_id> <tag>" >&2
            return 1
        fi

        local ticket_id="$1"
        local tag="$2"

        if [ -z "$ticket_id" ] || [ -z "$tag" ]; then
            echo "Error: ticket_id and tag must be non-empty" >&2
            return 1
        fi

        # Resolve 8-hex short ID to canonical 16-hex ticket dir name.
        # Without this, _tag_add → write_commit_event → mkdir creates an orphan
        # directory under the short ID instead of writing to the existing dir.
        # (6c0f-90bc — mirrors the resolution pattern used by ticket_show,
        # ticket_comment, ticket_edit, ticket_archive, ticket_delete, etc.)
        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            # behavior in 7 sibling ops (ticket_show, ticket_comment, ticket_edit, etc.) — empty
            # REPO_ROOT → TRACKER_DIR="/.tickets-tracker" → file ops fail downstream with clear
            # errors. A hard return 1 here breaks callers that supply GIT_DIR directly or rely on
            # subshell fall-through (test setups using isolated $tmp repos).
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi
        if ! ticket_id="$(_ticketlib_resolve_id "$ticket_id" "$TRACKER_DIR")"; then
            return 1
        fi

        _tag_add_checked "$ticket_id" "$tag"
    )
}

# ── ticket_untag ──────────────────────────────────────────────────────────────
# In-process replacement for ticket-untag.sh.
ticket_untag() {

    (
        set -euo pipefail
        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        # shellcheck source=/dev/null
        source "$_TICKETLIB_DIR/ticket-lib.sh"

        if [ $# -lt 2 ]; then
            echo "Usage: ticket untag <ticket_id> <tag>" >&2
            return 1
        fi

        local ticket_id="$1"
        local tag="$2"

        if [ -z "$ticket_id" ] || [ -z "$tag" ]; then
            echo "Error: ticket_id and tag must be non-empty" >&2
            return 1
        fi

        # Resolve 8-hex short ID to canonical 16-hex ticket dir name (6c0f-90bc).
        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            # behavior in 7 sibling ops (ticket_show, ticket_comment, ticket_edit, etc.) — empty
            # REPO_ROOT → TRACKER_DIR="/.tickets-tracker" → file ops fail downstream with clear
            # errors. A hard return 1 here breaks callers that supply GIT_DIR directly or rely on
            # subshell fall-through (test setups using isolated $tmp repos).
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi
        if ! ticket_id="$(_ticketlib_resolve_id "$ticket_id" "$TRACKER_DIR")"; then
            return 1
        fi

        _tag_remove "$ticket_id" "$tag"
    )
}

# ── ticket_edit ───────────────────────────────────────────────────────────────
# In-process replacement for ticket-edit.sh.
ticket_edit() {

    (
        set -euo pipefail
        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        # shellcheck source=/dev/null
        source "$_TICKETLIB_DIR/ticket-lib.sh"

        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            # (empty REPO_ROOT → TRACKER_DIR="/.tickets-tracker" → file ops fail downstream with clear errors).
            # A hard return 1 here breaks callers (e.g., test setups using isolated $tmp repos) that supply
            # GIT_DIR directly or rely on subshell fall-through. GIT_DISCOVERY_ACROSS_FILESYSTEM=1 is the
            # real fix for alpine volume-mount git discovery; the empty-guard adds no safety value.
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi

        if [ $# -lt 2 ]; then
            echo "Usage: ticket edit <ticket_id> [--title=VALUE] [--priority=VALUE] [--assignee=VALUE] [--ticket_type=VALUE] [--description=VALUE] [--tags=VALUE] [--parent=VALUE]" >&2
            return 1
        fi

        local ticket_id="$1"
        shift

        # --parent (bug 3f93-1b3d): user-facing flag; mapped to parent_id event field below.
        local ALLOWED_FIELDS="title priority assignee ticket_type description tags parent"

        _is_allowed_field_edit() {
            local field="$1"
            local f
            for f in $ALLOWED_FIELDS; do
                if [ "$f" = "$field" ]; then
                    return 0
                fi
            done
            return 1
        }

        # Parse --field=value and --field value pairs
        # Indexed array (bash 3.2 compatible; avoid declare -A which requires bash 4+)
        local _parsed_pairs
        _parsed_pairs=()
        while [ $# -gt 0 ]; do
            local arg="$1"
            case "$arg" in
                --*=*)
                    local field_name="${arg%%=*}"
                    field_name="${field_name#--}"
                    local field_value="${arg#*=}"
                    if ! _is_allowed_field_edit "$field_name"; then
                        echo "Error: unknown field '$field_name'. Allowed: $ALLOWED_FIELDS" >&2
                        return 1
                    fi
                    _parsed_pairs+=("$field_name=$field_value")
                    shift
                    ;;
                --*)
                    local field_name="${arg#--}"
                    if ! _is_allowed_field_edit "$field_name"; then
                        echo "Error: unknown field '$field_name'. Allowed: $ALLOWED_FIELDS" >&2
                        return 1
                    fi
                    if [ $# -lt 2 ]; then
                        echo "Error: --$field_name requires a value" >&2
                        return 1
                    fi
                    shift
                    _parsed_pairs+=("$field_name=$1")
                    shift
                    ;;
                *)
                    echo "Error: unexpected argument '$arg'" >&2
                    return 1
                    ;;
            esac
        done

        if [ ${#_parsed_pairs[@]} -eq 0 ]; then
            echo "Error: at least one --field=value pair is required" >&2
            return 1
        fi

        if [ ! -f "$TRACKER_DIR/.env-id" ]; then
            echo "Error: ticket system not initialized. Run 'ticket init' first." >&2
            return 1
        fi

        if ! ticket_id="$(_ticketlib_resolve_id "$ticket_id" "$TRACKER_DIR")"; then
            return 1
        fi

        if ! find "$TRACKER_DIR/$ticket_id" -maxdepth 1 \( -name '*-CREATE.json' -o -name '*-SNAPSHOT.json' \) ! -name '.*' 2>/dev/null | grep -q .; then
            echo "Error: ticket $ticket_id has no CREATE or SNAPSHOT event" >&2
            return 1
        fi

        # Field-level guards (bug 3f93-1b3d parent; bug e78f-9f79 description):
        #   description: reject empty value to prevent silent clobber
        #   parent:
        #     1. resolve new parent ID (accept short IDs / aliases)
        #     2. verify new parent ticket exists
        #     3. refuse self-parent
        #     4. refuse ancestor cycles (would-be parent has ticket_id as ancestor)
        # Replace the "parent=…" pair with "parent_id=<resolved>" before delegation.
        local _i _pair _new_parent_id_input _new_parent_id _new_desc
        for _i in "${!_parsed_pairs[@]}"; do
            _pair="${_parsed_pairs[_i]}"
            case "$_pair" in
                description=*)
                    # Reject empty --description= to prevent silent clobber of
                    # multi-KB structured descriptions when a heredoc/$(cat ...)
                    # substitution collapses to an empty string (bug e78f-9f79).
                    _new_desc="${_pair#description=}"
                    if [ -z "$_new_desc" ]; then
                        echo "Error: --description requires a non-empty value (empty values silently clobber prior content; bug e78f-9f79)" >&2
                        return 1
                    fi
                    ;;
                priority=*)
                    # Mirror ticket-create's priority validation (bug df7a-61b3):
                    # edit must not accept out-of-range or non-numeric priorities
                    # that create rejects (else edit is a validation-bypass path).
                    case "${_pair#priority=}" in
                        0|1|2|3|4) ;;
                        *)
                            echo "Error: invalid priority '${_pair#priority=}'. Must be 0-4" >&2
                            return 1
                            ;;
                    esac
                    ;;
                ticket_type=*)
                    # Mirror ticket-create's ticket_type validation (bug df7a-61b3).
                    case "${_pair#ticket_type=}" in
                        bug|epic|story|task) ;;
                        *)
                            echo "Error: invalid ticket type '${_pair#ticket_type=}'. Must be one of: bug, epic, story, task" >&2
                            return 1
                            ;;
                    esac
                    ;;
                parent=*)
                    _new_parent_id_input="${_pair#parent=}"
                    if [ -z "$_new_parent_id_input" ]; then
                        echo "Error: --parent requires a non-empty value (use --parent=null to detach)" >&2
                        return 1
                    fi
                    # Detach sentinel (bug 7f23-1a14): --parent=null clears
                    # parent_id. The snapshot rebuilder's jq logic normalizes
                    # an empty parent_id field to null, so we write "" into the
                    # EDIT event and skip the validation cascade below (no
                    # parent to resolve, no status check, no ancestor walk).
                    if [ "$_new_parent_id_input" = "null" ]; then
                        _parsed_pairs[_i]="parent_id="
                        continue
                    fi
                    if ! _new_parent_id="$(_ticketlib_resolve_id "$_new_parent_id_input" "$TRACKER_DIR" 2>/dev/null)"; then
                        echo "Error: parent ticket '$_new_parent_id_input' does not exist" >&2
                        return 1
                    fi
                    if [ "$_new_parent_id" = "$ticket_id" ]; then
                        echo "Error: ticket cannot be its own parent" >&2
                        return 1
                    fi
                    # Parent-status check: ticket_create enforces "cannot
                    # create child of closed ticket" at ticket-create.sh:158.
                    # ticket_edit must replicate the same invariant when
                    # re-parenting — a live ticket attached to a closed or
                    # deleted parent corrupts the hierarchy (PR #139 review).
                    #
                    # Fail-closed semantics: only an explicit active-state
                    # status crosses the gate. Empty (status lookup failed),
                    # closed, deleted, or any unrecognized state → reject.
                    local _new_parent_status
                    _new_parent_status=$(python3 "$_TICKETLIB_DIR/ticket-reads.py" show --no-sync "$_new_parent_id" 2>/dev/null \
                        | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status','') or '')" 2>/dev/null) || _new_parent_status=""
                    case "$_new_parent_status" in
                        open|in_progress)
                            : # explicit allow
                            ;;
                        "")
                            echo "Error: cannot verify status of parent ticket '$_new_parent_id' — refusing to re-parent (fail-closed). Verify the ticket exists and is in an active state, then retry." >&2
                            return 1
                            ;;
                        *)
                            echo "Error: cannot re-parent to $_new_parent_status ticket '$_new_parent_id'. Reopen the parent first with: ticket transition $_new_parent_id $_new_parent_status open" >&2
                            return 1
                            ;;
                    esac
                    # Ancestor walk: walking the proposed new parent's parent_id
                    # chain upward, refuse if ticket_id is reached.
                    local _walk_id="$_new_parent_id" _walk_count=0 _walk_parent
                    while [ -n "$_walk_id" ] && [ "$_walk_count" -lt 64 ]; do
                        _walk_parent=$(python3 "$_TICKETLIB_DIR/ticket-reads.py" show --no-sync "$_walk_id" 2>/dev/null \
                            | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('parent_id','') or '')" 2>/dev/null) || _walk_parent=""
                        if [ -z "$_walk_parent" ] || [ "$_walk_parent" = "None" ]; then
                            break
                        fi
                        if [ "$_walk_parent" = "$ticket_id" ]; then
                            echo "Error: cannot set parent — would create a cycle (ticket $ticket_id is an ancestor of $_new_parent_id)" >&2
                            return 1
                        fi
                        _walk_id="$_walk_parent"
                        _walk_count=$((_walk_count + 1))
                    done
                    _parsed_pairs[_i]="parent_id=$_new_parent_id"
                    ;;
            esac
        done

        local env_id
        env_id=$(cat "$TRACKER_DIR/.env-id")
        local author
        author=$(git config user.name 2>/dev/null || echo "Unknown")

        local temp_event
        temp_event=$(mktemp "$TRACKER_DIR/.tmp-edit-XXXXXX")
        # shellcheck disable=SC2064
        trap "rm -f '$temp_event'" EXIT

        # Delegate field parsing, unicode conversion, JSON building, and event
        # writing to python3 — consistent with sibling functions (ticket_create,
        # ticket_comment, ticket_transition). Pairs are passed as positional argv
        # ("key=value") so no bash 4+ associative-array syntax is needed.
        python3 -c "
import json, sys, time, uuid

args     = sys.argv[1:]
env_id   = args[0]
author   = args[1]
out_path = args[-1]

fields = {}
for pair in args[2:-1]:
    # partition splits on the FIRST '=' only; values may safely contain '='
    key, _, val = pair.partition('=')
    fields[key] = val

if 'title' in fields:
    fields['title'] = fields['title'].replace('\u2192', '->')

if 'priority' in fields and fields['priority'].lstrip('-').isdigit():
    fields['priority'] = int(fields['priority'])

event = {
    'timestamp': time.time_ns(),
    'uuid': str(uuid.uuid4()),
    'event_type': 'EDIT',
    'env_id': env_id,
    'author': author,
    'data': {'fields': fields}
}

with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(event, f, ensure_ascii=False)
" "$env_id" "$author" "${_parsed_pairs[@]}" "$temp_event" || {
            rm -f "$temp_event"
            echo "Error: failed to build EDIT event JSON" >&2
            return 1
        }

        write_commit_event "$ticket_id" "$temp_event" || {
            rm -f "$temp_event"
            echo "Error: failed to write and commit EDIT event" >&2
            return 1
        }

        rm -f "$temp_event"
    )
}

# ── ticket_link ───────────────────────────────────────────────────────────────
# In-process replacement for the `ticket link` dispatcher case.
# Thin wrapper — delegates to ticket-graph.py for cycle detection.
# --dry-run: position-independent flag; when present, delegates to ticket-link.sh
#   which prints a [DRY RUN] preview without writing any event (bug 3796-ccd3).
ticket_link() {

    (
        set -euo pipefail
        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        # Parse --dry-run flag (position-independent) before arg-count check.
        local dry_run=0
        local real_args=()
        local arg
        for arg in "$@"; do
            if [ "$arg" = "--dry-run" ]; then
                dry_run=1
            else
                real_args+=("$arg")
            fi
        done
        set -- "${real_args[@]+"${real_args[@]}"}"

        if [ $# -lt 3 ]; then
            echo "Usage: ticket link <id1> <id2> <relation>" >&2
            return 1
        fi

        # Resolve short IDs / aliases / prefixes for both endpoints before
        # delegating to ticket-graph.py (bug ec61-0e1f).
        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi

        local src_id="$1" tgt_id="$2" relation="$3"
        if ! src_id="$(_ticketlib_resolve_id "$src_id" "$TRACKER_DIR")"; then
            return 1
        fi
        if ! tgt_id="$(_ticketlib_resolve_id "$tgt_id" "$TRACKER_DIR")"; then
            return 1
        fi

        # --dry-run: delegate to ticket-link.sh which owns the preview logic.
        # This prints "[DRY RUN] Would create/promote/reject: ..." without writing
        # any event (bug 3796-ccd3-863f-4d63: canonical path was ignoring --dry-run).
        if [ "$dry_run" = "1" ]; then
            TICKETS_TRACKER_DIR="$TRACKER_DIR" \
                bash "$_TICKETLIB_DIR/ticket-link.sh" link "$src_id" "$tgt_id" "$relation" --dry-run
            return $?
        fi

        # Relation validation is delegated to ticket-graph.py (single source of truth)
        # to avoid drift if new relation types are added.
        python3 "$_TICKETLIB_DIR/ticket-graph.py" --link "$src_id" "$tgt_id" "$relation"
    )
}

# ── ticket_transition ─────────────────────────────────────────────────────────
# In-process replacement for ticket-transition.sh.
# Thin wrapper: reads current status, validates, writes STATUS event via python3.
# Does NOT replicate epic-close logic, unblock detection, or compact-on-close.
ticket_transition() {
    # Thin wrapper: resolve short ID / alias / prefix at the library boundary
    # (bug ec61-0e1f), then delegate to ticket-transition.sh to preserve unblock
    # logic, open-children guard, epic-close reminder, and flock-based concurrency.
    # Tracked for future in-process optimization in 161e-b2b4.
    if [ $# -lt 1 ]; then
        bash "$_TICKETLIB_DIR/ticket-transition.sh" "$@"
        return $?
    fi
    (
        set -uo pipefail
        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi

        # Strip the --output/-o flag here (so it can appear before the id) and
        # re-inject it as --output=<fmt> when delegating; ticket-transition.sh
        # owns the actual format handling.
        _resolve_output_format report "$@" || return 2
        local _tr_fmt="$_OUTPUT_FMT"
        _strip_output_flags "$@"
        set -- ${_OUTPUT_ARGS[@]+"${_OUTPUT_ARGS[@]}"}

        local ticket_id="$1"
        shift
        if ! ticket_id="$(_ticketlib_resolve_id "$ticket_id" "$TRACKER_DIR")"; then
            return 1
        fi
        bash "$_TICKETLIB_DIR/ticket-transition.sh" "$ticket_id" "$@" --output="$_tr_fmt"
    )
    return $?
}

# ── ticket_compact ────────────────────────────────────────────────────────────
# Thin wrapper: resolve short ID / alias / prefix at the library boundary
# (bug ec61-0e1f), then delegate to ticket-compact.sh.
ticket_compact() {
    if [ $# -lt 1 ]; then
        bash "$_TICKETLIB_DIR/ticket-compact.sh" "$@"
        return $?
    fi
    (
        set -uo pipefail
        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi

        local ticket_id="$1"
        shift
        if ! ticket_id="$(_ticketlib_resolve_id "$ticket_id" "$TRACKER_DIR")"; then
            return 1
        fi
        bash "$_TICKETLIB_DIR/ticket-compact.sh" "$ticket_id" "$@"
    )
    return $?
}

# ── ticket_exists ─────────────────────────────────────────────────────────────
ticket_exists() {
    # Canonical implementation: ticket-exists.sh is the canonical implementation (no prior script).
    bash "$_TICKETLIB_DIR/ticket-exists.sh" "$@"
    return $?
}

# ── ticket_validate ────────────────────────────────────────────────────────────
ticket_validate() {
    bash "$_TICKETLIB_DIR/validate-issues.sh" "$@"
    return $?
}

# ── ticket_clarity_check ───────────────────────────────────────────────────────
ticket_clarity_check() {
    bash "$_TICKETLIB_DIR/ticket-clarity-check.sh" "$@"
    return $?
}

# ── ticket_check_ac ────────────────────────────────────────────────────────────
ticket_check_ac() {
    bash "$_TICKETLIB_DIR/check-acceptance-criteria.sh" "$@"
    return $?
}

# ── ticket_quality_check ───────────────────────────────────────────────────────
ticket_quality_check() {
    bash "$_TICKETLIB_DIR/issue-quality-check.sh" "$@"
    return $?
}

# ── ticket_summary ─────────────────────────────────────────────────────────────
ticket_summary() {
    bash "$_TICKETLIB_DIR/issue-summary.sh" "$@"
    return $?
}

# ── ticket_ready ───────────────────────────────────────────────────────────────
ticket_ready() {
    # Single-source read: ticket_reads.py (story 23d2-e0f3). The dispatcher's
    # `ready` arm calls ticket-reads.py directly; this wrapper is kept so any
    # in-process caller of ticket_ready stays on the one read implementation.
    python3 "$_TICKETLIB_DIR/ticket-reads.py" ready "$@"
    return $?
}

# ── ticket_list_epics ──────────────────────────────────────────────────────────
ticket_list_epics() {
    # Canonical implementation: delegates to sprint-list-epics.sh (canonical; no prior script).
    bash "$_TICKETLIB_DIR/ticket-list-epics.sh" "$@"
    return $?
}

# ── ticket_list_descendants ────────────────────────────────────────────────────
ticket_list_descendants() {
    # Canonical implementation: ticket-list-descendants.sh is the canonical implementation (no prior script).
    bash "$_TICKETLIB_DIR/ticket-list-descendants.sh" "$@"
    return $?
}

# ── ticket_next_batch ─────────────────────────────────────────────────────────
ticket_next_batch() {
    # Canonical implementation: ticket-next-batch.sh is the canonical implementation (no prior script).
    bash "$_TICKETLIB_DIR/ticket-next-batch.sh" "$@"
    return $?
}

# ── ticket_archive ────────────────────────────────────────────────────────────
# Archive an open ticket by writing an ARCHIVED event and the .archived marker.
#
# Contract:
#   - Only works on tickets with status=open (rejects in_progress, closed, blocked)
#   - Idempotent: second call on an already-archived ticket exits 0 silently
#   - Does NOT require closing the ticket first
#   - After success, ticket is excluded from 'ticket list' (default)
#   - 'ticket list --include-archived' and 'ticket show' still surface the ticket
ticket_archive() {
    (
        set -euo pipefail

        # Unset git hook env vars so git commands target the correct repo.
        # Scoped to this subshell — does not leak to caller.
        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        # Source ticket-lib.sh for write_commit_event.
        # shellcheck source=/dev/null
        source "$_TICKETLIB_DIR/ticket-lib.sh"

        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            # (empty REPO_ROOT → TRACKER_DIR="/.tickets-tracker" → file ops fail downstream with clear errors).
            # A hard return 1 here breaks callers (e.g., test setups using isolated $tmp repos) that supply
            # GIT_DIR directly or rely on subshell fall-through. GIT_DISCOVERY_ACROSS_FILESYSTEM=1 is the
            # real fix for alpine volume-mount git discovery; the empty-guard adds no safety value.
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi

        if [ $# -ne 1 ]; then
            echo "Usage: ticket archive <ticket_id>" >&2
            return 1
        fi

        local ticket_id="$1"

        if [ -z "$ticket_id" ]; then
            echo "Error: ticket_id must be non-empty" >&2
            return 1
        fi

        if ! ticket_id="$(_ticketlib_resolve_id "$ticket_id" "$TRACKER_DIR")"; then
            return 1
        fi

        local TICKET_DIR="$TRACKER_DIR/$ticket_id"

        # ── Idempotency check: exit 0 silently if already archived ────────────
        if [ -f "$TICKET_DIR/.archived" ]; then
            return 0
        fi
        # Also check for ARCHIVED event file (marker may be missing after clone)
        if find "$TICKET_DIR" -maxdepth 1 -name '*-ARCHIVED.json' 2>/dev/null | grep -q .; then
            # Write the marker if it was missing, then exit 0
            python3 -c "
import sys
sys.path.insert(0, '$_TICKETLIB_DIR')
from ticket_reducer.marker import write_marker
write_marker(sys.argv[1])
" "$TICKET_DIR" 2>/dev/null || true
            return 0
        fi

        # ── Status gate: only open tickets may be archived ────────────────────
        local current_status
        current_status=$(
            TICKETS_TRACKER_DIR="$TRACKER_DIR" bash "$_TICKETLIB_DIR/ticket" show "$ticket_id" 2>/dev/null \
            | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null
        ) || current_status=""

        if [ -z "$current_status" ]; then
            echo "Error: could not read status for ticket '$ticket_id'" >&2
            return 1
        fi

        if [ "$current_status" != "open" ]; then
            echo "Error: ticket '$ticket_id' has status '$current_status'; archive only works on open tickets" >&2
            return 1
        fi

        # ── Write ARCHIVED event ──────────────────────────────────────────────
        local env_id author
        env_id=$(cat "$TRACKER_DIR/.env-id" 2>/dev/null || echo "unknown")
        author=$(git config user.name 2>/dev/null || echo "Unknown")

        local temp_event
        temp_event=$(mktemp "$TRACKER_DIR/.tmp-archive-XXXXXX")
        # shellcheck disable=SC2064
        trap "rm -f '$temp_event'" EXIT

        python3 -c "
import json, sys, time, uuid

event = {
    'timestamp': time.time_ns(),
    'uuid': str(uuid.uuid4()),
    'event_type': 'ARCHIVED',
    'env_id': sys.argv[1],
    'author': sys.argv[2],
    'data': {}
}

with open(sys.argv[3], 'w', encoding='utf-8') as f:
    json.dump(event, f, ensure_ascii=False)
" "$env_id" "$author" "$temp_event" || {
            rm -f "$temp_event"
            echo "Error: failed to build ARCHIVED event JSON" >&2
            return 1
        }

        write_commit_event "$ticket_id" "$temp_event" || {
            rm -f "$temp_event"
            echo "Error: failed to write and commit ARCHIVED event" >&2
            return 1
        }

        rm -f "$temp_event"

        # ── Write .archived marker (after event is durably committed) ─────────
        python3 -c "
import sys, os
sys.path.insert(0, '$_TICKETLIB_DIR')
from ticket_reducer.marker import write_marker
write_marker(sys.argv[1])
" "$TICKET_DIR" 2>/dev/null || true

        echo "Archived ticket '$ticket_id'"
    )
}

# ── ticket_format ────────────────────────────────────────────────────────────
# In-process wrapper for format_ticket_id().
ticket_format() {
    (
        set -euo pipefail
        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        # shellcheck source=/dev/null
        source "$_TICKETLIB_DIR/ticket-lib.sh"

        if [ $# -lt 1 ]; then
            echo "Usage: ticket format <ticket_id> [mode]" >&2
            return 1
        fi

        format_ticket_id "$@"
    )
}

# ── ticket_resolve ───────────────────────────────────────────────────────────
# In-process wrapper for resolve_ticket_id().
ticket_resolve() {
    (
        set -euo pipefail
        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        # shellcheck source=/dev/null
        source "$_TICKETLIB_DIR/ticket-lib.sh"

        if [ $# -lt 1 ]; then
            echo "Usage: ticket resolve <id_or_alias_or_prefix>" >&2
            return 1
        fi

        resolve_ticket_id "$1"
    )
}

# ── ticket_delete ─────────────────────────────────────────────────────────────
# Hard-delete a ticket: write UNLINK events, write ARCHIVED event, drop
# .tombstone.json + .archived marker.
#
# Contract:
#   - Requires --user-approved flag (explicit intent gate)
#   - Accepts tickets in any non-deleted status (open, in_progress, closed)
#   - Blocked if any child ticket lacks both .tombstone.json and .archived marker
#   - Writes UNLINK events for all net-active LINK events referencing the ticket
#   - Idempotent: re-invocation against an already-tombstoned ticket completes
#     any missing UNLINKs and exits 0
ticket_delete() {
    (
        set -euo pipefail
        unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

        # shellcheck source=/dev/null
        source "$_TICKETLIB_DIR/ticket-lib.sh"

        local TRACKER_DIR
        if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
            TRACKER_DIR="$TICKETS_TRACKER_DIR"
        else
            local REPO_ROOT
            REPO_ROOT="${PROJECT_ROOT:-$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null)}"
            TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
        fi

        # Resolve --output/-o (report: text|json) and strip it (ticket-output.sh
        # is already sourced at the top of this lib).
        _resolve_output_format report "$@" || return 2
        local _del_fmt="$_OUTPUT_FMT"
        _strip_output_flags "$@"
        set -- ${_OUTPUT_ARGS[@]+"${_OUTPUT_ARGS[@]}"}

        local user_approved=0
        local ticket_id=""
        local remaining_args=()
        for arg in "$@"; do
            if [ "$arg" = "--user-approved" ]; then
                user_approved=1
            else
                remaining_args+=("$arg")
            fi
        done
        set -- "${remaining_args[@]+"${remaining_args[@]}"}"

        if [ $# -ne 1 ]; then
            echo "Usage: ticket delete <ticket_id> --user-approved" >&2
            return 1
        fi

        ticket_id="$1"

        if [ -z "$ticket_id" ]; then
            echo "Error: ticket_id must be non-empty" >&2
            return 1
        fi

        if [ "$user_approved" -ne 1 ]; then
            echo "Error: ticket delete requires --user-approved flag (this is a destructive operation)" >&2
            echo "Usage: ticket delete <ticket_id> --user-approved" >&2
            return 1
        fi

        if ! ticket_id="$(_ticketlib_resolve_id "$ticket_id" "$TRACKER_DIR")"; then
            return 1
        fi

        local TICKET_DIR="$TRACKER_DIR/$ticket_id"

        local already_tombstoned=0
        if [ -f "$TICKET_DIR/.tombstone.json" ]; then
            already_tombstoned=1
        fi

        if [ "$already_tombstoned" -eq 0 ]; then
            # ── Children guard ────────────────────────────────────────────────
            local child_ids
            child_ids=$(python3 - "$TRACKER_DIR" "$ticket_id" <<'PYEOF'
import json, os, sys
from pathlib import Path

tracker_dir = sys.argv[1]
parent_id   = sys.argv[2]

children = []
for entry in sorted(Path(tracker_dir).iterdir()):
    if not entry.is_dir():
        continue
    tid = entry.name
    if tid.startswith('.') or tid == parent_id:
        continue
    tombstone = entry / '.tombstone.json'
    archived  = entry / '.archived'
    if tombstone.is_file() or archived.is_file():
        continue
    # Scan event files for a CREATE event that has this parent_id
    for ef in sorted(entry.glob('*-CREATE.json')):
        try:
            with open(ef, encoding='utf-8') as fh:
                ev = json.load(fh)
            if ev.get('data', {}).get('parent_id') == parent_id:
                children.append(tid)
                break
        except (OSError, json.JSONDecodeError):
            continue
print(' '.join(children))
PYEOF
)
            # set -euo pipefail (active in this subshell) aborts on non-zero python exit.
            if [ -n "$child_ids" ]; then
                echo "Cannot delete ticket '$ticket_id': has non-deleted children: $child_ids" >&2
                return 1
            fi
        fi

        local env_id author
        env_id=$(cat "$TRACKER_DIR/.env-id" 2>/dev/null || echo "unknown")
        author=$(git config user.name 2>/dev/null || echo "Unknown")

        # ── UNLINK scan: write event files, print paths to stdout (no commit) ──
        # All staging is deferred to the single atomic commit below.
        # Uses ticket-delete-unlink-scan.py (standalone helper) which uses
        # reduce_all_tickets() for O(N) scan with SNAPSHOT support.
        local unlink_files_raw
        unlink_files_raw=$(python3 "$_TICKETLIB_DIR/ticket-delete-unlink-scan.py" \
            "$TRACKER_DIR" "$ticket_id" "$env_id" "$author")
        # set -euo pipefail (active in this subshell) aborts on non-zero python exit.

        if [ "$already_tombstoned" -eq 1 ]; then
            # Re-invocation: commit any remaining UNLINK cleanup, then return.
            local _uf _unlink_staged=()
            while IFS= read -r _uf; do
                # Strip TRACKER_DIR prefix: git -C "$TRACKER_DIR" add expects relative paths.
                [ -n "$_uf" ] && _unlink_staged+=("${_uf#"$TRACKER_DIR/"}")
            done <<< "$unlink_files_raw"
            if [ "${#_unlink_staged[@]}" -gt 0 ]; then
                git -C "$TRACKER_DIR" add "${_unlink_staged[@]}"
                git -C "$TRACKER_DIR" commit -q --no-verify \
                    -m "ticket: UNLINK cleanup for already-deleted $ticket_id"
            fi
            return 0
        fi

        # ── Write STATUS(deleted) event ───────────────────────────────────────
        local status_event_path
        status_event_path=$(python3 -c "
import json, sys, time, uuid as _uuid
ts = time.time_ns()
ev = str(_uuid.uuid4())
event = {
    'timestamp': ts,
    'uuid': ev,
    'event_type': 'STATUS',
    'env_id': sys.argv[1],
    'author': sys.argv[2],
    'data': {'status': 'deleted'},
}
path = sys.argv[3] + '/' + str(ts) + '-' + ev + '-STATUS.json'
with open(path, 'w', encoding='utf-8') as f:
    json.dump(event, f, ensure_ascii=False)
print(path)
" "$env_id" "$author" "$TICKET_DIR") || {
            echo "Error: failed to write STATUS(deleted) event" >&2
            return 1
        }

        # ── Write ARCHIVED event ──────────────────────────────────────────────
        local archived_event_path
        archived_event_path=$(python3 -c "
import json, sys, time, uuid as _uuid
ts = time.time_ns()
ev = str(_uuid.uuid4())
event = {
    'timestamp': ts,
    'uuid': ev,
    'event_type': 'ARCHIVED',
    'env_id': sys.argv[1],
    'author': sys.argv[2],
    'data': {},
}
path = sys.argv[3] + '/' + str(ts) + '-' + ev + '-ARCHIVED.json'
with open(path, 'w', encoding='utf-8') as f:
    json.dump(event, f, ensure_ascii=False)
print(path)
" "$env_id" "$author" "$TICKET_DIR") || {
            echo "Error: failed to write ARCHIVED event" >&2
            return 1
        }

        # ── Write .tombstone.json ─────────────────────────────────────────────
        python3 -c "
import json, sys
with open(sys.argv[1], 'w', encoding='utf-8') as f:
    json.dump({'status': 'deleted'}, f, ensure_ascii=False)
" "$TICKET_DIR/.tombstone.json" || {
            echo "Error: failed to write .tombstone.json" >&2
            return 1
        }

        # ── Atomic commit: UNLINK + STATUS + ARCHIVED + .tombstone.json ───────
        # git -C "$TRACKER_DIR" add requires paths relative to TRACKER_DIR.
        local _sf _all_stage=()
        while IFS= read -r _sf; do
            [ -n "$_sf" ] && _all_stage+=("${_sf#"$TRACKER_DIR/"}")
        done <<< "$unlink_files_raw"
        _all_stage+=(
            "${status_event_path#"$TRACKER_DIR/"}"
            "${archived_event_path#"$TRACKER_DIR/"}"
            "${TICKET_DIR#"$TRACKER_DIR/"}/.tombstone.json"
        )
        git -C "$TRACKER_DIR" add "${_all_stage[@]}"
        git -C "$TRACKER_DIR" commit -q --no-verify -m "ticket: DELETE $ticket_id"

        # ── Write .archived marker (filesystem only, no commit needed) ────────
        python3 -c "
import sys
sys.path.insert(0, '$_TICKETLIB_DIR')
from ticket_reducer.marker import write_marker
write_marker(sys.argv[1])
" "$TICKET_DIR" 2>/dev/null || true

        # Scratch cleanup: remove per-ticket scratch dir (non-blocking; always returns 0)
        _scratch_cleanup_for_ticket "$ticket_id" 2>/dev/null || true

        # ── Detect tickets newly unblocked by this deletion (mirror transition) ──
        local _unblocked_ids=""
        local _batch_close_json
        _batch_close_json=$(python3 "$_TICKETLIB_DIR/ticket-unblock.py" --batch-close "$TRACKER_DIR" "$ticket_id" 2>/dev/null) || true
        if [ -n "$_batch_close_json" ]; then
            _unblocked_ids=$(printf '%s' "$_batch_close_json" | python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
ids = d.get('newly_unblocked', [])
print(','.join(ids)) if ids else None
" 2>/dev/null) || _unblocked_ids=""
        fi

        # ── Output ──────────────────────────────────────────────────────────────
        # --output json: {ticket_id, deleted, newly_unblocked[]}. Default (text):
        # the "Deleted ..." line + the UNBLOCKED: signal.
        if [ "$_del_fmt" = "json" ]; then
            python3 -c '
import json, sys
ids = [x for x in sys.argv[2].split(",") if x]
print(json.dumps({"ticket_id": sys.argv[1], "deleted": True, "newly_unblocked": ids}))' \
                "$ticket_id" "$_unblocked_ids"
        else
            echo "Deleted ticket '$ticket_id'"
            if [ -n "$_unblocked_ids" ]; then
                echo "UNBLOCKED: $_unblocked_ids"
            else
                echo "UNBLOCKED: none"
            fi
        fi
    )
}
