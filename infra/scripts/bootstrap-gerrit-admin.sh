#!/usr/bin/env bash
# Register the bootstrap admin's public SSH key directly in Gerrit's NoteDb.
# Development login creates the initial account before authenticated REST writes are
# available; the key then lives in All-Users at refs/users/<NN>/<accountId>.
#
# Args / env:
#   ADMIN_PUBKEY     (required) the admin SSH public key
#   ADMIN_ACCOUNT_ID (default 1000000)
#   GERRIT_CONTAINER (default compose-gerrit-1)
#   GERRIT_GIT_DIR   (default /var/gerrit/git, in-container path to the repos)
# Existing keys are left unchanged.
set -euo pipefail

ADMIN_PUBKEY="${ADMIN_PUBKEY:?ADMIN_PUBKEY (the admin SSH public key) is required}"
ADMIN_ACCOUNT_ID="${ADMIN_ACCOUNT_ID:-1000000}"
GERRIT_CONTAINER="${GERRIT_CONTAINER:-compose-gerrit-1}"
GERRIT_GIT_DIR="${GERRIT_GIT_DIR:-/var/gerrit/git}"
GERRIT_HTTP="${GERRIT_HTTP:-http://127.0.0.1:8080}"

# Create the development-login account and its user ref before fetching it.
curl -fsS "${GERRIT_HTTP}/login/%23%2F?account_id=${ADMIN_ACCOUNT_ID}" -o /dev/null \
  || echo "bootstrap: dev-login probe returned non-zero (continuing)" >&2

# Shard user refs by the final two digits.
shard="$(printf '%02d' "$((ADMIN_ACCOUNT_ID % 100))")"
ref="refs/users/${shard}/${ADMIN_ACCOUNT_ID}"
allusers="${GERRIT_GIT_DIR}/All-Users.git"

docker exec -e ADMIN_PUBKEY="$ADMIN_PUBKEY" -e REF="$ref" -e ALLUSERS="$allusers" \
  "$GERRIT_CONTAINER" sh -lc '
    set -e
    cd /tmp && rm -rf au && git clone "$ALLUSERS" au >/dev/null 2>&1
    cd au
    # A new site may still lack the ref; build it from an empty tree.
    if git fetch origin "$REF" >/dev/null 2>&1; then
      git checkout -q FETCH_HEAD
    else
      echo "bootstrap: $REF absent; starting a fresh user branch" >&2
      git checkout -q --orphan userbranch
      git rm -rfq --cached . 2>/dev/null || true
    fi
    touch authorized_keys
    if grep -qF "$(printf "%s" "$ADMIN_PUBKEY" | awk "{print \$2}")" authorized_keys; then
      echo "bootstrap: admin SSH key already present (no-op)"; exit 0
    fi
    printf "%s\n" "$ADMIN_PUBKEY" >> authorized_keys
    git config user.email admin@example.com
    git config user.name Administrator
    git add authorized_keys
    git commit -q -m "S2: register admin SSH key"
    git push origin "HEAD:${REF}" >/dev/null 2>&1
    echo "bootstrap: registered admin SSH key on ${REF}"
  '

# Reload the account from NoteDb.
docker restart "$GERRIT_CONTAINER" >/dev/null
echo "bootstrap-gerrit-admin: done; Gerrit restarted to reload account ${ADMIN_ACCOUNT_ID}." >&2
