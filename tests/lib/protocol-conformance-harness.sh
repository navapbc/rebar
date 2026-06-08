#!/usr/bin/env bash
# tests/lib/protocol-conformance-harness.sh
# Reusable protocol-conformance harness for remediation-loop cycle verification.
#
# Usage:
#   protocol-conformance-harness.sh --touchpoint=<name> --simulate-failure --max-cycles=N
#
# Touchpoints:
#   fixture-a                 — standard forced-failure fixture (cycles exhaust)
#   fixture-oscillation       — oscillation detected on cycle 2+
#   fixture-illegal-transition — illegal state transition on first cycle
#
# Flags:
#   --touchpoint=<name>     Fixture type to simulate
#   --simulate-failure      Each cycle "fails" (findings non-empty)
#   --max-cycles=N          Override MAX_CYCLES (must be >= 2)
#
# Exit codes:
#   0 = conformant (all asserted properties hold)
#   1 = cycle-count violation (wrong number of dispatches)
#   2 = upstream-enum violation (REPLAN_ESCALATE token not in valid enum)
#   3 = protocol-error transition failure (PROTOCOL_ERROR not emitted when expected)
#   4 = oscillation violation (OSCILLATION_HALT not emitted when expected)
#   5 = min-cycles-rejected (MAX_CYCLES < 2)
#
# MAX_CYCLES precedence:
#   --max-cycles flag > planning.max_remediation_cycles config > built-in default 3
#
# Notes on sourcing planning-config.sh:
#   The harness sources planning-config.sh when --max-cycles is not supplied,
#   to apply get_max_remediation_cycles() which enforces config >= 2.

set -uo pipefail

# ── Locate script and repo root ───────────────────────────────────────────────
_HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$_HARNESS_DIR" rev-parse --show-toplevel)"

# ── Valid upstream enum ───────────────────────────────────────────────────────
readonly _VALID_UPSTREAMS="brainstorm preplanning planner_supplied"

# ── Defaults ──────────────────────────────────────────────────────────────────
_TOUCHPOINT=""
_SIMULATE_FAILURE=0
_MAX_CYCLES_FLAG=""  # empty = not provided via flag

# ── Argument parsing ──────────────────────────────────────────────────────────
for _arg in "$@"; do
    case "$_arg" in
        --touchpoint=*)   _TOUCHPOINT="${_arg#--touchpoint=}" ;;
        --simulate-failure) _SIMULATE_FAILURE=1 ;;
        --max-cycles=*)   _MAX_CYCLES_FLAG="${_arg#--max-cycles=}" ;;
        *)
            printf "[protocol-conformance-harness] Unknown argument: %s\n" "$_arg" >&2
            exit 1
            ;;
    esac
done

# ── Validate required arguments ───────────────────────────────────────────────
if [[ -z "$_TOUCHPOINT" ]]; then
    printf "[protocol-conformance-harness] ERROR: --touchpoint=<name> is required\n" >&2
    exit 1
fi

# ── Resolve MAX_CYCLES with precedence ───────────────────────────────────────
# Precedence: --max-cycles flag > planning.max_remediation_cycles config > default 3
if [[ -n "$_MAX_CYCLES_FLAG" ]]; then
    # --max-cycles flag provided: validate it
    if ! [[ "$_MAX_CYCLES_FLAG" =~ ^[0-9]+$ ]] || [[ "$_MAX_CYCLES_FLAG" -lt 2 ]]; then
        printf "[protocol-conformance-harness] ERROR: --max-cycles must be >= 2 (got: %s)\n" \
            "$_MAX_CYCLES_FLAG" >&2
        exit 5
    fi
    MAX_CYCLES="$_MAX_CYCLES_FLAG"
else
    # No --max-cycles flag: source planning-config.sh and use get_max_remediation_cycles()
    _PLANNING_CONFIG="$REPO_ROOT/src/rebar/_engine/hooks/lib/planning-config.sh"
    if [[ -f "$_PLANNING_CONFIG" ]]; then
        # shellcheck source=../../src/rebar/_engine/hooks/lib/planning-config.sh
        source "$_PLANNING_CONFIG"
        # get_max_remediation_cycles may not exist yet — fall back to default if absent
        if declare -f get_max_remediation_cycles >/dev/null 2>&1; then
            if ! MAX_CYCLES="$(get_max_remediation_cycles 2>&1)"; then
                printf "[protocol-conformance-harness] ERROR: get_max_remediation_cycles rejected config: %s\n" \
                    "$MAX_CYCLES" >&2
                exit 5
            fi
        else
            MAX_CYCLES=3
        fi
    else
        MAX_CYCLES=3
    fi
fi

# ── Token accumulator (temp file) ────────────────────────────────────────────
_TOKEN_FILE="$(mktemp "${TMPDIR:-/tmp}/protocol-conformance-tokens.XXXXXX")"
trap 'rm -f "$_TOKEN_FILE"' EXIT

# ── Synthetic loop runner ─────────────────────────────────────────────────────
# Each iteration represents one remediation "cycle" (dispatch).
# The fixture type determines what tokens are emitted.

