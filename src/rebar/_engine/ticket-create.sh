#!/usr/bin/env bash
_ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ticket-create.sh
# Create a new ticket with a CREATE event committed to the tickets branch.
#
# Usage: ticket-create.sh <ticket_type> <title> [--parent <id>] [--priority <n>] [--assignee <name>]
#   ticket_type: one of bug, epic, story, task
#   title: non-empty string
#   --parent: optional parent ticket ID (must exist in .tickets-tracker/)
#   --priority: optional priority (0-4; 0=critical, 4=backlog; default: 2)
#   --assignee: optional assignee name (defaults to unassigned)
#
# Outputs the created ticket ID to stdout (only the ID — no other output).
set -euo pipefail

# Unset git hook env vars before any git commands so REPO_ROOT resolves from CWD.
unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_COMMON_DIR 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./ticket-lib.sh
source "$SCRIPT_DIR/ticket-lib.sh"
# Canonical structured-output flag (--output/-o); logic in ticket_output.py.
# shellcheck source=/dev/null
source "$SCRIPT_DIR/ticket-output.sh"

# Resolve --output/-o (report profile: text|json) and strip it before the
# positional <type> <title> + flag parsing below.
_resolve_output_format report "$@" || exit 2
_strip_output_flags "$@"
set -- ${_OUTPUT_ARGS[@]+"${_OUTPUT_ARGS[@]}"}

REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"
TRACKER_DIR="${TICKETS_TRACKER_DIR:-$REPO_ROOT/.tickets-tracker}"

# ── Usage ─────────────────────────────────────────────────────────────────────
_usage() {
    echo "Usage: ticket create <ticket_type> <title> [--parent <id>] [--priority <n>] [--assignee <name>] [--description <text>] [--tags <tag1,tag2>]" >&2
    echo "  ticket_type: bug | epic | story | task" >&2
    echo "  title: non-empty string" >&2
    echo "  --parent: optional parent ticket ID" >&2
    echo "  --priority, -p: 0-4 (0=critical, 4=backlog; default: 2)" >&2
    echo "  --assignee: assignee name (default: unassigned)" >&2
    echo "  --description, -d: optional description text" >&2
    echo "  --tags: comma-separated list of tags" >&2
    exit 1
}

# ── Validate arguments ───────────────────────────────────────────────────────
if [ $# -lt 2 ]; then
    _usage
fi

ticket_type="$1"
title="$2"
shift 2

# Parse remaining args: support both positional parent_id and --parent <id>
priority="2"
parent_id=""
assignee=""
description=""
tags=""
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
        -p)
            priority="$2"
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

# Assignee defaults to empty (unassigned) when not provided. The `author`
# field already records the creator (from `git config user.name`); the
# `assignee` field is for designated ownership, which is rarely the
# creator. Defaulting to git user.name conflated the two and caused
# bridge-side ACLI rejections when the local git user.name doesn't match a
# valid Jira user.

# Validate ticket_type
case "$ticket_type" in
    bug|epic|story|task) ;;
    *)
        echo "Error: invalid ticket type '$ticket_type'. Must be one of: bug, epic, story, task" >&2
        exit 1
        ;;
esac

# Validate title is non-empty
if [ -z "$title" ]; then
    echo "Error: title must be non-empty" >&2
    exit 1
fi

# ── Unicode arrow conversion (U+2192 → ASCII ->) ────────────────────────────
# Convert unicode arrow → to ASCII -> in title before event creation.
title=$(python3 -c "import sys; print(sys.argv[1].replace('\u2192', '->'))" "$title")

# ── Validate ticket system is initialized ─────────────────────────────────────
if [ ! -f "$TRACKER_DIR/.env-id" ]; then
    echo "Error: ticket system not initialized. Run 'ticket init' first." >&2
    exit 1
fi

# ── Validate parent_id exists if provided ─────────────────────────────────────
if [ -n "$parent_id" ]; then
    # Resolve alias or jira-* ID to canonical ticket_id (mirrors ticket-edit.sh:145)
    if ! parent_id=$(TICKETS_TRACKER_DIR="$TRACKER_DIR" resolve_ticket_id "$parent_id"); then
        exit 1
    fi
    if [ ! -d "$TRACKER_DIR/$parent_id" ]; then
        echo "Error: parent ticket '$parent_id' does not exist" >&2
        exit 1
    fi
    # Verify it has a CREATE or SNAPSHOT event (SNAPSHOT replaces CREATE after compaction)
    if ! find "$TRACKER_DIR/$parent_id" -maxdepth 1 \( -name '*-CREATE.json' -o -name '*-SNAPSHOT.json' \) ! -name '.*' 2>/dev/null | grep -q .; then
        echo "Error: parent ticket '$parent_id' has no CREATE or SNAPSHOT event" >&2
        exit 1
    fi
    # Guard: cannot create a child under a closed parent
    parent_status=$(ticket_read_status "$TRACKER_DIR" "$parent_id") || {
        echo "Error: could not read status for parent ticket '$parent_id'" >&2
        exit 1
    }
    if [ "$parent_status" = "closed" ]; then
        echo "Error: cannot create child of closed ticket '$parent_id'. Reopen the parent first with: ticket transition $parent_id closed open" >&2
        exit 1
    fi
fi

# ── Generate ticket ID and event metadata ─────────────────────────────────────
env_id=$(cat "$TRACKER_DIR/.env-id")
author=$(git config user.name 2>/dev/null || echo "Unknown")

