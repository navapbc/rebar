#!/usr/bin/env bash
# Validate infrastructure configuration before deployment. Checks cover:
#   1. Gerrit git-config files parse as git-config (project.config, replication.config)
#   2. the gerrit-to-platform ini template parses (python configparser)
#   3. docker-compose.yml parses as YAML without requiring Docker
#   4. every infra shell script is syntactically valid (`bash -n`)
#   5. every `external: true` volume in docker-compose.yml is provisioned by
#      compose-up.sh, with negative self-tests for the comparison
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
fail=0
note() { printf '  %s\n' "$*"; }
bad()  { printf 'config-check: FAIL — %s\n' "$*" >&2; fail=1; }

echo "config-check: 1. Gerrit git-config files parse"
for f in infra/gerrit/project.config infra/gerrit/replication.config; do
  if [ -f "$f" ]; then
    if git config -f "$f" --list >/dev/null 2>&1; then note "ok: $f"; else bad "$f is not valid git-config"; fi
  fi
done

echo "config-check: 1b. Autosubmit label security invariants (epic f1fa / S1)"
# The auto-lander opt-in label MUST stay non-gating + non-sticky so it can never weaken the
# two-vote (LLM-Review + Verified) gate (CVE-2025-1568 / GerriScary posture). Guard activates
# once the label exists; asserts the invariants that keep it safe.
PC=infra/gerrit/project.config
if [ -f "$PC" ] && git config -f "$PC" --get label.Autosubmit.function >/dev/null 2>&1; then
  if grep -Eq 'submittableIf.*Autosubmit|applicableIf.*Autosubmit' "$PC"; then
    bad "Autosubmit is referenced by a submit-requirement — it MUST stay NON-GATING"
  else note "ok: Autosubmit is non-gating (no submit-requirement references it)"; fi
  if git config -f "$PC" --get label.Autosubmit.function 2>/dev/null | grep -qx NoBlock; then
    note "ok: [label \"Autosubmit\"] function = NoBlock"
  else bad "Autosubmit label function must be NoBlock"; fi
  CC=$(git config -f "$PC" --get label.Autosubmit.copyCondition 2>/dev/null || true)
  if [ -z "$CC" ]; then note "ok: Autosubmit copyCondition empty (non-sticky)"
  else bad "Autosubmit copyCondition must be empty (non-sticky); got: '$CC'"; fi
  if git config -f "$PC" --get-all 'access.refs/heads/*.label-Autosubmit' 2>/dev/null | grep -q 'group Contributors'; then
    note "ok: Autosubmit vote ACL grants group Contributors (requester-votable)"
  else bad "Autosubmit vote ACL must grant 'group Contributors' on refs/heads/*"; fi
else
  note "skip: Autosubmit label not present yet (pre-S1 cutover)"
fi

echo "config-check: 2. gerrit-to-platform ini template parses"
INI=infra/gerrit/gerrit_to_platform.ini.template
if [ -f "$INI" ]; then
  if python3 -c "import configparser,sys; configparser.ConfigParser().read(sys.argv[1])" "$INI" 2>/dev/null; then
    note "ok: $INI"
  else
    bad "$INI is not a parseable ini"
  fi
fi

echo "config-check: 3. docker-compose.yml is valid YAML"
# Parse YAML directly so this gate neither interpolates environment variables nor needs Docker.
COMPOSE=infra/compose/docker-compose.yml
if [ -f "$COMPOSE" ]; then
  if python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" "$COMPOSE" 2>/dev/null; then
    note "ok: $COMPOSE (yaml syntax)"
  else
    bad "$COMPOSE is not valid YAML"
  fi
fi

echo "config-check: 4. infra shell scripts are syntactically valid (bash -n)"
while IFS= read -r s; do
  if bash -n "$s" 2>/dev/null; then :; else bad "$s has a bash syntax error"; fi
done < <(find infra -name '*.sh' -type f | sort)
note "checked $(find infra -name '*.sh' -type f | wc -l | tr -d ' ') shell scripts"

echo "config-check: 5. every external compose volume is provisioned by compose-up.sh"
# Compare parsed external volumes with compose-up.sh's own side-effect-free enumeration.
COMPOSE_UP=infra/scripts/compose-up.sh

# Parse declared external volumes from YAML.
external_volumes() {
  python3 -c "
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
for name, spec in (doc.get('volumes') or {}).items():
    if isinstance(spec, dict) and spec.get('external'):
        print(name)
" "$1"
}

# Treat failed or empty provisioning enumeration as an error, never an empty set.
provisioned_volumes() {
  local out
  if ! out="$(bash "$1" --print-volumes 2>/dev/null)" || [ -z "$out" ]; then
    return 1
  fi
  printf '%s\n' "$out"
}

# Shared comparison used by both self-tests and the real configuration.
check_volume_drift() {
  local compose_file="$1" provision_script="$2" declared provisioned missing
  declared="$(external_volumes "$compose_file")" || { echo "could not parse external volumes from $compose_file"; return 1; }
  provisioned="$(provisioned_volumes "$provision_script")" || { echo "could not enumerate provisioned volumes ($provision_script --print-volumes failed or printed nothing)"; return 1; }
  missing="$(comm -23 <(sort <<<"$declared") <(sort <<<"$provisioned"))"
  if [ -n "$missing" ]; then
    echo "external volume(s) declared in $compose_file but not provisioned by $provision_script:" \
      "$(tr '\n' ' ' <<<"$missing")— extend SITE_SUBDIRS in compose-up.sh"
    return 1
  fi
}

if [ -f "$COMPOSE" ] && [ -f "$COMPOSE_UP" ]; then
  # Reject a declared volume absent from the provisioning list.
  synthetic="$(mktemp)"
  printf 'volumes:\n  gerrit_git:\n    external: true\n  gerrit_notprovisioned:\n    external: true\n' > "$synthetic"
  selftest_out="$(check_volume_drift "$synthetic" "$COMPOSE_UP")" && selftest_rc=0 || selftest_rc=$?
  if [ "$selftest_rc" -ne 0 ] && grep -q 'gerrit_notprovisioned' <<<"$selftest_out" && grep -q 'compose-up.sh' <<<"$selftest_out"; then
    note "ok: self-test detects an unprovisioned external volume"
  else
    bad "drift-check self-test failed to detect a known-bad fixture (extraction logic regressed)"
  fi
  rm -f "$synthetic"

  # Reject a missing provisioning script rather than accepting an empty result.
  if selftest_out="$(check_volume_drift "$COMPOSE" /nonexistent/compose-up.sh)"; then
    bad "drift-check self-test: a missing provisioning script passed (must fail loud)"
  elif grep -q 'could not enumerate provisioned volumes' <<<"$selftest_out"; then
    note "ok: self-test fails loud when provisioning cannot be enumerated"
  else
    bad "drift-check self-test: unexpected enumeration-failure message: $selftest_out"
  fi

  # Check sources.
  if out="$(check_volume_drift "$COMPOSE" "$COMPOSE_UP")"; then
    note "ok: all external volumes in $COMPOSE are provisioned by $COMPOSE_UP"
  else
    bad "$out"
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "config-check: FAILED — a malformed config was found (see above). Fix before this can land on main." >&2
  exit 1
fi
echo "config-check: all infra configs valid."
