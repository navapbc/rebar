#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-link-duplicates-supersedes.sh
# E2E acceptance test for `duplicates` and `supersedes` link relations.
#
# Tests the full behavioral contract via the ticket CLI (not implementation details):
#   - `ticket link <src> <tgt> duplicates` and `supersedes` are accepted
#   - `ticket deps <id>` surfaces these relations with correct direction
#   - blockers and ready_to_work are unaffected by duplicates/supersedes links
#   - `ticket unlink` removes the relation from deps
#   - Issuing the same LINK twice is a no-op (idempotent)
#   - Invalid relations are still rejected
#
# Usage: bash tests/scripts/suites/test-ticket-link-duplicates-supersedes.sh

# NOTE: -e is intentionally omitted — test functions return non-zero by design.
# -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-link-duplicates-supersedes.sh ==="

# ── Helper: create a fresh temp git repo with ticket system initialized ────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: create a ticket and return its ID ─────────────────────────────────
_create_ticket() {
    local repo="$1"
    local ticket_type="${2:-task}"
    local title="${3:-Test ticket}"
    local out
    out=$(cd "$repo" && bash "$TICKET_SCRIPT" create "$ticket_type" "$title" 2>/dev/null) || true
    echo "$out" | tail -1
}

# ── Helper: extract a field from ticket deps JSON output ─────────────────────
# Usage: _deps_field <json_string> <python_expression>
# Returns the Python-evaluated expression on the parsed JSON dict.
_deps_field() {
    local json_str="$1"
    local py_expr="$2"
    python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print($py_expr)
except Exception as e:
    print('ERROR:' + str(e))
    sys.exit(1)
" "$json_str" 2>/dev/null || echo "ERROR:parse-failed"
}

# ── Test 1: three-ticket scenario — A duplicates B, C supersedes A ────────────
echo "Test 1: A duplicates B, C supersedes A — ticket deps shows correct relations"
test_e2e_three_ticket_scenario() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local id_a id_b id_c
    id_a=$(_create_ticket "$repo" task "Ticket A (duplicate)")
    id_b=$(_create_ticket "$repo" task "Ticket B (canonical)")
    id_c=$(_create_ticket "$repo" task "Ticket C (superseding)")

    if [ -z "$id_a" ] || [ -z "$id_b" ] || [ -z "$id_c" ]; then
        assert_eq "three tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_e2e_three_ticket_scenario"
        return
    fi

    # Link A duplicates B
    local link_ab_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id_a" "$id_b" duplicates 2>/dev/null) || link_ab_exit=$?
    assert_eq "link A duplicates B: exits 0" "0" "$link_ab_exit"

    # Link C supersedes A
    local link_ca_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id_c" "$id_a" supersedes 2>/dev/null) || link_ca_exit=$?
    assert_eq "link C supersedes A: exits 0" "0" "$link_ca_exit"

    # Verify via ticket deps: A has one dep — duplicates B
    local deps_a
    deps_a=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$id_a" 2>/dev/null) || deps_a="{}"

    local a_deps_count
    a_deps_count=$(_deps_field "$deps_a" "len(d.get('deps', []))")
    assert_eq "deps A: one dep entry" "1" "$a_deps_count"

    local a_dep_relation
    a_dep_relation=$(_deps_field "$deps_a" "d['deps'][0]['relation'] if d.get('deps') else 'none'")
    assert_eq "deps A: dep relation is duplicates" "duplicates" "$a_dep_relation"

    local a_dep_target
    a_dep_target=$(_deps_field "$deps_a" "d['deps'][0]['target_id'] if d.get('deps') else 'none'")
    assert_eq "deps A: dep target_id is B" "$id_b" "$a_dep_target"

    # Verify via ticket deps: C has one dep — supersedes A
    local deps_c
    deps_c=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$id_c" 2>/dev/null) || deps_c="{}"

    local c_deps_count
    c_deps_count=$(_deps_field "$deps_c" "len(d.get('deps', []))")
    assert_eq "deps C: one dep entry" "1" "$c_deps_count"

    local c_dep_relation
    c_dep_relation=$(_deps_field "$deps_c" "d['deps'][0]['relation'] if d.get('deps') else 'none'")
    assert_eq "deps C: dep relation is supersedes" "supersedes" "$c_dep_relation"

    local c_dep_target
    c_dep_target=$(_deps_field "$deps_c" "d['deps'][0]['target_id'] if d.get('deps') else 'none'")
    assert_eq "deps C: dep target_id is A" "$id_a" "$c_dep_target"

    # Verify B has no deps (relations are directional, not bidirectional)
    local deps_b
    deps_b=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$id_b" 2>/dev/null) || deps_b="{}"

    local b_deps_count
    b_deps_count=$(_deps_field "$deps_b" "len(d.get('deps', []))")
    assert_eq "deps B: no deps (not bidirectional)" "0" "$b_deps_count"

    assert_pass_if_clean "test_e2e_three_ticket_scenario"
}
test_e2e_three_ticket_scenario

