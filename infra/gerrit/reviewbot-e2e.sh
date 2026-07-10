#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# reviewbot-e2e.sh — the committed LIVE end-to-end verification artifact for the
# S4b "proven pipe" (epic d251). This is the artifact the AC asks for: it PROVES
# the live pipe against the RUNNING Gerrit + the deployed receiver — not FakeRunner
# / offline unit tests — by pushing a real change and observing the bot's vote,
# its INTEGER value, and (on PASS) the merged change APPEARING on the GitHub mirror.
#
# Apply-prove-rollback safe: the full chain runs on a DELETABLE TEST BRANCH
# ($TEST_BRANCH, default gerrit-e2e-test) — NEVER on `main`. Submitting on the
# test branch never advances real `main`, and cleanup deletes the GitHub test
# branch + the temp clone on exit. (The Gerrit test branch is left in place; it is
# harmless and reused by subsequent runs.)
#
# THREE DISTINCT TERMINAL STATES (a caller can tell them apart by exit code):
#   - exit 0  PASS          — bot voted == MAX, submit requirement SATISFIED, the
#                             change SUBMITTED → MERGED, AND the merged commit
#                             REPLICATED to the navapbc/rebar GitHub mirror.
#   - exit 2  HARNESS-FAILURE — no bot vote within the timeout, or a clone/push/
#                             Gerrit/SSM/replication error. The pipe could NOT be
#                             exercised; this is an infra/test problem, NOT a verdict.
#   - exit 3  GATE-BLOCKED  — the bot cast a NON-MAX value (e.g. -1). The gate is
#                             WORKING and correctly declined the change; it is a
#                             valid review outcome (not a harness bug) and the change
#                             is correctly non-submittable. Surfaced distinctly and
#                             non-zero so a caller can distinguish it from PASS.
#
# Fail-closed throughout. The bot token is read from SSM and NEVER echoed.
#
# DO NOT run this in CI by default — it makes LIVE calls to the running Gerrit and
# the deployed receiver, pushes a real (throwaway) change, and (on PASS) submits it
# on the test branch. Run it by hand from an operator box after a deploy.
#
# ---------------------------------------------------------------------------
# PREREQUISITE — replication of the test branch to GitHub
# ---------------------------------------------------------------------------
# The PASS path asserts the merged commit appears on the GitHub mirror's
# $TEST_BRANCH. That requires the Gerrit replication plugin to push that ref. The
# production replication.config typically only replicates `refs/heads/main` (+ the
# mirror-lock refs), so BEFORE running this e2e the operator must TEMPORARILY add a
# refspec covering the test branch to the site replication.config, e.g.:
#
#     [remote "github"]
#       push = +refs/heads/${TEST_BRANCH}:refs/heads/${TEST_BRANCH}
#
# Gerrit's replication plugin autoReload picks the change up (no restart needed).
# Remove the temporary refspec after the test. If the refspec is NOT present, the
# GitHub-appearance poll TIMES OUT into a clear
#   [HARNESS-FAILURE] test-branch not replicated — add the refspec (see header)
# rather than reporting a false negative.
#
# ---------------------------------------------------------------------------
# Env (all have defaults except the admin SSH key):
#   GERRIT_HOST          default rebar.solutions.navateam.com
#   GERRIT_SSH_USER      default admin
#   GERRIT_ADMIN_SSH_KEY (required) path to the admin SSH private key
#   BOT_USER             default rebar-review-bot
#   SSM_TOKEN_PARAM      default /rebar/prod/gerrit-bot-token
#   AWS_REGION           default us-east-1
#   PROJECT              default rebar
#   GERRIT_SSH_PORT      default 29418
#   TEST_BRANCH          default gerrit-e2e-test
#   LLM_REVIEW_MAX       default 1  (the MAX LLM-Review value that gates submit)
#   GITHUB_REPO          default navapbc/rebar
#   POLL_TIMEOUT_SECONDS default 240
#   POLL_INTERVAL_SECONDS default 6
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
TEST_BRANCH="${TEST_BRANCH:-gerrit-e2e-test}"
LLM_REVIEW_MAX="${LLM_REVIEW_MAX:-1}"
GITHUB_REPO="${GITHUB_REPO:-navapbc/rebar}"
POLL_TIMEOUT_SECONDS="${POLL_TIMEOUT_SECONDS:-240}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-6}"

