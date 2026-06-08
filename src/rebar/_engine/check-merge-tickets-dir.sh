#!/usr/bin/env bash
set -uo pipefail
# scripts/check-merge-tickets-dir.sh
# Annotation-driven literal guard for the merge/PR script surface.
#
# F-06 fixed a config-drift bug where merge-to-main-direct.sh read
# tickets.directory into _CFG_TKDIR but then ignored the variable at
# several later sites, hard-coding ".tickets-tracker/". This lint prevents  # tickets-boundary-ok
# the bug class from reappearing in the merge/PR script surface.
#
# Scope is intentionally narrow: only the four merge/PR scripts where
# the bug lived. The broader codebase (ticket-* CLI utilities, CI workflows,
# Python reconciler modules) has ~119 legitimate literals that hard-code
# the default path; annotating all of them would be a much larger change
# and is left as a follow-up audit (see remediation plan F-06 Step 2).
#
# In-scope files (under $PLUGIN_DIR/scripts/):
#   merge-to-main.sh
#   merge-to-main-direct.sh
#   merge-to-main-pr.sh
#   create-sprint-draft-pr.sh
#
# Pattern: \.tickets-tracker\b  # tickets-boundary-ok
#
# Per-line exemptions (any of these on the line suppresses the match):
#   # tickets-boundary-ok   — explicit per-line escape (existing convention)
#   :-                       — bash default-value fallback assignment (canonical)
#   Lines starting with #    — pure comments are documentation, not behavior
#
# Usage:
#   check-merge-tickets-dir.sh [file ...]
#
# Exit codes:
#   0 — no violations found
#   1 — one or more violations found

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Determine target files ───────────────────────────────────────────────────
_files=()
if [[ $# -gt 0 ]]; then
    _files=("$@")
else
    _files=(
        "$PLUGIN_DIR/scripts/merge-to-main.sh"
        "$PLUGIN_DIR/scripts/merge-to-main-direct.sh"
        "$PLUGIN_DIR/scripts/merge-to-main-pr.sh"
        "$PLUGIN_DIR/scripts/create-sprint-draft-pr.sh"
    )
fi

# ── Scan ─────────────────────────────────────────────────────────────────────
_violations=0
for _f in "${_files[@]}"; do
    [[ -f "$_f" ]] || continue

    while IFS= read -r _line_with_num; do
        _lineno="${_line_with_num%%:*}"
        _content="${_line_with_num#*:}"

        # Skip pure comment lines (whitespace + #)
        case "$_content" in
            *[![:space:]]*) ;;  # has non-whitespace; continue checks
            *) continue ;;
        esac
        _trimmed="${_content#"${_content%%[![:space:]]*}"}"
        case "$_trimmed" in
            '#'*) continue ;;
        esac

        # Skip lines with per-line escape annotation
        case "$_content" in
            *'# tickets-boundary-ok'*) continue ;;
        esac

        # Skip lines containing bash default-value fallback operator (:-)
        # combined with the literal — these are canonical fallback assignments.
        if [[ "$_content" == *":-"*".tickets-tracker"* ]]; then  # tickets-boundary-ok
            continue
        fi

        printf '%s:%s: %s\n' "$_f" "$_lineno" "$_content"
        _violations=$((_violations + 1))
    done < <(grep -nE '\.tickets-tracker' "$_f" 2>/dev/null || true)  # tickets-boundary-ok
done

# ── Report ───────────────────────────────────────────────────────────────────
if [[ "$_violations" -gt 0 ]]; then
    echo "" >&2
    echo "check-merge-tickets-dir: $_violations literal .tickets-tracker reference(s) found." >&2  # tickets-boundary-ok
    echo "" >&2
    echo "Merge/PR scripts must read tickets.directory from config (via" >&2
    echo "read-config.sh) and use the resolved variable, NOT the literal path." >&2
    echo "" >&2
    echo "To resolve each violation:" >&2
    echo "  (a) Replace the literal with the canonical \$_CFG_TKDIR variable, or" >&2
    echo "  (b) Convert to a fallback assignment using \${VAR:-.tickets-tracker}, or" >&2  # tickets-boundary-ok
    echo "  (c) Append '# tickets-boundary-ok' if the literal is intentional" >&2
    echo "      (e.g., a documentation reference or canonical-name string)." >&2
    exit 1
fi

echo "check-merge-tickets-dir: no violations found."
exit 0
