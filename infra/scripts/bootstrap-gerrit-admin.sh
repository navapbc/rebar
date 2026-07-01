#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# bootstrap-gerrit-admin.sh — register the admin SSH key on a fresh Gerrit site
# headlessly (story S2). Runs ON the box; drives the gerrit container via docker.
#
# WHY NOTEDB, NOT REST: Gerrit runs with auth.type=DEVELOPMENT_BECOME_ANY_ACCOUNT
# (PoC). The dev-login GET /login/<dest>?account_id=1000000 yields a GerritAccount
# session cookie that authenticates READ REST calls, but Gerrit rejects COOKIE-auth
# for MUTATIONS ("Invalid authentication method ... prefix with /a/"), and there is
# no obtainable XSRF token / HTTP password to bootstrap /a/ basic auth from a clean
# slate. So we register the admin's SSH key the deterministic way: Gerrit stores
# per-account SSH keys in the All-Users repo on the user branch
# refs/users/<NN>/<accountId> in an `authorized_keys` blob. We commit the key there
# and restart Gerrit so it reloads the account from NoteDb. The first dev-login
# (account 1000000) is auto-added to the Administrators group.
#
# Args / env:
#   ADMIN_PUBKEY     (required) the admin SSH public key
#   ADMIN_ACCOUNT_ID (default 1000000)
#   GERRIT_CONTAINER (default compose-gerrit-1)
#   GERRIT_GIT_DIR   (default /var/gerrit/git, in-container path to the repos)
# Idempotent: skips if the key is already in authorized_keys.
# ---------------------------------------------------------------------------
set -euo pipefail

ADMIN_PUBKEY="${ADMIN_PUBKEY:?ADMIN_PUBKEY (the admin SSH public key) is required}"
ADMIN_ACCOUNT_ID="${ADMIN_ACCOUNT_ID:-1000000}"
GERRIT_CONTAINER="${GERRIT_CONTAINER:-compose-gerrit-1}"
GERRIT_GIT_DIR="${GERRIT_GIT_DIR:-/var/gerrit/git}"
GERRIT_HTTP="${GERRIT_HTTP:-http://127.0.0.1:8080}"

# --- 0. Ensure account 1000000 EXISTS (and its NoteDb user branch) ----------
# On a FRESH NoteDb the user branch refs/users/<NN>/<id> does not exist until the
# account is first created. In DEVELOPMENT_BECOME_ANY_ACCOUNT, GET
# /login/<dest>?account_id=1000000 creates account 1000000 (auto-added to the
# Administrators group) — so we trigger it first, making the branch fetch below
# safe on a clean redeploy. Idempotent (a no-op once the account exists).
curl -fsS "${GERRIT_HTTP}/login/%23%2F?account_id=${ADMIN_ACCOUNT_ID}" -o /dev/null \
  || echo "bootstrap: dev-login probe returned non-zero (continuing)" >&2

# Shard: the user branch is refs/users/<last-two-digits>/<accountId>.
shard="$(printf '%02d' "$((ADMIN_ACCOUNT_ID % 100))")"
ref="refs/users/${shard}/${ADMIN_ACCOUNT_ID}"
allusers="${GERRIT_GIT_DIR}/All-Users.git"

docker exec -e ADMIN_PUBKEY="$ADMIN_PUBKEY" -e REF="$ref" -e ALLUSERS="$allusers" \
  "$GERRIT_CONTAINER" sh -lc '
    set -e
    cd /tmp && rm -rf au && git clone "$ALLUSERS" au >/dev/null 2>&1
    cd au
    # The user branch may not exist yet on a brand-new site; tolerate its absence
    # by starting an empty tree rather than aborting under set -e.
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

# Reload the account cache from NoteDb (a restart is the simplest reliable path).
docker restart "$GERRIT_CONTAINER" >/dev/null
echo "bootstrap-gerrit-admin: done; Gerrit restarted to reload account ${ADMIN_ACCOUNT_ID}." >&2
