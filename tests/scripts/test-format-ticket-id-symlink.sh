#!/usr/bin/env bash
# tests/scripts/test-format-ticket-id-symlink.sh
#
# RED tests for bug 3203-236a-09bc-44f4:
#   format_ticket_id() bash fallback paths use `find` without -L,
#   causing silent zero-result when .tickets-tracker is a symlink
#   (worktree sessions where TICKETS_TRACKER_DIR points to a symlink).
#
# RED assertions (MUST FAIL before the fix):
#   A. test_format_ticket_id_short_via_symlink_tracker
#      format_ticket_id "$id" short -> returns raw canonical id (bug: no -L)
#   B. test_format_ticket_id_auto_via_symlink_tracker
#      format_ticket_id "$id" auto  -> returns raw canonical id (bug: no -L)
#
# GREEN after fix:
#   A. format_ticket_id "$id" short -> returns a valid short prefix (≤ 32 chars, non-empty, != full id)
#   B. format_ticket_id "$id" auto  -> returns a valid short prefix (same)
#
# Usage: bash tests/scripts/test-format-ticket-id-symlink.sh

# -e omitted: test functions return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"
REPO_ROOT="${REPO_ROOT:-${GITHUB_WORKSPACE:-$(cd "$SCRIPT_DIR/../.." && pwd)}}"

TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_LIB="$REPO_ROOT/src/rebar/_engine/ticket-lib.sh"

# shellcheck source=/dev/null
source "$REPO_ROOT/tests/lib/assert.sh"
# shellcheck source=/dev/null
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-format-ticket-id-symlink.sh ==="

# ── Helper: create isolated ticket repo ──────────────────────────────────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/test-format-symlink.XXXXXX")
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: force bash fallback by making python3 unavailable ────────────────
# format_ticket_id uses python3 only for jira_key/alias extraction; the
# short-prefix scan loop is pure bash. To test the bash fallback path for the
# find calls, we don't need to mask python3 — we just need the ticket to have
# no jira_key/alias so the function falls through to the prefix scan.
# The tickets created via the CLI have no jira_key and no alias by default,
# so the auto mode cascades directly to the prefix scan.

# ── Test A: format_ticket_id short via symlink tracker ───────────────────────
echo ""
echo "--- test_format_ticket_id_short_via_symlink_tracker ---"
test_format_ticket_id_short_via_symlink_tracker() {
    local repo
    repo=$(_make_test_repo)

    # Create a ticket in the real .tickets-tracker
    local full_id
    full_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "symlink test ticket short" 2>/dev/null | tail -1) || true

    if [ -z "$full_id" ]; then
        assert_ne "ticket_created" "" "$full_id"
        return 1
    fi

    # Create a symlink pointing to the real .tickets-tracker
    local real_tracker="$repo/.tickets-tracker"
    local symlink_tracker
    symlink_tracker=$(mktemp -d "${TMPDIR:-/tmp}/symlink-parent.XXXXXX")
    _CLEANUP_DIRS+=("$symlink_tracker")
    local symlink_path="$symlink_tracker/tracker-link"
    ln -s "$real_tracker" "$symlink_path"

    # Verify the symlink was created and resolves correctly
    if [ ! -L "$symlink_path" ] || [ ! -d "$symlink_path" ]; then
        assert_eq "symlink_valid" "valid" "invalid"
        return 1
    fi

    # Source ticket-lib.sh and call format_ticket_id with the symlinked tracker dir
    local result
    result=$(
        # shellcheck source=/dev/null
        source "$TICKET_LIB" 2>/dev/null
        TICKETS_TRACKER_DIR="$symlink_path" format_ticket_id "$full_id" short
    ) || true

    # GREEN: result should be a valid short prefix — not equal to full_id, non-empty,
    # and shorter than the full id (or at minimum not the full canonical 32-char id).
    local full_nodash="${full_id//-/}"
    local result_nodash="${result//-/}"

    # Check: result must not be the full canonical ID (which would indicate fallback to raw)
    if [ "$result" = "$full_id" ]; then
        assert_ne "short_prefix_returned" "$full_id" "$result"
        return 1
    fi

    # Check: result must be non-empty
    assert_ne "short_prefix_result_nonempty" "" "$result"

    # Check: result (stripped of dashes) must be a prefix of the full id
    if [[ "$full_nodash" != "$result_nodash"* ]]; then
        assert_eq "result_is_prefix_of_full" "prefix_of_$full_nodash" "$result_nodash"
        return 1
    fi

    echo "PASS: format_ticket_id short returned valid prefix '$result' for '$full_id' via symlink tracker"
}
test_format_ticket_id_short_via_symlink_tracker

