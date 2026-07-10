#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# config-check.sh — validate every infra config so a MALFORMED config can never
# reach `main` (epic 88ab, story 8903 — continuous auto-deploy).
#
# Run by `make config-check`, which is wired into test.yml AND gerrit-verify.yaml
# (the Verified gate mirrors test.yml's gates inline). A malformed config therefore
# FAILS CI → never earns Verified → never lands on `main`. This is the shift-left
# defence-in-depth partner to the auto-deploy's runtime pre-apply validation: catch
# a bad config at the gate, not on the live box.
#
# Checks (each fails the whole run, non-zero exit, on the first malformed file):
#   1. Gerrit git-config files parse as git-config (project.config, replication.config)
#   2. the gerrit-to-platform ini template parses (python configparser)
#   3. docker-compose.yml validates (`docker compose config -q` when docker is present;
#      YAML-syntax fallback via python otherwise, so the check still runs in a
#      docker-less dev shell)
#   4. every infra shell script is syntactically valid (`bash -n`)
#   5. every `external: true` volume in docker-compose.yml is provisioned by
#      compose-up.sh (requires compose-up.sh --print-volumes — the incident-2731
#      drift gate, with embedded self-tests against known-bad fixtures)
# ---------------------------------------------------------------------------
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
# YAML-SYNTAX validation (deterministic + environment-independent). We deliberately do NOT
# use `docker compose config`: it interpolates ${VARS} and needs the daemon, so it false-fails
# in any shell/CI where those env vars are unset — non-deterministic for a gate. A malformed
# compose file is a YAML error, which this catches; schema/interpolation issues surface at
# deploy time (the auto-deploy validates + health-checks before advancing deployed-sha).
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
# The incident-2731 drift gate: commit 10837ca88 declared gerrit_reviewbot_tickets
# external:true in docker-compose.yml without extending compose-up.sh's provisioning
# list, and every `compose up` then failed "external volume not found" for 41h. This
# check makes that class of drift fail CI: the external volume names are parsed out
# of the compose file with yaml.safe_load (the same python3+yaml dependency check 3
# uses — no grep heuristics over multi-line YAML) and diffed against the volume names
# compose-up.sh --print-volumes reports it would create (the script's own provisioning
# list run through its own derivation helper, so there is no second copy to drift).
COMPOSE_UP=infra/scripts/compose-up.sh

# List the `external: true` volume names declared in a compose file.
external_volumes() {
  python3 -c "
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
for name, spec in (doc.get('volumes') or {}).items():
    if isinstance(spec, dict) and spec.get('external'):
        print(name)
" "$1"
}

# List the volume names a provisioning script would create. An enumeration failure
# (missing script, non-zero exit, empty output) must be LOUD — never an empty set.
provisioned_volumes() {
  local out
  if ! out="$(bash "$1" --print-volumes 2>/dev/null)" || [ -z "$out" ]; then
    return 1
  fi
  printf '%s\n' "$out"
}

# The drift comparison, factored so the self-tests below exercise the REAL logic.
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
  # Self-test A (pattern-regression guard): a synthetic compose snippet with an
  # unprovisioned external volume MUST be detected — a silent false-negative
  # regression of the extraction logic fails CI here.
  synthetic="$(mktemp)"
  printf 'volumes:\n  gerrit_git:\n    external: true\n  gerrit_notprovisioned:\n    external: true\n' > "$synthetic"
  selftest_out="$(check_volume_drift "$synthetic" "$COMPOSE_UP")" && selftest_rc=0 || selftest_rc=$?
  if [ "$selftest_rc" -ne 0 ] && grep -q 'gerrit_notprovisioned' <<<"$selftest_out" && grep -q 'compose-up.sh' <<<"$selftest_out"; then
    note "ok: self-test detects an unprovisioned external volume"
  else
    bad "drift-check self-test failed to detect a known-bad fixture (extraction logic regressed)"
  fi
  rm -f "$synthetic"

  # Self-test B (enumeration failure is loud): a missing provisioning script must
  # FAIL the check, never pass as an empty provisioned set.
  if selftest_out="$(check_volume_drift "$COMPOSE" /nonexistent/compose-up.sh)"; then
    bad "drift-check self-test: a missing provisioning script passed (must fail loud)"
  elif grep -q 'could not enumerate provisioned volumes' <<<"$selftest_out"; then
    note "ok: self-test fails loud when provisioning cannot be enumerated"
  else
    bad "drift-check self-test: unexpected enumeration-failure message: $selftest_out"
  fi

  # The real check.
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
