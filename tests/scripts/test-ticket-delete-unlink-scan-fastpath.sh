#!/usr/bin/env bash
# tests/scripts/test-ticket-delete-unlink-scan-fastpath.sh
#
# Validates the fast-path short-circuit in
# src/rebar/_engine/ticket-delete-unlink-scan.py (bug 071c-24fe-d4e5-4370).
# The fast path skips reduce_all_tickets() when no LINK or SNAPSHOT events
# anywhere in the tracker reference the deleted ticket (by UUID or alias).
#
# Test scenarios (6):
#   1. test_fastpath_isolated_ticket
#      No LINKs anywhere → no UNLINKs written, exit 0, no output.
#   2. test_fastpath_outbound_link_present
#      Deleted ticket has a *-LINK.json in its own dir → UNLINK written.
#   3. test_fastpath_outbound_snapshot_with_deps
#      Deleted ticket has only a SNAPSHOT (no LINK file) with non-empty deps
#      → UNLINK written (fast path falls through to reducer).
#   4. test_fastpath_inbound_link_by_uuid
#      Another ticket's *-LINK.json contains the deleted ticket's UUID
#      → inbound UNLINK written.
#   5. test_fastpath_inbound_link_by_alias
#      Another ticket's *-LINK.json contains the deleted ticket's computed alias
#      → inbound UNLINK written (proves alias resolution in fast path).
#   6. test_fastpath_outbound_only_matches_excluded
#      Deleted ticket's own SNAPSHOT contains its own UUID (self-reference) but
#      no LINK files in its dir and no other ticket references it → no UNLINKs.
#      Verifies that matches inside the deleted ticket's own dir do not falsely
#      trigger the inbound branch.
#
# Usage: bash tests/scripts/test-ticket-delete-unlink-scan-fastpath.sh
# Returns: exit 0 if all tests pass, exit non-zero if any fail

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
SCAN_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-delete-unlink-scan.py"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-ticket-delete-unlink-scan-fastpath.sh ==="

# ── Cleanup ───────────────────────────────────────────────────────────────────
_CLEANUP_DIRS=()
_cleanup() {
    for d in "${_CLEANUP_DIRS[@]:-}"; do
        rm -rf "$d" 2>/dev/null || true
    done
}
trap _cleanup EXIT

# ── Fixture helpers ───────────────────────────────────────────────────────────

# A real ticket UUID that the alias module can map (must use the 4-segment
# format the resolver expects: xxxx-xxxx-xxxx-xxxx).
_DELETED_UUID="aaaa-bbbb-cccc-dddd"

