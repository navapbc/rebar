#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# materialize-deploy-key.sh — materialise the GitHub replication deploy key into
# the gerrit user's ~/.ssh BEFORE Gerrit starts. Story S5.
#
# Runs on the HOST as root (it has the instance role, so it can read the
# SecureString from SSM). It writes into the host directory that is bind-mounted
# to the container's /var/gerrit/.ssh (the gerrit user's HOME is /var/gerrit, so
# its SSH dir is /var/gerrit/.ssh). The mount is:
#     host  $DOTSSH (/var/gerrit/site/dotssh)  ->  container /var/gerrit/.ssh
# (see infra/compose/docker-compose.yml). The in-container gerrit user is uid/gid
# 1000, so everything is chowned 1000:1000.
#
# FAIL-CLOSED: this script exits non-zero on ANY failure (empty/None/missing
# param, write/chown failure). It is meant to run BEFORE `docker compose up` so
# that Gerrit NEVER starts without a usable deploy key — a missing key must abort
# the boot, not silently disable replication.
#
# Env:
#   DOTSSH                host .ssh dir bind-mounted to /var/gerrit/.ssh
#                         (default /var/gerrit/site/dotssh)
#   AWS_REGION            (default us-east-1)
#   SSM_DEPLOY_KEY_PARAM  SSM param holding the ed25519 PRIVATE key
#                         (default /rebar/prod/github-replication-deploy-key)
#
# NOTE on the param name: an earlier ticket draft referred to
# `/rebar/prod/github-deploy-key`; the ACTUAL provisioned parameter is
# `/rebar/prod/github-replication-deploy-key` (the more specific name). This
# script defaults to the provisioned name. If you bootstrapped under the old
# name, either rename the SSM param or override SSM_DEPLOY_KEY_PARAM.
#
# The private key is NEVER echoed to stdout/stderr.
# ---------------------------------------------------------------------------
set -euo pipefail

DOTSSH="${DOTSSH:-/var/gerrit/site/dotssh}"
AWS_REGION="${AWS_REGION:-us-east-1}"
SSM_DEPLOY_KEY_PARAM="${SSM_DEPLOY_KEY_PARAM:-/rebar/prod/github-replication-deploy-key}"

# In-container gerrit user uid:gid (the bind mount is shared with the container).
GERRIT_UID=1000
GERRIT_GID=1000

KEY_FILE="${DOTSSH}/id_ed25519"
KNOWN_HOSTS="${DOTSSH}/known_hosts"
SSH_CONFIG="${DOTSSH}/config"

# In-container paths (what the ssh config IdentityFile/UserKnownHostsFile point
# at — these are inside the container, NOT the host DOTSSH path).
CONTAINER_SSH_DIR="/var/gerrit/.ssh"

echo "materialize-deploy-key: ensuring ${DOTSSH} exists" >&2
mkdir -p "$DOTSSH"
chmod 0700 "$DOTSSH"
chown "${GERRIT_UID}:${GERRIT_GID}" "$DOTSSH"

# --- 1. Fetch the private deploy key from SSM (fail-closed) -----------------
echo "materialize-deploy-key: fetching deploy key from SSM ${SSM_DEPLOY_KEY_PARAM}" >&2
key="$(aws ssm get-parameter \
	--region "$AWS_REGION" \
	--name "$SSM_DEPLOY_KEY_PARAM" \
	--with-decryption \
	--query 'Parameter.Value' \
	--output text)"

if [ -z "$key" ] || [ "$key" = "None" ]; then
	echo "materialize-deploy-key: FATAL — SSM param ${SSM_DEPLOY_KEY_PARAM} is empty/None; refusing to start Gerrit without a deploy key" >&2
	exit 1
fi

# Write the key with a restrictive umask so it is never briefly world-readable.
( umask 077; printf '%s\n' "$key" > "$KEY_FILE" )
unset key
chmod 0600 "$KEY_FILE"
chown "${GERRIT_UID}:${GERRIT_GID}" "$KEY_FILE"
echo "materialize-deploy-key: wrote private key to ${KEY_FILE} (0600, ${GERRIT_UID}:${GERRIT_GID})" >&2

# --- 2. Pin github.com host keys via ssh-keyscan (deduped) ------------------
echo "materialize-deploy-key: keyscanning github.com" >&2
scanned="$(ssh-keyscan github.com 2>/dev/null)"
if [ -z "$scanned" ]; then
	echo "materialize-deploy-key: FATAL — ssh-keyscan github.com returned nothing" >&2
	exit 1
fi
touch "$KNOWN_HOSTS"
# Append only host-key lines not already present (dedupe across re-runs).
while IFS= read -r line; do
	[ -z "$line" ] && continue
	grep -qxF -- "$line" "$KNOWN_HOSTS" || printf '%s\n' "$line" >> "$KNOWN_HOSTS"
done <<< "$scanned"
chmod 0644 "$KNOWN_HOSTS"
chown "${GERRIT_UID}:${GERRIT_GID}" "$KNOWN_HOSTS"
echo "materialize-deploy-key: known_hosts updated (${KNOWN_HOSTS})" >&2

# --- 3. Write the ssh client config ----------------------------------------
# Paths in this config are CONTAINER paths (/var/gerrit/.ssh/...), since the
# replication plugin's git/ssh runs inside the container. StrictHostKeyChecking
# stays on (yes) — github.com is pinned in known_hosts above.
cat > "$SSH_CONFIG" <<EOF
Host github.com
	HostName github.com
	User git
	IdentityFile ${CONTAINER_SSH_DIR}/id_ed25519
	IdentitiesOnly yes
	StrictHostKeyChecking yes
	UserKnownHostsFile ${CONTAINER_SSH_DIR}/known_hosts
EOF
chmod 0644 "$SSH_CONFIG"
chown "${GERRIT_UID}:${GERRIT_GID}" "$SSH_CONFIG"
echo "materialize-deploy-key: wrote ssh config (${SSH_CONFIG})" >&2

echo "materialize-deploy-key: DONE — deploy key materialised for the gerrit user" >&2
