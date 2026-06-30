#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# smoke-check.sh — verify the S4a review-bot plumbing end-to-end. Story S4a.
#
# Asserts (and DOCUMENTS, as runnable checks) the two properties S4b depends on:
#
#   1. DUAL-SCOPE BOT TOKEN — the bot's single HTTP token (SSM
#      /rebar/prod/gerrit-bot-token) authenticates BOTH:
#        (a) the Gerrit REST API at /a/  (GET /a/accounts/self), AND
#        (b) git-over-HTTPS clone of the rebar repo (the receiver's read path —
#            S4b clones the change ref into repo_root using this same credential).
#      So no separate read key is provisioned (the S5 deploy key is GitHub-write-only).
#
#   2. EVENTS-LOG BACKFILL ENDPOINT — `GET /a/plugins/events-log/events/` returns
#      recent events. NOTE: the TRAILING SLASH is REQUIRED — `/events` (no slash)
#      404s; `/events/` (with slash) is the correct path. S4b's reconciler MUST use
#      the trailing-slash form.
#
# Exits non-zero on any failed check (so CI / an operator can gate on it). The bot
# token is read from SSM with the operator/instance AWS creds and never echoed.
#
# Env: GERRIT_HOST (default rebar.solutions.navateam.com), BOT_USER
#      (default rebar-review-bot), SSM_TOKEN_PARAM (default
#      /rebar/prod/gerrit-bot-token), AWS_REGION (default us-east-1), PROJECT (rebar).
# ---------------------------------------------------------------------------
set -euo pipefail

GERRIT_HOST="${GERRIT_HOST:-rebar.solutions.navateam.com}"
BOT_USER="${BOT_USER:-rebar-review-bot}"
SSM_TOKEN_PARAM="${SSM_TOKEN_PARAM:-/rebar/prod/gerrit-bot-token}"
AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT:-rebar}"

TOKEN="$(aws ssm get-parameter --region "$AWS_REGION" --name "$SSM_TOKEN_PARAM" \
	--with-decryption --query 'Parameter.Value' --output text)"
[ -n "$TOKEN" ] && [ "$TOKEN" != "None" ] || { echo "smoke-check: bot token absent in SSM" >&2; exit 1; }

fail=0

# --- 1a. Bot token -> Gerrit REST /a/ --------------------------------------
code="$(curl -sS -o /dev/null -w '%{http_code}' -u "${BOT_USER}:${TOKEN}" \
	"https://${GERRIT_HOST}/a/accounts/self")"
if [ "$code" = "200" ]; then
	echo "smoke-check: [PASS] bot token authenticates Gerrit REST /a/ (GET /a/accounts/self = 200)" >&2
else
	echo "smoke-check: [FAIL] bot token REST /a/ returned $code (expected 200)" >&2; fail=1
fi

# --- 1b. Bot token -> git-over-HTTPS clone ---------------------------------
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
if git clone -q "https://${BOT_USER}:${TOKEN}@${GERRIT_HOST}/a/${PROJECT}" "$tmp/repo" 2>/dev/null; then
	echo "smoke-check: [PASS] bot token authenticates git-over-HTTPS clone of ${PROJECT}" >&2
else
	echo "smoke-check: [FAIL] bot token git-over-HTTPS clone of ${PROJECT} failed" >&2; fail=1
fi

# --- 2. events-log backfill endpoint (trailing slash REQUIRED) -------------
code="$(curl -sS -o /dev/null -w '%{http_code}' -u "${BOT_USER}:${TOKEN}" \
	"https://${GERRIT_HOST}/a/plugins/events-log/events/")"
if [ "$code" = "200" ]; then
	echo "smoke-check: [PASS] events-log endpoint /a/plugins/events-log/events/ = 200 (backfill ready)" >&2
else
	echo "smoke-check: [FAIL] events-log endpoint returned $code (expected 200; note the REQUIRED trailing slash)" >&2; fail=1
fi

if [ "$fail" -ne 0 ]; then
	echo "smoke-check: FAILED" >&2; exit 1
fi
echo "smoke-check: all checks PASSED" >&2
