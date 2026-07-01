#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# service-user.sh — provision the rebar review-bot Gerrit service account,
# rotate its HTTP token into SSM, and push the rendered webhooks.config to the
# rebar project's refs/meta/config. Story S4a (review-bot identity + plumbing).
#
# Run from a workstation that holds the Gerrit ADMIN ssh key. This PoC runs
# DEVELOPMENT_BECOME_ANY_ACCOUNT, so cookie-auth REST mutations are refused —
# the bot account + token are created via the Gerrit SSH admin command set
# (authenticated by the admin SSH key), NOT via REST.
#
# Idempotent + rotating: re-running creates the account if absent (else rotates
# its token via set-account), always overwrites the SSM param, and pushes a
# fresh webhooks.config. The bot always ends up in "Service Users" with the
# known token, and that same token lands in SSM and in refs/meta/config.
#
# NOTE: the bot's HTTP token DOUBLES as the inbound webhook URL token (the
# `__BOT_TOKEN__` substituted into webhooks.config), so the receiver's
# `/review/?token=…` and the SSM-stored bot HTTP token are one and the same
# secret. See ADR-0014.
#
# NOTE: the surgical refs/meta/config push below requires Administrators to have
# push on refs/meta/config in All-Projects. On a FRESH instance that grant may be
# absent and the push will be REJECTED; the operator must grant it first. (This
# was already done for this instance.)
#
# Env:
#   GERRIT_HOST           (default rebar.solutions.navateam.com)
#   GERRIT_SSH_PORT       (default 29418)
#   GERRIT_SSH_USER       (default admin)
#   GERRIT_ADMIN_SSH_KEY  (default ~/.ssh/gerrit_admin)
#   BOT_USER              (default rebar-review-bot)
#   PROJECT               (default rebar)
#   SSM_TOKEN_PARAM       (default /rebar/prod/gerrit-bot-token)
#   AWS_REGION            (default us-east-1)
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GERRIT_HOST="${GERRIT_HOST:-rebar.solutions.navateam.com}"
GERRIT_SSH_PORT="${GERRIT_SSH_PORT:-29418}"
GERRIT_SSH_USER="${GERRIT_SSH_USER:-admin}"
GERRIT_ADMIN_SSH_KEY="${GERRIT_ADMIN_SSH_KEY:-$HOME/.ssh/gerrit_admin}"
BOT_USER="${BOT_USER:-rebar-review-bot}"
BOT_EMAIL="${BOT_EMAIL:-rebar-review-bot@rebar.solutions.navateam.com}"
PROJECT="${PROJECT:-rebar}"
SSM_TOKEN_PARAM="${SSM_TOKEN_PARAM:-/rebar/prod/gerrit-bot-token}"
AWS_REGION="${AWS_REGION:-us-east-1}"

# --- 1. Fail-fast if the admin key is absent -------------------------------
[ -f "$GERRIT_ADMIN_SSH_KEY" ] || {
	echo "service-user: admin SSH key not found at $GERRIT_ADMIN_SSH_KEY" >&2
	exit 1
}

SSH="ssh -i $GERRIT_ADMIN_SSH_KEY -p $GERRIT_SSH_PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
GERRIT_SSH="$SSH ${GERRIT_SSH_USER}@${GERRIT_HOST}"
export GIT_SSH_COMMAND="$SSH"

# --- 2. Generate a strong token locally ------------------------------------
# 256 bits of hex. Never echoed to stdout (only written to SSM + refs/meta/config).
TOKEN="$(openssl rand -hex 32)"

# --- 3. Idempotent account create / token rotation -------------------------
# Try to create the bot in "Service Users" with the known token. If the account
# already exists, create-account fails — fall back to set-account to ROTATE the
# token. Either way the bot ends up in Service Users with $TOKEN as its HTTP pw.
#
# stderr is captured (not echoed wholesale) so a failure message never leaks the
# token if a future change were to include it; the token is not on this line.
# NOTE: Gerrit re-parses the SSH command string with its own shell-like tokenizer,
# so arguments containing spaces ("Service Users", the full name) must arrive with
# their quotes INTACT. The local shell strips one layer of quotes, so we embed an
# inner pair (\"...\") that survives transmission to Gerrit's parser.
# Capture stderr so a REAL failure (bad key, unreachable host, missing group) is
# surfaced — only an "already exists" error should fall through to rotation. The
# token is on the --http-password arg, never in $err, so this does not leak it.
if err="$($GERRIT_SSH gerrit create-account "$BOT_USER" \
	--group "\"Service Users\"" \
	--full-name "\"rebar review bot\"" \
	--email "$BOT_EMAIL" \
	--http-password "$TOKEN" 2>&1 1>/dev/null)"; then
	echo "service-user: created account '$BOT_USER' in Service Users" >&2
else
	case "$err" in
		*"already exists"*|*"already used"*|*"exists"*) : ;;  # fall through to rotate
		*) echo "service-user: create-account failed: $err" >&2; exit 1 ;;
	esac
	echo "service-user: account '$BOT_USER' exists — rotating HTTP token" >&2
	# Gerrit 3.14's auth-token model stores the HTTP password as a token with id
	# "default"; setting --http-password when it already exists fails ("token default
	# already exists"). DELETE the existing "default" token first (no-op-safe if
	# absent), then set the fresh one.
	$GERRIT_SSH gerrit set-account "$BOT_USER" --delete-token default 2>/dev/null || true
	$GERRIT_SSH gerrit set-account "$BOT_USER" --http-password "$TOKEN"
fi

# --- 4. Store the token in SSM (SecureString, overwrite) -------------------
# The instance role grants ssm:GetParameter on /rebar/prod/* for the box; this
# put runs with whatever AWS creds the operator workstation holds.
aws ssm put-parameter \
	--region "$AWS_REGION" \
	--name "$SSM_TOKEN_PARAM" \
	--type SecureString \
	--overwrite \
	--value "$TOKEN" >/dev/null
echo "service-user: stored bot token in SSM ${SSM_TOKEN_PARAM}" >&2

# --- 5. Render webhooks.config + surgical push to refs/meta/config ---------
# SURGICAL read-modify-write: clone the project, fetch + checkout refs/meta/config,
# write ONLY webhooks.config (DO NOT touch project.config or groups), commit, push.
# This is the ONLY place the webhooks plugin reads its remote config from.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
git clone -q "ssh://${GERRIT_SSH_USER}@${GERRIT_HOST}:${GERRIT_SSH_PORT}/${PROJECT}" "$work/repo"
cd "$work/repo"
git fetch -q origin refs/meta/config
git checkout -q FETCH_HEAD

# Render the committed template: substitute __BOT_TOKEN__ with the live token.
# Use awk (not sed) so token characters are never interpreted as sed replacement
# metacharacters. The rendered file is written ONLY to the working tree here.
awk -v tok="$TOKEN" '{ gsub(/__BOT_TOKEN__/, tok); print }' \
	"${SCRIPT_DIR}/webhooks.config" > webhooks.config

# Stage ONLY webhooks.config — leave project.config / groups untouched.
git add webhooks.config
if git diff --cached --quiet; then
	echo "service-user: webhooks.config already up to date (no push)" >&2
	exit 0
fi

git config user.email admin@example.com
git config user.name Administrator
git commit -q -m "S4a: rebar review-bot webhooks.config (patchset-created -> /review/)"
git push -q origin HEAD:refs/meta/config
echo "service-user: pushed webhooks.config to refs/meta/config for '$PROJECT'" >&2
echo "service-user: DONE" >&2
