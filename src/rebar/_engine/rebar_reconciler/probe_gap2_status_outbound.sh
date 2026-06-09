#!/usr/bin/env bash
# Gap 2 probe: outbound status transition design validation.
# Uses acli `workitem transition --status <name>` (ACLI has no separate
# "transitions list" subcommand — it resolves names per-issue server-side).
# Falls through to direct REST for transition enumeration.
set -euo pipefail

: "${JIRA_URL:?required}"
: "${JIRA_USER:?required}"
: "${JIRA_API_TOKEN:?required}"
: "${REBAR_FIELD_VALIDATION_PROBE:?must be set to 1}"

PROJECT="${JIRA_PROJECT:-DIG}"
TS=$(date +%s)
LABEL="gap2-probe-${TS}"
ISSUE_KEY=""
TMP_OUT=$(mktemp /tmp/gap2-probe.XXXXXX)

PASS=0; FAIL=0
pass() { echo "  PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $*"; FAIL=$((FAIL+1)); }

cleanup() {
  if [[ -n "$ISSUE_KEY" ]]; then
    echo ">> Cleanup: deleting $ISSUE_KEY"
    acli jira workitem delete --key "$ISSUE_KEY" --yes >/dev/null 2>&1 || echo "   (delete failed: $ISSUE_KEY)"
  fi
  rm -f "$TMP_OUT"
}
trap cleanup EXIT

curl_jira() {
  local path="$1"; shift
  curl -sS -u "$JIRA_USER:$JIRA_API_TOKEN" -H "Accept: application/json" \
    "$JIRA_URL$path" "$@"
}

echo "== Gap 2 probe: outbound status transitions =="

# Step 1: create issue
echo ">> Creating issue..."
acli jira workitem create \
  --project "$PROJECT" --type "Task" \
  --summary "DSO Gap2 probe $TS (auto-cleanup)" \
  --label "$LABEL" --json > "$TMP_OUT" 2>&1 || { cat "$TMP_OUT"; exit 2; }
ISSUE_KEY=$(python3 -c "import json; d=json.load(open('$TMP_OUT')); print(d.get('key') or d.get('issueKey') or '')")
[[ -n "$ISSUE_KEY" ]] || { echo "FAIL: no issue key"; cat "$TMP_OUT"; exit 2; }
echo "   created $ISSUE_KEY"

# Confirm starting status
acli jira workitem view "$ISSUE_KEY" --fields status --json > "$TMP_OUT"
START_STATUS=$(python3 -c "import json; d=json.load(open('$TMP_OUT')); print(d.get('fields',{}).get('status',{}).get('name') or d.get('status',{}).get('name') or '')")
echo "   start_status='$START_STATUS'"
if [[ -n "$START_STATUS" ]]; then pass "starting status readable: '$START_STATUS'"; else fail "could not read start status"; fi

# Step 2: enumerate transitions via REST (ACLI has no "list transitions" subcommand)
echo ">> Listing transitions via REST /issue/{key}/transitions..."
curl_jira "/rest/api/3/issue/$ISSUE_KEY/transitions" > "$TMP_OUT"
TRANSITION_COUNT=$(python3 -c "import json; print(len(json.load(open('$TMP_OUT')).get('transitions',[])))")
echo "   transitions available: $TRANSITION_COUNT"
if [[ "$TRANSITION_COUNT" -ge 1 ]]; then pass "at least one transition available"; else fail "no transitions available"; fi
python3 -c "
import json
ts = json.load(open('$TMP_OUT'))['transitions']
for t in ts:
    print(f\"     - id={t['id']:>3}  name='{t['name']}'  -> to='{t['to']['name']}'\")
"

# Step 3: transition to In Progress by name match (case-insensitive)
TARGET_NAME=$(python3 -c "
import json
ts = json.load(open('$TMP_OUT'))['transitions']
for t in ts:
    if t['to']['name'].lower() == 'in progress':
        print(t['name']); break
")
if [[ -z "$TARGET_NAME" ]]; then
  echo "   no 'In Progress' transition found — listing all 'to' states:"
  python3 -c "import json; print([t['to']['name'] for t in json.load(open('$TMP_OUT'))['transitions']])"
  fail "no 'In Progress' transition available from start state"
else
  echo ">> Transitioning via acli --status '$TARGET_NAME'..."
  if acli jira workitem transition --key "$ISSUE_KEY" --status "$TARGET_NAME" --yes >/dev/null 2>&1; then
    pass "acli transition executed"
  else
    fail "acli transition failed"
  fi
  acli jira workitem view "$ISSUE_KEY" --fields status --json > "$TMP_OUT"
  NEW_STATUS=$(python3 -c "import json; d=json.load(open('$TMP_OUT')); print(d.get('fields',{}).get('status',{}).get('name') or d.get('status',{}).get('name') or '')")
  echo "   new_status='$NEW_STATUS'"
  if [[ "${NEW_STATUS,,}" == "in progress" ]]; then
    pass "status changed to In Progress"
  else
    fail "status did not change (got '$NEW_STATUS')"
  fi
fi

# Step 5: invalid name should fail cleanly
echo ">> Attempting invalid transition 'NonexistentStatus'..."
set +e
acli jira workitem transition --key "$ISSUE_KEY" --status "NonexistentStatus" --yes > "$TMP_OUT" 2>&1
RC=$?
set -e
echo "   exit=$RC stderr/out:"
sed 's/^/     /' "$TMP_OUT" | head -5
if [[ $RC -ne 0 ]]; then
  pass "invalid transition exits non-zero"
else
  fail "invalid transition returned 0 exit (silent failure)"
fi
acli jira workitem view "$ISSUE_KEY" --fields status --json > "$TMP_OUT"
POST_INVALID=$(python3 -c "import json; d=json.load(open('$TMP_OUT')); print(d.get('fields',{}).get('status',{}).get('name') or d.get('status',{}).get('name') or '')")
if [[ "$POST_INVALID" == "$NEW_STATUS" ]]; then
  pass "no state change after invalid transition (still '$POST_INVALID')"
else
  fail "state changed unexpectedly after invalid transition"
fi

# Step 6: try multi-step "Done" (or "Closed") — does Jira require chain, or does ACLI resolve it?
echo ">> Trying 'Done' from In Progress (single-step probe)..."
curl_jira "/rest/api/3/issue/$ISSUE_KEY/transitions" > "$TMP_OUT"
HAS_DONE_DIRECT=$(python3 -c "
import json
ts = json.load(open('$TMP_OUT'))['transitions']
print('1' if any(t['to']['name'].lower() == 'done' for t in ts) else '0')
")
echo "   'Done' directly available from current state: $HAS_DONE_DIRECT"
if [[ "$HAS_DONE_DIRECT" == "1" ]]; then
  echo "   FINDING: Jira workflow allows direct In Progress -> Done; no chaining needed."
else
  echo "   FINDING: 'Done' NOT directly reachable. Available 'to' states:"
  python3 -c "import json; print('   ', [t['to']['name'] for t in json.load(open('$TMP_OUT'))['transitions']])"
  echo "   IMPLICATION: outbound impl must either (a) chain transitions or (b) document unreachable end states."
fi

echo
echo "== Gap 2 summary: PASS=$PASS FAIL=$FAIL =="
[[ $FAIL -eq 0 ]] || exit 1
