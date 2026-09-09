#!/usr/bin/env bash
# Materialize the SSM origin guard into host nginx and the compose environment.
# nginx defaults to deny-all until its direct map entry is written; the application
# independently checks the same X-Opcert-Guard value. Rotation may briefly return 403
# between the upstream update and this fail-closed rewrite. The guard is never logged.
#
# Env:
#   AWS_REGION            (default us-east-1)
#   OPCERT_GUARD_SSM_PARAM SSM SecureString holding the origin guard
#                         (default /rebar/prod/opcert-origin-guard — provisioned by opcert.tf)
#   NGINX_MAP_FILE        host nginx map file (default /etc/nginx/opcert-guard.map.conf)
#   ENV_FILE              compose .env to land REBAR_OPCERT_GUARD into
#                         (default: sibling ../compose/.env)
#   RELOAD_NGINX          set to 0 to skip `nginx -s reload` (default 1)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AWS_REGION="${AWS_REGION:-us-east-1}"
OPCERT_GUARD_SSM_PARAM="${OPCERT_GUARD_SSM_PARAM:-/rebar/prod/opcert-origin-guard}"
NGINX_MAP_FILE="${NGINX_MAP_FILE:-/etc/nginx/opcert-guard.map.conf}"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/../compose/.env}"
RELOAD_NGINX="${RELOAD_NGINX:-1}"

# 1. Fetch the nonempty SSM guard.
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

# 2. Write the exact nginx map entry under umask 077 without exposing the value.
mkdir -p "$(dirname "$NGINX_MAP_FILE")"
( umask 077; printf '"%s" 1;\n' "$guard" > "$NGINX_MAP_FILE" )
chmod 0600 "$NGINX_MAP_FILE"
echo "materialize-opcert-guard: wrote ${NGINX_MAP_FILE} (0600)" >&2

# 3. Atomically replace only REBAR_OPCERT_GUARD, preserving unrelated env entries.
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

# 4. Optionally reload host nginx.
if [ "$RELOAD_NGINX" != "0" ]; then
	if command -v nginx >/dev/null 2>&1; then
		echo "materialize-opcert-guard: reloading host nginx" >&2
		nginx -s reload
	else
		echo "materialize-opcert-guard: WARN — nginx not found on PATH; skipping reload" >&2
	fi
fi

echo "materialize-opcert-guard: DONE — origin guard materialised (nginx map + service env)" >&2
