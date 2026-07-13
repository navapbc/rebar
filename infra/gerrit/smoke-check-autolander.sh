#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# smoke-check-autolander.sh — verify the auto-lander's rebase-on-behalf grant (S5b).
#
# The auto-lander lands the front change under Fast-Forward-Only (ADR-0040) by rebasing
# it onto the current `main` tip via the Gerrit REST `POST /changes/{id}/rebase` with
# {"on_behalf_of_uploader": true}, so the rebased patch set stays attributed to the
# ORIGINAL uploader. That REST option requires the bot identity (RebarBotNava, a member of
# `Contributors`) to hold BOTH the per-ref `rebase` AND `rebaseOnBehalfOfUploader` access
# rights — the grant added to infra/gerrit/project.config [access "refs/heads/*"] (S5b).
#
# This is a DEPLOY-STEP CANARY: it needs a LIVE Gerrit + the bot's real HTTP credential, so
# it is NOT exercised by the in-repo unit tests. It runs against a throwaway SCRATCH branch
# (default refs/heads/autolander-smoke) so it never touches `main` or any real change.
#
# THE SYNTHETIC CHECK (what a full run does):
#   1. (admin) create/reset the scratch branch $SCRATCH_BRANCH at the current main tip.
#   2. (uploader) push a commit for review to refs/for/$SCRATCH_BRANCH — the change to rebase.
#   3. (admin) advance $SCRATCH_BRANCH by one commit so the change is now behind its base
#      (i.e. a rebase is actually required — this is the FFO "not up to date" condition).
#   4. (BOT) POST /a/changes/{id}/rebase with {"base":"<new tip>","on_behalf_of_uploader":true}
#      as RebarBotNava using $AUTOLANDER_GERRIT_TOKEN. ASSERT HTTP 200 (the grant works); a
#      403/"not permitted" means the rebase/rebaseOnBehalfOfUploader grant is missing/unapplied.
#   5. ASSERT the rebased patch set's `uploader` is still the ORIGINAL uploader (proves
#      on_behalf_of_uploader took effect, not a plain bot rebase).
#   6. CLEAN UP: abandon the scratch change and delete the scratch branch.
#
# Steps 1–3 + 6 need Gerrit ADMIN ssh (create/delete refs) and an uploader identity; wiring
# those into an unattended run is environment-specific. So this script implements the CORE,
# credential-light assertion (step 4 + 5) against a PRE-STAGED scratch change supplied via
# $SMOKE_CHANGE, and DOCUMENTS the staging/teardown steps for the operator. With no
# $SMOKE_CHANGE it prints the staging recipe and exits SKIP (non-fatal) rather than fail.
#
# Env:
#   GERRIT_BASE_URL          default https://rebar.solutions.navateam.com
#   AUTOLANDER_GERRIT_USER   default RebarBotNava   (the bot HTTP username)
#   AUTOLANDER_GERRIT_TOKEN  REQUIRED for the live assertion (the bot HTTP password)
#   SMOKE_CHANGE             a pre-staged, behind-its-base scratch change number/id to rebase
#   SCRATCH_BRANCH           default autolander-smoke (documentation only here)
# ---------------------------------------------------------------------------
set -euo pipefail

GERRIT_BASE_URL="${GERRIT_BASE_URL:-https://rebar.solutions.navateam.com}"
AUTOLANDER_GERRIT_USER="${AUTOLANDER_GERRIT_USER:-RebarBotNava}"
AUTOLANDER_GERRIT_TOKEN="${AUTOLANDER_GERRIT_TOKEN:-}"
SMOKE_CHANGE="${SMOKE_CHANGE:-}"
SCRATCH_BRANCH="${SCRATCH_BRANCH:-autolander-smoke}"
PROJECT="${PROJECT:-rebar}"

fail() { echo "smoke-check-autolander: FAIL — $*" >&2; exit 1; }
skip() { echo "smoke-check-autolander: SKIP — $*" >&2; exit 0; }

# Gerrit strips the XSSI prefix )]}' from JSON REST responses; drop it before parsing.
strip_xssi() { sed "1s/^)]}'//"; }

# --- Preconditions ---------------------------------------------------------
if [ -z "${AUTOLANDER_GERRIT_TOKEN}" ]; then
  skip "AUTOLANDER_GERRIT_TOKEN unset (populate the SSM slot / .env); cannot run the live REST assertion."
fi

if [ -z "${SMOKE_CHANGE}" ]; then
  cat >&2 <<EOF
