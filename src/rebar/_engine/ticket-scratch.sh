#!/usr/bin/env bash
# ticket-scratch.sh
# Thin dispatcher routing `dso ticket scratch <verb> [args...]` to the
# per-verb implementation scripts:
#   set   → ticket-scratch-set.sh   <ticket_id> <key> <value>
#   get   → ticket-scratch-get.sh   <ticket_id> <key>
#   clear → ticket-scratch-clear.sh <ticket_id> [<key>]
#
# Unknown verb exits non-zero with a structured JSON error envelope:
#   {"status":"error","code":"unknown_verb","verb":"<v>"}
#
# Usage:
#   ticket-scratch.sh <verb> [args...]

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_usage() {
    printf 'Usage: ticket scratch <verb> [args...]\n' >&2
    printf '\n' >&2
    printf 'Verbs:\n' >&2
    printf '  set   <ticket_id> <key> <value>  — write a scratch key\n' >&2
    printf '  get   <ticket_id> <key>           — read a scratch key\n' >&2
    printf '  clear <ticket_id> [<key>]         — remove a key or entire ticket scratch\n' >&2
    exit 1
}

if [ $# -lt 1 ]; then
    _usage
fi

verb="$1"
shift

case "$verb" in
    set)
        exec bash "$SCRIPT_DIR/ticket-scratch-set.sh" "$@"
        ;;
    get)
        exec bash "$SCRIPT_DIR/ticket-scratch-get.sh" "$@"
        ;;
    clear)
        exec bash "$SCRIPT_DIR/ticket-scratch-clear.sh" "$@"
        ;;
    *)
        printf '{"status":"error","code":"unknown_verb","verb":"%s"}\n' "$verb"
        exit 1
        ;;
esac
