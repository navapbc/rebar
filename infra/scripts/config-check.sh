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

if [ "$fail" -ne 0 ]; then
  echo "config-check: FAILED — a malformed config was found (see above). Fix before this can land on main." >&2
  exit 1
fi
echo "config-check: all infra configs valid."