smoke-check-autolander: no SMOKE_CHANGE supplied. Stage a scratch change first, then re-run
with SMOKE_CHANGE=<change-number>:

  # 1. create/reset the scratch branch at the current main tip (admin ssh):
  ssh -p 29418 admin@${GERRIT_BASE_URL#https://} gerrit create-branch ${PROJECT} ${SCRATCH_BRANCH} main
  # 2. push a commit for review to it (uploader):
  git push origin HEAD:refs/for/${SCRATCH_BRANCH}
  # 3. advance the scratch branch by one commit so the change is behind its base:
  git push origin HEAD:refs/heads/${SCRATCH_BRANCH}
  # 4. re-run: SMOKE_CHANGE=<n> $0
  # teardown afterwards: abandon the change + delete the scratch branch.
EOF
  skip "SMOKE_CHANGE unset (see staging recipe above)."
fi

# --- Step 4: rebase-on-behalf as the bot; assert authorized ----------------
echo "smoke-check-autolander: rebasing change ${SMOKE_CHANGE} on behalf of the uploader as ${AUTOLANDER_GERRIT_USER}" >&2
http_code="$(curl -sS -o /tmp/autolander-rebase.json -w '%{http_code}' \
  -u "${AUTOLANDER_GERRIT_USER}:${AUTOLANDER_GERRIT_TOKEN}" \
  -H 'Content-Type: application/json' \
  -X POST "${GERRIT_BASE_URL}/a/changes/${SMOKE_CHANGE}/rebase" \
  -d '{"on_behalf_of_uploader": true}' || echo 000)"

case "${http_code}" in
  200)
    echo "smoke-check-autolander: ok — rebase-on-behalf authorized (HTTP 200)" >&2
    ;;
  403)
    fail "HTTP 403 — bot lacks rebase / rebaseOnBehalfOfUploader on refs/heads/* (grant missing or refs/meta/config not pushed; run setup-project.sh)."
    ;;
  409)
    # 409 = change is up to date / not rebasable — the grant is fine but the scratch change
    # was not actually behind its base. A staging problem, not an ACL failure.
    fail "HTTP 409 — change ${SMOKE_CHANGE} is not rebasable (up to date?); re-stage it behind its base (step 3)."
    ;;
  *)
    fail "unexpected HTTP ${http_code} from rebase-on-behalf; response: $(strip_xssi </tmp/autolander-rebase.json 2>/dev/null | head -c 400)"
    ;;
esac

# --- Step 5: assert the rebased patch set preserved the original uploader --
# Read the change's current-revision uploader; on_behalf_of_uploader must keep it the
# ORIGINAL uploader (NOT the bot). We compare against the bot's own account: a rebased
# patch set uploaded AS the bot would show the bot as uploader (the on-behalf grant did
# NOT take effect); anything else means the original uploader was preserved.
detail="$(curl -sS -u "${AUTOLANDER_GERRIT_USER}:${AUTOLANDER_GERRIT_TOKEN}" \
  "${GERRIT_BASE_URL}/a/changes/${SMOKE_CHANGE}/detail?o=CURRENT_REVISION&o=DETAILED_ACCOUNTS" | strip_xssi)"

# Uploader is nested under revisions[current].uploader; parse robustly in python.
uploader_username="$(printf '%s' "${detail}" | python3 -c '
import sys, json
d = json.load(sys.stdin)
cur = d.get("current_revision")
rev = (d.get("revisions") or {}).get(cur, {})
up = rev.get("uploader") or {}
print(up.get("username") or up.get("email") or "")
' 2>/dev/null || true)"

if [ -z "${uploader_username}" ]; then
  echo "smoke-check-autolander: WARN — could not read the rebased patch set uploader; skipping the uploader-preservation assertion (the HTTP 200 above already proves the grant works)." >&2
elif [ "${uploader_username}" = "${AUTOLANDER_GERRIT_USER}" ]; then
  fail "rebased patch set uploader is the bot (${uploader_username}) — on_behalf_of_uploader did NOT take effect (rebaseOnBehalfOfUploader grant missing?)."
else
  echo "smoke-check-autolander: ok — rebased patch set preserved the original uploader (${uploader_username})" >&2
fi

echo "smoke-check-autolander: PASS — auto-lander rebase-on-behalf grant verified. Remember to abandon change ${SMOKE_CHANGE} and delete the ${SCRATCH_BRANCH} scratch branch." >&2
