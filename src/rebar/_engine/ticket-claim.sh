#!/usr/bin/env bash
# ticket-claim.sh
# Atomically claim an OPEN ticket: move it to in_progress AND set its assignee in
# ONE locked critical section (single commit), via ticket_txn.py claim.
#
# Usage: ticket claim <ticket_id> [--assignee=<name>]
#
# Exits 0 on success; 10 on optimistic-concurrency rejection (ticket not open —
# already claimed); 1 on validation / ghost / generic failure.
#
# Concurrency: delegates the lock+verify+write+commit critical section to
# ticket_txn.py (the same lock-holding entrypoint the transition path uses). See
# docs/concurrency.md (I4/I5). This script does only the pre-lock setup.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/ticket-lib.sh"

REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"
TRACKER_DIR="${TICKETS_TRACKER_DIR:-$REPO_ROOT/.tickets-tracker}"
REDUCER="$SCRIPT_DIR/ticket-reducer.py"

_usage() {
    echo "Usage: ticket claim <ticket_id> [--assignee=<name>]" >&2
    echo "  Claims an OPEN ticket (-> in_progress) and sets its assignee atomically." >&2
    echo "  Exits 10 if the ticket is not open (someone else already claimed it)." >&2
    exit 1
}

if [ $# -lt 1 ]; then
    _usage
fi

raw_id="$1"; shift
assignee=""
while [ $# -gt 0 ]; do
    case "$1" in
        --assignee=*) assignee="${1#--assignee=}"; shift ;;
        --assignee)
            if [ $# -lt 2 ]; then echo "Error: --assignee requires a value" >&2; exit 1; fi
            assignee="$2"; shift 2 ;;
        *) shift ;;
    esac
done

ticket_id=$(TICKETS_TRACKER_DIR="$TRACKER_DIR" resolve_ticket_id "$raw_id") || exit 1

# Ghost check (before the lock — read-only), mirrors ticket-transition.sh.
if [ ! -d "$TRACKER_DIR/$ticket_id" ]; then
    echo "Error: ticket '$ticket_id' does not exist" >&2
    exit 1
fi
if ! find "$TRACKER_DIR/$ticket_id" -maxdepth 1 \( -name '*-CREATE.json' -o -name '*-SNAPSHOT.json' \) ! -name '.*' 2>/dev/null | grep -q .; then
    echo "Error: ticket $ticket_id has no CREATE or SNAPSHOT event" >&2
    exit 1
fi
if [ ! -f "$TRACKER_DIR/.env-id" ]; then
    echo "Error: ticket system not initialized. Run 'ticket init' first." >&2
    exit 1
fi

env_id=$(cat "$TRACKER_DIR/.env-id")
author=$(git config user.name 2>/dev/null || echo "Unknown")
lock_file="$TRACKER_DIR/.ticket-write.lock"

claim_exit=0
python3 "$SCRIPT_DIR/ticket_txn.py" claim \
    "$lock_file" "$TRACKER_DIR" "$ticket_id" "$env_id" "$author" "$REDUCER" "$assignee" \
    || claim_exit=$?

if [ "$claim_exit" -eq 10 ]; then
    # Optimistic-concurrency rejection — preserve exit 10 (ConcurrencyError).
    exit 10
elif [ "$claim_exit" -ne 0 ]; then
    exit 1
fi

echo "CLAIMED: $ticket_id${assignee:+ (assignee: $assignee)}"
exit 0
