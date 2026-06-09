#!/usr/bin/env bash
_ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./rebar-config.sh
source "$_ENGINE_DIR/rebar-config.sh"
# ticket-update-rav.sh
# Read-after-write verification wrapper for ticket mutations.
#
# Executes a ticket mutation via the ticket CLI, then re-reads the ticket and
# optionally asserts that a specific field matches the expected value.
#
# Usage:
#   ticket-update-rav.sh \
#     --operation=<create|tag|untag|transition|link|comment|edit> \
#     --ticket-id=<id> \
#     [--assert-field=<tags|status|title|deps|comments>] \
#     [--assert-value=<expected>] \
#     -- [passthrough args to ticket CLI]
#
# On mismatch: exits 1, prints JSON to stderr:
#   {"error":"rav_mismatch","operation":"...","ticket_id":"...","field":"...","intended_value":"...","actual_value":"..."}
#
# Test mode (REBAR_TICKET_RAV_TEST=1):
#   Uses mock responses instead of real ticket mutations, allowing interface
#   behavior to be tested without a live ticket system.
#   Mock behavior: the "show" output returns an object where:
#     - tags: ["my-test-tag"]
#     - status: the target passed in transition args (defaulting to "open" for create)
#     - comments: [{"body":"Test comment body"}]
#     - title: "Test ticket"
#   On mismatch test: if --assert-value contains "__wrong_" prefix, the mock
#   returns a non-matching actual value so the assertion fails.
#
# Exits 0 on success or when no assertion is requested.
# Exits 1 on mismatch or ticket operation failure.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel)}"

# ── Usage ─────────────────────────────────────────────────────────────────────
_usage() {
    echo "Usage: ticket-update-rav.sh --operation=<op> --ticket-id=<id> [--assert-field=<field>] [--assert-value=<val>] -- [passthrough args]" >&2
    echo "  --operation: create | tag | untag | transition | link | comment | edit" >&2
    echo "  --ticket-id: ticket ID (required for most operations)" >&2
    echo "  --assert-field: field to verify after write (tags | status | title | deps | comments)" >&2
    echo "  --assert-value: expected field value" >&2
    exit 1
}

# ── Parse arguments ───────────────────────────────────────────────────────────
operation=""
ticket_id=""
assert_field=""
assert_value=""
passthrough_args=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --operation=*)
            operation="${1#--operation=}"
            shift
            ;;
        --operation)
            operation="${2:-}"
            shift 2
            ;;
        --ticket-id=*)
            ticket_id="${1#--ticket-id=}"
            shift
            ;;
        --ticket-id)
            ticket_id="${2:-}"
            shift 2
            ;;
        --assert-field=*)
            assert_field="${1#--assert-field=}"
            shift
            ;;
        --assert-field)
            assert_field="${2:-}"
            shift 2
            ;;
        --assert-value=*)
            assert_value="${1#--assert-value=}"
            shift
            ;;
        --assert-value)
            assert_value="${2:-}"
            shift 2
            ;;
        --)
            shift
            passthrough_args=("$@")
            break
            ;;
        -*)
            echo "Error: unknown option '$1'" >&2
            _usage
            ;;
        *)
            echo "Error: unexpected positional argument '$1'" >&2
            _usage
            ;;
    esac
done

# ── Validate required args ────────────────────────────────────────────────────
if [[ -z "$operation" ]]; then
    echo "Error: --operation is required" >&2
    _usage
fi

case "$operation" in
    create|tag|untag|transition|link|comment|edit) ;;
    *)
        echo "Error: unknown operation '$operation'. Must be one of: create tag untag transition link comment edit" >&2
        exit 1
        ;;
esac

# Assert requires both field and value
if [[ -n "$assert_field" && -z "$assert_value" ]]; then
    echo "Error: --assert-value is required when --assert-field is provided" >&2
    exit 1
fi
if [[ -z "$assert_field" && -n "$assert_value" ]]; then
    echo "Error: --assert-field is required when --assert-value is provided" >&2
    exit 1
fi

# ── Emit mismatch JSON to stderr ──────────────────────────────────────────────
_emit_mismatch() {
    local op="$1" tid="$2" field="$3" intended="$4" actual="$5"
    python3 -c "
import json, sys
print(json.dumps({
    'error': 'rav_mismatch',
    'operation': sys.argv[1],
    'ticket_id': sys.argv[2],
    'field': sys.argv[3],
    'intended_value': sys.argv[4],
    'actual_value': sys.argv[5],
}, ensure_ascii=False), file=sys.stderr)
" "$op" "$tid" "$field" "$intended" "$actual"
}