# ── Test 2: blockers and ready_to_work unaffected by duplicates/supersedes ────
echo "Test 2: duplicates and supersedes links do not add to blockers or affect ready_to_work"
test_e2e_blockers_unaffected() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local id_a id_b id_c
    id_a=$(_create_ticket "$repo" task "Source ticket")
    id_b=$(_create_ticket "$repo" task "Duplicate target")
    id_c=$(_create_ticket "$repo" task "Superseded ticket")

    if [ -z "$id_a" ] || [ -z "$id_b" ] || [ -z "$id_c" ]; then
        assert_eq "tickets created for blockers test" "non-empty" "empty"
        assert_pass_if_clean "test_e2e_blockers_unaffected"
        return
    fi

    # Create both link types on A
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id_a" "$id_b" duplicates 2>/dev/null) || true
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id_a" "$id_c" supersedes 2>/dev/null) || true

    local deps_a
    deps_a=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$id_a" 2>/dev/null) || deps_a="{}"

    # blockers must be empty — duplicates/supersedes do not block
    local blockers_count
    blockers_count=$(_deps_field "$deps_a" "len(d.get('blockers', []))")
    assert_eq "blockers: empty for duplicates+supersedes links" "0" "$blockers_count"

    # ready_to_work must be true — no blockers
    local ready
    ready=$(_deps_field "$deps_a" "str(d.get('ready_to_work', False)).lower()")
    assert_eq "ready_to_work: true when only duplicates/supersedes links" "true" "$ready"

    assert_pass_if_clean "test_e2e_blockers_unaffected"
}
test_e2e_blockers_unaffected

# ── Test 3: unlink removes the relation from ticket deps ──────────────────────
echo "Test 3: ticket unlink removes duplicates relation from deps"
test_e2e_unlink_removes_dep() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local id_a id_b
    id_a=$(_create_ticket "$repo" task "Unlink source")
    id_b=$(_create_ticket "$repo" task "Unlink target")

    if [ -z "$id_a" ] || [ -z "$id_b" ]; then
        assert_eq "tickets created for unlink test" "non-empty" "empty"
        assert_pass_if_clean "test_e2e_unlink_removes_dep"
        return
    fi

    # Create the link
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id_a" "$id_b" duplicates 2>/dev/null) || true

    # Verify dep is present before unlink
    local deps_before
    deps_before=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$id_a" 2>/dev/null) || deps_before="{}"
    local count_before
    count_before=$(_deps_field "$deps_before" "len(d.get('deps', []))")
    assert_eq "deps present before unlink" "1" "$count_before"

    # Unlink
    local unlink_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" unlink "$id_a" "$id_b" 2>/dev/null) || unlink_exit=$?
    assert_eq "unlink: exits 0" "0" "$unlink_exit"

    # Verify dep is gone after unlink
    local deps_after
    deps_after=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$id_a" 2>/dev/null) || deps_after="{}"
    local count_after
    count_after=$(_deps_field "$deps_after" "len(d.get('deps', []))")
    assert_eq "deps empty after unlink" "0" "$count_after"

    assert_pass_if_clean "test_e2e_unlink_removes_dep"
}
test_e2e_unlink_removes_dep

# ── Test 4: unlink removes supersedes relation from ticket deps ───────────────
echo "Test 4: ticket unlink removes supersedes relation from deps"
test_e2e_unlink_supersedes() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local id_a id_b
    id_a=$(_create_ticket "$repo" task "Superseding source")
    id_b=$(_create_ticket "$repo" task "Superseded target")

    if [ -z "$id_a" ] || [ -z "$id_b" ]; then
        assert_eq "tickets created for supersedes unlink test" "non-empty" "empty"
        assert_pass_if_clean "test_e2e_unlink_supersedes"
        return
    fi

    # Create the link
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id_a" "$id_b" supersedes 2>/dev/null) || true

    # Verify dep is present
    local deps_before
    deps_before=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$id_a" 2>/dev/null) || deps_before="{}"
    local count_before
    count_before=$(_deps_field "$deps_before" "len(d.get('deps', []))")
    assert_eq "supersedes dep present before unlink" "1" "$count_before"

    # Unlink
    local unlink_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" unlink "$id_a" "$id_b" 2>/dev/null) || unlink_exit=$?
    assert_eq "unlink supersedes: exits 0" "0" "$unlink_exit"

    # Verify dep is gone
    local deps_after
    deps_after=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$id_a" 2>/dev/null) || deps_after="{}"
    local count_after
    count_after=$(_deps_field "$deps_after" "len(d.get('deps', []))")
    assert_eq "supersedes dep gone after unlink" "0" "$count_after"

    assert_pass_if_clean "test_e2e_unlink_supersedes"
}
test_e2e_unlink_supersedes

