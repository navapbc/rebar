#!/usr/bin/env bash
# Apply Gerrit-to-GitHub replication on the box. Materialize the SSM deploy key
# first (fail closed), then copy replication.config with Gerrit uid/gid ownership.
# DRY_RUN=1 prints actions. ALLOW_REMOTE_RELOAD=1 opts into plugin reload.
# The default requires a Gerrit restart for first load. autoReload handles later
# config changes after the plugin starts. GERRIT_SITE, COMPOSE_FILE, admin SSH
# settings, and deploy-key settings may be overridden.
# To stop replication, remove the site replication.config and restart Gerrit.
# -- DEPLOY-KEY ROTATION LIFECYCLE --
# Register the new write-enabled public deploy key, update SSM, and rerun this
# script to rematerialize the private key.
# Restart Gerrit after rematerialization. JGit caches SSH sessions/keys.
# Verify replication uses the new key before you REMOVE the OLD deploy key.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GERRIT_SITE="${GERRIT_SITE:-/var/gerrit/site}"
COMPOSE_FILE="${COMPOSE_FILE:-infra/compose/docker-compose.yml}"
ALLOW_REMOTE_RELOAD="${ALLOW_REMOTE_RELOAD:-0}"
DRY_RUN="${DRY_RUN:-0}"

GERRIT_HOST="${GERRIT_HOST:-rebar.solutions.navateam.com}"
GERRIT_SSH_USER="${GERRIT_SSH_USER:-admin}"
GERRIT_SSH_PORT="${GERRIT_SSH_PORT:-29418}"
GERRIT_ADMIN_SSH_KEY="${GERRIT_ADMIN_SSH_KEY:-$HOME/.ssh/gerrit_admin}"

GERRIT_UID=1000
GERRIT_GID=1000

SRC_CONFIG="${SCRIPT_DIR}/replication.config"
DST_CONFIG="${GERRIT_SITE}/etc/replication.config"

[ -f "$SRC_CONFIG" ] || { echo "setup-replication: source config not found at $SRC_CONFIG" >&2; exit 1; }

run() {
	if [ "$DRY_RUN" = "1" ]; then
		echo "setup-replication: DRY_RUN would run: $*" >&2
	else
		"$@"
	fi
}

# --- 1. Materialise the deploy key (fail-closed) ---------------------------
echo "setup-replication: materialising deploy key" >&2
if [ "$DRY_RUN" = "1" ]; then
	echo "setup-replication: DRY_RUN would run: ${SCRIPT_DIR}/materialize-deploy-key.sh" >&2
else
	"${SCRIPT_DIR}/materialize-deploy-key.sh"
fi

# --- 2. Copy replication.config into the site etc dir ----------------------
echo "setup-replication: installing ${DST_CONFIG}" >&2
run mkdir -p "${GERRIT_SITE}/etc"
run cp "$SRC_CONFIG" "$DST_CONFIG"
run chmod 0644 "$DST_CONFIG"
run chown "${GERRIT_UID}:${GERRIT_GID}" "$DST_CONFIG"

# --- 3. Reload (if remote admin enabled) or instruct a restart -------------
if [ "$ALLOW_REMOTE_RELOAD" = "1" ]; then
	echo "setup-replication: attempting remote 'plugin reload replication'" >&2
	[ -f "$GERRIT_ADMIN_SSH_KEY" ] || { echo "setup-replication: admin SSH key not found at $GERRIT_ADMIN_SSH_KEY" >&2; exit 1; }
	SSH="ssh -i $GERRIT_ADMIN_SSH_KEY -p $GERRIT_SSH_PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
	run $SSH "${GERRIT_SSH_USER}@${GERRIT_HOST}" gerrit plugin reload replication
	echo "setup-replication: replication plugin reloaded" >&2
else
	cat >&2 <<EOF
setup-replication: replication.config installed.
  Remote plugin admin is DISABLED on this instance, so the new replication.config
  is loaded at Gerrit STARTUP. If the deploy key was re-materialised, Gerrit may keep using a cached SSH key until restart.
  RESTART Gerrit to load current config
  and key material:
      docker compose -f ${COMPOSE_FILE} restart gerrit
  (Set ALLOW_REMOTE_RELOAD=1 only if remote plugin admin has been enabled.)
EOF
fi

echo "setup-replication: DONE" >&2
