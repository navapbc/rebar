#!/usr/bin/env bash
# shellcheck disable=SC2030,SC2031
# tests/scripts/suites/test-ticket-id-resolution-direct-exec.sh
#
# Behavioral tests for ticket ID resolution across dispatcher subcommands
# that previously bypassed _ticketlib_dispatch (and a few additional callers
# that share the same resolver layer):
#   - ticket deps             -> src/rebar/_engine/ticket-reads.py (deps arm)
#   - ticket unlink           -> src/rebar/_engine/ticket-link.sh unlink
#   - ticket revert           -> src/rebar/_engine/ticket-revert.sh
#   - ticket list-descendants -> src/rebar/_engine/ticket-list-descendants.py
#   - ticket ready --epic=    -> src/rebar/_engine/ticket-reads.py (ready arm)
#   - ticket edit --parent=   -> src/rebar/_engine/ticket-lib-api.sh ticket_edit
#
# Each subcommand must accept the four documented ID forms:
#   - full canonical 16-hex ID
#   - 8-hex short ID
#   - friendly alias (kebab-case, computed from CREATE event)
#   - jira_key (stored in data.jira_key in CREATE event)
#
# RED before fix (deps/unlink/revert + list-descendants/ready/edit-parent):
#   <subcommand> <short|alias|jira>  -> exits 1, "ticket does not exist", OR
#   silently returns empty result for query subcommands.

# -e omitted: failed assertions return non-zero; -e would abort the whole run.
# pipefail omitted: assertions use `echo $output | grep -q PATTERN` and
# pipefail + grep-early-exit can return 141 (SIGPIPE) on a successful match,
# making `if ! ... | grep -q ...` evaluate as true on a match (false negative).
set -uo

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"
REPO_ROOT="${REPO_ROOT:-${GITHUB_WORKSPACE:-$(cd "$SCRIPT_DIR/../.." && pwd)}}"

TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

# shellcheck source=/dev/null
source "$REPO_ROOT/tests/lib/assert.sh"
# shellcheck source=/dev/null
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-id-resolution-direct-exec.sh ==="

# ── Helpers ──────────────────────────────────────────────────────────────────

_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# Create a ticket, inject jira_key into its CREATE event, echo the full_id.
_create_ticket_with_jira_key() {
    local repo="$1" jira_key="$2" title="${3:-resolution test}"
    local full_id
    full_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "$title" 2>/dev/null | tail -1) || true
    if [ -z "$full_id" ]; then echo ""; return; fi
    local create_event
    create_event=$(find "$repo/.tickets-tracker/$full_id" -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | sort | head -1) || true
    if [ -n "$create_event" ]; then
        python3 - "$create_event" "$jira_key" <<'PYEOF'
import json, sys
p, jk = sys.argv[1], sys.argv[2]
with open(p, encoding='utf-8') as f: ev = json.load(f)
ev.setdefault('data', {})['jira_key'] = jk
with open(p, 'w', encoding='utf-8') as f: json.dump(ev, f)
PYEOF
    fi
    echo "$full_id"
}

# Echo "full short alias jira" for a ticket created with the given jira_key.
# Aborts (returns 1) if create fails — caller asserts on the seed step.
_seed_ticket_ids() {
    local repo="$1" jira_key="$2" title="${3:-resolution test}"
    local full_id
    full_id=$(_create_ticket_with_jira_key "$repo" "$jira_key" "$title")
    [ -z "$full_id" ] && return 1
    local short_id="${full_id:0:9}"
    local alias_id
    alias_id=$(python3 - "$repo/.tickets-tracker/$full_id" <<'PYEOF'
import json, os, sys, glob
files = sorted(glob.glob(os.path.join(sys.argv[1], '*-CREATE.json')))
if not files: sys.exit(0)
with open(files[0], encoding='utf-8') as f:
    print(json.load(f).get('data', {}).get('alias') or '')
PYEOF
)
    echo "$full_id $short_id $alias_id $jira_key"
}

