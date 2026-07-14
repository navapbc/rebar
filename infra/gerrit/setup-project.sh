#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup-project.sh — provision the `rebar` Gerrit project + its managed groups + push
# its refs/meta/config (labels LLM-Review + Verified, submit requirements, submit-type
# pin, the feature-branch ACLs, and the Contributors Submit gate). Originally story S3;
# extended by epic 88ab / S1 (bored-tag-sale) to create/converge the
# `feature-branch-drivers` group and its ACLs, and later to add the `Contributors` group
# + the Submit ACL that restricts landing to authorized contributors + Administrators.
#
# Declarative + idempotent: creates the project + managed groups if absent, then pushes a
# fully declarative project.config to refs/meta/config (overwriting prior config). The
# SERVER-LEVEL change.submitWholeTopic (a global key, not project-scoped) is set
# out-of-band in the site gerrit.config (infra/compose/gerrit.config [change]) — it
# is NOT managed by this script.
#
# Auth (fail-fast): needs Gerrit ADMIN ssh access on 29418. Provide the admin key via
#   GERRIT_ADMIN_SSH_KEY  (path to the private key; default ~/.ssh/gerrit_admin)
#   GERRIT_SSH_USER       (default admin)
#   GERRIT_HOST           (default rebar.solutions.navateam.com)
#   GERRIT_SSH_PORT       (default 29418)
# Dry-run: set DRY_RUN=1 to print the diff of project.config vs live without pushing.
# ---------------------------------------------------------------------------
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

# --- 1b. Ensure managed groups exist (idempotent) --------------------------
# `feature-branch-drivers` (epic 88ab / S1 — bored-tag-sale; ADR 0025) holds the
# Create/Delete Reference (refs/heads/feature/*) + Push Merge Commit
# (refs/for/refs/heads/{main,feature/*}) ACLs in project.config. The `want`-dict step
# below only RESOLVES the UUIDs of groups that ALREADY exist (via `ls-groups`), so a
# group referenced by project.config but absent on the server would make Gerrit REJECT
# the refs/meta/config push with an "unknown group" error. So create it here if absent,
# and converge its membership on every run (both idempotent).
#
# Membership policy (ADR 0025): initial members = Administrators (included as a subgroup)
# + the named operating agents listed in FEATURE_BRANCH_DRIVER_MEMBERS (space-separated
# usernames; default empty = admins only). Membership changes flow through THIS script
# (an admin-approved edit), not ad-hoc UI grants.
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

# `Contributors` (landing-authorization gate; see the Submit ACL in project.config
# [access "refs/heads/*"]): its members + Administrators are the ONLY accounts allowed to
# Submit (land) a change — everyone else can still push to refs/for/* to PROPOSE. Created
# + converged here so the group referenced by project.config exists BEFORE the
# refs/meta/config push (an absent group makes Gerrit reject the push, same as the
# feature-branch-drivers group above).
#
# Membership policy (mirrors feature-branch-drivers): initial members = Administrators
# (included as a subgroup) + the named accounts in CONTRIBUTOR_MEMBERS (space-separated
# usernames; DEFAULT = "RebarBotNava", the landing bot). Set CONTRIBUTOR_MEMBERS to
# override the default list. Membership changes flow through THIS script (an
# admin-approved edit), not ad-hoc UI grants. NOTE: convergence is additive — to REMOVE
# a contributor, drop them from CONTRIBUTOR_MEMBERS AND run
# `gerrit set-members Contributors --remove <user>` as an admin; a re-run alone will not
# offboard.
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
