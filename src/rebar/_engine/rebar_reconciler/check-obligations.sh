#!/usr/bin/env bash
# check-obligations.sh — Audit open rollout obligation tickets and file
# overdue bugs.
#
# Iterates every open ticket tagged `obligation:rollout`. For each whose
# `Deadline: YYYY-MM-DD` (parsed from description) is in the past, files a
# P1 bug parented to the obligation's own parent story with body
# "OBLIGATION OVERDUE: <obligation-id>, validation command: <cmd>, days overdue: <N>".
#
# This is an AUDIT tool — it always exits 0 (even on parse errors or
# ticket-CLI failures) so it can be safely scheduled as a periodic monitor.
#
# Usage:
#   bash "${_PLUGIN_GIT_PATH}/scripts/rebar_reconciler/check-obligations.sh"
#
# Environment:
#   DSO_TICKET_CLI   — override the ticket CLI path (default: the bundled rebar CLI)
#   DSO_TODAY        — override "today" (YYYY-MM-DD) for deterministic testing
#
# Contract: see docs/contracts/obligation-ticket-schema.md within the plugin tree.

set -uo pipefail

TICKET_CLI="${DSO_TICKET_CLI:-${REBAR_TICKET_CLI:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)/rebar}}"
TODAY="${DSO_TODAY:-$(date -u +%Y-%m-%d)}"

if [[ ! -x "$TICKET_CLI" ]]; then
    echo "check-obligations: ticket CLI not found at $TICKET_CLI" >&2
    exit 0
fi

# Convert YYYY-MM-DD to epoch days (portable across macOS/Linux).
_epoch_days() {
    local d="$1"
    python3 -c "
import datetime, sys
y,m,d = '$d'.split('-')
print((datetime.date(int(y),int(m),int(d)) - datetime.date(1970,1,1)).days)
" 2>/dev/null
}

TODAY_DAYS=$(_epoch_days "$TODAY")
if [[ -z "$TODAY_DAYS" ]]; then
    echo "check-obligations: could not parse DSO_TODAY=$TODAY" >&2
    exit 0
fi

# List open obligations
LIST_JSON=$("$TICKET_CLI" ticket list --has-tag=obligation:rollout --status=open --format=llm 2>/dev/null) || {
    echo "check-obligations: ticket list failed" >&2
    exit 0
}

# Iterate ids
IDS=$(printf '%s' "$LIST_JSON" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
items = data if isinstance(data, list) else data.get('tickets', data.get('items', []))
for t in items:
    tid = t.get('ticket_id') or t.get('id')
    if tid:
        print(tid)
" 2>/dev/null)

OVERDUE_FILED=0
for obligation_id in $IDS; do
    SHOW_JSON=$("$TICKET_CLI" ticket show "$obligation_id" 2>/dev/null) || continue
    # Extract fields. NUL-delimited output between fields keeps the
    # validation command intact even if it contains double-quotes,
    # backslashes, or other characters that would confuse newline-based
    # parsing or shell re-evaluation. Parent is read from the tracker's
    # structured `parent_id` field — NOT from the obligation description —
    # so an actor with edit access to the description cannot re-parent
    # the overdue bug. (Tracker write access is, in this threat model,
    # already equivalent to direct bug-filing rights — see Finding 3
    # defense — but reading the structured field eliminates the parsing
    # surface entirely.)
    # Write NUL-delimited parser output to a temp file, then read each
    # field via `read -d ''`. We avoid `$(...)` capture because bash
    # command substitution silently strips NUL bytes, which would
    # destroy our field separator.
    parsed_file=$(mktemp /tmp/check-obl-parsed.XXXXXX)
    printf '%s' "$SHOW_JSON" | python3 -c "
import json, re, sys
try:
    t = json.load(sys.stdin)
except Exception:
    sys.exit(0)
desc = t.get('description','') or ''
parent = t.get('parent_id','') or ''
m_dl = re.search(r'Deadline:\s*(\d{4}-\d{2}-\d{2})', desc)
# Validation command bounded at first newline so multi-line descriptions
# cannot inject extra log lines or smuggle in shell-control sequences.
m_cmd = re.search(r'Validation command:\s*([^\n\r]*)', desc)
deadline = m_dl.group(1) if m_dl else ''
cmd = m_cmd.group(1).strip() if m_cmd else ''
# NUL-delimited so the bash consumer can split unambiguously even when
# cmd contains double-quotes or other shell metacharacters.
sys.stdout.buffer.write(('\0'.join([deadline, parent, cmd]) + '\0').encode('utf-8'))
" > "$parsed_file" 2>/dev/null
    deadline=""; parent_story=""; val_cmd=""
    {
        IFS= read -r -d '' deadline || true
        IFS= read -r -d '' parent_story || true
        IFS= read -r -d '' val_cmd || true
    } < "$parsed_file"
    rm -f "$parsed_file"

    # Belt-and-suspenders: validate parent_story looks like a tracker ID
    # (alphanumeric, dash, underscore only). Defense in depth against any
    # future regression that might let untrusted text reach this variable.
    if [[ -n "$parent_story" && ! "$parent_story" =~ ^[A-Za-z0-9_-]+$ ]]; then
        parent_story=""
    fi

    [[ -z "$deadline" ]] && continue
    deadline_days=$(_epoch_days "$deadline")
    [[ -z "$deadline_days" ]] && continue

    if (( deadline_days < TODAY_DAYS )); then
        days_overdue=$(( TODAY_DAYS - deadline_days ))
        # Bash variable expansion inside double quotes does NOT re-parse the
        # value, so embedded double-quotes / metacharacters in $val_cmd are
        # preserved verbatim and reach `ticket create --description` as a
        # single argv element. No shell-quoting transformation is applied
        # (and none is needed): the value is passed through argv, not eval.
        body="OBLIGATION OVERDUE: $obligation_id, validation command: ${val_cmd:-<unspecified>}, days overdue: $days_overdue"
        title="Overdue obligation: $obligation_id (${days_overdue}d past deadline)"
        create_args=(ticket create bug "$title" --description "$body" --priority 1)
        if [[ -n "$parent_story" ]]; then
            create_args+=(--parent "$parent_story")
        fi
        "$TICKET_CLI" "${create_args[@]}" >/dev/null 2>&1 && OVERDUE_FILED=$((OVERDUE_FILED+1))
    fi
done

echo "check-obligations: filed $OVERDUE_FILED overdue bug(s) (today=$TODAY)" >&2
exit 0
