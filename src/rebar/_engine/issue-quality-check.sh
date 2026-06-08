#!/usr/bin/env bash
set -euo pipefail
# scripts/issue-quality-check.sh
# Check whether a ticket has enough detail for issue-as-prompt dispatch.
# Sub-agents using issue-as-prompt read their own context via `ticket show`.
# This script validates the ticket is detailed enough for that pattern.
#
# Usage:
#   issue-quality-check.sh <id>
#
# Exit codes:
#   0 = quality sufficient (issue-as-prompt is safe)
#   1 = too sparse (fall back to inline prompt)
#
# Output (single line):
#   QUALITY: pass (<line_count> lines, <keyword_count> criteria, <ac_items> AC items, <file_impact> file impact)
#   QUALITY: fail - description too sparse (<line_count> lines), using inline prompt

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ $# -ne 1 ]; then
    echo "Usage: issue-quality-check.sh <id>" >&2
    exit 1
fi

ID="$1"

# Get the full issue output (stays in script, not in orchestrator context).
# TICKET_CMD is the sole interface post-v3 migration.
TICKET_CMD="${TICKET_CMD:-$SCRIPT_DIR/ticket}"
output=$("$TICKET_CMD" show "$ID" 2>/dev/null) || output=""
if [ -z "$output" ]; then
    echo "QUALITY: fail - could not load issue $ID, using inline prompt"
    exit 1
fi

# Extract fields from v3 JSON ticket show output.
ticket_type=$(echo "$output" | python3 -c "import json,sys; print(json.load(sys.stdin).get('ticket_type','task'))" 2>/dev/null || echo "task")

# Combine title, description, and all comment bodies as the quality text.
description=$(echo "$output" | python3 -c "
import json, sys
t = json.load(sys.stdin)
parts = []
if t.get('title'):
    parts.append(t['title'])
if t.get('description'):
    parts.append(t['description'])
for c in t.get('comments', []):
    body = c.get('body', '')
    if body:
        parts.append(body)
print('\n'.join(parts))
" 2>/dev/null || echo "")

# Count description lines (non-empty)
line_count=$(echo "$description" | grep -c '[^ ]' 2>/dev/null || echo "0")
line_count=$(echo "$line_count" | tr -d '[:space:]')

# Count acceptance criteria indicators
keyword_count=0
# File path patterns (src/, tests/, app/)
_kw_files=$(echo "$description" | grep -c -E '(src/|tests/|app/|\.py|\.ts|\.js|\.html)' 2>/dev/null || echo "0")
_kw_files=$(echo "$_kw_files" | tr -d '[:space:]')
keyword_count=$(( keyword_count + _kw_files ))
# Criteria keywords
_kw_criteria=$(echo "$description" | grep -c -iE '(must|should|given|when|then|acceptance|criteria|expect|verify|ensure)' 2>/dev/null || echo "0")
_kw_criteria=$(echo "$_kw_criteria" | tr -d '[:space:]')
keyword_count=$(( keyword_count + _kw_criteria ))

# Count acceptance criteria items in ## Acceptance Criteria section.
# $description includes headings from comment bodies.
ac_items=$(echo "$description" | awk '
  tolower($0) ~ /^## acceptance criteria/ { found=1; next }
  found && /^## / { exit }
  found && /^- \[/ { count++ }
  END { print count+0 }
')
ac_items="${ac_items:-0}"

# Count file impact items in ## File Impact or ### Files to modify section.
file_impact_items=$(echo "$description" | awk '
  tolower($0) ~ /^## file impact/ || tolower($0) ~ /^### files to modify/ { found=1; next }
  found && /^## / { exit }
  found && /^### / && tolower($0) !~ /^### files to/ { exit }
  found && /(src\/|tests\/|app\/|\.py|\.ts|\.js|\.html)/ { count++ }
  END { print count+0 }
')
file_impact_items="${file_impact_items:-0}"

# Supplement: check structured FILE_IMPACT events via ticket get-file-impact
if [ "$file_impact_items" -eq 0 ]; then
    _fi_count=$(${TICKET_CMD:-ticket} get-file-impact "$ID" 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d))' 2>/dev/null || echo 0)
    if [ "${_fi_count:-0}" -gt 0 ]; then
        file_impact_items=$_fi_count
    fi
fi

# Quality gate: branch on ticket type.
# Stories use prose done-definitions by design — no AC block required.
if [ "$ticket_type" = "story" ]; then
    if [ "$line_count" -ge 5 ] && [ "$keyword_count" -ge 1 ]; then
        echo "QUALITY: pass (story - prose done-definitions) ($line_count lines, $keyword_count criteria)"
        exit 0
    else
        echo "QUALITY: fail - description too sparse ($line_count lines), using inline prompt"
        exit 1
    fi
fi

# Phase 1: warn but don't enforce AC block requirement (tasks/bugs/epics)
if [ "$ac_items" -ge 1 ]; then
    echo "QUALITY: pass ($line_count lines, $keyword_count criteria, $ac_items AC items, $file_impact_items file impact)"
    exit 0
elif [ "$file_impact_items" -ge 1 ]; then
    echo "QUALITY: pass ($line_count lines, $keyword_count criteria, $file_impact_items file impact)"
    exit 0
elif [ "$line_count" -ge 5 ] && [ "$keyword_count" -ge 1 ]; then
    echo "QUALITY: pass (legacy - no AC/file impact) ($line_count lines, $keyword_count criteria)"
    echo "WARNING: Task lacks Acceptance block and File Impact section. Add via 'rebar comment <id> <note>'." >&2
    exit 0
else
    echo "QUALITY: fail - description too sparse ($line_count lines), using inline prompt"
    exit 1
fi
