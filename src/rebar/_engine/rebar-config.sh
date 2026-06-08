#!/usr/bin/env bash
# rebar-config.sh
# Sourceable helper that provides rebar-native root/config resolution and
# aliases the engine's internal env-var names to a clean REBAR_* surface.
#
# SOURCEABILITY CONTRACT: no file-scope `set`, `exit`, or `trap`; functions and
# vars use the `_rebar_` / `REBAR_` namespace; safe to source more than once.

# ── Env-var aliasing (read both, prefer REBAR_*) ──────────────────────────────
# The engine internals read DSO_* / PROJECT_ROOT; expose REBAR_* as the public
# surface and keep them in sync so the bash write path and the Python reconciler
# never disagree on repo-root.
: "${REBAR_ROOT:=${PROJECT_ROOT:-}}"
: "${PROJECT_ROOT:=${REBAR_ROOT:-}}"
: "${DSO_TICKET_LEGACY:=${REBAR_TICKET_LEGACY:-}}"
: "${DSO_AUTHOR:=${REBAR_AUTHOR:-}}"
: "${DSO_TICKET_CLI:=${REBAR_TICKET_CLI:-}}"
: "${DSO_CLI:=${REBAR_CLI:-}}"
: "${_DSO_GC_AUTO_ZERO:=${REBAR_GC_AUTO_ZERO:-}}"
: "${DSO_UNBLOCK_SCRIPT:=${REBAR_UNBLOCK_SCRIPT:-}}"
: "${DSO_COMPACT_SCRIPT:=${REBAR_COMPACT_SCRIPT:-}}"

# Strip empty aliases so `:-` fallbacks downstream still fire.
[ -z "${REBAR_ROOT:-}" ] && unset REBAR_ROOT
[ -z "${PROJECT_ROOT:-}" ] && unset PROJECT_ROOT
[ -z "${DSO_TICKET_LEGACY:-}" ] && unset DSO_TICKET_LEGACY
[ -z "${DSO_AUTHOR:-}" ] && unset DSO_AUTHOR
[ -z "${DSO_TICKET_CLI:-}" ] && unset DSO_TICKET_CLI
[ -z "${DSO_CLI:-}" ] && unset DSO_CLI
[ -z "${_DSO_GC_AUTO_ZERO:-}" ] && unset _DSO_GC_AUTO_ZERO
[ -z "${DSO_UNBLOCK_SCRIPT:-}" ] && unset DSO_UNBLOCK_SCRIPT
[ -z "${DSO_COMPACT_SCRIPT:-}" ] && unset DSO_COMPACT_SCRIPT

# ── Engine dir ────────────────────────────────────────────────────────────────
# _rebar_engine_dir: directory containing this helper (the flat engine dir).
_rebar_engine_dir() {
    cd "$(dirname "${BASH_SOURCE[0]}")" && pwd
}

# ── Repo root ─────────────────────────────────────────────────────────────────
# _rebar_root: REBAR_ROOT/PROJECT_ROOT if set, else the git toplevel.
_rebar_root() {
    if [ -n "${REBAR_ROOT:-}" ]; then
        printf '%s\n' "$REBAR_ROOT"
    elif [ -n "${PROJECT_ROOT:-}" ]; then
        printf '%s\n' "$PROJECT_ROOT"
    else
        GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null
    fi
}

# ── Config file ───────────────────────────────────────────────────────────────
# _rebar_config_file: first existing of $REBAR_CONFIG, $root/.rebar/config.conf,
# $root/.rebar.conf. Prints nothing if none exist.
_rebar_config_file() {
    local _root
    _root="$(_rebar_root)"
    if [ -n "${REBAR_CONFIG:-}" ] && [ -f "${REBAR_CONFIG}" ]; then
        printf '%s\n' "$REBAR_CONFIG"
    elif [ -n "$_root" ] && [ -f "$_root/.rebar/config.conf" ]; then
        printf '%s\n' "$_root/.rebar/config.conf"
    elif [ -n "$_root" ] && [ -f "$_root/.rebar.conf" ]; then
        printf '%s\n' "$_root/.rebar.conf"
    fi
}

# ── Ticket CLI ────────────────────────────────────────────────────────────────
# _rebar_ticket_cli: path to the rebar dispatcher (for scripts that shell out to
# the ticket CLI). Honors REBAR_TICKET_CLI / DSO_TICKET_CLI overrides.
_rebar_ticket_cli() {
    if [ -n "${REBAR_TICKET_CLI:-}" ]; then
        printf '%s\n' "$REBAR_TICKET_CLI"
    elif [ -n "${DSO_TICKET_CLI:-}" ]; then
        printf '%s\n' "$DSO_TICKET_CLI"
    else
        printf '%s\n' "$(_rebar_engine_dir)/rebar"
    fi
}
