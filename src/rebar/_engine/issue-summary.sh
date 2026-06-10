#!/usr/bin/env bash
set -euo pipefail
# scripts/issue-summary.sh
# Wrapper that produces a one-line summary from `ticket show <id>`.
# Use for orchestrator status checks instead of full `ticket show` output.
#
# Usage:
#   issue-summary.sh <id>           # Single issue
#   issue-summary.sh <id1> <id2>    # Multiple issues
#
# Output (one line per issue):
#   <id> [<status>] <title> (blocked by: <ids>|ready)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TICKET_CMD="${TICKET_CMD:-$SCRIPT_DIR/ticket}"
# Canonical structured-output flag (--output/-o); logic in ticket_output.py.
# shellcheck source=/dev/null
source "$SCRIPT_DIR/ticket-output.sh"

# Resolve --output/-o (report: text|json) and strip it from the args.
_resolve_output_format report "$@" || exit 2
_strip_output_flags "$@"
set -- ${_OUTPUT_ARGS[@]+"${_OUTPUT_ARGS[@]}"}

if [ $# -eq 0 ]; then
    echo "Usage: issue-summary.sh <id> [<id> ...]" >&2
    exit 1
fi

# In --output json we collect one object per id and emit a single JSON array.
_json_items=()

_emit_unknown() {  # <id>
    if [ "$_OUTPUT_FMT" = "json" ]; then
        _json_items+=("$(python3 -c 'import json,sys; print(json.dumps({"ticket_id": sys.argv[1], "status": "unknown", "title": None, "blocking_summary": None}))' "$1")")
    else
        echo "$1 [unknown]"
    fi
}

for id in "$@"; do
    # `show` and `deps` both emit JSON (bug aa2e-3dcd: the previous parser
    # scraped a long-retired human-readable `show` layout, so every ticket
    # rendered as "[unknown] {"). Parse the JSON directly instead.
    show_json=$("$TICKET_CMD" show "$id" 2>/dev/null) || { _emit_unknown "$id"; continue; }
    if [ -z "$show_json" ]; then
        _emit_unknown "$id"
        continue
    fi

    # title + status from `show`; blocked-by + readiness from `deps`. A failed
    # deps lookup degrades gracefully to "ready" rather than aborting the line.
    deps_json=$("$TICKET_CMD" deps "$id" 2>/dev/null) || deps_json=""

    summary_out=$(SHOW_JSON="$show_json" DEPS_JSON="$deps_json" TICKET_ID="$id" FMT="$_OUTPUT_FMT" python3 -c '
import json, os, sys

tid = os.environ["TICKET_ID"]
fmt = os.environ.get("FMT", "text")
try:
    show = json.loads(os.environ["SHOW_JSON"])
except Exception:
    if fmt == "json":
        print(json.dumps({"ticket_id": tid, "status": "unknown", "title": None, "blocking_summary": None}))
    else:
        print(f"{tid} [unknown]")
    sys.exit(0)

title = show.get("title") or "untitled"
status = show.get("status") or "unknown"

blockers, ready = [], True
deps_raw = os.environ.get("DEPS_JSON") or ""
if deps_raw:
    try:
        deps = json.loads(deps_raw)
        blockers = deps.get("blockers") or []
        ready = bool(deps.get("ready_to_work", True))
    except Exception:
        pass

if blockers and not ready:
    suffix = "blocked by: " + " ".join(blockers)
else:
    suffix = "ready"
if fmt == "json":
    print(json.dumps({"ticket_id": tid, "status": status, "title": title, "blocking_summary": suffix}))
else:
    print(f"{tid} [{status}] {title} ({suffix})")
') || { _emit_unknown "$id"; continue; }
    if [ "$_OUTPUT_FMT" = "json" ]; then
        _json_items+=("$summary_out")
    else
        echo "$summary_out"
    fi
done

# --output json: wrap the per-id objects into a single JSON array.
if [ "$_OUTPUT_FMT" = "json" ]; then
    printf '%s\n' ${_json_items[@]+"${_json_items[@]}"} \
        | python3 -c 'import json,sys; print(json.dumps([json.loads(l) for l in sys.stdin if l.strip()]))'
fi
