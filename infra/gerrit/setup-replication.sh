#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup-replication.sh — apply the Gerrit -> GitHub replication config on the box.
# Story S5.
#
# Run by the OPERATOR, on (or against) the box. Orchestrates:
#   1. materialize-deploy-key.sh  — put the deploy key into the gerrit user's
#      ~/.ssh (fail-closed; aborts if the key is missing).
#   2. Copy replication.config into the Gerrit site etc dir
#      (/var/gerrit/site/etc/replication.config), chowned to the gerrit uid.
#   3. Reload the `replication` plugin IF remote plugin admin is enabled; else
#      tell the operator a Gerrit RESTART is required to load the config.
#
# REMOTE PLUGIN ADMIN IS DISABLED on this instance, so step 3 falls through to
# the restart path: replication.config is loaded at Gerrit startup. `autoReload`
# in replication.config re-reads the file on CHANGE once the plugin is loaded,
# but the FIRST load of a brand-new replication.config still needs a restart
# (the plugin must be (re)initialised). Expect to restart Gerrit after first run.
#
# Env:
#   GERRIT_SITE          Gerrit site root on the host (default /var/gerrit/site)
#   COMPOSE_FILE         compose file for the restart hint
#                        (default infra/compose/docker-compose.yml)
#   GERRIT_HOST/USER/PORT/ADMIN_SSH_KEY  — only used IF you opt into a remote
#                        `plugin reload` (disabled by default; see ALLOW_REMOTE_RELOAD)
#   ALLOW_REMOTE_RELOAD  set to 1 to attempt `gerrit plugin reload replication`
#                        over admin SSH (only works if remote plugin admin is
#                        enabled on the server; default 0 -> restart path)
#   DRY_RUN              set to 1 to print actions without applying
#
# Also passes DOTSSH / AWS_REGION / SSM_DEPLOY_KEY_PARAM through to
# materialize-deploy-key.sh (same defaults).
#
# -- KILL-SWITCH ------------------------------------------------------------
# To STOP replication immediately: remove (or rename) the site
# replication.config and restart Gerrit, e.g.
#     mv /var/gerrit/site/etc/replication.config /var/gerrit/site/etc/replication.config.disabled
#     docker compose -f infra/compose/docker-compose.yml restart gerrit
# With no replication.config the plugin loads but configures no remotes, so no
# pushes happen. Restore by moving the file back and restarting.
#
# -- DEPLOY-KEY ROTATION LIFECYCLE ------------------------------------------
# 1. Generate a NEW ed25519 keypair.
# 2. Add the new PUBLIC key as a deploy key (write access) on navapbc/rebar
#    (operator, via infra/gerrit/register-deploy-key.sh or the GitHub UI).
# 3. Overwrite SSM /rebar/prod/github-replication-deploy-key with the new PRIVATE
#    key (aws ssm put-parameter --overwrite --type SecureString).
# 4. Re-run THIS script (re-materialises the key + reloads/restarts).
# 5. Once replication is confirmed healthy on the new key, REMOVE the OLD deploy
#    key from navapbc/rebar.
# ---------------------------------------------------------------------------
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
  is loaded at Gerrit STARTUP. RESTART Gerrit to load it:
      docker compose -f ${COMPOSE_FILE} restart gerrit
  (Set ALLOW_REMOTE_RELOAD=1 only if remote plugin admin has been enabled.)
EOF
fi

echo "setup-replication: DONE" >&2
