#!/usr/bin/env bash
# ticket-output.sh
# Sourced shim for rebar's canonical structured-output flag (--output / -o).
#
# All real LOGIC — accepted spellings' allowed VALUES, per-command defaults,
# validation, and the "unsupported output format" error text — lives once in
# ticket_output.py. This shim is mechanical bash plumbing ONLY (so the flag's
# behaviour is never duplicated between bash and Python):
#
#   _resolve_output_format <profile> "$@"
#       Sets _OUTPUT_FMT to the validated format token (profile default when the
#       flag is absent). Returns 2 on an invalid/missing value — ticket_output.py
#       has already printed "Error: ..." to stderr. <profile> is one of
#       reader | ready | report (see ticket_output.py).
#
#   _strip_output_flags "$@"
#       Sets the _OUTPUT_ARGS array to "$@" with the -o/--output flag (and its
#       space-form value) removed, so the caller can parse its own flags from
#       what remains. No validation here — _resolve_output_format already did it.
#       NOTE: expand the result as "${_OUTPUT_ARGS[@]+"${_OUTPUT_ARGS[@]}"}" so an
#       empty array is safe under `set -u` on bash 3.2 (macOS).
#
# Self-contained: depends only on python3 + this file's sibling ticket_output.py,
# so scripts that source nothing else can source it directly.

_TICKET_OUTPUT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_TICKET_OUTPUT_PY="$_TICKET_OUTPUT_DIR/ticket_output.py"

_resolve_output_format() {
    local _profile="$1"
    shift
    _OUTPUT_FMT="$(python3 "$_TICKET_OUTPUT_PY" resolve "$_profile" -- "$@")" || return 2
    return 0
}

_strip_output_flags() {
    _OUTPUT_ARGS=()
    local _skip=0 _a
    for _a in "$@"; do
        if [ "$_skip" -eq 1 ]; then
            _skip=0
            continue
        fi
        case "$_a" in
            -o|--output) _skip=1 ;;
            -o=*|--output=*) ;;
            *) _OUTPUT_ARGS+=("$_a") ;;
        esac
    done
}
