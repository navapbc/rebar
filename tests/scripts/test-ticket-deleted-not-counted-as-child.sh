#!/usr/bin/env bash
# tests/scripts/test-ticket-deleted-not-counted-as-child.sh
# Behavioral tests for bug f871-9869-9775-4aa0:
#   Tickets in terminal status `deleted` (tombstones: STATUS(deleted)+ARCHIVED,
#   so status=="deleted" AND archived==True) must NOT be counted as children
#   when determining whether an epic/story "has children".
#
# Axes:
#   A) ticket-list-epics.sh child_count for an epic whose only children are
#      deleted reports 0; a live (non-deleted) child IS counted.
#   B) reduce_all_tickets default (exclude_deleted omitted/False) INCLUDES
#      deleted tickets; exclude_deleted=True EXCLUDES them.
#   C) CONTRACT: `ticket list --parent=<epic> --include-archived` STILL surfaces
#      the deleted child (tombstone visibility preserved). The new opt-in
#      `--exclude-deleted` excludes it.
#   D) REGRESSION: `ticket deps <epic> --include-archived` still returns the
#      deleted child; a live child is still counted everywhere.
#
# Usage: bash tests/scripts/test-ticket-deleted-not-counted-as-child.sh
#
# NOTE: -e is intentionally omitted — test functions may return non-zero on RED.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
REDUCER_PKG_DIR="$REPO_ROOT/src/rebar/_engine"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-deleted-not-counted-as-child.sh ==="

# ── Helper: fresh temp git repo with ticket system initialized ────────────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/rebar-deleted-child.XXXXXX")
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: create a ticket, return its ID ────────────────────────────────────
_create_ticket() {
    local repo="$1" ticket_type="${2:-task}" title="${3:-Test ticket}" extra_args="${4:-}"
    local out
    # shellcheck disable=SC2086
    out=$(cd "$repo" && bash "$TICKET_SCRIPT" create "$ticket_type" "$title" $extra_args 2>/dev/null) || true
    echo "$out" | tail -1
}

