#!/usr/bin/env bash
# tests/lib/ticket-fixtures.sh
# Shared test helpers for ticket system tests.
#
# Source this file from test scripts:
#   source "$REPO_ROOT/tests/lib/ticket-fixtures.sh"

# ── Helper: write an event file to a ticket dir ─────────────────────────────
# Usage: _write_event <ticket_dir> <timestamp> <uuid> <event_type> <data_json>
#        [env_id] [author]
_write_event() {
    local ticket_dir="$1"
    local timestamp="$2"
    local uuid="$3"
    local event_type="$4"
    local data_json="$5"
    local env_id="${6:-00000000-0000-4000-8000-000000000001}"
    local author="${7:-Test User}"
    local filename="${timestamp}-${uuid}-${event_type}.json"

    python3 -c "
import json, sys
payload = {
    'timestamp': $timestamp,
    'uuid': '$uuid',
    'event_type': '$event_type',
    'env_id': '$env_id',
    'author': '$author',
    'data': json.loads('''$data_json''')
}
json.dump(payload, sys.stdout)
" > "$ticket_dir/$filename"
}
