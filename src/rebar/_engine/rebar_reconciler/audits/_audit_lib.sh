#!/usr/bin/env bash
# _audit_lib.sh — Shared helpers for the per-DD audit scripts.
#
# This is a sourceable library. It is NOT executable on its own.
#
# Exports:
#   _resolve_artifact_root   — prints the audit artifact root directory.
#                              Honours $AUDIT_ARTIFACTS_DIR; falls back to
#                              <repo-root>/.reconciler-audit-artifacts.
#
# Loading:
#   _SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   # shellcheck source=_audit_lib.sh
#   source "${_SELF_DIR}/_audit_lib.sh"
#
# Intentionally contains zero references to the plugin's installed path —
# any callsite resolves the script's own directory via BASH_SOURCE.

# Guard against multiple inclusion. (Sourced files use `return`; the `true`
# branch is defensive for direct execution and is intentionally unreachable
# when this file is sourced — silence shellcheck SC2317 accordingly.)
if [[ -n "${_AUDIT_LIB_LOADED:-}" ]]; then
    # shellcheck disable=SC2317
    return 0 2>/dev/null || true
fi
_AUDIT_LIB_LOADED=1

# ── Resolve artifact root ─────────────────────────────────────────────────────
# Prints the audit artifact root to stdout. Honours $AUDIT_ARTIFACTS_DIR;
# falls back to <repo-root>/.reconciler-audit-artifacts.
_resolve_artifact_root() {
    if [[ -n "${AUDIT_ARTIFACTS_DIR:-}" ]]; then
        printf '%s' "$AUDIT_ARTIFACTS_DIR"
        return
    fi
    local top
    top="$(git rev-parse --show-toplevel 2>/dev/null)" || top="$(pwd)"
    printf '%s' "${top}/.reconciler-audit-artifacts"
}