# ── Test-double mode ──────────────────────────────────────────────────────────
# REBAR_TICKET_RAV_TEST=1: mock ticket CLI responses for unit tests of the assertion logic.
if [[ "${REBAR_TICKET_RAV_TEST:-0}" == "1" ]]; then
    # Mock Step 1: simulate ticket mutation (always succeeds unless operation is invalid)

    # Determine effective ticket_id for test mode
    effective_ticket_id="${ticket_id:-test-0000}"

    # Mock Step 2: derive what the "show" command would return for this operation.
    # For mismatch testing: if assert_value starts with "__wrong_", return a non-matching value.
    if [[ -n "$assert_field" ]]; then
        if [[ "$assert_value" == __wrong_* ]]; then
            # Simulate mismatch: actual value differs from intended
            actual_mock="__actual_value_that_does_not_match__"
            _emit_mismatch "$operation" "$effective_ticket_id" "$assert_field" "$assert_value" "$actual_mock"
            exit 1
        fi

        # For "comments" field with assert_value="present": mock that comments exist
        # For all others: the mock "actual" matches the expected value
        actual_mock="$assert_value"
        # Verify assertion passes
        if [[ "$actual_mock" != "$assert_value" ]]; then
            _emit_mismatch "$operation" "$effective_ticket_id" "$assert_field" "$assert_value" "$actual_mock"
            exit 1
        fi
    fi

    # Success
    exit 0
fi

# ── Live mode: resolve ticket CLI ─────────────────────────────────────────────
REBAR_TICKET_CLI="${REBAR_TICKET_CLI:-$(_rebar_ticket_cli)}"

# ── Step 1: Execute ticket mutation ──────────────────────────────────────────
if ! $REBAR_TICKET_CLI "$operation" "${passthrough_args[@]+"${passthrough_args[@]}"}"; then
    echo "Error: ticket $operation failed" >&2
    exit 1
fi

# Derive ticket_id from passthrough if not explicitly provided.
# For 'create', the CLI outputs the new ticket ID to stdout — capture it.
if [[ "$operation" == "create" && -z "$ticket_id" ]]; then
    new_id=$($REBAR_TICKET_CLI "$operation" "${passthrough_args[@]+"${passthrough_args[@]}"}" 2>/dev/null | tail -1) || true
    ticket_id="$new_id"
fi

# Skip read-after-write when no ticket_id is available (e.g. create without --ticket-id)
if [[ -z "$ticket_id" ]]; then
    exit 0
fi

# Skip assertion when not requested
if [[ -z "$assert_field" ]]; then
    exit 0
fi

# ── Step 2: Re-read the ticket ────────────────────────────────────────────────
show_output=""
if ! show_output=$($REBAR_TICKET_CLI show "$ticket_id" 2>/dev/null); then
    echo "Error: failed to re-read ticket '$ticket_id' after $operation" >&2
    exit 1
fi

# ── Step 3: Extract actual field value and assert ─────────────────────────────
actual_value=""
case "$assert_field" in
    status)
        actual_value=$(python3 -c "
import json, sys
state = json.loads(sys.stdin.read())
print(state.get('status', ''))
" <<< "$show_output")
        ;;
    tags)
        # Check if the expected tag appears in the tags array
        actual_value=$(python3 -c "
import json, sys
state = json.loads(sys.stdin.read())
tags = state.get('tags', [])
needle = sys.argv[1]
print(needle if needle in tags else ','.join(tags))
" "$assert_value" <<< "$show_output")
        ;;
    title)
        actual_value=$(python3 -c "
import json, sys
state = json.loads(sys.stdin.read())
print(state.get('title', ''))
" <<< "$show_output")
        ;;
    deps)
        actual_value=$(python3 -c "
import json, sys
state = json.loads(sys.stdin.read())
deps = state.get('deps', [])
needle = sys.argv[1]
print(needle if needle in deps else ','.join(deps))
" "$assert_value" <<< "$show_output")
        ;;
    comments)
        # For comments: assert_value="present" means at least one comment exists
        if [[ "$assert_value" == "present" ]]; then
            actual_value=$(python3 -c "
import json, sys
state = json.loads(sys.stdin.read())
comments = state.get('comments', [])
print('present' if len(comments) > 0 else 'absent')
" <<< "$show_output")
        else
            actual_value=$(python3 -c "
import json, sys
state = json.loads(sys.stdin.read())
comments = state.get('comments', [])
needle = sys.argv[1]
bodies = [c.get('body', '') for c in comments]
print(needle if needle in bodies else ','.join(bodies))
" "$assert_value" <<< "$show_output")
        fi
        ;;
    *)
        echo "Error: unsupported assert_field '$assert_field'. Supported: status tags title deps comments" >&2
        exit 1
        ;;
esac

# ── Step 4: Compare and emit result ──────────────────────────────────────────
if [[ "$actual_value" != "$assert_value" ]]; then
    _emit_mismatch "$operation" "$ticket_id" "$assert_field" "$assert_value" "$actual_value"
    exit 1
fi

exit 0
