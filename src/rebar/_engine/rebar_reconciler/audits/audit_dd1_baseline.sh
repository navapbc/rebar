#!/usr/bin/env bash
# audit_dd1_baseline.sh — Pre-Phase-1 baseline capture of bridge-fsck label-only orphan count.
#
# Sources audit_dd4_phase_gate.sh (sibling file) and calls check_phase_gate
# before running the fsck command. Fails closed if the phase gate is not
# satisfied or if the fsck command errors or its output cannot be parsed.
#
# Writes structured JSON to:
#   ${AUDIT_ARTIFACTS_DIR:-<repo-root>/.reconciler-audit-artifacts}/<phase>/dd1.json
#
# Fields: phase, captured_at, label_only_orphan_count, bridge_fsck_command, git_sha
#
# Exit codes:
#   0 — success; dd1.json written
#   2 — phase gate not satisfied (propagated from check_phase_gate)
#   3 — fsck command exited non-zero, or label_only_orphans could not be parsed
#
# CLI: audit_dd1_baseline.sh <phase>
#
# Environment:
#   BRIDGE_FSCK_CMD          — override fsck command (default: bridge-fsck --count-only)
#   AUDIT_ARTIFACTS_DIR      — override artifact root (default: <repo-root>/.reconciler-audit-artifacts)
#   RECONCILER_PHASE_GATE_DIR — passed through to check_phase_gate

set -uo pipefail

# ── Source sibling libraries (phase gate + shared helpers) ───────────────────
_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=audit_dd4_phase_gate.sh
source "${_SELF_DIR}/audit_dd4_phase_gate.sh"
# shellcheck source=_audit_lib.sh
source "${_SELF_DIR}/_audit_lib.sh"

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

    # ── Run fsck ──────────────────────────────────────────────────────────────
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
    local orphan_count="${count_line#label_only_orphans=}"
    # Validate it is a pure integer
    if ! [[ "$orphan_count" =~ ^[0-9]+$ ]]; then
        printf 'ERROR: parsed label_only_orphans value is not an integer: %s\n' "$orphan_count" >&2
        exit 3
    fi

    # ── Resolve git SHA ───────────────────────────────────────────────────────
    local git_sha
    git_sha="$(git rev-parse HEAD 2>/dev/null)" || git_sha="unknown"

    # ── Captured-at timestamp (ISO 8601 UTC) ──────────────────────────────────
    local captured_at
    captured_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)" || captured_at="unknown"

    # ── Write artifact ────────────────────────────────────────────────────────
    local artifact_root
    artifact_root="$(_resolve_artifact_root)"
    local artifact_dir="${artifact_root}/${phase}"
    mkdir -p "$artifact_dir"
    local artifact_path="${artifact_dir}/dd1.json"

    # Emit JSON without relying on jq
    cat > "$artifact_path" <<EOF
{
  "phase": "${phase}",
  "captured_at": "${captured_at}",
  "label_only_orphan_count": ${orphan_count},
  "bridge_fsck_command": "${fsck_cmd}",
  "git_sha": "${git_sha}"
}
EOF

    printf 'dd1 baseline captured: label_only_orphan_count=%s artifact=%s\n' \
        "$orphan_count" "$artifact_path"
}

main "$@"
