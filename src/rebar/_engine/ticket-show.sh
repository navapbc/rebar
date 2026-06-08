#!/usr/bin/env bash
# ticket-show.sh
# Show compiled state for one or more tickets by invoking the event reducer.
#
# Usage: ticket show [--format=<fmt>] [--include-scratch] <ticket_id> [<ticket_id> ...]
#   ticket_id: ID(s) of the ticket(s) to show. Multiple IDs print one
#              compiled state per ticket; default format outputs each as a
#              standalone pretty-printed JSON document separated by blank
#              lines; --format=llm emits one minified JSON object per line
#              (NDJSON-compatible).
#   --format=llm  Minified single-line JSON with shortened keys, stripped nulls,
#                 and no verbose timestamps (created_at and env_id are omitted entirely).
#                 Key mapping:
#                   ticket_id   → id
#                   ticket_type → t
#                   title       → ttl
#                   status      → st
#                   author      → au
#                   parent_id   → pid
#                   priority    → pr
#                   assignee    → asn
#                   comments    → cm
#                   deps        → dp
#                   conflicts   → cf
#                   inbound_links → ibl (sub-keys: from_id→f, relation→r)
#                   children    → ch
#   --include-scratch  Merge per-ticket scratch store entries into the output
#                      as a top-level "scratch" object:
#                        { "<key>": { "ts": "<iso8601>", "value": "<string>" }, ... }
#                      When the scratch directory is absent or empty, emits
#                      scratch: {} (empty object, not absent). When this flag
#                      is omitted, no "scratch" key appears in the output
#                      (backward-compatible default).
#
# Exit code: 0 if all requested tickets resolve and reduce successfully; 1 if
# any one fails (the failure is reported and remaining tickets are still
# processed before exit, so callers can scan the full output).
#
# Bug jira-dig-2565: prior versions silently dropped all positional args
# after the first.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=${_PLUGIN_ROOT}/scripts/ticket-lib.sh
source "$SCRIPT_DIR/ticket-lib.sh"

# Unset git hook env vars so git commands target the correct repo.
unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

# Allow tests to inject a custom tracker directory via TICKETS_TRACKER_DIR env var.
if [ -n "${TICKETS_TRACKER_DIR:-}" ]; then
    TRACKER_DIR="$TICKETS_TRACKER_DIR"
else
    REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"
    TRACKER_DIR="$REPO_ROOT/.tickets-tracker"
fi

# ── Usage ─────────────────────────────────────────────────────────────────────
_usage() {
    echo "Usage: ticket show [--format=llm] [--include-scratch] <ticket_id> [<ticket_id> ...]" >&2
    exit 1
}

# ── Parse arguments ──────────────────────────────────────────────────────────
# Multi-ID support (bug jira-dig-2565): collect all positional args, not
# just the first. Each ID is resolved and reduced independently below.
format="default"
include_scratch="false"
ticket_ids=()

for arg in "$@"; do
    case "$arg" in
        --format=llm)
            format="llm"
            ;;
        --format=*)
            echo "Error: unsupported format '${arg#--format=}'. Supported: llm" >&2
            exit 1
            ;;
        --include-scratch)
            include_scratch="true"
            ;;
        -*)
            echo "Error: unknown option '$arg'" >&2
            _usage
            ;;
        *)
            ticket_ids+=("$arg")
            ;;
    esac
done

if [ "${#ticket_ids[@]}" -eq 0 ]; then
    _usage
fi

# ── Resolve, verify, and reduce each ID ──────────────────────────────────────
# Process each ticket independently. A failure on one does not abort the
# rest; the script exits 1 at the end if any failed.
_overall_rc=0
_idx=0
for _raw_id in "${ticket_ids[@]}"; do
    _idx=$((_idx + 1))

    # Resolve any ID form (full, short, alias, jira_key, prefix) to canonical.
    if ! ticket_id=$(TICKETS_TRACKER_DIR="$TRACKER_DIR" resolve_ticket_id "$_raw_id"); then
        _overall_rc=1
        continue
    fi

    if [ ! -d "$TRACKER_DIR/$ticket_id" ]; then
        echo "Error: Ticket '$ticket_id' not found" >&2
        _overall_rc=1
        continue
    fi

    # In default (pretty) format, separate multi-ticket output with a blank
    # line for readability; LLM format already emits one self-delimiting
    # object per line (NDJSON), so no separator is needed.
    if [ "$format" != "llm" ] && [ "$_idx" -gt 1 ]; then
        echo
    fi

    # ── Invoke reducer ────────────────────────────────────────────────────────
    # Single subprocess handles reduce + format (no pipeline).
    # Test invariant (test-ticket-subprocess-count.sh): exactly one direct
    # interpreter spawn between this marker and the next `^fi$`. The
    # multi-ID outer loop introduced for bug jira-dig-2565 runs this one
    # call per ID; the per-section count is unchanged.
    _TICKET_DIR="$TRACKER_DIR/$ticket_id" _TICKET_ID="$ticket_id" \
    _FORMAT="$format" _SCRIPT_DIR="$SCRIPT_DIR" \
    _INCLUDE_SCRATCH="$include_scratch" \
    _SCRATCH_BASE_DIR="${SCRATCH_BASE_DIR:-}" \
    python3 -c "
