#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-archive-eligibility.sh
# RED tests for transitive dependency traversal that determines archive eligibility.
#
# Tests call `python3 ticket-graph.py --archive-eligible --tickets-dir=<tracker>`
# and parse the JSON array output to assert on the eligible set.
#
# All test functions MUST FAIL until compute_archive_eligible() is implemented
# in ticket-graph.py.
#
# Usage: bash tests/scripts/suites/test-ticket-archive-eligibility.sh
# Returns: exit non-zero (RED) until archive eligibility is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
GRAPH_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-graph.py"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-archive-eligibility.sh ==="

# ── Suite-runner guard: skip RED tests when compute_archive_eligible not yet implemented ──
if [[ "${_RUN_ALL_ACTIVE:-0}" == "1" ]]; then
    if ! grep -q 'compute_archive_eligible' "$GRAPH_SCRIPT" 2>/dev/null; then
        echo "SKIP: compute_archive_eligible not yet in ticket-graph.py (RED tests)"
        echo ""
        printf "PASSED: 0  FAILED: 0\n"
        exit 0
    fi
fi

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
    # Extract just the ticket ID (last line, in case of multi-line output)
    echo "$out" | tail -1
}

# ── Helper: write a LINK event JSON directly into a ticket dir ─────────────────
_write_link_event() {
    local tracker_dir="$1"
    local source_id="$2"
    local target_id="$3"
    local relation="$4"
    local ts
    ts=$(date +%s)
    local link_uuid
    link_uuid="link-$(printf '%04x%04x' $RANDOM $RANDOM)"
    local event_file="$tracker_dir/$source_id/${ts}-${link_uuid}-LINK.json"
    python3 -c "
import json, sys
event = {
    'timestamp': int(sys.argv[1]),
    'uuid': sys.argv[2],
    'event_type': 'LINK',
    'env_id': 'test',
    'author': 'test',
    'data': {
        'target_id': sys.argv[3],
        'relation': sys.argv[4]
    }
}
with open(sys.argv[5], 'w') as f:
    json.dump(event, f)
" "$ts" "$link_uuid" "$target_id" "$relation" "$event_file"
    git -C "$tracker_dir" add "$source_id/" 2>/dev/null
    git -C "$tracker_dir" commit -q --no-verify -m "test: LINK $source_id -> $target_id ($relation)" 2>/dev/null || true
}

# ── Helper: run archive-eligible and capture output ────────────────────────────
_run_archive_eligible() {
    local tracker_dir="$1"
    python3 "$GRAPH_SCRIPT" --archive-eligible --tickets-dir="$tracker_dir" 2>/dev/null
}

# ── Helper: check if a ticket ID is in the JSON array output ───────────────────
_id_in_json_array() {
    local json_output="$1"
    local ticket_id="$2"
    python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    ids = data if isinstance(data, list) else []
    sys.exit(0 if sys.argv[2] in ids else 1)
except Exception:
    sys.exit(1)
" "$json_output" "$ticket_id"
}

# ── Test 1: unreachable closed ticket is eligible ──────────────────────────────
echo "Test 1: unreachable closed ticket is eligible"
test_unreachable_closed_ticket_is_eligible() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create 3 tickets: A (open), B (closed, depends on A), C (closed, no deps)
    local id_a id_b id_c
    id_a=$(_create_ticket "$repo" task "Ticket A open")
    id_b=$(_create_ticket "$repo" task "Ticket B closed dep on A")
    id_c=$(_create_ticket "$repo" task "Ticket C closed no deps")

    if [ -z "$id_a" ] || [ -z "$id_b" ] || [ -z "$id_c" ]; then
        assert_eq "all three tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_unreachable_closed_ticket_is_eligible"
        return
    fi

    # B depends_on A
    _write_link_event "$tracker_dir" "$id_b" "$id_a" "depends_on"

    # Close B and C
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$id_b" open closed 2>/dev/null) || true
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$id_c" open closed 2>/dev/null) || true

    # Run archive-eligible
    local output
    local exit_code=0
    output=$(_run_archive_eligible "$tracker_dir") || exit_code=$?

    # Assert: command exits 0
    assert_eq "archive-eligible exits 0" "0" "$exit_code"

    # Assert: C IS in eligible set (unreachable closed ticket)
    if _id_in_json_array "$output" "$id_c"; then
        assert_eq "C is eligible" "true" "true"
    else
        assert_eq "C is eligible" "true" "false"
    fi

    assert_pass_if_clean "test_unreachable_closed_ticket_is_eligible"
}
test_unreachable_closed_ticket_is_eligible

