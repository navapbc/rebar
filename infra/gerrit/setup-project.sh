#!/usr/bin/env bash
# Provision Gerrit project and managed groups, then replace project.config in
# refs/meta/config declaratively. Create missing groups before resolving their
# current UUIDs, including Registered Users. Converge memberships additively and
# remove members only when explicitly requested. DRY_RUN=1 prints the diff.
# Admin SSH credentials use GERRIT_ADMIN_SSH_KEY, GERRIT_SSH_USER/PORT/HOST.
# Server-wide change.submitWholeTopic is configured in gerrit.config, not here.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="${PROJECT:-rebar}"
GERRIT_HOST="${GERRIT_HOST:-rebar.solutions.navateam.com}"
GERRIT_SSH_USER="${GERRIT_SSH_USER:-admin}"
GERRIT_SSH_PORT="${GERRIT_SSH_PORT:-29418}"
GERRIT_ADMIN_SSH_KEY="${GERRIT_ADMIN_SSH_KEY:-$HOME/.ssh/gerrit_admin}"

[ -f "$GERRIT_ADMIN_SSH_KEY" ] || { echo "setup-project: admin SSH key not found at $GERRIT_ADMIN_SSH_KEY" >&2; exit 1; }

SSH="ssh -i $GERRIT_ADMIN_SSH_KEY -p $GERRIT_SSH_PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
GIT_SSH_COMMAND="$SSH"
export GIT_SSH_COMMAND
GERRIT_SSH="$SSH ${GERRIT_SSH_USER}@${GERRIT_HOST}"

# --- 1. Create the project if absent (idempotent) --------------------------
if $GERRIT_SSH gerrit ls-projects | grep -qxF -- "$PROJECT"; then
  echo "setup-project: project '$PROJECT' already exists" >&2
else
  echo "setup-project: creating project '$PROJECT'" >&2
  $GERRIT_SSH gerrit create-project "$PROJECT" --empty-commit
fi

# Create managed groups before resolving their UUIDs for project.config.
# Otherwise Gerrit rejects unknown groups. The feature-branch-drivers group grants
# feature ref create/delete and merge-change pushes. Administrators are a
# subgroup. FEATURE_BRANCH_DRIVER_MEMBERS adds named operators.
FB_GROUP="feature-branch-drivers"
if $GERRIT_SSH gerrit ls-groups | grep -qxF -- "$FB_GROUP"; then
  echo "setup-project: group '$FB_GROUP' already exists" >&2
else
  echo "setup-project: creating group '$FB_GROUP'" >&2
  $GERRIT_SSH gerrit create-group "$FB_GROUP" \
    --description "Feature-branch drivers: create/delete feature/* branches + push merge commits (epic 88ab / ADR 0025)" \
    --group Administrators
fi
# Converge membership idempotently (re-adding an existing member/subgroup is a no-op).
$GERRIT_SSH gerrit set-members "$FB_GROUP" --include Administrators >/dev/null 2>&1 || true
for _m in ${FEATURE_BRANCH_DRIVER_MEMBERS:-}; do
  echo "setup-project: ensuring '$_m' in '$FB_GROUP'" >&2
  $GERRIT_SSH gerrit set-members "$FB_GROUP" --add "$_m" >/dev/null 2>&1 || true
done

# Contributors and Administrators may Submit. Other users may only propose.
# CONTRIBUTOR_MEMBERS defaults to RebarBotNava. Membership convergence adds
# accounts only. To offboard an operator, remove it from the variable and run
# `gerrit set-members Contributors --remove <user>` to offboard them.
CONTRIB_GROUP="Contributors"
CONTRIBUTOR_MEMBERS="${CONTRIBUTOR_MEMBERS:-RebarBotNava}"
if $GERRIT_SSH gerrit ls-groups | grep -qxF -- "$CONTRIB_GROUP"; then
  echo "setup-project: group '$CONTRIB_GROUP' already exists" >&2
else
  echo "setup-project: creating group '$CONTRIB_GROUP'" >&2
  $GERRIT_SSH gerrit create-group "$CONTRIB_GROUP" \
    --description "Authorized contributors: the only non-admin accounts allowed to Submit (land) changes" \
    --owner Administrators \
    --group Administrators
fi
# Converge membership idempotently (re-adding an existing member/subgroup is a no-op).
$GERRIT_SSH gerrit set-members "$CONTRIB_GROUP" --include Administrators >/dev/null 2>&1 || true
for _m in $CONTRIBUTOR_MEMBERS; do
  echo "setup-project: ensuring '$_m' in '$CONTRIB_GROUP'" >&2
  $GERRIT_SSH gerrit set-members "$CONTRIB_GROUP" --add "$_m" >/dev/null 2>&1 || true
done

# --- 2. Push the declarative refs/meta/config ------------------------------
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
git clone -q "ssh://${GERRIT_SSH_USER}@${GERRIT_HOST}:${GERRIT_SSH_PORT}/${PROJECT}" "$work/repo"
cd "$work/repo"
git fetch -q origin refs/meta/config
git checkout -q FETCH_HEAD
cp "${SCRIPT_DIR}/project.config" project.config

# The `groups` file maps the UUID of EVERY group referenced in project.config (named
# AND system/global) to its name; Gerrit validates refs/meta/config against it and
# rejects the push if any referenced group is absent. Named groups (Administrators,
# Service Users) get their UUID from the live group list; the system group "Registered
# Users" has the well-known global: UUID.
declare -A want=( ["Administrators"]=1 ["Service Users"]=1 ["feature-branch-drivers"]=1 ["Contributors"]=1 )
{
  echo "# UUID	Group Name"
  echo "global:Registered-Users	Registered Users"
  $GERRIT_SSH gerrit ls-groups --verbose | while IFS=$'\t' read -r name uuid rest; do
    if [ -n "${want[$name]:-}" ]; then printf '%s\t%s\n' "$uuid" "$name"; fi
  done
} > groups

# Stage first so a NEW (untracked) `groups` file is counted as a change; then a
# clean index means refs/meta/config already matches the desired state.
git add -A
if git diff --cached --quiet; then
  echo "setup-project: refs/meta/config already up to date (no-op)" >&2
  exit 0
fi

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "setup-project: DRY_RUN — staged diff vs live refs/meta/config:" >&2
  git --no-pager diff --cached
  exit 0
fi

git config user.email admin@example.com
git config user.name Administrator
git commit -q -m "rebar project.config: LLM-Review + Verified labels + submit requirements + submit-type = rebase-if-necessary (ADR 0047; Verified carries TRIVIAL_REBASE) + feature-branch ACLs + Contributors submit gate"
git push -q origin HEAD:refs/meta/config
echo "setup-project: pushed refs/meta/config for '$PROJECT'" >&2
