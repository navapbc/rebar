#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup-project.sh — provision the `rebar` Gerrit project + push its refs/meta/config
# (label LLM-Review, submit requirement, ACL grant). Story S3.
#
# Declarative + idempotent: creates the project if absent, then pushes a fully
# declarative project.config to refs/meta/config (overwriting prior config). The
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
declare -A want=( ["Administrators"]=1 ["Service Users"]=1 )
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
git commit -q -m "S3: rebar LLM-Review label + submit requirement + ACL grant"
git push -q origin HEAD:refs/meta/config
echo "setup-project: pushed refs/meta/config for '$PROJECT'" >&2
