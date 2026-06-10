#!/usr/bin/env bash
# tests/scripts/test-ticket-help-overview-coverage.sh
# Drift guard: every routable top-level dispatcher arm must be listed in the
# `rebar help` overview (_print_overview).
#
# How it works:
#   - Extract the main `case "$subcommand"` arms from the dispatcher. They sit
#     at 4-space indent (`^    arm)`), which distinguishes them from the
#     8-space `_print_subcommand_help` arms.
#   - Split alternation arms (`a|b|c)`) on `|` so each alias is checked.
#   - Assert each arm appears in `rebar help` output as a listed subcommand,
#     i.e. a line matching `^  <arm>( |$)`.
#   - ALLOWLIST internal arms that are intentionally NOT advertised in the
#     overview (currently only `help`, the top-level help word itself).
#
# Against an unpatched dispatcher (missing reopen/delete/compact/compact-all/
# fsck/fsck-recover/revert/resolve/format) this guard reports exactly those 9
# arms as missing and exits 1.
#
# Usage: bash tests/scripts/test-ticket-help-overview-coverage.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
DISPATCHER="$REPO_ROOT/src/rebar/_engine/rebar"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-ticket-help-overview-coverage.sh ==="

# Arms intentionally not advertised in the overview (internal only).
ALLOWLIST="help"

_is_allowlisted() {
    local arm="$1" a
    for a in $ALLOWLIST; do
        [ "$arm" = "$a" ] && return 0
    done
    return 1
}

# Extract every public dispatch arm (4-space indent), splitting alternations.
mapfile -t ARMS < <(
    grep -E '^    [a-z][a-z0-9-]*(\|[a-z0-9-]+)*\)' "$DISPATCHER" \
        | sed -E 's/^    //; s/\).*$//' \
        | tr '|' '\n' \
        | sort -u
)

assert_ne "found dispatcher arms" "0" "${#ARMS[@]}"

# Capture the help overview once.
HELP_OUT=$(bash "$TICKET_SCRIPT" help 2>&1)

missing=()
for arm in "${ARMS[@]}"; do
    [ -z "$arm" ] && continue
    if _is_allowlisted "$arm"; then
        continue
    fi
    if ! grep -qE "^  ${arm}( |\$)" <<<"$HELP_OUT"; then
        missing+=("$arm")
    fi
done

echo "Test: every public dispatcher arm appears in 'rebar help' overview"
assert_eq "no dispatcher arms missing from help overview (${missing[*]:-none})" \
    "" "${missing[*]:-}"

print_summary
