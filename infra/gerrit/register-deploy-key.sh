#!/usr/bin/env bash
# Operator-only GitHub change: add the public half of Gerrit's replication key
# as a write-enabled deploy key on REPO (default navapbc/rebar). The private half
# stays in SSM. Supply PUBKEY_FILE or PUBKEY. KEY_TITLE identifies an existing
# key and leaves it untouched. DRY_RUN=1 prints the action without an API call.
# Rotation removes the old key separately. Use the operator's gh credentials.
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
