#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# reviewbot-e2e.sh — the committed LIVE end-to-end verification artifact for the
# S4b "proven pipe" (epic d251). This is the artifact the AC asks for: it PROVES
# the live pipe against the RUNNING Gerrit + the deployed receiver — not FakeRunner
# / offline unit tests — by pushing a real change and observing the bot's vote.
#
# It exercises the full loop end-to-end:
#   1. Clone rebar over SSH (admin key), make a trivial commit on `main`, install the
#      commit-msg hook (Change-Id), and push to refs/for/main → capture the change number.
#   2. Poll (up to ~3 min) the change's LLM-Review label until rebar-review-bot has cast
#      a NON-ZERO vote (the receiver reviewed the patchset and voted) → PASS; timeout → FAIL.
#   3. Assert the change's `LLM-Review` SUBMIT REQUIREMENT is SATISFIED (the vote actually
#      gates submittability, ADR-0013).
#   4. Re-trigger the same patchset (call /rerun) and assert NO duplicate bot vote row
#      appears — the bot's vote count stays exactly 1 (dedup / single-flight).
#
# Fail-closed throughout: any failed check exits non-zero so an operator / CI can gate.
# The bot token is read from SSM and never echoed.
#
# DO NOT run this in CI by default — it makes LIVE calls to the running Gerrit and the
# deployed receiver and pushes a real (throwaway) change. Run it by hand from an operator
# box after a deploy to certify the live e2e.
#
# Env: GERRIT_HOST (default rebar.solutions.navateam.com), GERRIT_ADMIN_SSH_KEY (path to
#      the admin SSH private key), BOT_USER (default rebar-review-bot), SSM_TOKEN_PARAM
#      (default /rebar/prod/gerrit-bot-token), AWS_REGION (default us-east-1),
#      PROJECT (default rebar), GERRIT_SSH_PORT (default 29418),
#      RECEIVER_BASE (default https://${GERRIT_HOST}/review) — the receiver behind nginx,
#      POLL_TIMEOUT_SECONDS (default 180), POLL_INTERVAL_SECONDS (default 6).
# ---------------------------------------------------------------------------
set -euo pipefail

GERRIT_HOST="${GERRIT_HOST:-rebar.solutions.navateam.com}"
GERRIT_SSH_USER="${GERRIT_SSH_USER:-admin}"
GERRIT_ADMIN_SSH_KEY="${GERRIT_ADMIN_SSH_KEY:?GERRIT_ADMIN_SSH_KEY (path to admin SSH key) is required}"
BOT_USER="${BOT_USER:-rebar-review-bot}"
SSM_TOKEN_PARAM="${SSM_TOKEN_PARAM:-/rebar/prod/gerrit-bot-token}"
AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT:-rebar}"
GERRIT_SSH_PORT="${GERRIT_SSH_PORT:-29418}"
RECEIVER_BASE="${RECEIVER_BASE:-https://${GERRIT_HOST}/review}"
POLL_TIMEOUT_SECONDS="${POLL_TIMEOUT_SECONDS:-180}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-6}"

log()  { echo "reviewbot-e2e: $*" >&2; }
pass() { echo "reviewbot-e2e: [PASS] $*" >&2; }
fail() { echo "reviewbot-e2e: [FAIL] $*" >&2; exit 1; }

# --- bot token (SSM, never echoed) ------------------------------------------
TOKEN="$(aws ssm get-parameter --region "$AWS_REGION" --name "$SSM_TOKEN_PARAM" \
	--with-decryption --query 'Parameter.Value' --output text)"
[ -n "$TOKEN" ] && [ "$TOKEN" != "None" ] || fail "bot token absent in SSM ($SSM_TOKEN_PARAM)"

# Admin SSH wrapper (StrictHostKeyChecking off → unattended; this is a throwaway op box).
SSH_CMD="ssh -i ${GERRIT_ADMIN_SSH_KEY} -p ${GERRIT_SSH_PORT} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
GIT_SSH_URL="ssh://${GERRIT_SSH_USER}@${GERRIT_HOST}:${GERRIT_SSH_PORT}/${PROJECT}"

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# --- 1. clone, commit, install hook, push to refs/for/main ------------------
log "cloning ${PROJECT} over SSH (admin key)…"
GIT_SSH_COMMAND="$SSH_CMD" git clone -q "$GIT_SSH_URL" "$tmp/repo" \
	|| fail "git clone over SSH failed"
cd "$tmp/repo"

# Base the probe commit on origin/main (the branch we push the review for). The
# clone's default branch may be `master` (Gerrit's empty-init branch) whose history
# is unrelated to `main` — committing there and pushing to refs/for/main fails with
# "no common ancestry". Fetch + check out main explicitly.
GIT_SSH_COMMAND="$SSH_CMD" git fetch -q origin main \
	|| fail "could not fetch origin/main (does the rebar project have a main branch?)"