# ── ticket deps: 4 ID forms ──────────────────────────────────────────────────
echo ""
echo "--- test_deps_resolves_all_id_forms ---"
test_deps_resolves_all_id_forms() {
    local repo
    repo=$(_make_test_repo)
    local ids
    ids=$(_seed_ticket_ids "$repo" "DSO-D1" "deps test") || {
        assert_eq "test_deps: seed ticket" "ok" "fail"; return
    }
    # shellcheck disable=SC2206
    local arr=($ids)
    local full_id="${arr[0]}" short_id="${arr[1]}" alias_id="${arr[2]}" jira_id="${arr[3]}"

    assert_ne "test_deps: alias non-empty" "" "$alias_id"

    local form
    for form in "full:$full_id" "short:$short_id" "alias:$alias_id" "jira:$jira_id"; do
        local label="${form%%:*}" val="${form#*:}"
        if [ -z "$val" ]; then
            assert_eq "test_deps[$label]: input non-empty" "non-empty" "empty"
            continue
        fi
        local exit_code=0 output
        output=$(
            cd "$repo" || exit 1
            export _TICKET_TEST_NO_SYNC=1
            export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
            bash "$TICKET_SCRIPT" deps "$val" 2>/dev/null
        ) || exit_code=$?
        assert_eq "test_deps[$label]: exits 0 for input=$val" "0" "$exit_code"

        local resolved
        resolved=$(echo "$output" | python3 -c "import json,sys
try: print(json.load(sys.stdin).get('ticket_id',''))
except Exception: print('')" 2>/dev/null || echo "")
        assert_eq "test_deps[$label]: returns canonical ticket_id" "$full_id" "$resolved"
    done
}
test_deps_resolves_all_id_forms

# ── ticket unlink: 4 ID forms on both endpoints ──────────────────────────────
echo ""
echo "--- test_unlink_resolves_all_id_forms ---"
test_unlink_resolves_all_id_forms() {
    local repo
    repo=$(_make_test_repo)

    local src_ids tgt_ids
    src_ids=$(_seed_ticket_ids "$repo" "DSO-USRC" "unlink src") || {
        assert_eq "test_unlink: src seed" "ok" "fail"; return
    }
    tgt_ids=$(_seed_ticket_ids "$repo" "DSO-UTGT" "unlink tgt") || {
        assert_eq "test_unlink: tgt seed" "ok" "fail"; return
    }
    # shellcheck disable=SC2206
    local sarr=($src_ids) tarr=($tgt_ids)
    local s_full="${sarr[0]}" s_short="${sarr[1]}" s_alias="${sarr[2]}" s_jira="${sarr[3]}"
    local t_full="${tarr[0]}" t_short="${tarr[1]}" t_alias="${tarr[2]}" t_jira="${tarr[3]}"

    assert_ne "test_unlink: src alias non-empty" "" "$s_alias"
    assert_ne "test_unlink: tgt alias non-empty" "" "$t_alias"

    # Iterate the 4 forms — same form for both endpoints in each iteration.
    # Each iteration: link (via canonical, which always works) then unlink (form under test).
    local label src_in tgt_in
    for label in full short alias jira; do
        case "$label" in
            full)  src_in="$s_full";  tgt_in="$t_full" ;;
            short) src_in="$s_short"; tgt_in="$t_short" ;;
            alias) src_in="$s_alias"; tgt_in="$t_alias" ;;
            jira)  src_in="$s_jira";  tgt_in="$t_jira" ;;
        esac

        # Set up a fresh link using canonical IDs so unlink has something to remove.
        (
            cd "$repo" || exit 1
            export _TICKET_TEST_NO_SYNC=1
            export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
            bash "$TICKET_SCRIPT" link "$s_full" "$t_full" relates_to >/dev/null 2>&1
        ) || true

        local exit_code=0
        (
            cd "$repo" || exit 1
            export _TICKET_TEST_NO_SYNC=1
            export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
            bash "$TICKET_SCRIPT" unlink "$src_in" "$tgt_in" >/dev/null 2>&1
        ) || exit_code=$?
        assert_eq "test_unlink[$label]: exits 0 for $src_in -> $tgt_in" "0" "$exit_code"
    done

    # No orphan dirs should have been created under any non-canonical input
    local orphan
    for orphan in "$s_short" "$s_alias" "$s_jira" "$t_short" "$t_alias" "$t_jira"; do
        if [ -d "$repo/.tickets-tracker/$orphan" ]; then
            assert_eq "test_unlink: no orphan dir under '$orphan'" "absent" "present"
        fi
    done
}
test_unlink_resolves_all_id_forms