# Generate collision-resistant short ID + full UUID4 + timestamp via single python3 call
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

# ── Compute human-readable alias from ticket ID ───────────────────────────────
# Honour TICKET_WORDLIST_PATH env override (for testing); fall back to the
# wordlist bundled with the plugin.
# ${SCRIPT_DIR%/scripts} strips the trailing "/scripts" suffix — assumes this script lives
# in a directory named "scripts" with "resources" as a sibling. Matches the plugin layout.
_wordlist="${TICKET_WORDLIST_PATH:-${SCRIPT_DIR%/scripts}/resources/ticket-wordlist.txt}"
_alias_stderr=$(mktemp /tmp/ticket-alias-stderr.XXXXXX)
ticket_alias=$(python3 "$SCRIPT_DIR/ticket-alias-compute.py" "$ticket_id" "$_wordlist" 2>"$_alias_stderr")
if grep -q "^FALLBACK$" "$_alias_stderr" 2>/dev/null; then
    echo "WARN: ticket-wordlist.txt not found — using hex fallback alias" >&2
fi
rm -f "$_alias_stderr"

# ── Build CREATE event JSON via python3 ───────────────────────────────────────
temp_event=$(mktemp "$TRACKER_DIR/.tmp-create-XXXXXX")
# Write description to temp file to avoid ARG_MAX limits on large payloads (195e-b410)
desc_file=$(mktemp "$TRACKER_DIR/.tmp-desc-XXXXXX")
# Ensure both temp files are cleaned up on any exit path.
# The explicit rm -f calls in error blocks below are intentional belt-and-suspenders: they make the
# cleanup intent visible at the failure site and run before `exit 1` in the normal error path.
# rm -f is idempotent so double-cleanup is harmless. The pattern is: trap covers unexpected paths;
# explicit rm covers expected error paths. Both are needed for clarity and correctness.
trap 'rm -f "$temp_event" "$desc_file"' EXIT
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
    exit 1
}
rm -f "$desc_file"

# ── Write and commit via ticket-lib.sh ────────────────────────────────────────
write_commit_event "$ticket_id" "$temp_event" || {
    rm -f "$temp_event"
    echo "Error: failed to write and commit CREATE event" >&2
    exit 1
}

# Clean up temp file (write_commit_event stages it, but original temp may remain)
rm -f "$temp_event"

# ── Output ────────────────────────────────────────────────────────────────────
# --output json: one structured object {id, alias, title}. Default (text): the
# human summary first, canonical ID last (both stdout; `| tail -1` id-scrape).
if [ "$_OUTPUT_FMT" = "json" ]; then
    python3 -c 'import json,sys; print(json.dumps({"id": sys.argv[1], "alias": (sys.argv[2] or None), "title": sys.argv[3]}))' \
        "$ticket_id" "$ticket_alias" "$title"
else
    # Lead with the human-readable alias when available; canonical ID is parenthetical.
    if [ -n "$ticket_alias" ] && [ "$ticket_alias" != "$ticket_id" ]; then
        echo "Created ticket $ticket_alias ($ticket_id): $title"
    else
        echo "Created ticket $ticket_id: $title"
    fi
    echo "$ticket_id"
fi

# ── Post-creation validation (warnings only, never blocks, exit 0) ───────────
# Only applies to bug tickets.
if [ "$ticket_type" = "bug" ]; then
    # Read config for title warning opt-out
    _title_warning_enabled="true"
    _read_config="$SCRIPT_DIR/read-config.sh"
    if [ -f "$_read_config" ]; then
        _cfg_val=$(bash "$_read_config" "bug_report.title_warning_enabled" 2>/dev/null) || true
        if [ "$_cfg_val" = "false" ]; then
            _title_warning_enabled="false"
        fi
    fi

    # (a) Title pattern warning: [Component]: [Condition] -> [Observed Result]
    if [ "$_title_warning_enabled" = "true" ]; then
        if ! echo "$title" | grep -qE '^[^:]+: .+ -> .+$'; then
            echo "ERROR: Bug title does not match required pattern: [Component]: [Condition] -> [Observed Result]" >&2
            echo "  To fix: ticket edit $ticket_id --title=\"[Component]: [Condition] -> [Observed Result]\"" >&2
        fi
    fi

    # (b) Description headers warning: check for Expected Behavior and Actual Behavior
    if [ -n "$description" ]; then
        _desc_lower=$(echo "$description" | tr '[:upper:]' '[:lower:]')
        _missing_headers=""
        if ! echo "$_desc_lower" | grep -q "expected behavior"; then
            _missing_headers="Expected Behavior"
        fi
        if ! echo "$_desc_lower" | grep -q "actual behavior"; then
            if [ -n "$_missing_headers" ]; then
                _missing_headers="$_missing_headers, Actual Behavior"
            else
                _missing_headers="Actual Behavior"
            fi
        fi
        if [ -n "$_missing_headers" ]; then
            echo "Warning: Bug description missing recommended headers: $_missing_headers" >&2
            echo "  To fix: ticket edit $ticket_id --description=\"...\"" >&2
            echo "  Recommended bug headers: Steps to Reproduce, Expected, Actual." >&2
        fi
    fi

    # (c) Description size warning: > 30000 characters
    if [ -n "$description" ]; then
        _desc_len=${#description}
        if [ "$_desc_len" -gt 30000 ]; then
            echo "Warning: Bug description exceeds 30000 characters ($_desc_len chars)" >&2
        fi
    fi
fi