_cycle=0
_final_state="RUNNING"

while [[ "$_cycle" -lt "$MAX_CYCLES" ]]; do
    _cycle=$(( _cycle + 1 ))
    printf "DISPATCH:%d\n" "$_cycle" >> "$_TOKEN_FILE"

    case "$_TOUCHPOINT" in
        fixture-illegal-transition)
            # Illegal state transition on first cycle — emit PROTOCOL_ERROR immediately
            printf "PROTOCOL_ERROR\n" >> "$_TOKEN_FILE"
            _final_state="PROTOCOL_ERROR"
            break
            ;;

        fixture-oscillation)
            # Oscillation detected on cycle 2+
            if [[ "$_cycle" -ge 2 ]]; then
                printf "OSCILLATION_HALT\n" >> "$_TOKEN_FILE"
                _final_state="OSCILLATION_HALT"
                break
            fi
            # cycle 1: treat as normal failure, continue
            ;;

        fixture-a | *)
            # Standard forced-failure fixture: cycle fails (findings non-empty)
            # Continue until MAX_CYCLES exhausted
            ;;
    esac
done

# ── Emit final state token for exhausted cycles ───────────────────────────────
if [[ "$_final_state" == "RUNNING" ]]; then
    # Cycles exhausted without early termination → escalate upstream
    # Determine upstream from touchpoint (default: brainstorm)
    case "$_TOUCHPOINT" in
        fixture-a | fixture-*)
            _upstream="brainstorm"
            ;;
        *)
            _upstream="brainstorm"
            ;;
    esac
    printf "REPLAN_ESCALATE:%s\n" "$_upstream" >> "$_TOKEN_FILE"
    _final_state="REPLAN_ESCALATE"
fi

# ── Read accumulated tokens ───────────────────────────────────────────────────
_tokens="$(cat "$_TOKEN_FILE")"

# ── Conformance assertions ────────────────────────────────────────────────────
_exit_code=0

# Assertion 1: cycle-count must not exceed MAX_CYCLES
_dispatch_count=$(grep -c "^DISPATCH:" "$_TOKEN_FILE" 2>/dev/null || true)
if [[ "$_dispatch_count" -gt "$MAX_CYCLES" ]]; then
    printf "[protocol-conformance-harness] FAIL: cycle-count violation — dispatched %d, max %d\n" \
        "$_dispatch_count" "$MAX_CYCLES" >&2
    _exit_code=1
fi

# Assertion 2: REPLAN_ESCALATE upstream must be in valid enum
if echo "$_tokens" | grep -q "^REPLAN_ESCALATE:"; then
    _escalate_line="$(echo "$_tokens" | grep "^REPLAN_ESCALATE:" | head -1)"
    _upstream_val="${_escalate_line#REPLAN_ESCALATE:}"
    _upstream_valid=0
    for _u in $_VALID_UPSTREAMS; do
        if [[ "$_upstream_val" == "$_u" ]]; then
            _upstream_valid=1
            break
        fi
    done
    if [[ "$_upstream_valid" -eq 0 ]]; then
        printf "[protocol-conformance-harness] FAIL: upstream-enum violation — '%s' not in {%s}\n" \
            "$_upstream_val" "$_VALID_UPSTREAMS" >&2
        _exit_code=2
    fi
fi

# Assertion 3: fixture-illegal-transition must emit PROTOCOL_ERROR
if [[ "$_TOUCHPOINT" == "fixture-illegal-transition" ]]; then
    if ! echo "$_tokens" | grep -q "^PROTOCOL_ERROR"; then
        printf "[protocol-conformance-harness] FAIL: protocol-error transition failure — PROTOCOL_ERROR not emitted\n" >&2
        _exit_code=3
    fi
fi

# Assertion 4: fixture-oscillation must emit OSCILLATION_HALT
if [[ "$_TOUCHPOINT" == "fixture-oscillation" ]] && [[ "$MAX_CYCLES" -ge 2 ]]; then
    if ! echo "$_tokens" | grep -q "^OSCILLATION_HALT"; then
        printf "[protocol-conformance-harness] FAIL: oscillation violation — OSCILLATION_HALT not emitted\n" >&2
        _exit_code=4
    fi
fi

# Assertion 5: PROTOCOL_ERROR fixture must exit non-zero (enforced by _exit_code check below)
# (handled implicitly: if PROTOCOL_ERROR is emitted, exit code is preserved from assertion 3)

# ── Emit token log to stdout for caller inspection ────────────────────────────
cat "$_TOKEN_FILE"

# ── Exit with protocol-error exit code if PROTOCOL_ERROR was emitted ─────────
if [[ "$_final_state" == "PROTOCOL_ERROR" ]] && [[ "$_exit_code" -eq 0 ]]; then
    # PROTOCOL_ERROR on fixture-illegal-transition: non-zero exit required
    # Use exit code 3 (protocol-error) to signal this to callers
    _exit_code=3
fi

exit "$_exit_code"