git checkout -q -B e2e-probe FETCH_HEAD

# Install the commit-msg hook so the push carries a Change-Id (Gerrit requires it).
mkdir -p .git/hooks
curl -sS -Lo .git/hooks/commit-msg \
	"https://${BOT_USER}:${TOKEN}@${GERRIT_HOST}/tools/hooks/commit-msg" \
	|| fail "could not fetch the commit-msg hook"
chmod +x .git/hooks/commit-msg

git config user.email "reviewbot-e2e@navateam.com"
git config user.name  "reviewbot e2e"

stamp="$(date -u +%Y%m%d%H%M%S)"
echo "reviewbot-e2e probe ${stamp}" >> "reviewbot-e2e-${stamp}.txt"
git add -A
git commit -q -m "test: reviewbot e2e probe ${stamp}"

log "pushing to refs/for/main…"
push_out="$(GIT_SSH_COMMAND="$SSH_CMD" git push origin HEAD:refs/for/main 2>&1)" \
	|| { echo "$push_out" >&2; fail "git push to refs/for/main failed"; }
echo "$push_out" >&2

# Gerrit echoes the new change URL ( …/c/rebar/+/<number> ) on push.
CHANGE_NUM="$(echo "$push_out" | grep -oE '/\+/[0-9]+' | head -n1 | grep -oE '[0-9]+')"
[ -n "$CHANGE_NUM" ] || fail "could not parse the new change number from the push output"
pass "pushed change #${CHANGE_NUM}"

# --- helpers: authenticated Gerrit REST GET (strips XSSI guard) -------------
api_get() {  # $1 = path under /a
	curl -sS -u "${BOT_USER}:${TOKEN}" "https://${GERRIT_HOST}/a$1" | sed "1s/^)]}'//"
}

# Count NON-ZERO LLM-Review votes cast by the bot account on the change.
bot_vote_count() {
	api_get "/changes/${CHANGE_NUM}/detail?o=DETAILED_LABELS" \
		| python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(0); sys.exit(0)
votes = (d.get("labels") or {}).get("LLM-Review", {}).get("all") or []
bot = "'"$BOT_USER"'"
n = 0
for v in votes:
    name = (v.get("username") or v.get("name") or "")
    if bot in name and int(v.get("value") or 0) != 0:
        n += 1
print(n)
'
}

# --- 2. poll for the bot's non-zero LLM-Review vote -------------------------
log "polling up to ${POLL_TIMEOUT_SECONDS}s for ${BOT_USER}'s LLM-Review vote…"
deadline=$(( $(date +%s) + POLL_TIMEOUT_SECONDS ))
voted=0
while [ "$(date +%s)" -lt "$deadline" ]; do
	if [ "$(bot_vote_count)" -ge 1 ]; then
		voted=1
		break
	fi
	sleep "$POLL_INTERVAL_SECONDS"
done
[ "$voted" -eq 1 ] || fail "timed out: ${BOT_USER} cast no LLM-Review vote within ${POLL_TIMEOUT_SECONDS}s"
pass "${BOT_USER} cast a non-zero LLM-Review vote on change #${CHANGE_NUM}"

# --- 3. assert the LLM-Review submit requirement is SATISFIED ---------------
req_status="$(api_get "/changes/${CHANGE_NUM}?o=SUBMIT_REQUIREMENTS" \
	| python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE_ERROR"); sys.exit(0)
for r in d.get("submit_requirements") or []:
    if r.get("name") == "LLM-Review":
        print(r.get("status") or "UNKNOWN"); break
else:
    print("ABSENT")
')"
if [ "$req_status" = "SATISFIED" ]; then
	pass "LLM-Review submit requirement is SATISFIED"
else
	fail "LLM-Review submit requirement is '${req_status}' (expected SATISFIED)"
fi

# --- 4. re-trigger /rerun and assert NO duplicate bot vote row --------------
before="$(bot_vote_count)"
log "calling /rerun for change #${CHANGE_NUM} (dedup check)…"
curl -sS -o /dev/null -X POST \
	"${RECEIVER_BASE}/rerun?token=${TOKEN}&change=${CHANGE_NUM}" \
	|| fail "/rerun call failed"
# Give the worker a moment to (re)process; dedup/single-flight must keep it at one row.
sleep "$POLL_INTERVAL_SECONDS"
after="$(bot_vote_count)"
if [ "$after" -le "$before" ] && [ "$after" -ge 1 ]; then
	pass "no duplicate bot vote after /rerun (vote count stayed at ${after})"
else
	fail "duplicate bot vote after /rerun (before=${before}, after=${after})"
fi

echo "reviewbot-e2e: all checks PASSED (live S4b pipe verified on change #${CHANGE_NUM})" >&2
