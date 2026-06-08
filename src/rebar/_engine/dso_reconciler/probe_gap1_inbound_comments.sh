#!/usr/bin/env bash
# Gap 1 probe: inbound comment propagation design validation.
# Tests marker-token loop-breaker pattern + ADF→text round-trip.
# Standalone — does NOT touch reconciler outbound apply path.
set -euo pipefail

# Resolve this script's own directory so the inline python can import
# sibling modules (adf.py). Derive from BASH_SOURCE — no literal plugin paths.
_RECONCILER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${JIRA_URL:?required}"
: "${JIRA_USER:?required}"
: "${JIRA_API_TOKEN:?required}"
: "${DSO_FIELD_VALIDATION_PROBE:?must be set to 1}"

PROJECT="${JIRA_PROJECT:-DIG}"
TS=$(date +%s)
LABEL="gap1-probe-${TS}"
MARKER='<!-- dso:src:abc123 -->'
ISSUE_KEY=""
TMP_OUT=$(mktemp /tmp/gap1-probe.XXXXXX)
TMP_PY=$(mktemp /tmp/gap1-probe-py.XXXXXX.py)

PASS=0; FAIL=0
pass() { echo "  PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $*"; FAIL=$((FAIL+1)); }

cleanup() {
  if [[ -n "$ISSUE_KEY" ]]; then
    echo ">> Cleanup: deleting $ISSUE_KEY"
    acli jira workitem delete --key "$ISSUE_KEY" --yes >/dev/null 2>&1 || echo "   (delete failed; manual cleanup needed: $ISSUE_KEY)"
  fi
  rm -f "$TMP_OUT" "$TMP_PY"
}
trap cleanup EXIT

echo "== Gap 1 probe: inbound comment propagation =="
echo ">> Project=$PROJECT label=$LABEL"

# Step 1: create temp issue
echo ">> Creating issue..."
acli jira workitem create \
  --project "$PROJECT" \
  --type "Task" \
  --summary "DSO Gap1 probe $TS (auto-cleanup)" \
  --label "$LABEL" \
  --json > "$TMP_OUT" 2>&1 || { cat "$TMP_OUT"; exit 2; }
ISSUE_KEY=$(python3 -c "import json,sys; d=json.load(open('$TMP_OUT')); print(d.get('key') or d.get('issueKey') or (d[0]['key'] if isinstance(d,list) else ''))")
[[ -n "$ISSUE_KEY" ]] || { echo "FAIL: could not parse issue key"; cat "$TMP_OUT"; exit 2; }
echo "   created $ISSUE_KEY"

# Step 2: comment WITH marker
echo ">> Adding marker comment..."
BODY_MARKED="${MARKER}
This is our own outbound echo. Should be filtered."
acli jira workitem comment create --key "$ISSUE_KEY" --body "$BODY_MARKED" >/dev/null

# Step 3: comment WITHOUT marker (simulates a human Jira-side comment)
echo ">> Adding unmarked comment..."
acli jira workitem comment create --key "$ISSUE_KEY" --body "Human comment from Jira side - should be picked up inbound." >/dev/null

# Step 4: list comments and parse IDs
echo ">> Listing comments..."
acli jira workitem comment list --key "$ISSUE_KEY" --json > "$TMP_OUT"

cat > "$TMP_PY" <<PYEOF
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath("$TMP_PY")))
# Add reconciler dir (this script's own directory) for adf module
sys.path.insert(0, "$_RECONCILER_DIR")
from adf import adf_to_text

MARKER = "$MARKER"
data = json.load(open("$TMP_OUT"))
# ACLI shape: either list of comments directly, or {"comments":[...]}
if isinstance(data, dict) and "comments" in data:
    comments = data["comments"]
elif isinstance(data, list):
    comments = data
else:
    comments = data.get("values", []) if isinstance(data, dict) else []

print(f"  parsed {len(comments)} comments")
ids = []
marked_ids = []
unmarked_ids = []
texts = {}
for c in comments:
    cid = str(c.get("id") or c.get("commentId") or "")
    ids.append(cid)
    body = c.get("body")
    # Body may be ADF dict, or plain string
    if isinstance(body, dict):
        text = adf_to_text(body)
    else:
        text = str(body or "")
    texts[cid] = text
    if MARKER in text:
        marked_ids.append(cid)
    else:
        unmarked_ids.append(cid)

print(f"  ids={ids}")
print(f"  marked_ids={marked_ids}")
print(f"  unmarked_ids={unmarked_ids}")

# Assertions
results = []
results.append(("marker comment identified", len(marked_ids) == 1))
results.append(("unmarked comment NOT filtered", len(unmarked_ids) == 1))
results.append(("ADF round-trip non-empty for unmarked", all(texts[i].strip() for i in unmarked_ids)))
results.append(("ADF round-trip non-empty for marked", all(texts[i].strip() for i in marked_ids)))

# Set-diff simulation: assume only the first two are "known"
known_set = set(ids[:2])
# Step 7-8: emulated below in shell after adding 3rd
with open("$TMP_OUT.known", "w") as f:
    json.dump({"known": list(known_set), "all_after_step4": ids, "marked": marked_ids}, f)

ok = all(r[1] for r in results)
for name, val in results:
    print(f"  {'OK' if val else 'BAD'}: {name}")
sys.exit(0 if ok else 1)
PYEOF

if python3 "$TMP_PY"; then
  pass "marker filter + ADF round-trip (steps 4-6, 9)"
else
  fail "marker filter or ADF round-trip"
fi

# Step 7: add third comment (new since "last sync")
echo ">> Adding third comment (new-since-last-sync)..."
acli jira workitem comment create --key "$ISSUE_KEY" --body "Third comment, posted after sync cursor." >/dev/null

# Step 8: re-list and set-diff
acli jira workitem comment list --key "$ISSUE_KEY" --json > "$TMP_OUT"
cat > "$TMP_PY" <<PYEOF
import json, sys
data = json.load(open("$TMP_OUT"))
comments = data.get("comments", data) if isinstance(data, dict) else data
ids_now = [str(c.get("id") or c.get("commentId")) for c in comments]
known = json.load(open("$TMP_OUT.known"))["known"]
new_ids = [i for i in ids_now if i not in set(known)]
print(f"  ids_now ({len(ids_now)})={ids_now}")
print(f"  known   ({len(known)})={known}")
print(f"  new_ids ({len(new_ids)})={new_ids}")
sys.exit(0 if len(new_ids) == 1 else 1)
PYEOF
if python3 "$TMP_PY"; then
  pass "set-diff picks up exactly the new comment (steps 7-8)"
else
  fail "set-diff did not isolate the new comment"
fi

echo
echo "== Gap 1 summary: PASS=$PASS FAIL=$FAIL =="
[[ $FAIL -eq 0 ]] || exit 1