# ── Test B: format_ticket_id auto via symlink tracker (no-alias ticket) ──────
# The auto-mode cascade is: jira_key → alias → short → canonical.
# Tickets with an auto-generated alias return the alias before reaching the
# prefix scan. To exercise the prefix-scan code path (and the find -L bug),
# we strip the alias from the CREATE event after ticket creation.
echo ""
echo "--- test_format_ticket_id_auto_via_symlink_tracker ---"
test_format_ticket_id_auto_via_symlink_tracker() {
    local repo
    repo=$(_make_test_repo)

    # Create a ticket in the real .tickets-tracker
    local full_id
    full_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "symlink test ticket auto" 2>/dev/null | tail -1) || true

    if [ -z "$full_id" ]; then
        assert_ne "ticket_created" "" "$full_id"
        return 1
    fi

    # Strip the alias from the CREATE event so format_ticket_id falls through
    # to the prefix scan (the code path containing the find-without-L bug).
    local real_tracker="$repo/.tickets-tracker"
    local create_event
    create_event=$(find "$real_tracker/$full_id" -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | sort | head -1) || true
    if [ -n "$create_event" ]; then
        python3 - "$create_event" <<'PYEOF'
import json, sys
p = sys.argv[1]
with open(p, encoding='utf-8') as f:
    ev = json.load(f)
ev.setdefault('data', {})['alias'] = ''
ev.setdefault('data', {})['jira_key'] = ''
with open(p, 'w', encoding='utf-8') as f:
    json.dump(ev, f)
PYEOF
    fi

    # Create a symlink pointing to the real .tickets-tracker
    local symlink_tracker
    symlink_tracker=$(mktemp -d "${TMPDIR:-/tmp}/symlink-parent.XXXXXX")
    _CLEANUP_DIRS+=("$symlink_tracker")
    local symlink_path="$symlink_tracker/tracker-link"
    ln -s "$real_tracker" "$symlink_path"

    # Verify the symlink was created and resolves correctly
    if [ ! -L "$symlink_path" ] || [ ! -d "$symlink_path" ]; then
        assert_eq "symlink_valid" "valid" "invalid"
        return 1
    fi

    # Source ticket-lib.sh and call format_ticket_id auto with the symlinked tracker dir.
    # With the bug (find without -L), the prefix scan returns 0 dirs → falls back to
    # the raw canonical id. With the fix (find -L), it returns a valid short prefix.
    local result
    result=$(
        # shellcheck source=/dev/null
        source "$TICKET_LIB" 2>/dev/null
        TICKETS_TRACKER_DIR="$symlink_path" format_ticket_id "$full_id" auto
    ) || true

    # GREEN: result should be a valid short prefix — not equal to full_id, non-empty,
    # and the result_nodash should be a prefix of full_nodash.
    local full_nodash="${full_id//-/}"
    local result_nodash="${result//-/}"

    # Check: result must not be the full canonical ID (which would indicate fallback to raw)
    if [ "$result" = "$full_id" ]; then
        assert_ne "auto_prefix_returned" "$full_id" "$result"
        return 1
    fi

    # Check: result must be non-empty
    assert_ne "auto_prefix_result_nonempty" "" "$result"

    # Check: result (stripped of dashes) must be a prefix of the full id
    if [[ "$full_nodash" != "$result_nodash"* ]]; then
        assert_eq "auto_result_is_prefix_of_full" "prefix_of_$full_nodash" "$result_nodash"
        return 1
    fi

    echo "PASS: format_ticket_id auto returned valid prefix '$result' for '$full_id' via symlink tracker"
}
test_format_ticket_id_auto_via_symlink_tracker

# ── Cleanup ───────────────────────────────────────────────────────────────────
echo ""
if declare -f _cleanup_test_dirs >/dev/null 2>&1; then
    _cleanup_test_dirs
fi

echo "=== DONE ==="