# ── Test 2: reachable closed ticket is ineligible ──────────────────────────────
echo "Test 2: reachable closed ticket is ineligible"
test_reachable_closed_ticket_is_ineligible() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # A (open) depends_on B (closed). B is reachable from A → ineligible.
    local id_a id_b
    id_a=$(_create_ticket "$repo" task "Ticket A open depends on B")
    id_b=$(_create_ticket "$repo" task "Ticket B closed")

    if [ -z "$id_a" ] || [ -z "$id_b" ]; then
        assert_eq "both tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_reachable_closed_ticket_is_ineligible"
        return
    fi

    # A depends_on B
    _write_link_event "$tracker_dir" "$id_a" "$id_b" "depends_on"

    # Close B
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$id_b" open closed 2>/dev/null) || true

    # Run archive-eligible
    local output
    local exit_code=0
    output=$(_run_archive_eligible "$tracker_dir") || exit_code=$?

    # Assert: command exits 0
    assert_eq "archive-eligible exits 0" "0" "$exit_code"

    # Assert: B is NOT in eligible set (reachable from open A via depends_on)
    if _id_in_json_array "$output" "$id_b"; then
        assert_eq "B is NOT eligible" "false" "true"
    else
        assert_eq "B is NOT eligible" "false" "false"
    fi

    assert_pass_if_clean "test_reachable_closed_ticket_is_ineligible"
}
test_reachable_closed_ticket_is_ineligible

# ── Test 3: transitive reachability makes both ineligible ──────────────────────
echo "Test 3: transitive reachability"
test_transitive_reachability() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # A (open) depends_on B (closed) depends_on C (closed). Both B and C ineligible.
    local id_a id_b id_c
    id_a=$(_create_ticket "$repo" task "Ticket A open")
    id_b=$(_create_ticket "$repo" task "Ticket B closed mid-chain")
    id_c=$(_create_ticket "$repo" task "Ticket C closed end-chain")

    if [ -z "$id_a" ] || [ -z "$id_b" ] || [ -z "$id_c" ]; then
        assert_eq "all three tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_transitive_reachability"
        return
    fi

    # A depends_on B, B depends_on C
    _write_link_event "$tracker_dir" "$id_a" "$id_b" "depends_on"
    _write_link_event "$tracker_dir" "$id_b" "$id_c" "depends_on"

    # Close B and C
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$id_b" open closed 2>/dev/null) || true
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$id_c" open closed 2>/dev/null) || true

    # Run archive-eligible
    local output
    local exit_code=0
    output=$(_run_archive_eligible "$tracker_dir") || exit_code=$?

    # Assert: command exits 0
    assert_eq "archive-eligible exits 0" "0" "$exit_code"

    # Assert: B is NOT eligible (directly reachable from open A)
    if _id_in_json_array "$output" "$id_b"; then
        assert_eq "B is NOT eligible" "false" "true"
    else
        assert_eq "B is NOT eligible" "false" "false"
    fi

    # Assert: C is NOT eligible (transitively reachable from open A)
    if _id_in_json_array "$output" "$id_c"; then
        assert_eq "C is NOT eligible" "false" "true"
    else
        assert_eq "C is NOT eligible" "false" "false"
    fi

    assert_pass_if_clean "test_transitive_reachability"
}
test_transitive_reachability

