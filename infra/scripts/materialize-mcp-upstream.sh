#!/usr/bin/env bash
# Seed host nginx's MCP upstream before compose starts. nginx requires the seed to
# contain a `server` line; this script checks only that the committed seed exists,
# copies it only when the target is absent, and optionally reloads nginx. It does not
# validate or replace an existing target. compose-up treats failure as non-fatal.
#
# Env:
#   NGINX_UPSTREAM_FILE  host nginx include target
#                        (default /etc/nginx/mcp-upstream.conf)
#   RELOAD_NGINX         set to 0 to skip `nginx -s reload` (default 1)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEED_FILE="${SCRIPT_DIR}/../nginx/mcp-upstream.conf"
NGINX_UPSTREAM_FILE="${NGINX_UPSTREAM_FILE:-/etc/nginx/mcp-upstream.conf}"
RELOAD_NGINX="${RELOAD_NGINX:-1}"

if [ ! -f "$SEED_FILE" ]; then
	echo "materialize-mcp-upstream: FATAL — committed seed ${SEED_FILE} is missing" >&2
	exit 1
fi

# Install only when absent so a blue-green flip remains authoritative.
if [ -f "$NGINX_UPSTREAM_FILE" ]; then
	echo "materialize-mcp-upstream: ${NGINX_UPSTREAM_FILE} already present; leaving it (a blue-green flip may own it)" >&2
else
	mkdir -p "$(dirname "$NGINX_UPSTREAM_FILE")"
	cp "$SEED_FILE" "$NGINX_UPSTREAM_FILE"
	echo "materialize-mcp-upstream: installed committed seed -> ${NGINX_UPSTREAM_FILE}" >&2
fi

# Optionally reload host nginx.
if [ "$RELOAD_NGINX" != "0" ]; then
	if command -v nginx >/dev/null 2>&1; then
		echo "materialize-mcp-upstream: reloading host nginx" >&2
		nginx -s reload
	else
		echo "materialize-mcp-upstream: WARN — nginx not found on PATH; skipping reload" >&2
	fi
fi

echo "materialize-mcp-upstream: DONE — MCP upstream include ready" >&2