# ── ticket revert: 4 ID forms ────────────────────────────────────────────────
echo ""
echo "--- test_revert_resolves_all_id_forms ---"
test_revert_resolves_all_id_forms() {
    local repo
    repo=$(_make_test_repo)
    local ids
    ids=$(_seed_ticket_ids "$repo" "DSO-R1" "revert test") || {
        assert_eq "test_revert: seed" "ok" "fail"; return
    }
    # shellcheck disable=SC2206
    local arr=($ids)
    local full_id="${arr[0]}" short_id="${arr[1]}" alias_id="${arr[2]}" jira_id="${arr[3]}"

    assert_ne "test_revert: alias non-empty" "" "$alias_id"

    local label val
    for form in "full:$full_id" "short:$short_id" "alias:$alias_id" "jira:$jira_id"; do
        label="${form%%:*}" val="${form#*:}"
        if [ -z "$val" ]; then
            assert_eq "test_revert: $label form non-empty" "non-empty" "empty"
            continue
        fi

        # Fresh COMMENT per iteration so each revert has a unique target uuid.
        (
            cd "$repo" || exit 1
            export _TICKET_TEST_NO_SYNC=1
            export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
            bash "$TICKET_SCRIPT" comment "$full_id" "to-revert-$label" >/dev/null 2>&1
        ) || true

        local target_uuid
        target_uuid=$(python3 - "$repo/.tickets-tracker/$full_id" "$label" <<'PYEOF'
import json, os, sys, glob
d, marker = sys.argv[1], sys.argv[2]
for f in sorted(glob.glob(os.path.join(d, '*-COMMENT.json'))):
    with open(f, encoding='utf-8') as fh:
        ev = json.load(fh)
    body = (ev.get('data', {}) or {}).get('body', '')
    if f"to-revert-{marker}" in body:
        print(ev.get('uuid', ''))
        sys.exit(0)
PYEOF
)
        if [ -z "$target_uuid" ]; then
            assert_eq "test_revert[$label]: comment uuid found" "non-empty" "empty"
            continue
        fi

        local exit_code=0
        (
            cd "$repo" || exit 1
            export _TICKET_TEST_NO_SYNC=1
            export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
            bash "$TICKET_SCRIPT" revert "$val" "$target_uuid" >/dev/null 2>&1
        ) || exit_code=$?
        assert_eq "test_revert[$label]: exits 0 for input=$val" "0" "$exit_code"
    done

    # After 4 successful reverts, REVERT events should land under the canonical dir
    local revert_count
    revert_count=$(find "$repo/.tickets-tracker/$full_id" -maxdepth 1 -name '*-REVERT.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "test_revert: 4 REVERT events under canonical dir" "4" "$revert_count"
}
test_revert_resolves_all_id_forms

# ── ticket list-descendants: 4 ID forms ─────────────────────────────────────
echo ""
echo "--- test_list_descendants_resolves_all_id_forms ---"
test_list_descendants_resolves_all_id_forms() {
    local repo
    repo=$(_make_test_repo)
    local parent_ids child_ids
    parent_ids=$(_seed_ticket_ids "$repo" "DSO-LD1" "parent epic") || {
        assert_eq "test_list_descendants: parent seed" "ok" "fail"; return
    }
    # shellcheck disable=SC2206
    local parr=($parent_ids)
    local p_full="${parr[0]}" p_short="${parr[1]}" p_alias="${parr[2]}" p_jira="${parr[3]}"

    # Create a child ticket so list-descendants has something to return
    local child_full
    child_full=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "ld child" --parent "$p_full" 2>/dev/null | tail -1) || true
    if [ -z "$child_full" ]; then
        assert_eq "test_list_descendants: child create" "ok" "fail"; return
    fi

    local form label val
    for form in "full:$p_full" "short:$p_short" "alias:$p_alias" "jira:$p_jira"; do
        label="${form%%:*}" val="${form#*:}"
        if [ -z "$val" ]; then
            assert_eq "test_list_descendants[$label]: input non-empty" "non-empty" "empty"
            continue
        fi
        local output exit_code=0
        output=$(
            cd "$repo" || exit 1
            export _TICKET_TEST_NO_SYNC=1
            export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
            bash "$TICKET_SCRIPT" list-descendants "$val" 2>/dev/null
        ) || exit_code=$?
        assert_eq "test_list_descendants[$label]: exits 0 for input=$val" "0" "$exit_code"

        local found_child
        found_child=$(echo "$output" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    ids = [t.get('id') if isinstance(t, dict) else t for v in d.values() if isinstance(v, list) for t in v]
    print('yes' if '$child_full' in ids else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")
        assert_eq "test_list_descendants[$label]: includes child of canonical parent" "yes" "$found_child"
    done
}
test_list_descendants_resolves_all_id_forms

# ── ticket ready --epic: 4 ID forms ─────────────────────────────────────────
echo ""
echo "--- test_ready_epic_resolves_all_id_forms ---"
test_ready_epic_resolves_all_id_forms() {
    local repo
    repo=$(_make_test_repo)
    local parent_ids
    parent_ids=$(_seed_ticket_ids "$repo" "DSO-RD1" "ready epic parent") || {
        assert_eq "test_ready_epic: parent seed" "ok" "fail"; return
    }
    # shellcheck disable=SC2206
    local parr=($parent_ids)
    local p_full="${parr[0]}" p_short="${parr[1]}" p_alias="${parr[2]}" p_jira="${parr[3]}"

    local child_full
    child_full=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "ready child" --parent "$p_full" 2>/dev/null | tail -1) || true
    if [ -z "$child_full" ]; then
        assert_eq "test_ready_epic: child create" "ok" "fail"; return
    fi

    local form label val
    for form in "full:$p_full" "short:$p_short" "alias:$p_alias" "jira:$p_jira"; do
        label="${form%%:*}" val="${form#*:}"
        if [ -z "$val" ]; then
            assert_eq "test_ready_epic[$label]: input non-empty" "non-empty" "empty"
            continue
        fi
        local output exit_code=0
        output=$(
            cd "$repo" || exit 1
            export _TICKET_TEST_NO_SYNC=1
            export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
            bash "$TICKET_SCRIPT" ready --epic="$val" 2>/dev/null
        ) || exit_code=$?
        assert_eq "test_ready_epic[$label]: exits 0 for --epic=$val" "0" "$exit_code"

        if ! echo "$output" | grep -q "$child_full"; then
            assert_eq "test_ready_epic[$label]: child appears under canonical epic" "child-found" "missing"
        fi
    done
}
test_ready_epic_resolves_all_id_forms

# ── ticket edit --parent: 4 ID forms (lib-api ticket_edit path) ──────────────
echo ""
echo "--- test_edit_parent_resolves_all_id_forms ---"
test_edit_parent_resolves_all_id_forms() {
    local repo
    repo=$(_make_test_repo)

    # Create the future-parent ticket whose ID we'll address in 4 forms
    local parent_ids
    parent_ids=$(_seed_ticket_ids "$repo" "DSO-EP1" "edit parent target") || {
        assert_eq "test_edit_parent: parent seed" "ok" "fail"; return
    }
    # shellcheck disable=SC2206
    local parr=($parent_ids)
    local p_full="${parr[0]}" p_short="${parr[1]}" p_alias="${parr[2]}" p_jira="${parr[3]}"

    local form label val
    for form in "full:$p_full" "short:$p_short" "alias:$p_alias" "jira:$p_jira"; do
        label="${form%%:*}" val="${form#*:}"
        if [ -z "$val" ]; then
            assert_eq "test_edit_parent[$label]: input non-empty" "non-empty" "empty"
            continue
        fi

        # Each iteration uses a fresh child so prior reparent doesn't interfere
        local child_full
        child_full=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "edit-parent child $label" 2>/dev/null | tail -1) || true
        if [ -z "$child_full" ]; then
            assert_eq "test_edit_parent[$label]: child create" "ok" "fail"; continue
        fi

        local exit_code=0
        (
            cd "$repo" || exit 1
            export _TICKET_TEST_NO_SYNC=1
            export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
            bash "$TICKET_SCRIPT" edit "$child_full" --parent="$val" >/dev/null 2>&1
        ) || exit_code=$?
        assert_eq "test_edit_parent[$label]: exits 0 for --parent=$val" "0" "$exit_code"

        local got_parent
        got_parent=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$child_full" 2>/dev/null \
            | python3 -c "import json,sys; print(json.load(sys.stdin).get('parent_id','') or '')" 2>/dev/null || echo "")
        assert_eq "test_edit_parent[$label]: child parent_id is canonical" "$p_full" "$got_parent"
    done
}
test_edit_parent_resolves_all_id_forms

print_summary