# --- logging + terminal-state helpers ---------------------------------------
log()      { echo "reviewbot-e2e: $*" >&2; }
pass()     { echo "reviewbot-e2e: [PASS] $*" >&2; }
# HARNESS FAILURE (exit 2): the pipe could not be exercised — infra/test problem.
harness()  { echo "reviewbot-e2e: [HARNESS-FAILURE] $*" >&2; exit 2; }
# GATE BLOCKED (exit 3): the gate worked and correctly declined — a valid outcome.
blocked()  { echo "reviewbot-e2e: [GATE-BLOCKED] $*" >&2; exit 3; }

# --- bot token (SSM, never echoed) ------------------------------------------
TOKEN="$(aws ssm get-parameter --region "$AWS_REGION" --name "$SSM_TOKEN_PARAM" \
	--with-decryption --query 'Parameter.Value' --output text)" \
	|| harness "could not read bot token from SSM ($SSM_TOKEN_PARAM)"
[ -n "$TOKEN" ] && [ "$TOKEN" != "None" ] || harness "bot token absent in SSM ($SSM_TOKEN_PARAM)"

# Admin SSH wrapper (StrictHostKeyChecking off → unattended; throwaway op box).
SSH_CMD="ssh -i ${GERRIT_ADMIN_SSH_KEY} -p ${GERRIT_SSH_PORT} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
GIT_SSH_URL="ssh://${GERRIT_SSH_USER}@${GERRIT_HOST}:${GERRIT_SSH_PORT}/${PROJECT}"

# --- cleanup trap (always runs): drop the GitHub test branch + temp clone ----
tmp="$(mktemp -d)"
cleanup() {
	# Delete the throwaway GitHub test branch (ignore errors — it may not exist).
	gh api -X DELETE "repos/${GITHUB_REPO}/git/refs/heads/${TEST_BRANCH}" >/dev/null 2>&1 || true
	rm -rf "$tmp"
	# NOTE: the Gerrit test branch ($TEST_BRANCH) is intentionally LEFT in place —
	# it is harmless and reused on the next run.
}
trap cleanup EXIT

# --- helpers: authenticated Gerrit REST GET (strips XSSI guard) -------------
api_get() {  # $1 = path under /a
	curl -sS -u "${BOT_USER}:${TOKEN}" "https://${GERRIT_HOST}/a$1" | sed "1s/^)]}'//"
}

# --- 1. ensure the Gerrit test branch exists --------------------------------
log "ensuring Gerrit test branch '${TEST_BRANCH}' exists (from main)…"
branch_out="$($SSH_CMD "${GERRIT_SSH_USER}@${GERRIT_HOST}" \
	gerrit create-branch "$PROJECT" "$TEST_BRANCH" main 2>&1)" || true
# "already exists" is fine; any other error is a harness failure.
if echo "$branch_out" | grep -qiE 'already exists'; then
	log "test branch already exists (reusing)"
elif [ -z "$branch_out" ]; then
	log "test branch created"
else
	echo "$branch_out" >&2
	# Tolerate benign messages; treat a hard SSH/Gerrit failure as harness failure
	# by re-probing the branch below (the push step will surface a real problem).
	log "create-branch returned a message (see above) — continuing"
fi

# --- 2. clone, checkout test branch, install hook, commit, push -------------
log "cloning ${PROJECT} over SSH (admin key)…"
GIT_SSH_COMMAND="$SSH_CMD" git clone -q "$GIT_SSH_URL" "$tmp/repo" \
	|| harness "git clone over SSH failed"
cd "$tmp/repo"

# Fetch + check out the TEST BRANCH (never main) — all probe work happens here.
GIT_SSH_COMMAND="$SSH_CMD" git fetch -q origin "$TEST_BRANCH" \
	|| harness "could not fetch origin/${TEST_BRANCH} (was create-branch successful?)"
git checkout -q -B e2e-probe FETCH_HEAD

# Install the commit-msg hook so the push carries a Change-Id (Gerrit requires it).
mkdir -p .git/hooks
curl -sS -Lo .git/hooks/commit-msg \
	"https://${BOT_USER}:${TOKEN}@${GERRIT_HOST}/tools/hooks/commit-msg" \
	|| harness "could not fetch the commit-msg hook"
chmod +x .git/hooks/commit-msg

git config user.email "reviewbot-e2e@navateam.com"
git config user.name  "reviewbot e2e"

stamp="$(date -u +%Y%m%d%H%M%S)"
echo "reviewbot-e2e probe ${stamp}" >> "reviewbot-e2e-${stamp}.txt"
git add -A
git commit -q -s -m "test: reviewbot e2e probe ${stamp}" \
	|| harness "git commit failed (commit-msg hook?)"