import sys, os, json
sys.path.insert(0, os.environ['_SCRIPT_DIR'])
from ticket_reducer import reduce_ticket, find_inbound_relationships

ticket_dir = os.environ['_TICKET_DIR']
ticket_id = os.environ['_TICKET_ID']
fmt = os.environ.get('_FORMAT', 'default')
include_scratch = os.environ.get('_INCLUDE_SCRATCH', 'false') == 'true'

state = reduce_ticket(ticket_dir)
if state is None:
    print(f'Error: ticket \"{ticket_id}\" has no CREATE or SNAPSHOT event', file=sys.stderr)
    sys.exit(1)
if state.get('status') in ('error', 'fsck_needed'):
    print(json.dumps(state, ensure_ascii=False))
    print(f'Error: ticket \"{ticket_id}\" has status \"{state[\"status\"]}\"', file=sys.stderr)
    sys.exit(1)

# Augment with inbound relationships (incoming links + child tickets) so
# 'ticket show' presents the complete relationship picture, not just the
# outgoing links stored in this ticket's own directory. Derived read-only by
# scanning only the tickets that mention this ID — no events are written.
_inbound = find_inbound_relationships(ticket_id, os.path.dirname(ticket_dir))
state['inbound_links'] = _inbound['inbound_links']
state['children'] = _inbound['children']

if include_scratch:
    # Resolve scratch base directory: prefer explicit env override, else
    # fall back to <repo_root>/.claude/scratch/ relative to the tracker dir.
    scratch_base = os.environ.get('_SCRATCH_BASE_DIR', '').strip()
    if not scratch_base:
        # Infer repo root as two levels above tracker dir
        # (.tickets-tracker/ is at repo root, so parent of tracker_dir is repo root)
        tracker_parent = os.path.dirname(ticket_dir)  # tracker dir itself
        repo_root = os.path.dirname(tracker_parent)   # repo root
        scratch_base = os.path.join(repo_root, '.claude', 'scratch')
    scratch_ticket_dir = os.path.join(scratch_base, ticket_id)
    scratch_data = {}
    if os.path.isdir(scratch_ticket_dir):
        for entry in sorted(os.listdir(scratch_ticket_dir)):
            entry_path = os.path.join(scratch_ticket_dir, entry)
            # Skip non-files (subdirs, hidden files, tmp artifacts)
            if not os.path.isfile(entry_path):
                continue
            if entry.startswith('.') or '.tmp.' in entry:
                continue
            try:
                with open(entry_path, 'r', encoding='utf-8') as f:
                    envelope = json.load(f)
                scratch_data[entry] = {
                    'ts': envelope.get('ts', ''),
                    'value': envelope.get('value', ''),
                }
            except (OSError, json.JSONDecodeError):
                pass  # skip unreadable/corrupt entries silently
    state['scratch'] = scratch_data

if fmt == 'llm':
    print(json.dumps(__import__('ticket_reducer.llm_format', fromlist=['to_llm']).to_llm(state), ensure_ascii=False, separators=(',', ':')))
else:
    print(json.dumps(state, indent=2, ensure_ascii=False))
    alerts = state.get('bridge_alerts', [])
    unresolved = sum(1 for a in alerts if not a.get('resolved', False))
    if unresolved > 0:
        print(
            f'WARNING: ticket {ticket_id} has {unresolved} unresolved bridge alert(s).'
            ' Run: ticket bridge-status for details.',
            file=sys.stderr,
        )
" || _overall_rc=1
done

exit "$_overall_rc"
