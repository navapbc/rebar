#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# register-deploy-key.sh — register the GitHub deploy key on navapbc/rebar.
# Story S5. Run by the OPERATOR from a workstation (NOT on the box).
#
# This is the GitHub side of the replication identity: it adds the PUBLIC half of
# the ed25519 deploy key (whose PRIVATE half lives in SSM
# /rebar/prod/github-replication-deploy-key and is materialised on the box by
# materialize-deploy-key.sh) as a WRITE-ENABLED deploy key on github.com/navapbc/
# rebar, so Gerrit can push to it.
#
# We do this with `gh` rather than the Terraform github provider deliberately:
# the github provider would require a GitHub token/credential wired into the
# terraform run + state, pulling a second provider and a long-lived PAT into the
# AWS-only state. A one-shot, idempotent `gh` script keeps the GitHub mutation
# out of terraform state and uses the operator's own already-authenticated gh.
# (If you DO prefer terraform, you would add the `integrations/github` provider
# and a `github_repository_deploy_key` resource, and supply GITHUB_TOKEN — note
# that requirement explicitly; this script is the simpler, stateless path.)
#
# Idempotent: if a deploy key with the same TITLE already exists it is left as-is
# (a deploy key's public key is immutable; to ROTATE, delete the old one — see
# the rotation lifecycle in setup-replication.sh).
#
# Env:
#   REPO          GitHub repo (default navapbc/rebar)
#   KEY_TITLE     deploy key title (default rebar-gerrit-replication)
#   PUBKEY_FILE   path to the PUBLIC key file (e.g. id_ed25519.pub). Required
#                 UNLESS PUBKEY is set.
#   PUBKEY        the public key string itself (alternative to PUBKEY_FILE)
#   DRY_RUN       set to 1 to print the action without calling the GitHub API
#
# AUTHORING NOTE: this script makes a LIVE GitHub mutation when actually run; it
# is checked in for the operator to run, and is NOT executed during S5 file
# authoring/validation.
# ---------------------------------------------------------------------------
set -euo pipefail

REPO="${REPO:-navapbc/rebar}"
KEY_TITLE="${KEY_TITLE:-rebar-gerrit-replication}"
DRY_RUN="${DRY_RUN:-0}"

command -v gh >/dev/null 2>&1 || { echo "register-deploy-key: gh CLI not found" >&2; exit 1; }

# Resolve the public key (from PUBKEY or PUBKEY_FILE).
if [ -n "${PUBKEY:-}" ]; then
	pubkey="$PUBKEY"
elif [ -n "${PUBKEY_FILE:-}" ]; then
	[ -f "$PUBKEY_FILE" ] || { echo "register-deploy-key: PUBKEY_FILE not found at $PUBKEY_FILE" >&2; exit 1; }
	pubkey="$(cat "$PUBKEY_FILE")"
else
	echo "register-deploy-key: set PUBKEY or PUBKEY_FILE (the deploy key's PUBLIC half)" >&2
	exit 1
fi

[ -n "$pubkey" ] || { echo "register-deploy-key: empty public key" >&2; exit 1; }

# --- Idempotency: skip if a deploy key with this title already exists -------
if gh api "repos/${REPO}/keys" --jq '.[].title' 2>/dev/null | grep -qxF -- "$KEY_TITLE"; then
	echo "register-deploy-key: deploy key '${KEY_TITLE}' already present on ${REPO} (no-op)" >&2
	exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
	echo "register-deploy-key: DRY_RUN would add deploy key '${KEY_TITLE}' (read_only=false) to ${REPO}" >&2
	exit 0
fi

# read_only=false -> WRITE access, required so Gerrit can push.
gh api --method POST "repos/${REPO}/keys" \
	-f title="$KEY_TITLE" \
	-f key="$pubkey" \
	-F read_only=false >/dev/null
echo "register-deploy-key: added write-enabled deploy key '${KEY_TITLE}' to ${REPO}" >&2
