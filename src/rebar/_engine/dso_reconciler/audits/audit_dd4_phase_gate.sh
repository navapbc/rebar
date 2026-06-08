#!/usr/bin/env bash
# audit_dd4_phase_gate.sh — Sourceable phase-gate library for the reconciler.
#
# Exports:
#   check_phase_gate <phase>
#
# Gate-file schemas
# =================
# .reconciler-phase-gate  (plain text, single line)
#   Contains the canonical phase name, one of:
#     dry-run | bootstrap-strict | bootstrap-throttle | live
#   Example:  bootstrap-strict
#
# .reconciler-phase-gate.ops  (JSON Lines — one JSON object per line)
#   Each line:  {"operator":"<name>","timestamp":"<ISO-8601>","phase":"<name>","comment":"<text>"}
#   The ops file records operator-approved phase transitions.
#   check_phase_gate treats any entry whose "phase" field matches as a gate pass.
#
# Resolution rules
# ================
#   1. Locate gate files via RECONCILER_PHASE_GATE_DIR if set, else
#      $(git rev-parse --show-toplevel)/.reconciler-state/
#   2. If .reconciler-phase-gate exists and its content matches <phase>  → exit 0
#   3. If .reconciler-phase-gate.ops exists and any entry has "phase":<phase> → exit 0
#   4. If .reconciler-phase-gate.ops is absent → emit WARNING to stderr, continue
#      (degradation is recoverable; ops-log absence is not a hard failure)
#   5. If neither gate matches → stderr diagnostic, exit 2
#
# CLI usage:
#   audit_dd4_phase_gate.sh <phase>

set -uo pipefail

# ── Gate-file directory resolution ───────────────────────────────────────────
_resolve_gate_dir() {
    if [[ -n "${RECONCILER_PHASE_GATE_DIR:-}" ]]; then
        printf '%s' "$RECONCILER_PHASE_GATE_DIR"
        return
    fi
    local top
    top="$(git rev-parse --show-toplevel 2>/dev/null)" || {
        printf '%s' "$(pwd)/.reconciler-state"
        return
    }
    printf '%s' "$top/.reconciler-state"
}

# ── check_phase_gate <phase> ──────────────────────────────────────────────────
# Returns 0 if the requested phase is authorised, 2 otherwise.
check_phase_gate() {
    local requested_phase="${1:-}"
    if [[ -z "$requested_phase" ]]; then
        printf 'check_phase_gate: phase argument required\n' >&2
        return 2
    fi

    local gate_dir
    gate_dir="$(_resolve_gate_dir)"

    local gate_file="${gate_dir}/.reconciler-phase-gate"
    local ops_file="${gate_dir}/.reconciler-phase-gate.ops"

    # Override via legacy env vars (backward compat)
    gate_file="${PHASE_GATE_FILE:-$gate_file}"
    ops_file="${PHASE_GATE_OPS_FILE:-$ops_file}"

    # ── Emit warning early if ops-log is absent (non-fatal degradation) ─────────
    if [[ ! -f "$ops_file" ]]; then
        printf 'WARNING: ops-log unavailable (ops_log_available=false): %s\n' "$ops_file" >&2
    fi

    # ── Check 1: plain gate file ──────────────────────────────────────────────
    if [[ -f "$gate_file" ]]; then
        local gate_content
        gate_content="$(tr -d '[:space:]' < "$gate_file")"
        if [[ "$gate_content" == "$requested_phase" ]]; then
            return 0
        fi
    fi

    # ── Check 2: ops-log JSON Lines ───────────────────────────────────────────
    if [[ -f "$ops_file" ]]; then
        local line
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue
            # Minimal JSON field extraction — no jq dependency required.
            # Extract the value of the "phase" key: ..."phase":"<value>"...
            local phase_val
            phase_val="$(printf '%s' "$line" | \
                grep -oE '"phase"\s*:\s*"[^"]+"' | \
                grep -oE '"[^"]+"$' | \
                tr -d '"')" || true
            if [[ "$phase_val" == "$requested_phase" ]]; then
                return 0
            fi
        done < "$ops_file"
    fi

    # ── No match ──────────────────────────────────────────────────────────────
    printf 'phase not advanced: phase=%s gate_missing\n' "$requested_phase" >&2
    return 2
}

# ── CLI entry point ───────────────────────────────────────────────────────────
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    if [[ $# -lt 1 ]]; then
        printf 'usage: %s <phase>\n' "$(basename "$0")" >&2
        exit 2
    fi
    check_phase_gate "$1"
    exit $?
fi
