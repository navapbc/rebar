#!/usr/bin/env bash
# tests/scripts/test-ticket-set-get-verify-commands.sh
# Integration tests for `ticket set-verify-commands` and `ticket get-verify-commands`.
#
# Covers:
#   1. set-verify-commands exits 0 and writes a VERIFY_COMMANDS event
#   2. get-verify-commands outputs a JSON array containing the set entries
#   3. ticket show includes a non-empty verify_commands field after set
#   4. set-verify-commands with invalid JSON exits non-zero
#   5. set-verify-commands with a JSON object (not array) exits non-zero
#   6. set-verify-commands with [] exits 0 and get returns []
#   7. last-write-wins: second set replaces first
#
# Usage: bash tests/scripts/test-ticket-set-get-verify-commands.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-set-get-verify-commands.sh ==="

_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

_create_ticket() {
    local repo="$1"
    local ticket_type="${2:-task}"
    local title="${3:-Test ticket}"
    local out
    out=$(cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 DSO_TICKET_LEGACY=0 \
        bash "$TICKET_SCRIPT" create "$ticket_type" "$title" 2>/dev/null) || true
    echo "$out" | tail -1
}

_count_verify_commands_events() {
    local tracker_dir="$1"
    local ticket_id="$2"
    find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-VERIFY_COMMANDS.json' ! -name '.*' \
        2>/dev/null | wc -l | tr -d ' '
}

# ── Test 1: set-verify-commands exits 0 and writes event ─────────────────────
echo "Test 1: ticket set-verify-commands exits 0 and writes a VERIFY_COMMANDS event"
test_set_verify_commands_happy_path() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Verify commands test")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_set_verify_commands_happy_path"
        return
    fi

    local before_count
    before_count=$(_count_verify_commands_events "$tracker_dir" "$ticket_id")

    local exit_code=0
    (cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 DSO_TICKET_LEGACY=0 \
        bash "$TICKET_SCRIPT" set-verify-commands "$ticket_id" \
            '[{"dd_id":"dd-1","dd_text":"The feature works","command":"pytest tests/test_feature.py"}]' \
    ) >/dev/null 2>&1 || exit_code=$?

    assert_eq "set-verify-commands exits 0" "0" "$exit_code"

    local after_count
    after_count=$(_count_verify_commands_events "$tracker_dir" "$ticket_id")

    assert_eq "VERIFY_COMMANDS event count increased" "1" "$((after_count - before_count))"

    assert_pass_if_clean "test_set_verify_commands_happy_path"
}
test_set_verify_commands_happy_path

# ── Test 2: get-verify-commands returns the set data ─────────────────────────
echo "Test 2: get-verify-commands outputs JSON array with set entries"
test_get_verify_commands_roundtrip() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Roundtrip test")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_get_verify_commands_roundtrip"
        return
    fi

    (cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 DSO_TICKET_LEGACY=0 \
        bash "$TICKET_SCRIPT" set-verify-commands "$ticket_id" \
            '[{"dd_id":"dd-1","dd_text":"Feature A","command":"pytest tests/a.py"},{"dd_id":"dd-2","dd_text":"Feature B","command":"bash tests/b.sh"}]' \
    ) >/dev/null 2>&1

    local got
    got=$(cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 DSO_TICKET_LEGACY=0 \
        bash "$TICKET_SCRIPT" get-verify-commands "$ticket_id" 2>/dev/null)

    local count
    count=$(echo "$got" | jq 'length')
    assert_eq "get returns 2 entries" "2" "$count"

    local first_dd_id
    first_dd_id=$(echo "$got" | jq -r '.[0].dd_id')
    assert_eq "first entry dd_id" "dd-1" "$first_dd_id"

    local second_command
    second_command=$(echo "$got" | jq -r '.[1].command')
    assert_eq "second entry command" "bash tests/b.sh" "$second_command"

    assert_pass_if_clean "test_get_verify_commands_roundtrip"
}
test_get_verify_commands_roundtrip