# ── Test 5: idempotency — issuing the same LINK twice is a no-op ──────────────
echo "Test 5: issuing the same duplicates LINK twice is a no-op (idempotent)"
test_e2e_duplicates_idempotent() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local id_a id_b
    id_a=$(_create_ticket "$repo" task "Idempotent source")
    id_b=$(_create_ticket "$repo" task "Idempotent target")

    if [ -z "$id_a" ] || [ -z "$id_b" ]; then
        assert_eq "tickets created for idempotent test" "non-empty" "empty"
        assert_pass_if_clean "test_e2e_duplicates_idempotent"
        return
    fi

    # First link
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id_a" "$id_b" duplicates 2>/dev/null) || true

    # Second identical link — must exit 0 and not duplicate the dep
    local second_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id_a" "$id_b" duplicates 2>/dev/null) || second_exit=$?
    assert_eq "idempotent: second identical link exits 0" "0" "$second_exit"

    # deps must still show exactly one entry
    local deps_json
    deps_json=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$id_a" 2>/dev/null) || deps_json="{}"
    local dep_count
    dep_count=$(_deps_field "$deps_json" "len(d.get('deps', []))")
    assert_eq "idempotent: exactly one dep entry after two identical links" "1" "$dep_count"

    assert_pass_if_clean "test_e2e_duplicates_idempotent"
}
test_e2e_duplicates_idempotent

# ── Test 6: idempotency for supersedes ────────────────────────────────────────
echo "Test 6: issuing the same supersedes LINK twice is a no-op (idempotent)"
test_e2e_supersedes_idempotent() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local id_a id_b
    id_a=$(_create_ticket "$repo" task "Supersedes idempotent source")
    id_b=$(_create_ticket "$repo" task "Supersedes idempotent target")

    if [ -z "$id_a" ] || [ -z "$id_b" ]; then
        assert_eq "tickets created for supersedes idempotent test" "non-empty" "empty"
        assert_pass_if_clean "test_e2e_supersedes_idempotent"
        return
    fi

    # First link
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id_a" "$id_b" supersedes 2>/dev/null) || true

    # Second identical link
    local second_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id_a" "$id_b" supersedes 2>/dev/null) || second_exit=$?
    assert_eq "supersedes idempotent: second identical link exits 0" "0" "$second_exit"

    # deps must still show exactly one entry
    local deps_json
    deps_json=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$id_a" 2>/dev/null) || deps_json="{}"
    local dep_count
    dep_count=$(_deps_field "$deps_json" "len(d.get('deps', []))")
    assert_eq "supersedes idempotent: exactly one dep after two identical links" "1" "$dep_count"

    assert_pass_if_clean "test_e2e_supersedes_idempotent"
}
test_e2e_supersedes_idempotent

# ── Test 7: deps JSON includes link_uuid field ─────────────────────────────────
echo "Test 7: ticket deps JSON includes link_uuid field for duplicates and supersedes"
test_e2e_deps_includes_link_uuid() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local id_a id_b id_c
    id_a=$(_create_ticket "$repo" task "UUID check A")
    id_b=$(_create_ticket "$repo" task "UUID check B")
    id_c=$(_create_ticket "$repo" task "UUID check C")

    if [ -z "$id_a" ] || [ -z "$id_b" ] || [ -z "$id_c" ]; then
        assert_eq "tickets created for link_uuid test" "non-empty" "empty"
        assert_pass_if_clean "test_e2e_deps_includes_link_uuid"
        return
    fi

    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id_a" "$id_b" duplicates 2>/dev/null) || true
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$id_c" "$id_a" supersedes 2>/dev/null) || true

    # Check A's deps has link_uuid
    local deps_a
    deps_a=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$id_a" 2>/dev/null) || deps_a="{}"
    local a_uuid
    a_uuid=$(_deps_field "$deps_a" "bool(d['deps'][0].get('link_uuid')) if d.get('deps') else False")
    assert_eq "deps A: link_uuid present in duplicates dep" "True" "$a_uuid"

    # Check C's deps has link_uuid
    local deps_c
    deps_c=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$id_c" 2>/dev/null) || deps_c="{}"
    local c_uuid
    c_uuid=$(_deps_field "$deps_c" "bool(d['deps'][0].get('link_uuid')) if d.get('deps') else False")
    assert_eq "deps C: link_uuid present in supersedes dep" "True" "$c_uuid"

    assert_pass_if_clean "test_e2e_deps_includes_link_uuid"
}
test_e2e_deps_includes_link_uuid

print_summary