# ── Test 4: relates_to edges are ignored ───────────────────────────────────────
echo "Test 4: relates_to edges ignored"
test_relates_to_edges_ignored() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # A (open) relates_to B (closed). B IS eligible (relates_to ignored).
    local id_a id_b
    id_a=$(_create_ticket "$repo" task "Ticket A open relates")
    id_b=$(_create_ticket "$repo" task "Ticket B closed relates")

    if [ -z "$id_a" ] || [ -z "$id_b" ]; then
        assert_eq "both tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_relates_to_edges_ignored"
        return
    fi

    # A relates_to B
    _write_link_event "$tracker_dir" "$id_a" "$id_b" "relates_to"

    # Close B
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$id_b" open closed 2>/dev/null) || true

    # Run archive-eligible
    local output
    local exit_code=0
    output=$(_run_archive_eligible "$tracker_dir") || exit_code=$?

    # Assert: command exits 0
    assert_eq "archive-eligible exits 0" "0" "$exit_code"

    # Assert: B IS eligible (relates_to does not make ineligible)
    if _id_in_json_array "$output" "$id_b"; then
        assert_eq "B is eligible" "true" "true"
    else
        assert_eq "B is eligible" "true" "false"
    fi

    assert_pass_if_clean "test_relates_to_edges_ignored"
}
test_relates_to_edges_ignored

# ── Test 5: depends_on makes ineligible (edge direction) ──────────────────────
echo "Test 5: depends_on makes ineligible (edge direction)"
test_depends_on_makes_ineligible() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # open A depends_on closed B → B is NOT eligible
    local id_a id_b
    id_a=$(_create_ticket "$repo" task "Ticket A open depends")
    id_b=$(_create_ticket "$repo" task "Ticket B closed depended-on")

    if [ -z "$id_a" ] || [ -z "$id_b" ]; then
        assert_eq "both tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_depends_on_makes_ineligible"
        return
    fi

    # A depends_on B
    _write_link_event "$tracker_dir" "$id_a" "$id_b" "depends_on"

    # Close B
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$id_b" open closed 2>/dev/null) || true

    # Run archive-eligible
    local output
    local exit_code=0
    output=$(_run_archive_eligible "$tracker_dir") || exit_code=$?

    # Assert: command exits 0
    assert_eq "archive-eligible exits 0" "0" "$exit_code"

    # Assert: B is NOT eligible (open A depends_on it)
    if _id_in_json_array "$output" "$id_b"; then
        assert_eq "B is NOT eligible" "false" "true"
    else
        assert_eq "B is NOT eligible" "false" "false"
    fi

    assert_pass_if_clean "test_depends_on_makes_ineligible"
}
test_depends_on_makes_ineligible

# ── Test 6: blocks makes ineligible (edge direction) ──────────────────────────
echo "Test 6: blocks makes ineligible (edge direction)"
test_blocks_makes_ineligible() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # closed C blocks open D → C is NOT eligible
    local id_c id_d
    id_c=$(_create_ticket "$repo" task "Ticket C closed blocks")
    id_d=$(_create_ticket "$repo" task "Ticket D open blocked")

    if [ -z "$id_c" ] || [ -z "$id_d" ]; then
        assert_eq "both tickets created" "non-empty" "empty"
        assert_pass_if_clean "test_blocks_makes_ineligible"
        return
    fi

    # C blocks D
    _write_link_event "$tracker_dir" "$id_c" "$id_d" "blocks"

    # Close C (D stays open)
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$id_c" open closed 2>/dev/null) || true

    # Run archive-eligible
    local output
    local exit_code=0
    output=$(_run_archive_eligible "$tracker_dir") || exit_code=$?

    # Assert: command exits 0
    assert_eq "archive-eligible exits 0" "0" "$exit_code"

    # Assert: C is NOT eligible (it blocks open D)
    if _id_in_json_array "$output" "$id_c"; then
        assert_eq "C is NOT eligible" "false" "true"
    else
        assert_eq "C is NOT eligible" "false" "false"
    fi

    assert_pass_if_clean "test_blocks_makes_ineligible"
}
test_blocks_makes_ineligible

print_summary