log "pushing to refs/for/${TEST_BRANCH}…"
push_out="$(GIT_SSH_COMMAND="$SSH_CMD" git push origin "HEAD:refs/for/${TEST_BRANCH}" 2>&1)" \
	|| { echo "$push_out" >&2; harness "git push to refs/for/${TEST_BRANCH} failed"; }
echo "$push_out" >&2

# Gerrit echoes the new change URL ( …/c/rebar/+/<number> ) on push.
CHANGE_NUM="$(echo "$push_out" | grep -oE '/\+/[0-9]+' | head -n1 | grep -oE '[0-9]+')"
[ -n "$CHANGE_NUM" ] || harness "could not parse the new change number from the push output"
pass "pushed change #${CHANGE_NUM} on ${TEST_BRANCH}"

# --- helpers: read the bot's MAX integer LLM-Review vote --------------------
# Prints the maximum LLM-Review value cast by the bot account, or "NONE" if the
# bot has not voted yet.
bot_max_vote() {
	api_get "/changes/${CHANGE_NUM}/detail?o=DETAILED_LABELS" \
		| python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("NONE"); sys.exit(0)
votes = (d.get("labels") or {}).get("LLM-Review", {}).get("all") or []
bot = "'"$BOT_USER"'"
vals = []
for v in votes:
    name = (v.get("username") or v.get("name") or "")
    if bot in name and v.get("value") is not None:
        try:
            vals.append(int(v.get("value")))
        except (TypeError, ValueError):
            pass
print(max(vals) if vals else "NONE")
'
}

# --- 3. poll for the bot's LLM-Review vote, reading the INTEGER value --------
log "polling up to ${POLL_TIMEOUT_SECONDS}s for ${BOT_USER}'s LLM-Review vote…"
deadline=$(( $(date +%s) + POLL_TIMEOUT_SECONDS ))
vote="NONE"
while [ "$(date +%s)" -lt "$deadline" ]; do
	vote="$(bot_max_vote)"
	if [ "$vote" != "NONE" ]; then
		break
	fi
	sleep "$POLL_INTERVAL_SECONDS"
done
# No vote at all within the timeout → the pipe did not run → HARNESS FAILURE.
[ "$vote" != "NONE" ] || harness "no ${BOT_USER} LLM-Review vote within ${POLL_TIMEOUT_SECONDS}s (receiver down? not wired?)"
log "${BOT_USER} cast LLM-Review value ${vote} (MAX gate = ${LLM_REVIEW_MAX})"

# --- 4. branch on the integer value: GATE-BLOCKED vs candidate-PASS ----------
if [ "$vote" -lt "$LLM_REVIEW_MAX" ]; then
	# The gate WORKED and declined — a valid review outcome, surfaced distinctly.
	blocked "bot voted ${vote} (< MAX=${LLM_REVIEW_MAX}); change is correctly non-submittable"
fi
pass "vote==MAX (bot voted ${vote} == MAX=${LLM_REVIEW_MAX})"

# --- 5. assert the LLM-Review submit requirement is SATISFIED ---------------
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
[ "$req_status" = "SATISFIED" ] \
	|| harness "vote==MAX but LLM-Review submit requirement is '${req_status}' (expected SATISFIED)"
pass "submittable (LLM-Review submit requirement is SATISFIED)"

# --- 6. submit, confirm MERGED ----------------------------------------------
# Determine the current patchset number to address the submit at <change>,<ps>.
PATCHSET="$(api_get "/changes/${CHANGE_NUM}?o=CURRENT_REVISION" \
	| python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
cur = d.get("current_revision")
revs = d.get("revisions") or {}
print((revs.get(cur) or {}).get("_number") or "")
')"
[ -n "$PATCHSET" ] || harness "could not determine current patchset number for change #${CHANGE_NUM}"

log "submitting change #${CHANGE_NUM},${PATCHSET}…"
$SSH_CMD "${GERRIT_SSH_USER}@${GERRIT_HOST}" \
	gerrit review "${CHANGE_NUM},${PATCHSET}" --submit \
	|| harness "gerrit review --submit failed for #${CHANGE_NUM},${PATCHSET}"

status="$(api_get "/changes/${CHANGE_NUM}?o=CURRENT_REVISION" \
	| python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE_ERROR"); sys.exit(0)
print(d.get("status") or "UNKNOWN")
')"
[ "$status" = "MERGED" ] || harness "change #${CHANGE_NUM} status is '${status}' after submit (expected MERGED)"
pass "MERGED (change #${CHANGE_NUM} submitted and merged on ${TEST_BRANCH})"

