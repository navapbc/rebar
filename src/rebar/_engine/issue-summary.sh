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
    output=$("$TICKET_CMD" show "$id" 2>/dev/null) || { echo "$id [unknown]"; continue; }

    if [ -z "$output" ]; then
        echo "$id [unknown]"
        continue
    fi

    # Parse title and status from first line:
    # Format: ○ <id> · <title>   [● P<N> · <STATUS>]
    first_line=$(echo "$output" | head -n1)

    # Extract title: between first '·' and the '[' bracket
    title=$(echo "$first_line" | sed 's/^[^·]*·[[:space:]]*//' | sed 's/[[:space:]]*\[.*$//' | sed 's/[[:space:]]*$//')
    if [ -z "$title" ]; then
        title="untitled"
    fi

    # Extract status: last word before closing ']' in the bracket section
    # e.g. [● P2 · OPEN] -> OPEN
    status=$(echo "$first_line" | grep -oE '\[[^]]+\]' | tail -1 | sed 's/.*·[[:space:]]*//' | sed 's/\]//' | tr '[:upper:]' '[:lower:]' || true)
    if [ -z "$status" ]; then
        status="unknown"
    fi

    # Extract blocked-by deps from DEPENDS ON section
    # Lines look like:  → ○ tk-test-001: First task ● P2
    # We capture the ID (text before the first colon on those lines)
    deps=""
    in_depends_on=0
    while IFS= read -r line; do
        if echo "$line" | grep -qE '^DEPENDS ON'; then
            in_depends_on=1
            continue
        fi
        # A new section header (all caps word(s) on own line, no leading whitespace) ends DEPENDS ON
        if [ "$in_depends_on" -eq 1 ]; then
            if echo "$line" | grep -qE '^[A-Z][A-Z ]+$'; then
                in_depends_on=0
                continue
            fi
            # Extract dep ID: the token matching tk-* or similar before ':'
            dep_id=$(echo "$line" | grep -oE '[a-z][a-z0-9._-]+:[[:space:]]' | head -1 | sed 's/:[[:space:]]*//')
            if [ -n "$dep_id" ]; then
                if [ -z "$deps" ]; then
                    deps="$dep_id"
                else
                    deps="$deps $dep_id"
                fi
            fi
        fi
    done <<< "$output"

    if [ -z "$deps" ]; then
        echo "$id [$status] $title (ready)"
    else
        echo "$id [$status] $title (blocked by: $deps)"
    fi
done
