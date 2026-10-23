#!/usr/bin/env bash
# Provision the review-bot account over Gerrit admin SSH, rotate its HTTP
# token into SSM, and surgically publish webhooks.config to refs/meta/config.
# Detect an existing account before create. Rotate with set-account and keep
# Service Users membership. The deployed OAuth setup still uses admin SSH for
# this operation. Public /review/ uses X-Rebar-Token. Internal Gerrit webhooks
# use the built-in X-Origin-Url header over the compose network.
# Admins need refs/meta/config push access. GERRIT_HOST/SSH_PORT/SSH_USER,
# GERRIT_ADMIN_SSH_KEY, BOT_USER, PROJECT, SSM_TOKEN_PARAM, and AWS_REGION
# select the endpoints and credentials. Never print the token.
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

# Create the bot or rotate its token only when the account already exists.
# Gerrit's SSH parser needs inner quotes around names with spaces. Capture
# errors without printing the token. Unexpected failures must stop setup.
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
	# Replace Gerrit's default HTTP token. Setting a second default fails.
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

# Render the committed template. Kept as an awk substitution so older checked-out
# templates with __BOT_TOKEN__ still rotate safely; current templates deliberately
# do not embed the secret in webhook URLs or unsupported header keys.
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
