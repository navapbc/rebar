#!/usr/bin/env bash
# Run as root before Gerrit boots. Read the GitHub PAT from G2P_SSM_PARAM
# (default /rebar/prod/g2p-github-pat) through the instance role. Missing or
# empty values fail closed. Render the template in G2P_DIR (default
# /var/gerrit/site/g2p-config), mounted in the container at
# /var/gerrit/.config/gerrit_to_platform, and copy REPLICATION_CONF beside it.
# Files belong to uid/gid 1000 with restrictive modes. Never log the PAT.
# AWS_REGION defaults to us-east-1. TEMPLATE and REPLICATION_CONF are
# overridable paths. A missing Verified vote still blocks submission.
set -euo pipefail

G2P_DIR="${G2P_DIR:-/var/gerrit/site/g2p-config}"
AWS_REGION="${AWS_REGION:-us-east-1}"
G2P_SSM_PARAM="${G2P_SSM_PARAM:-/rebar/prod/g2p-github-pat}"
TEMPLATE="${TEMPLATE:-$(dirname "$0")/gerrit_to_platform.ini.template}"
REPLICATION_CONF="${REPLICATION_CONF:-/var/gerrit/site/etc/replication.config}"

GERRIT_UID=1000
GERRIT_GID=1000
INI="${G2P_DIR}/gerrit_to_platform.ini"

echo "materialize-g2p-config: ensuring ${G2P_DIR} exists" >&2
mkdir -p "$G2P_DIR"
chmod 0700 "$G2P_DIR"
chown "${GERRIT_UID}:${GERRIT_GID}" "$G2P_DIR"

# --- 1. Fetch the fine-grained PAT from SSM (fail-closed) -------------------
echo "materialize-g2p-config: fetching PAT from SSM ${G2P_SSM_PARAM}" >&2
pat="$(aws ssm get-parameter \
	--region "$AWS_REGION" \
	--name "$G2P_SSM_PARAM" \
	--with-decryption \
	--query 'Parameter.Value' \
	--output text)"

if [ -z "$pat" ] || [ "$pat" = "None" ]; then
	echo "materialize-g2p-config: FATAL — SSM param ${G2P_SSM_PARAM} is empty/None; refusing to start" >&2
	exit 1
fi

# --- 2. Render the ini (token substituted); 0600 (holds the PAT) ------------
if [ ! -f "$TEMPLATE" ]; then
	echo "materialize-g2p-config: FATAL — template not found: ${TEMPLATE}" >&2
	exit 1
fi
# Substitute via a shell replace (not sed) so PAT metacharacters can't break the
# expression, and never expose the token on a command line.
template_body="$(cat "$TEMPLATE")"
( umask 077; printf '%s\n' "${template_body//__GITHUB_PAT__/$pat}" > "$INI" )
unset pat template_body
chmod 0600 "$INI"
chown "${GERRIT_UID}:${GERRIT_GID}" "$INI"
echo "materialize-g2p-config: wrote ${INI} (0600, ${GERRIT_UID}:${GERRIT_GID})" >&2

# Copy replication.config into g2p's container view. A symlink to the host
# path dangles there. Re-copy on boot so owner/repo edits propagate. The copied
# file has no PAT and uses mode 0644.
if [ -e "$REPLICATION_CONF" ]; then
	# Remove a stale symlink first. cp can otherwise see the same host inode.
	rm -f "${G2P_DIR}/replication.config"
	cp -f "$REPLICATION_CONF" "${G2P_DIR}/replication.config"
	chmod 0644 "${G2P_DIR}/replication.config"
	chown "${GERRIT_UID}:${GERRIT_GID}" "${G2P_DIR}/replication.config"
	echo "materialize-g2p-config: copied replication.config" >&2
else
	echo "materialize-g2p-config: WARN — ${REPLICATION_CONF} not found; g2p owner/repo discovery will fail until replication is set up (story S2/S5)" >&2
fi

echo "materialize-g2p-config: DONE — g2p config materialised for the gerrit user" >&2
