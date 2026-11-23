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

# --- 2. Create the bot if absent ------------------------------------------
# No password on this command line: a process's argv is readable by every local
# user (ps, /proc/<pid>/cmdline), so the token is generated server-side in step 3
# (bug immoral-heated-doctorfish). Gerrit's SSH parser needs inner quotes around
# names with spaces. Capture errors without printing. Unexpected failures stop setup.
if err="$($GERRIT_SSH gerrit create-account "$BOT_USER" \
	--group "\"Service Users\"" \
	--full-name "\"rebar review bot\"" \
	--email "$BOT_EMAIL" 2>&1 1>/dev/null)"; then
	echo "service-user: created account '$BOT_USER' in Service Users" >&2
else
	case "$err" in
		*"already exists"*|*"already used"*|*"exists"*) : ;;  # fall through to rotate
		*) echo "service-user: create-account failed: $err" >&2; exit 1 ;;
	esac
	echo "service-user: account '$BOT_USER' exists — rotating HTTP token" >&2
fi

# --- 3. Rotate: Gerrit generates the new HTTP token -------------------------
# Replace Gerrit's default HTTP token; setting a second default fails. Gerrit 3.14
# prints "New password: <token>" on stdout, captured here and never echoed, so the
# token reaches no local argv. Fail closed on any other shape: the value check also
# keeps & and \ out of the template substitution in step 5.
#
# While Gerrit holds a token that SSM does not, ANY exit leaves the bot locked out:
# a set -e failure (the generator or aws exits non-zero), the shape check, or an
# interrupt. So an EXIT trap tells the operator, armed once Gerrit's token is gone
# (the delete succeeded) or replaced (the generator succeeded), and disarmed only
# after the SSM write succeeds (bug wary-unmindful-ibisbill). It never prints the
# token. A failed delete (Gerrit unreachable, or no default token yet) replaced nothing.
rotation_notice() {
	if [ -n "${interrupted:-}" ]; then
		# bash runs a trapped signal only after the foreground aws returns, so the
		# write may have committed: do not claim it did not.
		echo "service-user: interrupted after Gerrit's token for '$BOT_USER' was replaced; SSM may" >&2
		echo "service-user: not hold the new token, and the review-bot may be unable to authenticate" >&2
		echo "service-user: until this script succeeds." >&2
	else
		echo "service-user: Gerrit's token for '$BOT_USER' was already replaced and SSM was NOT" >&2
		echo "service-user: updated; the review-bot cannot authenticate until this script succeeds." >&2
	fi
}
arm_lockout_notice() {
	trap rotation_notice EXIT
	trap 'interrupted=1; exit 129' HUP
	trap 'interrupted=1; exit 130' INT
	trap 'interrupted=1; exit 143' TERM
}
if $GERRIT_SSH gerrit set-account "$BOT_USER" --delete-token default 2>/dev/null; then
	arm_lockout_notice
fi
generated="$($GERRIT_SSH gerrit set-account "$BOT_USER" --generate-http-password)"
arm_lockout_notice
generated="${generated%$'\r'}"
TOKEN="${generated#New password: }"
# The prefix must be present (a missing one leaves TOKEN == generated) and the rest one
# token-shaped word. Gerrit has already replaced the token; the EXIT trap says so.
if [ "$TOKEN" = "$generated" ] || case "$TOKEN" in "" | *[!A-Za-z0-9+/=._~-]*) true ;; *) false ;; esac; then
	unset generated TOKEN
	echo "service-user: unexpected --generate-http-password output (value not printed)." >&2
	exit 1
fi
unset generated

# --- 4. Store the token in SSM (SecureString, overwrite) -------------------
# The instance role grants ssm:GetParameter on /rebar/prod/* for the box; this
# put runs with whatever AWS creds the operator workstation holds. The value goes
# on stdin (printf is a builtin), never as `--value <token>` in aws's argv.
printf '%s' "$TOKEN" | aws ssm put-parameter \
	--region "$AWS_REGION" \
	--name "$SSM_TOKEN_PARAM" \
	--type SecureString \
	--overwrite \
	--value file:///dev/stdin >/dev/null
trap - EXIT HUP INT TERM # SSM holds the new token: the bot can authenticate again
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

# Render the committed template. The substitution stays so older checked-out
# templates with __BOT_TOKEN__ still rotate safely; current templates deliberately
# do not embed the secret in webhook URLs or unsupported header keys. A bash loop,
# not `awk -v tok=<token>`, so no process carries the token in its argv.
while IFS= read -r line || [ -n "$line" ]; do
	printf '%s\n' "${line//__BOT_TOKEN__/$TOKEN}"
done <"${SCRIPT_DIR}/webhooks.config" >webhooks.config

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