# Resolve the merged SHA on the Gerrit test branch (source of truth for the
# replication assertion below).
GERRIT_SHA="$(GIT_SSH_COMMAND="$SSH_CMD" git ls-remote "$GIT_SSH_URL" "refs/heads/${TEST_BRANCH}" \
	| awk '{print $1}')"
[ -n "$GERRIT_SHA" ] || harness "could not read Gerrit ${TEST_BRANCH} HEAD after merge"
log "Gerrit ${TEST_BRANCH} HEAD is ${GERRIT_SHA}"

# --- 7. assert the merged commit APPEARS ON GITHUB (replication) ------------
# Poll the public GitHub mirror until refs/heads/$TEST_BRANCH equals the merged
# Gerrit SHA. If it never appears, the replication refspec for the test branch is
# (almost certainly) missing — report a CLEAR HARNESS FAILURE, not a false PASS/FAIL.
log "polling GitHub mirror for ${TEST_BRANCH} == ${GERRIT_SHA}…"
deadline=$(( $(date +%s) + POLL_TIMEOUT_SECONDS ))
gh_sha=""
while [ "$(date +%s)" -lt "$deadline" ]; do
	gh_sha="$(git ls-remote "https://github.com/${GITHUB_REPO}" "refs/heads/${TEST_BRANCH}" 2>/dev/null \
		| awk '{print $1}')"
	if [ "$gh_sha" = "$GERRIT_SHA" ]; then
		break
	fi
	sleep "$POLL_INTERVAL_SECONDS"
done
if [ "$gh_sha" != "$GERRIT_SHA" ]; then
	harness "test-branch not replicated — add the refspec (see header) [github=${gh_sha:-<absent>} gerrit=${GERRIT_SHA}]"
fi
pass "replicated-to-GitHub (${TEST_BRANCH} on ${GITHUB_REPO} == ${GERRIT_SHA})"

# ---------------------------------------------------------------------------
# NEGATIVE PATH (complementary assertion) — direct push/PR-merge to `main` by a
# non-bypass identity is REJECTED.
# ---------------------------------------------------------------------------
# The PASS path above proves the FORWARD direction (Gerrit-gated change → GitHub).
# The complementary guarantee — that GitHub `main` cannot be advanced OUT of band
# (a direct push or a PR merge by a non-bypass identity is rejected) — is owned and
# PROVEN by S6's GitHub mirror-lock: see infra/runbooks/github-mirror-lock.md and
# the S6 apply-prove-rollback. That lock is NOT permanently active under
# apply-prove-rollback (it is applied, proven, then rolled back), so this e2e does
# NOT assume it is on. We only opportunistically check it:
if gh api "repos/${GITHUB_REPO}/rulesets" 2>/dev/null \
	| python3 -c '
import json, sys
try:
    rs = json.load(sys.stdin)
except Exception:
    sys.exit(1)
names = [r.get("name") for r in (rs if isinstance(rs, list) else [])]
sys.exit(0 if "gerrit-mirror-lock-main" in names else 1)
'; then
	log "gerrit-mirror-lock-main ruleset is ACTIVE — attempting a direct push to main (must be REJECTED)…"
	probe_branch="e2e-neg-${stamp}"
	git checkout -q -B "$probe_branch" "$GERRIT_SHA" 2>/dev/null || git checkout -q -B "$probe_branch"
	echo "neg-path probe ${stamp}" >> "neg-${stamp}.txt"
	git add -A
	git commit -q -s -m "test: negative-path direct-push probe ${stamp}" || true
	if git push -q "https://github.com/${GITHUB_REPO}" "HEAD:refs/heads/main" >/dev/null 2>&1; then
		# It SUCCEEDED — the lock failed to reject an out-of-band push. That is a
		# real failure of the protection; surface it (try to undo is out of scope).
		harness "direct push to GitHub main SUCCEEDED while mirror-lock active — lock did NOT reject it"
	fi
	pass "negative-path: direct push to GitHub main was REJECTED by the mirror-lock"
else
	log "SKIP negative-path: gerrit-mirror-lock-main not active (apply-prove-rollback) — proven by S6 (see infra/runbooks/github-mirror-lock.md)"
fi

echo "reviewbot-e2e: all checks PASSED (live S4b pipe verified end-to-end on change #${CHANGE_NUM}, branch ${TEST_BRANCH})" >&2
exit 0
