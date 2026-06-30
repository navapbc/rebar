#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# apply-mirror-lock.sh — S6 GitHub mirror-lock (gh-api path).
#
# Locks navapbc/rebar so ONLY the Gerrit replication deploy key
# (title "rebar-gerrit-replication", registered in S5) can update `main` and
# tags. Every human/admin push, PR-merge, force-push, deletion, and tag write
# is rejected.
#
# HOW THE LOCK WORKS
#   A repository ruleset with the `update` rule means "restrict updates": only
#   the ruleset's bypass_actors may update the matched refs. Because a PR merge
#   into main is itself an *update to main* performed by a non-bypass actor, the
#   `update` rule rejects BOTH direct pushes AND PR merges. We add `deletion`
#   and `non_fast_forward` so the branch can't be deleted or force-pushed
#   either. A second ruleset locks tags (creation/update/deletion).
#
#   The sole bypass actor is the deploy key, expressed in the REST API as:
#       {"actor_type": "DeployKey", "bypass_mode": "always"}
#   (no actor_id — GitHub identifies the single repo deploy-key bypass slot by
#   actor_type alone; a DeployKey bypass entry carries no numeric actor_id.)
#
# THIS IS THE OPERATOR'S RELIABLE LIVE PATH and the fallback when the Terraform
# provider is older than 6.8.0 (which lacks the native DeployKey bypass).
#
# AUTH: gh must be authenticated with a token holding `Administration:write`
# on navapbc/rebar (via `gh auth login` or GH_TOKEN env). NO token in this file.
#
# USAGE:
#   ./apply-mirror-lock.sh                 # create the two lock rulesets
#   ./apply-mirror-lock.sh --delete-main-protection
#                                          # also delete the pre-existing
#                                          # main-protection ruleset (id 18048287)
# ---------------------------------------------------------------------------
set -euo pipefail

OWNER="navapbc"
REPO="rebar"
DEPLOY_KEY_TITLE="rebar-gerrit-replication"
MAIN_PROTECTION_ID="18048287"
MAIN_RULESET_NAME="gerrit-mirror-lock-main"
TAG_RULESET_NAME="gerrit-mirror-lock-tags"

DELETE_MAIN_PROTECTION=0
for arg in "$@"; do
  case "$arg" in
    --delete-main-protection) DELETE_MAIN_PROTECTION=1 ;;
    -h | --help)
      sed -n '2,40p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

api() { gh api -H "Accept: application/vnd.github+json" "$@"; }

echo "==> S6 mirror-lock: ${OWNER}/${REPO}"

# 1. (optional) delete the pre-existing main-protection ruleset. The new
#    update-rule lock supersedes it; leaving it in place is harmless but the
#    operator may want a clean board. Snapshot lives at
#    infra/github/main-protection.snapshot.json for rollback.
if [[ "$DELETE_MAIN_PROTECTION" -eq 1 ]]; then
  echo "==> deleting pre-existing main-protection ruleset (id ${MAIN_PROTECTION_ID})"
  api -X DELETE "/repos/${OWNER}/${REPO}/rulesets/${MAIN_PROTECTION_ID}" ||
    echo "    (already absent or delete failed — continuing)"
fi

# 2. EXISTENCE GATE — the replication deploy key MUST exist (registered in S5).
#    Fail loudly if absent: a lock whose only bypass is a missing key would lock
#    EVERYONE out, including replication.
echo "==> verifying deploy key '${DEPLOY_KEY_TITLE}' is present"
KEY_ID="$(api "/repos/${OWNER}/${REPO}/keys" \
  --jq ".[] | select(.title == \"${DEPLOY_KEY_TITLE}\") | .id")"
if [[ -z "${KEY_ID}" ]]; then
  echo "FATAL: deploy key titled '${DEPLOY_KEY_TITLE}' not found on ${OWNER}/${REPO}." >&2
  echo "       Register it (S5) before applying the lock, or replication breaks." >&2
  exit 1
fi
echo "    found deploy key id=${KEY_ID} (bypass is by actor_type=DeployKey, not this id)"

# Helper: does a ruleset with this name already exist? echoes its id or empty.
ruleset_id_by_name() {
  local name="$1"
  api "/repos/${OWNER}/${REPO}/rulesets" \
    --jq ".[] | select(.name == \"${name}\") | .id"
}

# 3a. Branch lock for main.
EXISTING_MAIN="$(ruleset_id_by_name "${MAIN_RULESET_NAME}")"
if [[ -n "${EXISTING_MAIN}" ]]; then
  echo "==> branch lock '${MAIN_RULESET_NAME}' already exists (id ${EXISTING_MAIN}) — skipping create"
else
  echo "==> creating branch lock '${MAIN_RULESET_NAME}'"
  MAIN_ID="$(api -X POST "/repos/${OWNER}/${REPO}/rulesets" --input - <<'JSON' --jq '.id'
{
  "name": "gerrit-mirror-lock-main",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": { "include": ["refs/heads/main"], "exclude": [] }
  },
  "rules": [
    { "type": "update" },
    { "type": "deletion" },
    { "type": "non_fast_forward" }
  ],
  "bypass_actors": [
    { "actor_type": "DeployKey", "bypass_mode": "always" }
  ]
}
JSON
)"
  echo "    created branch lock id=${MAIN_ID}"
fi

# 3b. Tag lock for all tags.
EXISTING_TAG="$(ruleset_id_by_name "${TAG_RULESET_NAME}")"
if [[ -n "${EXISTING_TAG}" ]]; then
  echo "==> tag lock '${TAG_RULESET_NAME}' already exists (id ${EXISTING_TAG}) — skipping create"
else
  echo "==> creating tag lock '${TAG_RULESET_NAME}'"
  TAG_ID="$(api -X POST "/repos/${OWNER}/${REPO}/rulesets" --input - <<'JSON' --jq '.id'
{
  "name": "gerrit-mirror-lock-tags",
  "target": "tag",
  "enforcement": "active",
  "conditions": {
    "ref_name": { "include": ["refs/tags/**"], "exclude": [] }
  },
  "rules": [
    { "type": "creation" },
    { "type": "update" },
    { "type": "deletion" }
  ],
  "bypass_actors": [
    { "actor_type": "DeployKey", "bypass_mode": "always" }
  ]
}
JSON
)"
  echo "    created tag lock id=${TAG_ID}"
fi

echo "==> DONE. Verify empirically (see infra/runbooks/github-mirror-lock.md):"
echo "    a non-bypass push/PR-merge/force-push/deletion to main, and any tag"
echo "    push, must be REJECTED; replication via the deploy key must still pass."