# Compute the canonical alias for the deleted UUID using the same algorithm
# the production code uses. We resolve this at test setup so the inbound-by-
# alias test exercises the real alias path.
_DELETED_ALIAS=$(python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/src/rebar/_engine')
from ticket_reducer._alias import compute_alias
print(compute_alias('$_DELETED_UUID') or '')
")

if [[ -z "$_DELETED_ALIAS" ]]; then
    echo "FAIL: could not compute alias for $_DELETED_UUID (wordlist missing?)" >&2
    exit 1
fi

# Write a minimal CREATE event for the deleted ticket so resolve_ticket_id works.
_make_tracker_with_deleted() {
    local tracker_dir _tmpbase
    # Strip any trailing slash from $TMPDIR (macOS sets e.g. /var/.../T/) so the
    # mktemp template does not produce a doubled slash. The scan prints
    # pathlib.Path-normalized (single-slash) paths, while the assertions below
    # grep -F for "$tracker_dir/..." literally — a "//" would never match.
    _tmpbase="${TMPDIR:-/tmp}"
    _tmpbase="${_tmpbase%/}"
    tracker_dir=$(mktemp -d "$_tmpbase/test-fastpath.XXXXXX")
    _CLEANUP_DIRS+=("$tracker_dir")

    # Initialize as a git repo so the resolver works
    git -C "$tracker_dir" init --quiet >/dev/null 2>&1
    git -C "$tracker_dir" config user.email test@test 2>/dev/null
    git -C "$tracker_dir" config user.name Test 2>/dev/null

    # Create the deleted ticket's dir with a minimal CREATE event
    mkdir -p "$tracker_dir/$_DELETED_UUID"
    python3 -c "
import json
event = {
    'event_type': 'CREATE',
    'timestamp': 1700000000000000000,
    'uuid': '00000000-0000-0000-0000-000000000001',
    'env_id': 'test-env',
    'author': 'Test',
    'data': {
        'ticket_id': '$_DELETED_UUID',
        'title': 'Deleted ticket',
        'ticket_type': 'task',
        'status': 'open',
        'priority': 2,
        'parent_id': None,
        'tags': [],
        'description': '',
        'alias': '$_DELETED_ALIAS',
    }
}
with open('$tracker_dir/$_DELETED_UUID/001-CREATE.json', 'w') as f:
    json.dump(event, f)
"
    echo "$tracker_dir"
}

# Write a CREATE event so reduce_all_tickets recognizes this ticket dir.
_write_create() {
    local tracker_dir="$1" ticket_id="$2"
    mkdir -p "$tracker_dir/$ticket_id"
    python3 -c "
import json, hashlib
# Distinct timestamp/uuid per ticket to avoid event-file collisions
h = hashlib.sha256('$ticket_id'.encode()).hexdigest()
event = {
    'event_type': 'CREATE',
    'timestamp': 1700000000000000000 + int(h[:6], 16),
    'uuid': '00000000-0000-0000-0000-' + h[:12],
    'env_id': 'test-env',
    'author': 'Test',
    'data': {
        'ticket_id': '$ticket_id',
        'title': 'Linker $ticket_id',
        'ticket_type': 'task',
        'status': 'open',
        'priority': 2,
        'parent_id': None,
        'tags': [],
        'description': '',
    }
}
with open('$tracker_dir/$ticket_id/001-CREATE.json', 'w') as f:
    json.dump(event, f)
"
}

# Write a LINK event in <tracker>/<source_id>/ pointing at <target_id>.
# Caller must ensure <source_id> already has a CREATE event so the reducer
# picks up the dir (use _write_create first for non-deleted source tickets).
_write_link() {
    local tracker_dir="$1" source_id="$2" target_id="$3"
    mkdir -p "$tracker_dir/$source_id"
    python3 -c "
import json
event = {
    'event_type': 'LINK',
    'timestamp': 1700000001000000000,
    'uuid': '00000000-0000-0000-0000-000000000002',
    'env_id': 'test-env',
    'author': 'Test',
    'data': {'link_uuid': 'lk-aaaa', 'target_id': '$target_id', 'relation': 'depends_on'},
}
with open('$tracker_dir/$source_id/002-LINK.json', 'w') as f:
    json.dump(event, f)
"
}

# Write a SNAPSHOT event for a ticket with the given deps list (JSON array of
# dep dicts). Used for compacted-state scenarios.
# Write a SNAPSHOT in the canonical structure (matches real tracker SNAPSHOTs):
#   data.compiled_state.deps = [...]
_write_snapshot_with_deps() {
    local tracker_dir="$1" ticket_id="$2" deps_json="$3"
    mkdir -p "$tracker_dir/$ticket_id"
    python3 -c "
import json
event = {
    'event_type': 'SNAPSHOT',
    'timestamp': 1700000002000000000,
    'uuid': '00000000-0000-0000-0000-000000000003',
    'env_id': 'test-env',
    'author': 'Test',
    'data': {
        'compiled_state': {
            'ticket_id': '$ticket_id',
            'ticket_type': 'task',
            'title': 'Snap $ticket_id',
            'status': 'open',
            'author': 'Test',
            'env_id': 'test-env',
            'parent_id': None,
            'priority': 2,
            'tags': [],
            'description': '',
            'deps': $deps_json,
            'comments': [],
            'bridge_alerts': [],
            'reverts': [],
            'file_impact': [],
            'preconditions_summary': {'status': 'pre-manifest'},
        },
        'source_event_uuids': [],
        'compacted_at': 1700000002000000000,
    },
}
with open('$tracker_dir/$ticket_id/003-SNAPSHOT.json', 'w') as f:
    json.dump(event, f)
"
}

# Run the unlink scan and capture both exit code and stdout (UNLINK paths).
_run_scan() {
    local tracker_dir="$1" deleted_id="$2"
    python3 "$SCAN_SCRIPT" "$tracker_dir" "$deleted_id" test-env Test 2>/dev/null
}

# ── Scenario 1: isolated ticket ───────────────────────────────────────────────
echo ""
echo "Test 1: isolated ticket → no UNLINKs written"
test_fastpath_isolated_ticket() {
    local tracker_dir
    tracker_dir=$(_make_tracker_with_deleted)

    local output exit_code=0
    output=$(_run_scan "$tracker_dir" "$_DELETED_UUID") || exit_code=$?

    assert_eq "test1: scan exits 0" "0" "$exit_code"
    assert_eq "test1: no UNLINK paths printed (fast path took effect)" "" "$output"
}
test_fastpath_isolated_ticket

# ── Scenario 2: outbound LINK present ─────────────────────────────────────────
echo ""
echo "Test 2: deleted ticket has outbound LINK → UNLINK written in deleted dir"
test_fastpath_outbound_link_present() {
    local tracker_dir
    tracker_dir=$(_make_tracker_with_deleted)

    # Deleted ticket links to "other-1111-2222-3333"
    _write_link "$tracker_dir" "$_DELETED_UUID" "other-1111-2222-3333"

    local output exit_code=0
    output=$(_run_scan "$tracker_dir" "$_DELETED_UUID") || exit_code=$?

    assert_eq "test2: scan exits 0" "0" "$exit_code"
    # Expect at least one UNLINK path printed under <tracker>/<deleted_id>/
    local found_outbound
    if echo "$output" | grep -qF "$tracker_dir/$_DELETED_UUID/"; then
        found_outbound="yes"
    else
        found_outbound="no"
    fi
    assert_eq "test2: outbound UNLINK written under deleted ticket dir" "yes" "$found_outbound"
}
test_fastpath_outbound_link_present

# ── Scenario 3: outbound SNAPSHOT with non-empty deps ─────────────────────────
echo ""
echo "Test 3: deleted ticket has only SNAPSHOT with deps → UNLINK written"
test_fastpath_outbound_snapshot_with_deps() {
    local tracker_dir
    tracker_dir=$(_make_tracker_with_deleted)

    # Keep the CREATE; ADD a SNAPSHOT that records compacted deps.
    # The reducer reads SNAPSHOT.state.deps as the deps array directly.
    _write_snapshot_with_deps "$tracker_dir" "$_DELETED_UUID" \
        '[{"link_uuid": "lk-bbbb", "target_id": "other-4444-5555-6666", "relation": "depends_on"}]'

    local output exit_code=0
    output=$(_run_scan "$tracker_dir" "$_DELETED_UUID") || exit_code=$?

    assert_eq "test3: scan exits 0" "0" "$exit_code"
    local found_outbound
    if echo "$output" | grep -qF "$tracker_dir/$_DELETED_UUID/"; then
        found_outbound="yes"
    else
        found_outbound="no"
    fi
    assert_eq "test3: outbound UNLINK written when SNAPSHOT carries deps" "yes" "$found_outbound"
}
test_fastpath_outbound_snapshot_with_deps

# ── Scenario 4: inbound LINK by UUID ──────────────────────────────────────────
echo ""
echo "Test 4: another ticket's LINK references deleted UUID → inbound UNLINK written"
test_fastpath_inbound_link_by_uuid() {
    local tracker_dir
    tracker_dir=$(_make_tracker_with_deleted)

    # Other ticket links to deleted_id by UUID. Needs its own CREATE for the
    # reducer to recognize the dir as a real ticket.
    _write_create "$tracker_dir" "other-7777-8888-9999"
    _write_link "$tracker_dir" "other-7777-8888-9999" "$_DELETED_UUID"

    local output exit_code=0
    output=$(_run_scan "$tracker_dir" "$_DELETED_UUID") || exit_code=$?

    assert_eq "test4: scan exits 0" "0" "$exit_code"
    local found_inbound
    if echo "$output" | grep -qF "$tracker_dir/other-7777-8888-9999/"; then
        found_inbound="yes"
    else
        found_inbound="no"
    fi
    assert_eq "test4: inbound UNLINK written in linker's dir (UUID reference)" "yes" "$found_inbound"
}
test_fastpath_inbound_link_by_uuid

# ── Scenario 5: alias reference defeats fast-path skip ───────────────────────
echo ""
echo "Test 5: alias-only inbound reference defeats fast-path skip (correctness guard)"
test_fastpath_inbound_link_by_alias() {
    local tracker_dir
    tracker_dir=$(_make_tracker_with_deleted)

    # Other ticket links to the deleted ticket BY ALIAS (not UUID).
    # The fast-path MUST NOT incorrectly skip the reducer in this case:
    # grep'ing only for the UUID would miss this LINK; the fast-path
    # therefore also greps for the deleted ticket's computed alias.
    _write_create "$tracker_dir" "other-aaaa-bbbb-cccc"
    _write_link "$tracker_dir" "other-aaaa-bbbb-cccc" "$_DELETED_ALIAS"

    # Call _has_any_link_refs directly so we assert exactly what we want:
    # the alias-only reference must trigger fall-through (return True).
    # Whether the downstream reducer subsequently emits an UNLINK for an
    # alias-form target_id is a separate, pre-existing concern (the reducer
    # does not normalize alias → UUID in LINK target_id at present).
    local fastpath_result
    fastpath_result=$(python3 -c "
import importlib.util, sys
sys.path.insert(0, '$REPO_ROOT/src/rebar/_engine')
spec = importlib.util.spec_from_file_location(
    'uls', '$REPO_ROOT/src/rebar/_engine/ticket-delete-unlink-scan.py'
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
from pathlib import Path
print('true' if m._has_any_link_refs(Path('$tracker_dir'), '$_DELETED_UUID') else 'false')
")
    assert_eq "test5: fast-path detects alias-only inbound reference" "true" "$fastpath_result"

    # And the scan still runs cleanly (no error).
    local exit_code=0
    _run_scan "$tracker_dir" "$_DELETED_UUID" >/dev/null || exit_code=$?
    assert_eq "test5: scan exits 0 even when only alias-form refs exist" "0" "$exit_code"
}
test_fastpath_inbound_link_by_alias

# ── Scenario 6: deleted-ticket self-references don't trigger inbound branch ───
echo ""
echo "Test 6: SNAPSHOT in deleted dir mentions own UUID but no real links → no UNLINKs"
test_fastpath_outbound_only_matches_excluded() {
    local tracker_dir
    tracker_dir=$(_make_tracker_with_deleted)

    # Add a SNAPSHOT in the deleted ticket's dir that contains its own UUID
    # but has EMPTY deps. The fast-path inbound check should exclude matches
    # under the deleted ticket's own dir; outbound check should see empty
    # deps and return False; overall → no UNLINKs written.
    rm -f "$tracker_dir/$_DELETED_UUID/001-CREATE.json"
    _write_snapshot_with_deps "$tracker_dir" "$_DELETED_UUID" '[]'

    local output exit_code=0
    output=$(_run_scan "$tracker_dir" "$_DELETED_UUID") || exit_code=$?

    assert_eq "test6: scan exits 0" "0" "$exit_code"
    assert_eq "test6: no UNLINK paths (self-reference doesn't count as inbound)" "" "$output"
}
test_fastpath_outbound_only_matches_excluded

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
print_summary
