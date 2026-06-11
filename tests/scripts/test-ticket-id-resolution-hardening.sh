#!/usr/bin/env bash
# shellcheck disable=SC2030,SC2031
# Each test uses `( export FOO=1; ... )` for hermetic env isolation.
# The export-in-subshell pattern is intentional — env should NOT leak to subsequent tests.
# tests/scripts/test-ticket-id-resolution-hardening.sh
#
# RED tests for ticket CLI ID-resolution hardening
# (cluster: 023d-2d40-e099-4b97 + 2eea-e443-270c-4e92)
#
# Root cause: _ticketlib_resolve_short_id only handles 8-hex IDs. Jira-style
# keys (jira-dig-2529) and aliases are passed through unchanged, causing the
# subsequent dir-existence check to fail. ticket_show emits empty stdout on
# miss instead of JSON. ticket_read_status lacks resolution.
#
# RED assertions (tests that MUST FAIL before the fix):
#   A. test_comment_via_jira_key
#   B. test_tag_via_jira_key
#   C. test_untag_via_jira_key
#   D. test_edit_via_jira_key
#   E. test_set_file_impact_via_jira_key
#   F. test_get_file_impact_via_jira_key
#   G. test_archive_via_jira_key
#   H. test_delete_via_jira_key
#   I. test_ticket_show_json_error_on_miss
#   J. test_ticket_read_status_via_short_id
#
# Usage: bash tests/scripts/test-ticket-id-resolution-hardening.sh

# NOTE: -e intentionally omitted — test functions return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"
REPO_ROOT="${REPO_ROOT:-${GITHUB_WORKSPACE:-$(cd "$SCRIPT_DIR/../.." && pwd)}}"

TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_LIB="$REPO_ROOT/src/rebar/_engine/ticket-lib.sh"
TICKET_LIB_API="$REPO_ROOT/src/rebar/_engine/ticket-lib-api.sh"

# shellcheck source=/dev/null
source "$REPO_ROOT/tests/lib/assert.sh"
# shellcheck source=/dev/null
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-id-resolution-hardening.sh ==="

# ── Helper: create isolated ticket repo ──────────────────────────────────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: create a ticket and inject a jira_key into its CREATE event ───────
# Returns the full canonical ticket_id on stdout.
# The jira_key is written directly into data.jira_key of the CREATE event
# (mirrors what the Jira bridge writes during inbound sync).
_create_ticket_with_jira_key() {
    local repo="$1"
    local ticket_type="${2:-task}"
    local title="${3:-Jira key test ticket}"
    local jira_key="$4"

    local full_id
    full_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create "$ticket_type" "$title" 2>/dev/null | tail -1) || true

    if [ -z "$full_id" ]; then
        echo ""
        return
    fi

    # Inject jira_key into the CREATE event
    local tracker_dir="$repo/.tickets-tracker"
    local create_event
    create_event=$(find "$tracker_dir/$full_id" -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | sort | head -1) || true

    if [ -z "$create_event" ]; then
        echo "$full_id"
        return
    fi

    python3 - "$create_event" "$jira_key" <<'PYEOF'
import json, sys
path, jkey = sys.argv[1], sys.argv[2]
with open(path, encoding='utf-8') as f:
    event = json.load(f)
event.setdefault('data', {})['jira_key'] = jkey
with open(path, 'w', encoding='utf-8') as f:
    json.dump(event, f)
PYEOF

    echo "$full_id"
}

# ── Helper: invoke a ticket-lib-api function via source + dispatch ────────────
_invoke_lib_op() {
    local op="$1"
    shift
    TICKET_LIB_API="$TICKET_LIB_API" bash -c '
        # shellcheck source=/dev/null
        source "$TICKET_LIB_API" || exit 97
        op="$1"; shift
        if ! declare -f "$op" >/dev/null 2>&1; then exit 98; fi
        "$op" "$@"
    ' _invoke_lib_op "$op" "$@"
}

