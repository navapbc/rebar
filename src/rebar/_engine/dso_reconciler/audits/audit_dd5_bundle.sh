#!/usr/bin/env bash
# audit_dd5_bundle.sh — Artifact bundler: verifies all per-DD prerequisites,
# computes SHA256 manifest, writes dd5.json, and posts one epic-ticket comment.
#
# Sources audit_dd4_phase_gate.sh (sibling file) and calls check_phase_gate
# before proceeding. Verifies that the four prerequisite artifacts
# (dd1.json, dd2.json, dd3.json, quarantine.json) exist and contain non-empty
# JSON. On success writes dd5.json with per-artifact SHA256 hashes plus per-DD
# pass/fail fields and overall_result, then posts one comment to the epic ticket.
#
# Writes structured JSON to:
#   ${AUDIT_ARTIFACTS_DIR:-<repo-root>/.reconciler-audit-artifacts}/<phase>/dd5.json
#
# Fields: phase, artifacts, bundled_at, epic_id,
#         dd1_pass, dd2_pass, dd3_pass, dd4_pass, overall_result
#
# Exit codes:
#   0 — success; dd5.json written, epic comment posted, overall_result=true
#   2 — phase gate not satisfied (propagated from check_phase_gate)
#   6 — either (a) one or more prerequisite artifacts missing / empty / lacking
#       a required field (no dd5.json written) OR (b) all artifacts present but
#       overall_result=false; dd5.json IS written + comment posted, then exit 6
#       so callers see the failure. Silent certification is never allowed.
#
# CLI: audit_dd5_bundle.sh <phase> --epic <epic-id>
#
# Environment:
#   AUDIT_ARTIFACTS_DIR      — override artifact root (default: <repo-root>/.reconciler-audit-artifacts)
#   QUARANTINE_LIST          — override quarantine artifact path (default: <artifact-dir>/quarantine.json)
#   TICKET_CLI               — override ticket CLI (default: .claude/scripts/dso)
#   RECONCILER_PHASE_GATE_DIR — passed through to check_phase_gate

set -uo pipefail

# ── Source sibling libraries (phase gate + shared helpers) ───────────────────
_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=audit_dd4_phase_gate.sh
source "${_SELF_DIR}/audit_dd4_phase_gate.sh"
# shellcheck source=_audit_lib.sh
source "${_SELF_DIR}/_audit_lib.sh"

# ── Compute SHA256 of a file — stdout: hex digest ────────────────────────────
_sha256_file() {
    local path="$1"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$path" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$path" | awk '{print $1}'
    else
        printf 'ERROR: no sha256 tool available (sha256sum or shasum required)\n' >&2
        return 1
    fi
}

# ── _is_nonempty_json <path> — returns 0 if file exists + contains non-whitespace ──
_is_nonempty_json() {
    local path="$1"
    [[ -f "$path" ]] || return 1
    local content
    content="$(tr -d '[:space:]' < "$path")"
    [[ -n "$content" ]]
}

# ── _extract_bool_field <json-path> <field> — prints true/false ─────────────
# Returns 0 on success, 1 if field is missing or unparseable.
_extract_bool_field() {
    local json_path="$1"
    local field="$2"
    local val
    if command -v python3 >/dev/null 2>&1; then
        val="$(python3 -c "
import json, sys
try:
    d = json.load(open('${json_path}'))
    v = d.get('${field}')
    if v is None:
        sys.exit(1)
    print('true' if bool(v) else 'false')
except Exception:
    sys.exit(1)
" 2>/dev/null)" || return 1
    elif command -v jq >/dev/null 2>&1; then
        val="$(jq -re ".${field}" "$json_path" 2>/dev/null)" || return 1
    else
        # Minimal grep-based extraction for boolean fields
        val="$(grep -oE "\"${field}\"[[:space:]]*:[[:space:]]*(true|false)" "$json_path" \
            | grep -oE '(true|false)$')" || return 1
        [[ -n "$val" ]] || return 1
    fi
    printf '%s' "$val"
}

# ── _ticket_cli_path — resolves path to ticket CLI ───────────────────────────
_ticket_cli_path() {
    if [[ -n "${TICKET_CLI:-}" ]]; then
        printf '%s' "$TICKET_CLI"
        return
    fi
    local top
    top="$(git rev-parse --show-toplevel 2>/dev/null)" || top="$(pwd)"
    printf '%s' "${REBAR_TICKET_CLI:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)/rebar}"
}

