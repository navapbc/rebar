#!/usr/bin/env bash
# purge-non-project-tickets.sh — thin wrapper; canonical implementation in ticket-purge-bridge.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/ticket-purge-bridge.sh" "$@"
