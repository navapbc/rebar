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

if [ $# -eq 0 ]; then
    echo "Usage: issue-summary.sh <id> [<id> ...]" >&2
    exit 1
fi

for id in "$@"; do
    # `show` and `deps` both emit JSON (bug aa2e-3dcd: the previous parser
    # scraped a long-retired human-readable `show` layout, so every ticket
    # rendered as "[unknown] {"). Parse the JSON directly instead.
    show_json=$("$TICKET_CMD" show "$id" 2>/dev/null) || { echo "$id [unknown]"; continue; }
    if [ -z "$show_json" ]; then
        echo "$id [unknown]"
        continue
    fi

    # title + status from `show`; blocked-by + readiness from `deps`. A failed
    # deps lookup degrades gracefully to "ready" rather than aborting the line.
    deps_json=$("$TICKET_CMD" deps "$id" 2>/dev/null) || deps_json=""

    summary_line=$(SHOW_JSON="$show_json" DEPS_JSON="$deps_json" TICKET_ID="$id" python3 -c '
import json, os, sys

tid = os.environ["TICKET_ID"]
try:
    show = json.loads(os.environ["SHOW_JSON"])
except Exception:
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
print(f"{tid} [{status}] {title} ({suffix})")
') || { echo "$id [unknown]"; continue; }
    echo "$summary_line"
done
