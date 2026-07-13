#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# smoke-check-autolander.sh — READ-ONLY provisioning verification for the auto-lander's
# Gerrit ACL + label (epic f1fa / S5b, AC5).
#
# This is a DEPLOY-STEP CANARY that confirms the auto-lander's Gerrit-side provisioning is in
# place, using ONLY read-only checks — a `grep` of the committed project.config plus
# authenticated GETs of the live project's access + label surfaces. It performs NO writes
# (no scratch branch, no push, no rebase), so it is safe to run against the live instance at
# any time and never touches `main` or any change.
#
# What it verifies (all read-only):
#   1. infra/gerrit/project.config declares the per-ref `rebase` grant to Contributors and the
#      non-gating `label-Autosubmit` grant (the SOURCE of the provisioning).
#   2. GET /a/projects/rebar/access shows the LIVE refs/heads/* permissions include `rebase`
#      and `label-Autosubmit` (the cutover landed).
#   3. GET /a/projects/rebar/labels/Autosubmit shows the label is provisioned + non-gating
#      (function NoBlock, values -1..+1).
#
# Auth: needs the admin HTTP credential. Provide GERRIT_HOST (default
# rebar.solutions.navateam.com) and GERRIT_ADMIN_USER + GERRIT_ADMIN_TOKEN (HTTP basic).
# Without credentials the live GETs are SKIPPED (non-fatal) and only the config grep runs, so
# the script is safe to invoke anywhere (CI config-check exercises the grep path).
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_CONFIG="${SCRIPT_DIR}/project.config"
GERRIT_HOST="${GERRIT_HOST:-rebar.solutions.navateam.com}"
GERRIT_ADMIN_USER="${GERRIT_ADMIN_USER:-}"
GERRIT_ADMIN_TOKEN="${GERRIT_ADMIN_TOKEN:-}"

fail() { echo "smoke-check-autolander: FAIL — $1" >&2; exit 1; }
ok() { echo "  ok: $1"; }

# --- 1. Config source of truth (grep; always runs) -------------------------
echo "smoke-check-autolander: 1. project.config declares the auto-lander grants"
grep -qE '^[[:space:]]*rebase = group Contributors' "$PROJECT_CONFIG" \
  || fail "project.config missing 'rebase = group Contributors' (rebase-on-behalf grant, S5b)"
ok "rebase grant to Contributors present"
grep -qE '^[[:space:]]*label-Autosubmit = -1\.\.\+1 group Contributors' "$PROJECT_CONFIG" \
  || fail "project.config missing 'label-Autosubmit = -1..+1 group Contributors' (S1)"
ok "label-Autosubmit vote grant present"

# --- Live GETs (skipped without admin credentials) -------------------------
if [ -z "$GERRIT_ADMIN_USER" ] || [ -z "$GERRIT_ADMIN_TOKEN" ]; then
  echo "smoke-check-autolander: SKIP live GETs (no GERRIT_ADMIN_USER/GERRIT_ADMIN_TOKEN) — config grep passed."
  exit 0
fi

BASE="https://${GERRIT_HOST}/a"
# Gerrit prefixes JSON with an XSSI guard line ")]}'"; strip it before parsing.
get_json() { curl -fsS -u "${GERRIT_ADMIN_USER}:${GERRIT_ADMIN_TOKEN}" "$1" | tail -n +2; }

# --- 2. Live access: rebase + label-Autosubmit on refs/heads/* -------------
echo "smoke-check-autolander: 2. live GET /a/projects/rebar/access shows the grants"
get_json "${BASE}/projects/rebar/access" | python3 -c '
import json,sys
d=json.load(sys.stdin)
perms=d.get("local",{}).get("refs/heads/*",{}).get("permissions",{})
missing=[p for p in ("rebase","label-Autosubmit") if p not in perms]
if missing: sys.exit("live refs/heads/* missing permission(s): "+", ".join(missing))
print("  ok: live access has rebase + label-Autosubmit on refs/heads/*")
' || fail "live access missing the auto-lander grant(s) — cutover not applied?"

# --- 3. Live label: Autosubmit provisioned + non-gating --------------------
echo "smoke-check-autolander: 3. live GET /a/projects/rebar/labels/Autosubmit (non-gating)"
get_json "${BASE}/projects/rebar/labels/Autosubmit" | python3 -c '
import json,sys
d=json.load(sys.stdin)
if d.get("function")!="NoBlock": sys.exit("Autosubmit function is not NoBlock (must stay non-gating)")
if "+1" not in "".join(d.get("values",{}).keys()): sys.exit("Autosubmit is missing its +1 value")
print("  ok: Autosubmit label present, function=NoBlock (non-gating)")
' || fail "Autosubmit label not provisioned as a non-gating -1..+1 label"

echo "smoke-check-autolander: all read-only provisioning checks passed."
