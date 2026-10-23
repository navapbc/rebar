#!/usr/bin/env bash
# Provision the CI service account over admin SSH so only Service Users can
# cast Verified. Register its validated PUBLIC SSH key, never a private key or
# HTTP password. The operator delivers the private half to GitHub Actions.
# Re-runs preserve group membership and accept an already-registered key.
# GERRIT_HOST/SSH_PORT/SSH_USER/ADMIN_SSH_KEY and CI_BOT_USER/EMAIL/PUBKEY_FILE
# select the account and public-key input. OAuth does not replace SSH voting.
set -euo pipefail

GERRIT_HOST="${GERRIT_HOST:-rebar.solutions.navateam.com}"
GERRIT_SSH_PORT="${GERRIT_SSH_PORT:-29418}"
GERRIT_SSH_USER="${GERRIT_SSH_USER:-admin}"
GERRIT_ADMIN_SSH_KEY="${GERRIT_ADMIN_SSH_KEY:-$HOME/.ssh/gerrit_admin}"
CI_BOT_USER="${CI_BOT_USER:-rebar-ci-bot}"
CI_BOT_EMAIL="${CI_BOT_EMAIL:-rebar-ci-bot@rebar.solutions.navateam.com}"
CI_SSH_PUBKEY_FILE="${CI_SSH_PUBKEY_FILE:-$HOME/.ssh/rebar_ci_gerrit.pub}"

# --- 1. Fail-fast on missing inputs ----------------------------------------
[ -f "$GERRIT_ADMIN_SSH_KEY" ] || {
	echo "setup-ci-service-account: admin SSH key not found at $GERRIT_ADMIN_SSH_KEY" >&2
	exit 1
}
[ -f "$CI_SSH_PUBKEY_FILE" ] || {
	echo "setup-ci-service-account: CI public key not found at $CI_SSH_PUBKEY_FILE" >&2
	echo "  Generate the keypair first (see infra/runbooks/g2p-ci-credentials.md):" >&2
	echo "    ssh-keygen -t ed25519 -C rebar-ci-gerrit -f ~/.ssh/rebar_ci_gerrit -N ''" >&2
	exit 1
}

# Sanity: the file must LOOK like an OpenSSH public key (single line, ssh-*/ecdsa-*/
# sk-* prefix). This guards against accidentally handing this script a PRIVATE key.
pubkey="$(head -n1 "$CI_SSH_PUBKEY_FILE")"
case "$pubkey" in
	ssh-* | ecdsa-* | sk-ssh-* | sk-ecdsa-*) : ;;
	*)
		echo "setup-ci-service-account: ${CI_SSH_PUBKEY_FILE} does not look like an SSH PUBLIC key" >&2
		echo "  (expected a line starting with ssh-ed25519 / ssh-rsa / ecdsa-… ). Refusing." >&2
		echo "  Did you point CI_SSH_PUBKEY_FILE at the PRIVATE key by mistake?" >&2
		exit 1
		;;
esac
if grep -q 'PRIVATE KEY' "$CI_SSH_PUBKEY_FILE"; then
	echo "setup-ci-service-account: ${CI_SSH_PUBKEY_FILE} contains a PRIVATE KEY block — refusing." >&2
	exit 1
fi

SSH="ssh -i $GERRIT_ADMIN_SSH_KEY -p $GERRIT_SSH_PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
GERRIT_SSH="$SSH ${GERRIT_SSH_USER}@${GERRIT_HOST}"

# Create the SSH-only CI account or ensure its Service Users membership and key
# when it already exists. Pass the public key on stdin. Gerrit's SSH parser
# still needs inner quotes for names with spaces. Unexpected failures stop setup.
if err="$($GERRIT_SSH gerrit create-account "$CI_BOT_USER" \
	--group "\"Service Users\"" \
	--full-name "\"rebar CI bot\"" \
	--email "$CI_BOT_EMAIL" \
	--ssh-key - <"$CI_SSH_PUBKEY_FILE" 2>&1 1>/dev/null)"; then
	echo "setup-ci-service-account: created account '$CI_BOT_USER' in Service Users with its SSH key" >&2
else
	case "$err" in
		*"already exists"*|*"already used"*|*"exists"*) : ;;  # fall through to ensure state
		*) echo "setup-ci-service-account: create-account failed: $err" >&2; exit 1 ;;
	esac
	echo "setup-ci-service-account: account '$CI_BOT_USER' exists — ensuring group + key" >&2

	# Ensure "Service Users" membership (idempotent: re-adding an existing member is
	# benign). This is the ACL hook that grants label-Verified (project.config).
	$GERRIT_SSH gerrit set-members "\"Service Users\"" --add "$CI_BOT_USER"
	echo "setup-ci-service-account: ensured '$CI_BOT_USER' in Service Users" >&2

	# (Re-)register the public key. Adding a key that is already present makes Gerrit
	# error ("already"/"duplicate") — treat that as a no-op so re-runs are idempotent.
	if kerr="$($GERRIT_SSH gerrit set-account "$CI_BOT_USER" \
		--add-ssh-key - <"$CI_SSH_PUBKEY_FILE" 2>&1 1>/dev/null)"; then
		echo "setup-ci-service-account: registered SSH public key on '$CI_BOT_USER'" >&2
	else
		case "$kerr" in
			*"already"*|*"duplicate"*|*"exists"*) echo "setup-ci-service-account: SSH key already present (no-op)" >&2 ;;
			*) echo "setup-ci-service-account: add-ssh-key failed: $kerr" >&2; exit 1 ;;
		esac
	fi
fi

echo "setup-ci-service-account: DONE — CI service account '$CI_BOT_USER' ready to cast Verified over SSH" >&2
echo "  Next (operator): put the matching PRIVATE key in SSM /rebar/prod/ci-gerrit-ssh-key" >&2
echo "  and in the GitHub Actions secret GERRIT_SSH_PRIVKEY. See g2p-ci-credentials.md." >&2