# ─────────────────────────────────────────────────────────────────────────────
# Axis A: epic whose only children are deleted → child_count 0; live child counted
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test A: ticket-list-epics child_count excludes deleted children, counts live ones"
test_list_epics_excludes_deleted_children() {
    _snapshot_fail
    local repo epic deleted_child live_child
    repo=$(_make_test_repo)

    epic=$(_create_ticket "$repo" epic "Epic with deleted child")
    deleted_child=$(_create_ticket "$repo" task "Doomed child" "--parent $epic")
    if [ -z "$epic" ] || [ -z "$deleted_child" ]; then
        assert_eq "epic + child created" "non-empty" "empty"
        assert_pass_if_clean "test_list_epics_excludes_deleted_children"
        return
    fi

    # Delete the leaf child (writes STATUS(deleted)+ARCHIVED tombstone)
    (cd "$repo" && bash "$TICKET_SCRIPT" delete "$deleted_child" --user-approved >/dev/null 2>&1) || true

    # ticket-list-epics.sh emits tab-separated rows; the LAST column is child_count.
    # Locate the row for our epic (column 1 is alias-or-id; resolve via alias too).
    # _epic_child_count <repo> <epic_id> → prints the child_count column (or "no_row")
    _epic_child_count() {
        local r="$1" eid="$2" alias rows
        alias=$(cd "$r" && bash "$TICKET_SCRIPT" show "$eid" 2>/dev/null | python3 -c "
import json,sys
try:
    print(json.load(sys.stdin).get('alias') or '')
except Exception:
    print('')
" 2>/dev/null) || alias=""
        rows=$(cd "$r" && bash "$TICKET_SCRIPT" list-epics --all 2>/dev/null) || rows=""
        printf '%s\n' "$rows" | awk -F'\t' -v id="$eid" -v al="$alias" '
            { c1=$1; gsub(/^BLOCKED/, "", c1) }
            ($1==id || $1==al || $2==id || $2==al) { print $NF; found=1; exit }
            END { if (!found) print "no_row" }'
    }

    # An epic whose only child is deleted → child_count must be 0
    local count_deleted_only
    count_deleted_only=$(_epic_child_count "$repo" "$epic")
    assert_eq "epic with only-deleted child has child_count 0" "0" "$count_deleted_only"

    # Now add a live child and confirm it IS counted
    live_child=$(_create_ticket "$repo" task "Live child" "--parent $epic")
    local count_with_live
    count_with_live=$(_epic_child_count "$repo" "$epic")
    assert_eq "live child IS counted (deleted still excluded)" "1" "$count_with_live"

    assert_pass_if_clean "test_list_epics_excludes_deleted_children"
}
test_list_epics_excludes_deleted_children

# ─────────────────────────────────────────────────────────────────────────────
# Axis B: reduce_all_tickets default includes deleted; exclude_deleted=True excludes
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test B: reduce_all_tickets honors exclude_deleted (default False includes deleted)"
test_reduce_all_tickets_exclude_deleted() {
    _snapshot_fail
    local repo epic doomed
    repo=$(_make_test_repo)

    epic=$(_create_ticket "$repo" epic "Epic B")
    doomed=$(_create_ticket "$repo" task "Doomed B" "--parent $epic")
    if [ -z "$doomed" ]; then
        assert_eq "child created" "non-empty" "empty"
        assert_pass_if_clean "test_reduce_all_tickets_exclude_deleted"
        return
    fi
    (cd "$repo" && bash "$TICKET_SCRIPT" delete "$doomed" --user-approved >/dev/null 2>&1) || true

    local tracker="$repo/.tickets-tracker"

    # Default (exclude_deleted omitted) → deleted ticket PRESENT
    local default_has
    default_has=$(_PKG="$REDUCER_PKG_DIR" _TRACKER="$tracker" _ID="$doomed" python3 -c "
import sys, os
sys.path.insert(0, os.environ['_PKG'])
from ticket_reducer import reduce_all_tickets
res = reduce_all_tickets(os.environ['_TRACKER'])
ids = {t.get('ticket_id') for t in res}
print('yes' if os.environ['_ID'] in ids else 'no')
") || default_has="error"
    assert_eq "default reduce_all_tickets INCLUDES deleted" "yes" "$default_has"

    # exclude_deleted=True → deleted ticket ABSENT
    local excl_has
    excl_has=$(_PKG="$REDUCER_PKG_DIR" _TRACKER="$tracker" _ID="$doomed" python3 -c "
import sys, os
sys.path.insert(0, os.environ['_PKG'])
from ticket_reducer import reduce_all_tickets
res = reduce_all_tickets(os.environ['_TRACKER'], exclude_deleted=True)
ids = {t.get('ticket_id') for t in res}
print('yes' if os.environ['_ID'] in ids else 'no')
") || excl_has="error"
    assert_eq "exclude_deleted=True EXCLUDES deleted" "no" "$excl_has"

    assert_pass_if_clean "test_reduce_all_tickets_exclude_deleted"
}
test_reduce_all_tickets_exclude_deleted

# ─────────────────────────────────────────────────────────────────────────────
# Axis C: CONTRACT — --include-archived still surfaces deleted; --exclude-deleted excludes
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test C: --include-archived preserves tombstone visibility; --exclude-deleted opt-in excludes"
test_list_include_archived_contract() {
    _snapshot_fail
    local repo epic doomed
    repo=$(_make_test_repo)

    epic=$(_create_ticket "$repo" epic "Epic C")
    doomed=$(_create_ticket "$repo" task "Doomed C" "--parent $epic")
    if [ -z "$doomed" ]; then
        assert_eq "child created" "non-empty" "empty"
        assert_pass_if_clean "test_list_include_archived_contract"
        return
    fi
    (cd "$repo" && bash "$TICKET_SCRIPT" delete "$doomed" --user-approved >/dev/null 2>&1) || true

    # CONTRACT: --include-archived MUST still surface the deleted child
    local list_ia
    list_ia=$(cd "$repo" && bash "$TICKET_SCRIPT" list --parent="$epic" --include-archived 2>/dev/null) || list_ia=""
    assert_contains "deleted child present with --include-archived (tombstone contract)" "$doomed" "$list_ia"

    # New opt-in: --exclude-deleted must be a recognized flag (no "unknown option")
    local list_ed ed_stderr ed_exit=0
    ed_stderr=$(cd "$repo" && bash "$TICKET_SCRIPT" list --parent="$epic" --include-archived --exclude-deleted 2>&1 >/dev/null) || true
    list_ed=$(cd "$repo" && bash "$TICKET_SCRIPT" list --parent="$epic" --include-archived --exclude-deleted 2>/dev/null) || ed_exit=$?
    assert_not_contains "--exclude-deleted is a recognized flag" "unknown option" "$ed_stderr"
    assert_eq "--exclude-deleted exits 0" "0" "$ed_exit"
    # ...and it removes the deleted child even with --include-archived
    assert_not_contains "deleted child absent with --exclude-deleted" "$doomed" "$list_ed"

    assert_pass_if_clean "test_list_include_archived_contract"
}
test_list_include_archived_contract

# ─────────────────────────────────────────────────────────────────────────────
# Axis D: REGRESSION — ticket deps still returns the deleted child; live child counted
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Test D: ticket deps --include-archived still returns deleted child (regression)"
test_deps_still_returns_deleted_child() {
    _snapshot_fail
    local repo epic doomed live
    repo=$(_make_test_repo)

    epic=$(_create_ticket "$repo" epic "Epic D")
    doomed=$(_create_ticket "$repo" task "Doomed D" "--parent $epic")
    live=$(_create_ticket "$repo" task "Live D" "--parent $epic")
    if [ -z "$doomed" ] || [ -z "$live" ]; then
        assert_eq "children created" "non-empty" "empty"
        assert_pass_if_clean "test_deps_still_returns_deleted_child"
        return
    fi
    (cd "$repo" && bash "$TICKET_SCRIPT" delete "$doomed" --user-approved >/dev/null 2>&1) || true

    # ticket deps must NOT be altered by this fix — deleted child still appears
    local deps_out
    deps_out=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$epic" --include-archived 2>/dev/null) || deps_out=""
    assert_contains "deps --include-archived still returns deleted child" "$doomed" "$deps_out"
    assert_contains "deps --include-archived returns live child" "$live" "$deps_out"

    assert_pass_if_clean "test_deps_still_returns_deleted_child"
}
test_deps_still_returns_deleted_child

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "=== Summary ==="
echo "PASS: $PASS"
echo "FAIL: $FAIL"
[ "$FAIL" -eq 0 ]