# ── Test 3: ticket show includes verify_commands field ───────────────────────
echo "Test 3: ticket show includes verify_commands after set"
test_show_includes_verify_commands() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Show test")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created" "non-empty" "empty"
        assert_pass_if_clean "test_show_includes_verify_commands"
        return
    fi

    (cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 DSO_TICKET_LEGACY=0 \
        bash "$TICKET_SCRIPT" set-verify-commands "$ticket_id" \
            '[{"dd_id":"dd-1","dd_text":"Check","command":"echo ok"}]' \
    ) >/dev/null 2>&1

    local show_out
    show_out=$(cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 DSO_TICKET_LEGACY=0 \
        bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null)

    local vc_length
    vc_length=$(echo "$show_out" | jq '.verify_commands | length')
    assert_eq "verify_commands in show output has 1 entry" "1" "$vc_length"

    assert_pass_if_clean "test_show_includes_verify_commands"
}
test_show_includes_verify_commands

# ── Test 4: invalid JSON exits non-zero ──────────────────────────────────────
echo "Test 4: set-verify-commands with invalid JSON exits non-zero"
test_invalid_json() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Invalid JSON test")

    local exit_code=0
    (cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 DSO_TICKET_LEGACY=0 \
        bash "$TICKET_SCRIPT" set-verify-commands "$ticket_id" "not-json" \
    ) >/dev/null 2>&1 || exit_code=$?

    assert_eq "invalid JSON exits non-zero" "1" "$exit_code"

    assert_pass_if_clean "test_invalid_json"
}
test_invalid_json

# ── Test 5: JSON object (not array) exits non-zero ───────────────────────────
echo "Test 5: set-verify-commands with JSON object exits non-zero"
test_json_object() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Object test")

    local exit_code=0
    (cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 DSO_TICKET_LEGACY=0 \
        bash "$TICKET_SCRIPT" set-verify-commands "$ticket_id" '{"not":"array"}' \
    ) >/dev/null 2>&1 || exit_code=$?

    assert_eq "JSON object exits non-zero" "1" "$exit_code"

    assert_pass_if_clean "test_json_object"
}
test_json_object

# ── Test 6: empty array is valid ─────────────────────────────────────────────
echo "Test 6: set-verify-commands with [] exits 0, get returns []"
test_empty_array() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Empty array test")

    local exit_code=0
    (cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 DSO_TICKET_LEGACY=0 \
        bash "$TICKET_SCRIPT" set-verify-commands "$ticket_id" '[]' \
    ) >/dev/null 2>&1 || exit_code=$?

    assert_eq "empty array exits 0" "0" "$exit_code"

    local got
    got=$(cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 DSO_TICKET_LEGACY=0 \
        bash "$TICKET_SCRIPT" get-verify-commands "$ticket_id" 2>/dev/null)

    assert_eq "get returns empty array" "[]" "$got"

    assert_pass_if_clean "test_empty_array"
}
test_empty_array

# ── Test 7: last-write-wins ──────────────────────────────────────────────────
echo "Test 7: second set replaces first (last-write-wins)"
test_last_write_wins() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "LWW test")

    (cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 DSO_TICKET_LEGACY=0 \
        bash "$TICKET_SCRIPT" set-verify-commands "$ticket_id" \
            '[{"dd_id":"dd-1","dd_text":"First","command":"echo first"}]' \
    ) >/dev/null 2>&1

    (cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 DSO_TICKET_LEGACY=0 \
        bash "$TICKET_SCRIPT" set-verify-commands "$ticket_id" \
            '[{"dd_id":"dd-1","dd_text":"Second","command":"echo second"}]' \
    ) >/dev/null 2>&1

    local got
    got=$(cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 DSO_TICKET_LEGACY=0 \
        bash "$TICKET_SCRIPT" get-verify-commands "$ticket_id" 2>/dev/null)

    local dd_text
    dd_text=$(echo "$got" | jq -r '.[0].dd_text')
    assert_eq "last-write-wins: second set replaces first" "Second" "$dd_text"

    assert_pass_if_clean "test_last_write_wins"
}
test_last_write_wins

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "=== test-ticket-set-get-verify-commands.sh complete ==="
print_summary
