#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# materialize-opcert-guard.sh — materialise the op-cert ORIGIN GUARD from SSM into
# (1) the HOST-nginx map file and (2) the compose service .env, then reload nginx.
# Story 76d2. Mirrors the SSM->file precedent (infra/gerrit/materialize-deploy-key.sh,
# materialize-g2p-config.sh).
#
# The guard is the shared secret that API Gateway injects as the static request header
# `X-Opcert-Guard` (Terraform maps random_password.opcert_guard.result onto the integration and
# stores the SAME value in SSM /rebar/prod/opcert-origin-guard). This script closes the loop on
# the box:
#   1. HOST-nginx map entry: writes /etc/nginx/opcert-guard.map.conf = `"<value>" 1;` — the one
#      line the fail-closed guard map in infra/nginx/rebar.conf.template glob-includes. Until
#      this file exists, the map's `default 0` makes /opcert/ serve 403 to everyone (STRUCTURAL
#      deny-all); once written, requests carrying the matching header get $opcert_guard_ok = 1.
#   2. Service env: sets REBAR_OPCERT_GUARD in the compose .env (defense in depth — the app ALSO
#      rejects a mismatched header with 403 before enqueuing any work).
#   3. `nginx -s reload` so the new/rotated map takes effect on the host nginx (a no-op-safe
#      refresh at first boot).
#
# ROTATION runbook (fail-closed window between the two steps):
#   terraform apply -replace=random_password.opcert_guard   # new SSM value + API GW header
#   infra/scripts/materialize-opcert-guard.sh               # rewrite the nginx map + reload
# Between the two, /opcert/ serves 403 — brief and acceptable (the existing materialize precedent).
#
# FAIL-CLOSED: exits non-zero on ANY failure (empty/None/missing guard, write failure). Run it
# BEFORE `docker compose up` (wired into infra/scripts/compose-up.sh). The guard is NEVER echoed.
#
# Env:
#   AWS_REGION            (default us-east-1)
#   OPCERT_GUARD_SSM_PARAM SSM SecureString holding the origin guard
#                         (default /rebar/prod/opcert-origin-guard — provisioned by opcert.tf)
#   NGINX_MAP_FILE        host nginx map file (default /etc/nginx/opcert-guard.map.conf)
#   ENV_FILE              compose .env to land REBAR_OPCERT_GUARD into
#                         (default: sibling ../compose/.env)
#   RELOAD_NGINX          set to 0 to skip `nginx -s reload` (default 1)
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AWS_REGION="${AWS_REGION:-us-east-1}"
OPCERT_GUARD_SSM_PARAM="${OPCERT_GUARD_SSM_PARAM:-/rebar/prod/opcert-origin-guard}"
NGINX_MAP_FILE="${NGINX_MAP_FILE:-/etc/nginx/opcert-guard.map.conf}"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/../compose/.env}"
RELOAD_NGINX="${RELOAD_NGINX:-1}"

# --- 1. Fetch the origin guard from SSM (fail-closed) -----------------------
echo "materialize-opcert-guard: fetching guard from SSM ${OPCERT_GUARD_SSM_PARAM}" >&2
guard="$(aws ssm get-parameter \
	--region "$AWS_REGION" \
	--name "$OPCERT_GUARD_SSM_PARAM" \
	--with-decryption \
	--query 'Parameter.Value' \
	--output text)"

if [ -z "$guard" ] || [ "$guard" = "None" ]; then
	echo "materialize-opcert-guard: FATAL — SSM param ${OPCERT_GUARD_SSM_PARAM} is empty/None; refusing (fail-closed: /opcert/ stays 403)" >&2
	exit 1
fi

# --- 2. Write the one-line nginx map entry `"<value>" 1;` (0600) ------------
# Use printf with shell substitution (not sed) so guard metacharacters can't break the line,
# and never expose the value on a command line. The map key is the exact header value.
mkdir -p "$(dirname "$NGINX_MAP_FILE")"
( umask 077; printf '"%s" 1;\n' "$guard" > "$NGINX_MAP_FILE" )
chmod 0600 "$NGINX_MAP_FILE"
echo "materialize-opcert-guard: wrote ${NGINX_MAP_FILE} (0600)" >&2

# --- 3. Land REBAR_OPCERT_GUARD in the compose .env (idempotent) ------------
# Strip any prior REBAR_OPCERT_GUARD line, then append the current value, preserving the rest.
if [ -f "$ENV_FILE" ]; then
	tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
	chmod 600 "$tmp"
	grep -v '^REBAR_OPCERT_GUARD=' "$ENV_FILE" > "$tmp" || true
	printf 'REBAR_OPCERT_GUARD=%s\n' "$guard" >> "$tmp"
	mv -f "$tmp" "$ENV_FILE"
	chmod 600 "$ENV_FILE"
	echo "materialize-opcert-guard: set REBAR_OPCERT_GUARD in ${ENV_FILE} (0600)" >&2
else
	echo "materialize-opcert-guard: WARN — ${ENV_FILE} not found; skipping service-env guard (nginx map still written)" >&2
fi
unset guard

# --- 4. Reload host nginx so the new/rotated map takes effect ---------------
if [ "$RELOAD_NGINX" != "0" ]; then
	if command -v nginx >/dev/null 2>&1; then
		echo "materialize-opcert-guard: reloading host nginx" >&2
		nginx -s reload
	else
		echo "materialize-opcert-guard: WARN — nginx not found on PATH; skipping reload" >&2
	fi
fi

echo "materialize-opcert-guard: DONE — origin guard materialised (nginx map + service env)" >&2
