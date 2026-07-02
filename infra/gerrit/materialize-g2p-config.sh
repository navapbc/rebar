#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# materialize-g2p-config.sh — materialise the gerrit-to-platform config (incl. the
# GitHub PAT) into the gerrit user's config dir BEFORE Gerrit starts. Epic 1fa8 /
# story S3. Mirrors materialize-deploy-key.sh (story S5).
#
# Runs on the HOST as root (instance role can read the SecureString from SSM). It
# writes into the host dir bind-mounted to the container's g2p config dir:
#     host  $G2P_DIR (/var/gerrit/site/g2p-config)
#       ->  container /var/gerrit/.config/gerrit_to_platform
# (see infra/compose/docker-compose.yml). g2p (running in the Gerrit container) reads
# gerrit_to_platform.ini from there for the GitHub token, and replication.config to
# discover the owner/repo to workflow_dispatch. The in-container gerrit user is uid/gid
# 1000, so everything is chowned 1000:1000.
#
# FAIL-CLOSED: exits non-zero on ANY failure (empty/None/missing PAT, write failure).
# Run it BEFORE `docker compose up` so Gerrit never starts able to receive patchsets
# but unable to dispatch CI. (CI is still fail-closed at the gate regardless: no
# Verified vote -> no submit.)
#
# Env:
#   G2P_DIR         host dir bind-mounted to the container g2p config dir
#                   (default /var/gerrit/site/g2p-config)
#   AWS_REGION      (default us-east-1)
#   G2P_SSM_PARAM   SSM SecureString holding the fine-grained GitHub PAT
#                   (default /rebar/prod/g2p-github-pat — provisioned by story S4/ssm.tf)
#   TEMPLATE        ini template (default: sibling gerrit_to_platform.ini.template)
#   REPLICATION_CONF host path to the live replication.config
#                   (default /var/gerrit/site/etc/replication.config)
#
# The PAT is NEVER echoed to stdout/stderr.
# ---------------------------------------------------------------------------
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

# --- 3. Symlink replication.config so g2p can discover owner/repo -----------
if [ -e "$REPLICATION_CONF" ]; then
	ln -sf "$REPLICATION_CONF" "${G2P_DIR}/replication.config"
	chown -h "${GERRIT_UID}:${GERRIT_GID}" "${G2P_DIR}/replication.config"
	echo "materialize-g2p-config: linked replication.config" >&2
else
	echo "materialize-g2p-config: WARN — ${REPLICATION_CONF} not found; g2p owner/repo discovery will fail until replication is set up (story S2/S5)" >&2
fi

echo "materialize-g2p-config: DONE — g2p config materialised for the gerrit user" >&2