# ── _post_comment_with_retry <epic_id> <comment> — up to 3 attempts ─────────
_post_comment_with_retry() {
    local epic_id="$1"
    local comment="$2"
    local ticket_cli
    ticket_cli="$(_ticket_cli_path)"

    local attempt delay
    for attempt in 1 2 3; do
        if "$ticket_cli" ticket comment "$epic_id" "$comment" 2>&1; then
            return 0
        fi
        if [[ "$attempt" -lt 3 ]]; then
            delay=$(( 250 * attempt ))
            sleep "$(echo "scale=3; $delay / 1000" | bc 2>/dev/null || printf '0.25')"
        fi
    done

    printf 'ERROR: tickets branch lock conflict — retry exhausted (epic=%s)\n' "$epic_id" >&2
    return 6
}

# ── main ──────────────────────────────────────────────────────────────────────
main() {
    # ── Argument parsing ──────────────────────────────────────────────────────
    if [[ $# -lt 3 ]]; then
        printf 'usage: %s <phase> --epic <epic-id>\n' "$(basename "$0")" >&2
        exit 2
    fi

    local phase="$1"
    shift

    local epic_id=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --epic)
                epic_id="${2:-}"
                shift 2
                ;;
            *)
                printf 'unknown argument: %s\n' "$1" >&2
                exit 2
                ;;
        esac
    done

    if [[ -z "$epic_id" ]]; then
        printf 'usage: %s <phase> --epic <epic-id>\n' "$(basename "$0")" >&2
        exit 2
    fi

    # ── Phase gate ────────────────────────────────────────────────────────────
    check_phase_gate "$phase"
    local gate_rc=$?
    if [[ "$gate_rc" -ne 0 ]]; then
        exit "$gate_rc"
    fi

    # ── Resolve artifact directory ────────────────────────────────────────────
    local artifact_root
    artifact_root="$(_resolve_artifact_root)"
    local artifact_dir="${artifact_root}/${phase}"

    # ── Resolve prerequisite paths ────────────────────────────────────────────
    local dd1_path="${artifact_dir}/dd1.json"
    local dd2_path="${artifact_dir}/dd2.json"
    local dd3_path="${artifact_dir}/dd3.json"
    local quarantine_path="${QUARANTINE_LIST:-${artifact_dir}/quarantine.json}"

    # ── Verify all prerequisites exist and are non-empty JSON ─────────────────
    local prereq_ok=1
    local prereq_name
    for prereq_name in "dd1:${dd1_path}" "dd2:${dd2_path}" "dd3:${dd3_path}" "quarantine:${quarantine_path}"; do
        local label="${prereq_name%%:*}"
        local prereq_path="${prereq_name#*:}"
        if ! _is_nonempty_json "$prereq_path"; then
            printf 'ERROR: prerequisite artifact missing or empty: %s (%s)\n' \
                "$label" "$prereq_path" >&2
            prereq_ok=0
        fi
    done

    if [[ "$prereq_ok" -eq 0 ]]; then
        exit 6
    fi

    # ── Compute SHA256 + byte size for each artifact ──────────────────────────
    local -a artifact_names=("dd1.json" "dd2.json" "dd3.json" "quarantine.json")
    local -a artifact_paths=("$dd1_path" "$dd2_path" "$dd3_path" "$quarantine_path")

    local -a sha256s=()
    local -a byte_sizes=()
    local i
    for i in 0 1 2 3; do
        local apath="${artifact_paths[$i]}"
        local digest
        if ! digest="$(_sha256_file "$apath")"; then
            printf 'ERROR: could not compute SHA256 for %s\n' "$apath" >&2
            exit 6
        fi
        sha256s+=("$digest")

        local bsize
        bsize="$(wc -c < "$apath" | tr -d ' ')"
        byte_sizes+=("$bsize")
    done

    # ── Extract per-DD pass/fail fields ───────────────────────────────────────
    # dd1.json is data-capture only — no boolean pass field is written by the
    # producer. dd1_pass=true iff the prerequisite check above confirmed the
    # file exists and is non-empty JSON containing label_only_orphan_count.
    # We re-check the required field here so a corrupted dd1.json fails CLOSED.
    local dd1_pass="true"
    if ! grep -qE '"label_only_orphan_count"[[:space:]]*:[[:space:]]*[0-9]+' "$dd1_path"; then
        printf 'ERROR: dd1.json missing required field label_only_orphan_count: %s\n' "$dd1_path" >&2
        exit 6
    fi

    # dd2.json uses sc8_pass (the SC-8 verdict). Missing field → fail CLOSED.
    local dd2_pass
    if ! dd2_pass="$(_extract_bool_field "$dd2_path" "sc8_pass" 2>/dev/null)"; then
        printf 'ERROR: dd2.json missing required boolean field sc8_pass: %s\n' "$dd2_path" >&2
        exit 6
    fi

    # dd3.json uses overall_pass (per the cap-verifier contract). Missing field
    # → fail CLOSED. Previously this fell through to literal 'true' for any
    # unrecognised pass-field, silently certifying real failures.
    local dd3_pass
    if ! dd3_pass="$(_extract_bool_field "$dd3_path" "overall_pass" 2>/dev/null)"; then
        printf 'ERROR: dd3.json missing required boolean field overall_pass: %s\n' "$dd3_path" >&2
        exit 6
    fi

    # dd4 is the phase gate — if we got here it passed
    local dd4_pass="true"

    # overall_result = AND of all four
    local overall_result="true"
    if [[ "$dd1_pass" != "true" || "$dd2_pass" != "true" || \
          "$dd3_pass" != "true" || "$dd4_pass" != "true" ]]; then
        overall_result="false"
    fi

    # ── ISO 8601 UTC timestamp ────────────────────────────────────────────────
    local bundled_at
    bundled_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)" || bundled_at="unknown"

    # ── Write dd5.json ────────────────────────────────────────────────────────
    mkdir -p "$artifact_dir"
    local dd5_path="${artifact_dir}/dd5.json"

    # Build artifacts JSON array inline (no jq required)
    local artifacts_json
    artifacts_json="$(printf '    {"name": "%s", "path": "%s", "sha256": "%s", "bytes": %s}' \
        "${artifact_names[0]}" "${artifact_paths[0]}" "${sha256s[0]}" "${byte_sizes[0]}")"
    for i in 1 2 3; do
        artifacts_json="${artifacts_json},