# =============================================================================
# Section A: Jira-key resolution tests for 8 API functions
#
# All of these call `_ticketlib_resolve_short_id` which only handles 8-hex IDs.
# A jira-style key (e.g., "DSO-42") is passed through unchanged, causing the
# downstream `[ ! -d "$TRACKER_DIR/$ticket_id" ]` check to fail because no
# directory named "DSO-42" exists. These tests must FAIL until the fix adds
# `_ticketlib_resolve_id` (wrapping resolve_ticket_id) to each function.
# =============================================================================

# A1: ticket comment via jira_key ─────────────────────────────────────────────
echo ""
echo "--- test_comment_via_jira_key ---"
test_comment_via_jira_key() {
    local repo
    repo=$(_make_test_repo)

    local jira_key="DSO-101"
    local full_id
    full_id=$(_create_ticket_with_jira_key "$repo" task "Comment jira-key test" "$jira_key")

    if [ -z "$full_id" ]; then
        assert_eq "test_comment_via_jira_key: ticket created" "non-empty" "empty"
        return
    fi

    local exit_code=0
    (
        cd "$repo" || exit 1
        export _TICKET_TEST_NO_SYNC=1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_comment "$jira_key" "hello from jira key" >/dev/null 2>&1
    ) || exit_code=$?

    # After correct implementation: exit 0, comment appears on full_id's ticket.
    # RED: exit_code is non-zero because jira_key is not resolved → dir not found.
    assert_eq "test_comment_via_jira_key: exits 0 when resolving jira_key" "0" "$exit_code"

    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$full_id" 2>/dev/null) || true

    local found_comment
    found_comment=$(echo "$show_output" | python3 -c "
import json, sys
d=json.load(sys.stdin)
comments = d.get('comments', [])
found = any('hello from jira key' in (c.get('body','') if isinstance(c,dict) else str(c)) for c in comments)
print('yes' if found else 'no')
" 2>/dev/null || echo "no")

    assert_eq "test_comment_via_jira_key: comment body present on canonical ticket" "yes" "$found_comment"
}
test_comment_via_jira_key

# A2: ticket tag via jira_key ─────────────────────────────────────────────────
echo ""
echo "--- test_tag_via_jira_key ---"
test_tag_via_jira_key() {
    local repo
    repo=$(_make_test_repo)

    local jira_key="DSO-102"
    local full_id
    full_id=$(_create_ticket_with_jira_key "$repo" task "Tag jira-key test" "$jira_key")

    if [ -z "$full_id" ]; then
        assert_eq "test_tag_via_jira_key: ticket created" "non-empty" "empty"
        return
    fi

    local exit_code=0
    (
        cd "$repo" || exit 1
        export _TICKET_TEST_NO_SYNC=1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_tag "$jira_key" jira-resolved-tag >/dev/null 2>&1
    ) || exit_code=$?

    # RED: _ticketlib_resolve_short_id passes jira_key through unchanged → dir not found
    assert_eq "test_tag_via_jira_key: exits 0 when resolving jira_key" "0" "$exit_code"

    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$full_id" 2>/dev/null) || true

    local has_tag
    has_tag=$(echo "$show_output" | python3 -c "
import json, sys
d=json.load(sys.stdin)
print('yes' if 'jira-resolved-tag' in d.get('tags',[]) else 'no')
" 2>/dev/null || echo "no")

    assert_eq "test_tag_via_jira_key: tag applied to canonical ticket" "yes" "$has_tag"

    # Verify no orphan directory under the jira_key name
    if [ -d "$repo/.tickets-tracker/$jira_key" ]; then
        assert_eq "test_tag_via_jira_key: no orphan dir under jira_key" "absent" "present"
    else
        assert_eq "test_tag_via_jira_key: no orphan dir under jira_key" "absent" "absent"
    fi
}
test_tag_via_jira_key

# A3: ticket untag via jira_key ───────────────────────────────────────────────
echo ""
echo "--- test_untag_via_jira_key ---"
test_untag_via_jira_key() {
    local repo
    repo=$(_make_test_repo)

    local jira_key="DSO-103"
    local full_id
    full_id=$(_create_ticket_with_jira_key "$repo" task "Untag jira-key test" "$jira_key")

    if [ -z "$full_id" ]; then
        assert_eq "test_untag_via_jira_key: ticket created" "non-empty" "empty"
        return
    fi

    # First add the tag via canonical ID so we know it exists
    (
        cd "$repo" || exit 1
        export _TICKET_TEST_NO_SYNC=1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_tag "$full_id" removethis >/dev/null 2>&1
    ) || true

    local exit_code=0
    (
        cd "$repo" || exit 1
        export _TICKET_TEST_NO_SYNC=1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_untag "$jira_key" removethis >/dev/null 2>&1
    ) || exit_code=$?

    # RED: jira_key not resolved → dir not found
    assert_eq "test_untag_via_jira_key: exits 0 when resolving jira_key" "0" "$exit_code"

    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$full_id" 2>/dev/null) || true

    local has_tag
    has_tag=$(echo "$show_output" | python3 -c "
import json, sys
d=json.load(sys.stdin)
print('yes' if 'removethis' in d.get('tags',[]) else 'no')
" 2>/dev/null || echo "yes")

    assert_eq "test_untag_via_jira_key: tag removed from canonical ticket" "no" "$has_tag"
}
test_untag_via_jira_key

# A4: ticket edit via jira_key ────────────────────────────────────────────────
echo ""
echo "--- test_edit_via_jira_key ---"
test_edit_via_jira_key() {
    local repo
    repo=$(_make_test_repo)

    local jira_key="DSO-104"
    local full_id
    full_id=$(_create_ticket_with_jira_key "$repo" task "Edit jira-key test" "$jira_key")

    if [ -z "$full_id" ]; then
        assert_eq "test_edit_via_jira_key: ticket created" "non-empty" "empty"
        return
    fi

    local exit_code=0
    (
        cd "$repo" || exit 1
        export _TICKET_TEST_NO_SYNC=1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_edit "$jira_key" --title "Updated via jira key" >/dev/null 2>&1
    ) || exit_code=$?

    # RED: jira_key passed through unchanged → dir not found
    assert_eq "test_edit_via_jira_key: exits 0 when resolving jira_key" "0" "$exit_code"

    local title
    title=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$full_id" 2>/dev/null \
        | python3 -c "import json,sys; print(json.load(sys.stdin).get('title',''))" 2>/dev/null || echo "")

    assert_eq "test_edit_via_jira_key: title updated on canonical ticket" "Updated via jira key" "$title"
}
test_edit_via_jira_key

# A5: ticket set-file-impact via jira_key ─────────────────────────────────────
echo ""
echo "--- test_set_file_impact_via_jira_key ---"
test_set_file_impact_via_jira_key() {
    local repo
    repo=$(_make_test_repo)

    local jira_key="DSO-105"
    local full_id
    full_id=$(_create_ticket_with_jira_key "$repo" task "Set-file-impact jira-key test" "$jira_key")

    if [ -z "$full_id" ]; then
        assert_eq "test_set_file_impact_via_jira_key: ticket created" "non-empty" "empty"
        return
    fi

    local exit_code=0
    (
        cd "$repo" || exit 1
        export _TICKET_TEST_NO_SYNC=1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_set_file_impact "$jira_key" '[{"path":"src/rebar/_engine/ticket-lib-api.sh","reason":"test"}]' >/dev/null 2>&1
    ) || exit_code=$?

    # RED: jira_key passed through unchanged → dir not found
    assert_eq "test_set_file_impact_via_jira_key: exits 0 when resolving jira_key" "0" "$exit_code"

    # Verify the file impact was recorded on the canonical ticket
    local impact_output
    impact_output=$(
        cd "$repo" || exit 1
        export _TICKET_TEST_NO_SYNC=1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_get_file_impact "$full_id" 2>/dev/null
    ) || true

    local has_file
    has_file=$(echo "$impact_output" | python3 -c "
import json, sys
try:
    d=json.load(sys.stdin)
    files = d if isinstance(d,list) else d.get('files', d.get('file_impact', []))
    print('yes' if any('ticket-lib-api' in str(f) for f in files) else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")

    assert_eq "test_set_file_impact_via_jira_key: file impact recorded on canonical ticket" "yes" "$has_file"
}
test_set_file_impact_via_jira_key

# A6: ticket get-file-impact via jira_key ─────────────────────────────────────
echo ""
echo "--- test_get_file_impact_via_jira_key ---"
test_get_file_impact_via_jira_key() {
    local repo
    repo=$(_make_test_repo)

    local jira_key="DSO-106"
    local full_id
    full_id=$(_create_ticket_with_jira_key "$repo" task "Get-file-impact jira-key test" "$jira_key")

    if [ -z "$full_id" ]; then
        assert_eq "test_get_file_impact_via_jira_key: ticket created" "non-empty" "empty"
        return
    fi

    # First set file impact via canonical ID
    (
        cd "$repo" || exit 1
        export _TICKET_TEST_NO_SYNC=1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_set_file_impact "$full_id" '[{"path":"src/rebar/_engine/ticket.sh","reason":"test"}]' >/dev/null 2>&1
    ) || true

    local exit_code=0
    local impact_output
    impact_output=$(
        cd "$repo" || exit 1
        export _TICKET_TEST_NO_SYNC=1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_get_file_impact "$jira_key" 2>/dev/null
    ) || exit_code=$?

    # RED: jira_key not resolved → dir not found, exit non-zero or empty output
    assert_eq "test_get_file_impact_via_jira_key: exits 0 when resolving jira_key" "0" "$exit_code"

    # Verify it returns the file impact data
    local has_file
    has_file=$(echo "$impact_output" | python3 -c "
import json, sys
try:
    raw = sys.stdin.read().strip()
    if not raw:
        print('no'); sys.exit(0)
    d=json.loads(raw)
    files = d if isinstance(d,list) else d.get('files', d.get('file_impact', []))
    print('yes' if any('ticket' in str(f) for f in files) else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")

    assert_eq "test_get_file_impact_via_jira_key: returns canonical ticket file impact data" "yes" "$has_file"
}
test_get_file_impact_via_jira_key

# A7: ticket archive via jira_key ─────────────────────────────────────────────
echo ""
echo "--- test_archive_via_jira_key ---"
test_archive_via_jira_key() {
    local repo
    repo=$(_make_test_repo)

    local jira_key="DSO-107"
    local full_id
    full_id=$(_create_ticket_with_jira_key "$repo" task "Archive jira-key test" "$jira_key")

    if [ -z "$full_id" ]; then
        assert_eq "test_archive_via_jira_key: ticket created" "non-empty" "empty"
        return
    fi

    local exit_code=0
    (
        cd "$repo" || exit 1
        export _TICKET_TEST_NO_SYNC=1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_archive "$jira_key" >/dev/null 2>&1
    ) || exit_code=$?

    # RED: jira_key not resolved → dir not found
    assert_eq "test_archive_via_jira_key: exits 0 when resolving jira_key" "0" "$exit_code"

    # Verify the canonical ticket is archived (status should be archived)
    local status
    status=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$full_id" 2>/dev/null \
        | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")

    assert_eq "test_archive_via_jira_key: canonical ticket is archived" "archived" "$status"
}
test_archive_via_jira_key

# A8: ticket delete via jira_key ──────────────────────────────────────────────
echo ""
echo "--- test_delete_via_jira_key ---"
test_delete_via_jira_key() {
    local repo
    repo=$(_make_test_repo)

    local jira_key="DSO-108"
    local full_id
    full_id=$(_create_ticket_with_jira_key "$repo" task "Delete jira-key test" "$jira_key")

    if [ -z "$full_id" ]; then
        assert_eq "test_delete_via_jira_key: ticket created" "non-empty" "empty"
        return
    fi

    local exit_code=0
    local stderr_out
    stderr_out=$(
        cd "$repo" || exit 1
        export _TICKET_TEST_NO_SYNC=1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_delete "$jira_key" --user-approved 2>&1 >/dev/null
    ) || exit_code=$?

    # RED: jira_key not resolved → dir not found → error about missing ticket
    # After fix: either exits 0 (deleted) OR exits with "children must be deleted"
    # (if open children exist). Either way, the error must NOT be "does not exist".
    # We assert that the error is NOT the resolution-failure message.
    local is_not_found_error=0
    if echo "$stderr_out" | grep -q "does not exist"; then
        is_not_found_error=1
    fi
    assert_eq "test_delete_via_jira_key: not a 'does not exist' resolution failure" "0" "$is_not_found_error"

    # Verify the operation reached the canonical ticket directory (tombstone or deleted)
    # After fix: tombstone written under full_id, not under jira_key
    if [ "$exit_code" -eq 0 ]; then
        # Successful delete: tombstone should be present under canonical dir
        local has_tombstone=0
        if [ -f "$repo/.tickets-tracker/$full_id/.tombstone.json" ]; then
            has_tombstone=1
        fi
        assert_eq "test_delete_via_jira_key: tombstone written under canonical ticket dir" "1" "$has_tombstone"
    else
        # Non-zero exit is acceptable if it's a domain error (open children, etc.)
        # but must not be a resolution error ("does not exist")
        assert_eq "test_delete_via_jira_key: error is domain error, not resolution failure" "0" "$is_not_found_error"
    fi
}
test_delete_via_jira_key

# =============================================================================
# Section B: JSON error contract for ticket show on miss (bug 2eea)
#
# Currently ticket_show emits "Error: Ticket '...' not found" to stderr and
# returns empty stdout + non-zero exit. The orchestrator's json.load(sys.stdin)
# pattern then throws JSONDecodeError. After fix: stdout must be valid JSON
# with {"error": "ticket_not_found", "input": <id>} AND non-zero exit.
# =============================================================================

echo ""
echo "--- test_ticket_show_json_error_on_miss ---"
test_ticket_show_json_error_on_miss() {
    local repo
    repo=$(_make_test_repo)

    local fake_id="f5f9-dead-beef-0000"
    local exit_code=0
    local output
    output=$(
        cd "$repo" || exit 1
        export _TICKET_TEST_NO_SYNC=1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        bash "$TICKET_SCRIPT" show "$fake_id" 2>/dev/null
    ) || exit_code=$?

    # 1. Must exit non-zero
    assert_ne "test_ticket_show_json_error_on_miss: exits non-zero on miss" "0" "$exit_code"

    # 2. stdout must be valid JSON parseable by json.load
    local parse_result
    parse_result=$(echo "$output" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    err = d.get('error', '')
    inp = d.get('input', '')
    if err == 'ticket_not_found' and 'f5f9' in inp:
        print('OK')
    else:
        print(f'WRONG_FIELDS: error={err!r} input={inp!r}')
except Exception as e:
    print(f'PARSE_FAIL: {e}')
" 2>/dev/null || echo "PARSE_FAIL: python3 error")

    assert_eq "test_ticket_show_json_error_on_miss: stdout is JSON with error+input fields" "OK" "$parse_result"
}
test_ticket_show_json_error_on_miss

# Additional ticket show miss test: 8-hex short ID that doesn't resolve
echo ""
echo "--- test_ticket_show_json_error_on_short_id_miss ---"
test_ticket_show_json_error_on_short_id_miss() {
    local repo
    repo=$(_make_test_repo)

    local fake_short="dead-beef"
    local exit_code=0
    local output
    output=$(
        cd "$repo" || exit 1
        export _TICKET_TEST_NO_SYNC=1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        bash "$TICKET_SCRIPT" show "$fake_short" 2>/dev/null
    ) || exit_code=$?

    # Must exit non-zero
    assert_ne "test_ticket_show_json_error_on_short_id_miss: exits non-zero" "0" "$exit_code"

    # stdout must be valid JSON
    local parse_result
    parse_result=$(echo "$output" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    err = d.get('error', '')
    if err == 'ticket_not_found':
        print('OK')
    else:
        print(f'WRONG_ERROR_FIELD: {err!r}')
except Exception as e:
    print(f'PARSE_FAIL: {e}')
" 2>/dev/null || echo "PARSE_FAIL: python3 error")

    assert_eq "test_ticket_show_json_error_on_short_id_miss: stdout is JSON with ticket_not_found" "OK" "$parse_result"
}
test_ticket_show_json_error_on_short_id_miss

# =============================================================================
# Section C: ticket_read_status hardening via short ID and jira_key
#
# Currently ticket_read_status takes (tracker_dir, ticket_id) and calls
# `[ ! -d "$ticket_dir" ]` where ticket_dir = tracker_dir/$ticket_id.
# This works for full canonical IDs but fails for 8-hex short IDs and
# jira-style keys. After fix: ticket_read_status must call resolve_ticket_id
# internally so non-canonical inputs find the correct ticket.
# =============================================================================

echo ""
echo "--- test_ticket_read_status_via_short_id ---"
test_ticket_read_status_via_short_id() {
    local repo
    repo=$(_make_test_repo)

    local full_id
    full_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "read-status short-id test" 2>/dev/null | tail -1) || true

    if [ -z "$full_id" ]; then
        assert_eq "test_ticket_read_status_via_short_id: ticket created" "non-empty" "empty"
        return
    fi

    # Derive the 8-hex short ID
    local short_id="${full_id:0:9}"

    # ticket_read_status is sourced from ticket-lib.sh; call it with the short ID
    local status_exit=0
    local status_val
    status_val=$(
        cd "$repo" || exit 1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        bash -c "
            source '$TICKET_LIB' 2>/dev/null || exit 97
            ticket_read_status '$repo/.tickets-tracker' '$short_id'
        " 2>/dev/null
    ) || status_exit=$?

    # RED: ticket_read_status does not call resolve_ticket_id — dir check fails for short ID
    assert_eq "test_ticket_read_status_via_short_id: exits 0 for short ID" "0" "$status_exit"
    assert_eq "test_ticket_read_status_via_short_id: returns correct status" "open" "$status_val"
}
test_ticket_read_status_via_short_id

echo ""
echo "--- test_ticket_read_status_via_jira_key ---"
test_ticket_read_status_via_jira_key() {
    local repo
    repo=$(_make_test_repo)

    local jira_key="DSO-201"
    local full_id
    full_id=$(_create_ticket_with_jira_key "$repo" task "read-status jira-key test" "$jira_key")

    if [ -z "$full_id" ]; then
        assert_eq "test_ticket_read_status_via_jira_key: ticket created" "non-empty" "empty"
        return
    fi

    # Call ticket_read_status with the jira_key
    local status_exit=0
    local status_val
    status_val=$(
        cd "$repo" || exit 1
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        bash -c "
            source '$TICKET_LIB' 2>/dev/null || exit 97
            ticket_read_status '$repo/.tickets-tracker' '$jira_key'
        " 2>/dev/null
    ) || status_exit=$?

    # RED: ticket_read_status does not resolve jira_key → no such dir → exit 1
    assert_eq "test_ticket_read_status_via_jira_key: exits 0 when resolving jira_key" "0" "$status_exit"
    assert_eq "test_ticket_read_status_via_jira_key: returns correct status" "open" "$status_val"
}
test_ticket_read_status_via_jira_key

# ── Summary ───────────────────────────────────────────────────────────────────
print_summary
