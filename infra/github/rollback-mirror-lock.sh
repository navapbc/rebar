#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# rollback-mirror-lock.sh — S6 <15-minute rollback of the GitHub mirror-lock.
#
# Reverses apply-mirror-lock.sh:
#   1. Removes the two lock rulesets (gerrit-mirror-lock-main / -tags), found by
#      name. Default is DELETE; pass --disable to instead set enforcement to
#      "disabled" (keeps them for inspection).
#   2. Recreates the original `main-protection` ruleset by POSTing the snapshot
#      at infra/github/main-protection.snapshot.json (read-only fields stripped).
#   3. (optional --reenable-features) re-enables PRs/Issues/Actions in case the
#      cutover disabled them for mirror hygiene.
#
# AUTH: gh authenticated with a token holding Administration:write on
# navapbc/rebar (gh auth login or GH_TOKEN). NO token in this file.
#
# USAGE:
#   ./rollback-mirror-lock.sh                       # delete locks + restore main-protection
#   ./rollback-mirror-lock.sh --disable             # disable (not delete) the locks
#   ./rollback-mirror-lock.sh --reenable-features   # also re-enable PRs/Issues/Actions
# ---------------------------------------------------------------------------
set -euo pipefail

OWNER="navapbc"
REPO="rebar"
MAIN_RULESET_NAME="gerrit-mirror-lock-main"
TAG_RULESET_NAME="gerrit-mirror-lock-tags"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SNAPSHOT="${SCRIPT_DIR}/main-protection.snapshot.json"

MODE="delete" # delete | disable
REENABLE_FEATURES=0
for arg in "$@"; do
  case "$arg" in
    --disable) MODE="disable" ;;
    --reenable-features) REENABLE_FEATURES=1 ;;
    -h | --help)
      sed -n '2,30p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

api() { gh api -H "Accept: application/vnd.github+json" "$@"; }

ruleset_id_by_name() {
  local name="$1"
  api "/repos/${OWNER}/${REPO}/rulesets" \
    --jq ".[] | select(.name == \"${name}\") | .id"
}

echo "==> S6 mirror-lock ROLLBACK: ${OWNER}/${REPO} (mode=${MODE})"

# 1. Remove / disable the two lock rulesets.
for name in "${MAIN_RULESET_NAME}" "${TAG_RULESET_NAME}"; do
  rid="$(ruleset_id_by_name "${name}")"
  if [[ -z "${rid}" ]]; then
    echo "    '${name}' not present — nothing to undo"
    continue
  fi
  if [[ "${MODE}" == "delete" ]]; then
    echo "    deleting ruleset '${name}' (id ${rid})"
    api -X DELETE "/repos/${OWNER}/${REPO}/rulesets/${rid}"
  else
    echo "    disabling ruleset '${name}' (id ${rid})"
    api -X PUT "/repos/${OWNER}/${REPO}/rulesets/${rid}" \
      -f enforcement=disabled >/dev/null
  fi
done

# 2. Recreate the original main-protection ruleset from the S6-pre snapshot.
#    Strip server-managed/read-only fields before POSTing: the create endpoint
#    accepts only name/target/enforcement/conditions/rules/bypass_actors.
if [[ -f "${SNAPSHOT}" ]]; then
  if [[ -n "$(ruleset_id_by_name "main-protection")" ]]; then
    echo "    'main-protection' already present — skipping recreate"
  else
    echo "    recreating 'main-protection' from snapshot"
    jq '{name, target, enforcement, conditions, rules, bypass_actors}' "${SNAPSHOT}" |
      api -X POST "/repos/${OWNER}/${REPO}/rulesets" --input - --jq '.id' |
      xargs -I{} echo "    recreated main-protection id={}"
  fi
else
  echo "WARNING: snapshot ${SNAPSHOT} missing — cannot restore main-protection" >&2
fi

# 3. (optional) re-enable repo features that mirror hygiene may have disabled.
if [[ "${REENABLE_FEATURES}" -eq 1 ]]; then
  echo "    re-enabling PRs/Issues/Actions"
  api -X PATCH "/repos/${OWNER}/${REPO}" \
    -F has_issues=true -F allow_merge_commit=true \
    -F allow_squash_merge=true -F allow_rebase_merge=true >/dev/null
  api -X PUT "/repos/${OWNER}/${REPO}/actions/permissions" \
    -F enabled=true -f allowed_actions=all >/dev/null
fi

echo "==> ROLLBACK DONE. Verify a normal PR-merge to main works again."