$(printf '    {"name": "%s", "path": "%s", "sha256": "%s", "bytes": %s}' \
        "${artifact_names[$i]}" "${artifact_paths[$i]}" "${sha256s[$i]}" "${byte_sizes[$i]}")"
    done

    cat > "$dd5_path" <<EOF
{
  "phase": "${phase}",
  "epic_id": "${epic_id}",
  "bundled_at": "${bundled_at}",
  "dd1_pass": ${dd1_pass},
  "dd2_pass": ${dd2_pass},
  "dd3_pass": ${dd3_pass},
  "dd4_pass": ${dd4_pass},
  "overall_result": ${overall_result},
  "artifacts": [
${artifacts_json}
  ]
}
EOF

    printf 'dd5 bundle written: phase=%s epic=%s artifact=%s overall_result=%s\n' \
        "$phase" "$epic_id" "$dd5_path" "$overall_result"

    # ── Post epic comment with artifact dir path ───────────────────────────────
    if ! _post_comment_with_retry "$epic_id" "$artifact_dir"; then
        printf 'ERROR: failed to post epic comment after retries (epic=%s)\n' "$epic_id" >&2
        exit 6
    fi

    printf 'epic comment posted: epic=%s artifact_dir=%s\n' "$epic_id" "$artifact_dir"

    # ── Exit non-zero when any per-DD verdict failed ─────────────────────────
    # The artifact is written and the comment is posted (operators need the
    # evidence record) but a real audit failure MUST surface as a non-zero
    # exit so callers (Make pipeline, CI) short-circuit.
    if [[ "$overall_result" != "true" ]]; then
        printf 'AUDIT FAIL: overall_result=false (dd1=%s dd2=%s dd3=%s dd4=%s)\n' \
            "$dd1_pass" "$dd2_pass" "$dd3_pass" "$dd4_pass" >&2
        exit 6
    fi
}

main "$@"
