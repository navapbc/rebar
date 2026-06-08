#!/usr/bin/env bash
# audit_dd2_orphan_count.sh — Post-bootstrap-throttle orphan count diff (dd-2).
#
# Sources audit_dd4_phase_gate.sh (sibling file) and calls check_phase_gate
# before proceeding. Reads the pre-Phase-1 baseline from dd1.json, runs
# bridge-fsck again for the post-bootstrap-throttle count, and writes dd2.json
# with the before/after diff and SC-8 pass/fail verdict.
#
# dd1.json data contract (consumed fields):
#   label_only_orphan_count: integer  — baseline orphan count (before value)
#
# Writes structured JSON to:
#   ${AUDIT_ARTIFACTS_DIR:-<repo-root>/.reconciler-audit-artifacts}/<phase>/dd2.json
#
# Fields: phase, before_count, after_count, delta, sc8_pass
#
# Exit codes:
#   0 — success; dd2.json written; sc8_pass=true (after_count==0)
#   2 — phase gate not satisfied (propagated from check_phase_gate)
#   3 — dd1.json missing or unparseable (fail-closed)
#   4 — sc8_pass=false (after_count > 0); dd2.json is still written
#
# CLI: audit_dd2_orphan_count.sh <phase>
#
# Environment:
#   BRIDGE_FSCK_CMD           — override fsck command (default: bridge-fsck --count-only)
#   AUDIT_ARTIFACTS_DIR       — override artifact root (default: <repo-root>/.reconciler-audit-artifacts)
#   RECONCILER_PHASE_GATE_DIR — passed through to check_phase_gate

set -uo pipefail

# ── Source sibling libraries (phase gate + shared helpers) ───────────────────
_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=audit_dd4_phase_gate.sh
source "${_SELF_DIR}/audit_dd4_phase_gate.sh"
# shellcheck source=_audit_lib.sh
source "${_SELF_DIR}/_audit_lib.sh"

# ── _parse_before_count <dd1_path> — prints integer to stdout; returns non-zero on failure ──
# Note: called in main() directly (not via $(...)) so exit 3 propagates to the process.
_parse_before_count() {
    local dd1_path="$1"

    # Prefer python3, then python, then jq for JSON parsing
    local _parsed
    if command -v python3 >/dev/null 2>&1; then
        _parsed="$(python3 -c "
import json, sys
try:
    d = json.load(open('${dd1_path}'))
    v = d.get('label_only_orphan_count')
    if v is None:
        raise KeyError('label_only_orphan_count missing')
    print(int(v))
except Exception as e:
    print('PARSE_ERROR: ' + str(e), file=sys.stderr)
    sys.exit(1)
" 2>&1)" || return 1
    elif command -v python >/dev/null 2>&1; then
        _parsed="$(python -c "
import json, sys
try:
    d = json.load(open('${dd1_path}'))
    v = d.get('label_only_orphan_count')
    if v is None:
        raise KeyError('label_only_orphan_count missing')
    print(int(v))
except Exception as e:
    print('PARSE_ERROR: ' + str(e), file=sys.stderr)
    sys.exit(1)
" 2>&1)" || return 1
    elif command -v jq >/dev/null 2>&1; then
        _parsed="$(jq -e '.label_only_orphan_count' "$dd1_path" 2>/dev/null)" || return 1
    else
        printf 'ERROR: no JSON parser available (python3, python, or jq required)\n' >&2
        return 1
    fi

    # Validate it is a pure integer
    if ! [[ "$_parsed" =~ ^[0-9]+$ ]]; then
        printf 'ERROR: label_only_orphan_count is not a non-negative integer: %s\n' "$_parsed" >&2
        return 1
    fi
    printf '%s' "$_parsed"
}

# ── main ──────────────────────────────────────────────────────────────────────
main() {
    if [[ $# -lt 1 ]]; then
        printf 'usage: %s <phase>\n' "$(basename "$0")" >&2
        exit 2
    fi

    local phase="$1"

    # ── Phase gate ────────────────────────────────────────────────────────────
    check_phase_gate "$phase"
    local gate_rc=$?
    if [[ "$gate_rc" -ne 0 ]]; then
        exit "$gate_rc"
    fi

    # ── Read baseline before_count from dd1.json ──────────────────────────────
    local artifact_root
    artifact_root="$(_resolve_artifact_root)"
    local artifact_dir="${artifact_root}/${phase}"
    local dd1_path="${artifact_dir}/dd1.json"

    if [[ ! -f "$dd1_path" ]]; then
        printf 'ERROR: dd1.json baseline not found: %s\n' "$dd1_path" >&2
        exit 3
    fi

    local before_count
    if ! before_count="$(_parse_before_count "$dd1_path")"; then
        printf 'ERROR: could not parse label_only_orphan_count from dd1.json\n' >&2
        exit 3
    fi

    # ── Run fsck for after_count ──────────────────────────────────────────────
    local fsck_cmd="${BRIDGE_FSCK_CMD:-bridge-fsck --count-only}"
    local fsck_output
    read -ra fsck_args <<< "$fsck_cmd"
    if ! fsck_output="$("${fsck_args[@]}" 2>&1)"; then
        printf 'ERROR: fsck command exited non-zero: %s\n' "$fsck_cmd" >&2
        printf '%s\n' "$fsck_output" >&2
        exit 3
    fi

    # ── Parse label_only_orphans=N ────────────────────────────────────────────
    local count_line
    count_line="$(printf '%s\n' "$fsck_output" | grep -E '^label_only_orphans=[0-9]+')" || true
    if [[ -z "$count_line" ]]; then
        printf 'ERROR: could not parse label_only_orphans from fsck output\n' >&2
        printf 'output was:\n%s\n' "$fsck_output" >&2
        exit 3
    fi
    local after_count="${count_line#label_only_orphans=}"
    if ! [[ "$after_count" =~ ^[0-9]+$ ]]; then
        printf 'ERROR: parsed label_only_orphans value is not an integer: %s\n' "$after_count" >&2
        exit 3
    fi

    # ── Compute delta and SC-8 verdict ────────────────────────────────────────
    local delta=$(( before_count - after_count ))
    local sc8_pass="false"
    if [[ "$after_count" -eq 0 ]]; then
        sc8_pass="true"
    fi

    # ── Write dd2.json ────────────────────────────────────────────────────────
    mkdir -p "$artifact_dir"
    local artifact_path="${artifact_dir}/dd2.json"

    cat > "$artifact_path" <<EOF
{
  "phase": "${phase}",
  "before_count": ${before_count},
  "after_count": ${after_count},
  "delta": ${delta},
  "sc8_pass": ${sc8_pass}
}
EOF

    printf 'dd2 orphan count diff: before=%s after=%s delta=%s sc8_pass=%s artifact=%s\n' \
        "$before_count" "$after_count" "$delta" "$sc8_pass" "$artifact_path"

    # ── Exit 4 if sc8_pass=false ──────────────────────────────────────────────
    if [[ "$sc8_pass" == "false" ]]; then
        printf 'SC-8 FAIL: after_count=%s (expected 0)\n' "$after_count" >&2
        exit 4
    fi
}

main "$@"
