#!/usr/bin/env bash
# ticket-untag.sh
# Remove a tag from a ticket using the _tag_remove helper.
#
# Usage: ticket-untag.sh <ticket_id> <tag>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=${_PLUGIN_ROOT}/scripts/ticket-lib.sh
source "$SCRIPT_DIR/ticket-lib.sh"

_usage() {
    echo "Usage: ticket untag <ticket_id> <tag>" >&2
    echo "  ticket_id: ticket directory name (e.g., abcd-1234)" >&2
    echo "  tag:       tag to remove (e.g., area:frontend)" >&2
    exit 1
}

[[ $# -lt 2 ]] && _usage

ticket_id=$(resolve_ticket_id "$1") || exit 1
tag="$2"

_tag_remove "$ticket_id" "$tag"
