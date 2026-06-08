#!/usr/bin/env bash
# scripts/check-acceptance-criteria.sh
# Verify a ticket contains a structured ACCEPTANCE CRITERIA section before sub-agent dispatch.
#
# Usage: check-acceptance-criteria.sh <id>
# Exit codes: 0 = block found, 1 = block missing
# Output: AC_CHECK: pass (<N> criteria lines) | AC_CHECK: fail - no ACCEPTANCE CRITERIA section

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TICKET_CMD="${TICKET_CMD:-$SCRIPT_DIR/ticket}"

ID="${1:?Usage: check-acceptance-criteria.sh <id>}"

# ticket show exits non-zero when a ticket is not found.
# Capture output and check exit code to detect failure.
output=$("$TICKET_CMD" show "$ID" 2>/dev/null) || {
    echo "AC_CHECK: fail - could not load $ID"
    exit 1
}
if [ -z "$output" ]; then
    echo "AC_CHECK: fail - could not load $ID"
    exit 1
fi

# Extract text from v3 JSON ticket output: combine title, description, and
# all comment bodies into a single markdown text block for awk parsing.
text=$(echo "$output" | python3 -c "
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

# Count checklist items in the ## Acceptance Criteria section using awk.
# Matches the "## Acceptance Criteria" heading from the extracted text.
# Terminates on the next ## heading.
# Blank lines within the block are allowed (does NOT terminate on blank lines).
ac_count=$(echo "$text" | awk '
  tolower($0) ~ /^## acceptance criteria/ { found=1; next }
  found && /^## / { found=0; next }
  found && /^- \[/ { count++ }
  END { print count+0 }
')

# Defensive default: ensure ac_count is numeric
ac_count="${ac_count:-0}"

if [ "$ac_count" -ge 1 ]; then
    echo "AC_CHECK: pass ($ac_count criteria lines)"
    exit 0
else
    echo "AC_CHECK: fail - no ACCEPTANCE CRITERIA section in $ID (use: rebar create with an '## Acceptance Criteria' section with checklist items)"
    exit 1
fi
