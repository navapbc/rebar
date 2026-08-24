#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# materialize-mcp-upstream.sh — install the committed MCP upstream SEED into the
# HOST-nginx include dir on a fresh boot, then reload nginx. ADR
# deft-evolutive-mosasaur / story cibophobic-holohedral-esok. Mirrors the
# materialize-opcert-guard.sh SSM->file precedent, but the source is a COMMITTED
# local seed (no SSM): the MCP upstream target is not a secret.
#
# `upstream rebar_mcp {}` in infra/nginx/rebar.conf.template glob-includes
# /etc/nginx/mcp-upstream*.conf. An upstream with ZERO `server` lines is a config
# error (unlike the /opcert/ `map`'s zero-match-safe glob), so a valid backend file
# MUST exist before `nginx -t`. This script guarantees it:
#   * copies infra/nginx/mcp-upstream.conf -> /etc/nginx/mcp-upstream.conf
#     ONLY IF the target is absent — so a foxterrier blue-green flip (which rewrites
#     that same installed file to re-point the upstream) is NEVER clobbered on a
#     subsequent redeploy/boot;
#   * reloads host nginx so the include takes effect.
#
# Idempotent + non-fatal by design: run BEFORE `docker compose up` (wired into
# infra/scripts/compose-up.sh with the same non-fatal WARN pattern as the opcert
# guard). If the copy/reload fails the whole stack must still boot — /mcp/ simply has
# no working upstream until fixed, which surfaces as a 502, not a boot failure.
#
# Env:
#   NGINX_UPSTREAM_FILE  host nginx include target
#                        (default /etc/nginx/mcp-upstream.conf)
#   RELOAD_NGINX         set to 0 to skip `nginx -s reload` (default 1)
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEED_FILE="${SCRIPT_DIR}/../nginx/mcp-upstream.conf"
NGINX_UPSTREAM_FILE="${NGINX_UPSTREAM_FILE:-/etc/nginx/mcp-upstream.conf}"
RELOAD_NGINX="${RELOAD_NGINX:-1}"

if [ ! -f "$SEED_FILE" ]; then
	echo "materialize-mcp-upstream: FATAL — committed seed ${SEED_FILE} is missing" >&2
	exit 1
fi

# --- Install the seed ONLY IF absent (never clobber a foxterrier flip) ------
if [ -f "$NGINX_UPSTREAM_FILE" ]; then
	echo "materialize-mcp-upstream: ${NGINX_UPSTREAM_FILE} already present; leaving it (a blue-green flip may own it)" >&2
else
	mkdir -p "$(dirname "$NGINX_UPSTREAM_FILE")"
	cp "$SEED_FILE" "$NGINX_UPSTREAM_FILE"
	echo "materialize-mcp-upstream: installed committed seed -> ${NGINX_UPSTREAM_FILE}" >&2
fi

# --- Reload host nginx so the include takes effect --------------------------
if [ "$RELOAD_NGINX" != "0" ]; then
	if command -v nginx >/dev/null 2>&1; then
		echo "materialize-mcp-upstream: reloading host nginx" >&2
		nginx -s reload
	else
		echo "materialize-mcp-upstream: WARN — nginx not found on PATH; skipping reload" >&2
	fi
fi

echo "materialize-mcp-upstream: DONE — MCP upstream include ready" >&2
