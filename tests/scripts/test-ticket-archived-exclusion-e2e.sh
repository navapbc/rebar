#!/usr/bin/env bash
# tests/scripts/test-ticket-archived-exclusion-e2e.sh
# E2E integration test for the full archived exclusion workflow.
#
# Exercises the cross-command consistency of the archived exclusion feature by
# verifying that ticket list and ticket deps behave correctly with respect to
# archived tickets — both individually and when archived tickets appear mid-chain
# in a dependency graph.
#
# Coverage:
#   1. ticket list  default excludes archived tickets
#   2. ticket list --include-archived restores archived tickets
#   3. ticket deps on a non-archived ticket excludes archived deps from output
#   4. ticket deps on an archived ticket directly exits 1 with error message
#   5. ticket deps --include-archived on an archived ticket exits 0 with full graph
#   6. deps traversal: cycle detection still works through archived mid-chain tickets
#
# Usage: bash tests/scripts/test-ticket-archived-exclusion-e2e.sh
# Returns: 0 if all tests pass, 1 if any fail.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-archived-exclusion-e2e.sh ==="

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

# ── Helper: write CREATE + ARCHIVED events directly for a given ticket ID ─────
# This injects an archived ticket without going through a (not-yet-existing)
# `ticket archive` subcommand.
_inject_archived_ticket() {
    local tracker_dir="$1"
    local ticket_id="$2"
    local title="${3:-Archived ticket}"
    local uuid_prefix="${4:-e2e-arch}"

    mkdir -p "$tracker_dir/$ticket_id"
    python3 - "$tracker_dir/$ticket_id" "$title" "$uuid_prefix" <<'PYEOF'
import json, time, sys
base, title, uuid_prefix = sys.argv[1], sys.argv[2], sys.argv[3]
ts = int(time.time())
create_event = {
    "timestamp": ts,
    "uuid": f"{uuid_prefix}-CREATE-0001",
    "event_type": "CREATE",
    "env_id": "test-env",
    "author": "test-author",
    "data": {
        "ticket_type": "task",
        "title": title,
        "status": "open",
        "priority": 2,
        "assignee": "test-author"
    }
}
archive_event = {
    "timestamp": ts + 1,
    "uuid": f"{uuid_prefix}-ARCHIVED-0002",
    "event_type": "ARCHIVED",
    "env_id": "test-env",
    "author": "test-author",
    "data": {}
}
with open(f"{base}/0000000001-{uuid_prefix}-CREATE.json", "w") as f:
    json.dump(create_event, f)
with open(f"{base}/0000000002-{uuid_prefix}-ARCHIVED.json", "w") as f:
    json.dump(archive_event, f)
PYEOF
    git -C "$tracker_dir" add "$ticket_id/" 2>/dev/null
    git -C "$tracker_dir" commit -q -m "test: inject archived ticket $ticket_id" 2>/dev/null || true
}

# ── Test 1: ticket list default excludes archived tickets ─────────────────────
echo "Test 1: ticket list default excludes archived ticket, shows open tickets"
test_e2e_list_default_excludes_archived() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create 2 open tickets
    local open1 open2
    open1=$(_create_ticket "$repo" task "E2E open ticket alpha")
    open2=$(_create_ticket "$repo" task "E2E open ticket beta")

    if [ -z "$open1" ] || [ -z "$open2" ]; then
        assert_eq "both open tickets created for test 1" "non-empty" "empty"
        assert_pass_if_clean "test_e2e_list_default_excludes_archived"
        return
    fi

    # Inject 1 archived ticket
    local arch_id="e2e-arch-t1"
    _inject_archived_ticket "$tracker_dir" "$arch_id" "Archived for E2E test 1" "t1arch"

    local list_output exit_code=0
    list_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null) || exit_code=$?

    assert_eq "test 1: ticket list exits 0" "0" "$exit_code"

    local check_result
    check_result=$(python3 - "$list_output" "$open1" "$open2" "$arch_id" <<'PYEOF'
import json, sys
try:
    tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)
open1, open2, arch_id = sys.argv[2], sys.argv[3], sys.argv[4]
if not isinstance(tickets, list):
    print(f"NOT_ARRAY:{type(tickets).__name__}")
    sys.exit(2)
ticket_ids = [t.get("ticket_id") for t in tickets if isinstance(t, dict)]
errors = []
if open1 not in ticket_ids:
    errors.append(f"open ticket {open1!r} missing from default list")
if open2 not in ticket_ids:
    errors.append(f"open ticket {open2!r} missing from default list")
if arch_id in ticket_ids:
    errors.append(f"archived ticket {arch_id!r} present in default list (should be excluded)")
print("ERRORS:" + "; ".join(errors) if errors else "OK")
PYEOF
    ) || true

    assert_eq "test 1: default list excludes archived, shows open" "OK" "$check_result"
    assert_pass_if_clean "test_e2e_list_default_excludes_archived"
}
test_e2e_list_default_excludes_archived

# ── Test 2: ticket list --include-archived restores archived tickets ──────────
echo "Test 2: ticket list --include-archived returns all tickets including archived"
test_e2e_list_include_archived_flag() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local open1 open2
    open1=$(_create_ticket "$repo" task "E2E include-archived open one")
    open2=$(_create_ticket "$repo" task "E2E include-archived open two")

    if [ -z "$open1" ] || [ -z "$open2" ]; then
        assert_eq "both open tickets created for test 2" "non-empty" "empty"
        assert_pass_if_clean "test_e2e_list_include_archived_flag"
        return
    fi

    local arch_id="e2e-arch-t2"
    _inject_archived_ticket "$tracker_dir" "$arch_id" "Archived for E2E test 2" "t2arch"

    local list_output exit_code=0
    list_output=$(cd "$repo" && bash "$TICKET_SCRIPT" list --include-archived 2>/dev/null) || exit_code=$?

    assert_eq "test 2: --include-archived exits 0" "0" "$exit_code"

    local check_result
    check_result=$(python3 - "$list_output" "$open1" "$open2" "$arch_id" <<'PYEOF'
import json, sys
try:
    tickets = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)
open1, open2, arch_id = sys.argv[2], sys.argv[3], sys.argv[4]
if not isinstance(tickets, list):
    print(f"NOT_ARRAY:{type(tickets).__name__}")
    sys.exit(2)
ticket_ids = [t.get("ticket_id") for t in tickets if isinstance(t, dict)]
errors = []
if open1 not in ticket_ids:
    errors.append(f"open ticket {open1!r} missing from --include-archived list")
if open2 not in ticket_ids:
    errors.append(f"open ticket {open2!r} missing from --include-archived list")
if arch_id not in ticket_ids:
    errors.append(f"archived ticket {arch_id!r} missing from --include-archived list")
# Verify archived flag is set on the archived ticket
arch_ticket = next((t for t in tickets if isinstance(t, dict) and t.get("ticket_id") == arch_id), None)
if arch_ticket and not arch_ticket.get("archived"):
    errors.append(f"archived ticket {arch_id!r} missing archived=true flag in output")
print("ERRORS:" + "; ".join(errors) if errors else "OK")
PYEOF
    ) || true

    assert_eq "test 2: --include-archived shows all 3 tickets with archived flag" "OK" "$check_result"
    assert_pass_if_clean "test_e2e_list_include_archived_flag"
}
test_e2e_list_include_archived_flag

# ── Test 3: ticket deps on a non-archived ticket excludes archived deps ────────
echo "Test 3: ticket deps on non-archived ticket excludes archived blockers from output"
test_e2e_deps_excludes_archived_blockers() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create the target ticket (not archived)
    local target_id open_blocker_id
    target_id=$(_create_ticket "$repo" task "E2E deps target ticket")
    open_blocker_id=$(_create_ticket "$repo" task "E2E open blocker for target")

    if [ -z "$target_id" ] || [ -z "$open_blocker_id" ]; then
        assert_eq "target and open blocker tickets created for test 3" "non-empty" "empty"
        assert_pass_if_clean "test_e2e_deps_excludes_archived_blockers"
        return
    fi

    # Link open blocker → target
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$open_blocker_id" "$target_id" blocks >/dev/null 2>/dev/null) || true

    # Inject an archived ticket and link it as a blocker for target
    local arch_blocker_id="e2e-arch-blk"
    _inject_archived_ticket "$tracker_dir" "$arch_blocker_id" "Archived blocker for E2E test 3" "t3arch"
    # Link archived ticket → target (add LINK event directly since archived ticket
    # can't use the CLI after archiving — inject via tracker)
    python3 - "$tracker_dir/$arch_blocker_id" "$arch_blocker_id" "$target_id" <<'PYEOF'
import json, time, sys
base, src_id, tgt_id = sys.argv[1], sys.argv[2], sys.argv[3]
ts = int(time.time())
link_event = {
    "timestamp": ts + 2,
    "uuid": "t3arch-LINK-0003",
    "event_type": "LINK",
    "env_id": "test-env",
    "author": "test-author",
    "data": {
        "source_id": src_id,
        "target_id": tgt_id,
        "relation": "blocks",
        "link_uuid": "t3arch-link-uuid-0001"
    }
}
with open(f"{base}/0000000003-t3arch-LINK.json", "w") as f:
    json.dump(link_event, f)
PYEOF
    git -C "$tracker_dir" add "$arch_blocker_id/" 2>/dev/null
    git -C "$tracker_dir" commit -q -m "test: add link from archived blocker to target" 2>/dev/null || true

    local deps_output exit_code=0
    deps_output=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$target_id" 2>/dev/null) || exit_code=$?

    assert_eq "test 3: ticket deps exits 0 for non-archived target" "0" "$exit_code"

    local check_result
    check_result=$(python3 - "$deps_output" "$open_blocker_id" "$arch_blocker_id" <<'PYEOF'
import json, sys
try:
    data = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)
open_blocker_id, arch_blocker_id = sys.argv[2], sys.argv[3]
blockers = data.get("blockers", [])
errors = []
if open_blocker_id not in blockers:
    errors.append(f"open blocker {open_blocker_id!r} missing from blockers list")
if arch_blocker_id in blockers:
    errors.append(f"archived blocker {arch_blocker_id!r} present in default deps output (should be excluded)")
print("ERRORS:" + "; ".join(errors) if errors else "OK")
PYEOF
    ) || true

    assert_eq "test 3: deps excludes archived blocker, keeps open blocker" "OK" "$check_result"
    assert_pass_if_clean "test_e2e_deps_excludes_archived_blockers"
}
test_e2e_deps_excludes_archived_blockers

# ── Test 4: ticket deps on an archived ticket exits 1 with error message ──────
echo "Test 4: ticket deps on archived ticket exits 1 with 'archived' error message"
test_e2e_deps_archived_target_exits_error() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local arch_id="e2e-arch-t4"
    _inject_archived_ticket "$tracker_dir" "$arch_id" "Archived for E2E test 4" "t4arch"

    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$arch_id" 2>&1 >/dev/null) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "test 4: ticket deps on archived ticket exits non-zero" "1" \
        "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error message mentions 'archived' and hints at --include-archived
    if [[ "${stderr_out,,}" == *"archived"* ]]; then
        assert_eq "test 4: error message mentions 'archived'" "found" "found"
    else
        assert_eq "test 4: error message mentions 'archived'" "found" "not found: $stderr_out"
    fi

    if [[ "$stderr_out" == *"include-archived"* ]]; then
        assert_eq "test 4: error message hints at --include-archived" "found" "found"
    else
        assert_eq "test 4: error message hints at --include-archived" "found" "not found: $stderr_out"
    fi

    assert_pass_if_clean "test_e2e_deps_archived_target_exits_error"
}
test_e2e_deps_archived_target_exits_error

# ── Test 5: ticket deps --include-archived on archived ticket exits 0 ─────────
echo "Test 5: ticket deps --include-archived on archived ticket exits 0 with full graph"
test_e2e_deps_include_archived_on_archived_ticket() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Create a dependency: open_dep → arch_id (arch_id depends on open_dep)
    local open_dep_id
    open_dep_id=$(_create_ticket "$repo" task "E2E open dep for archived target")

    if [ -z "$open_dep_id" ]; then
        assert_eq "open dep ticket created for test 5" "non-empty" "empty"
        assert_pass_if_clean "test_e2e_deps_include_archived_on_archived_ticket"
        return
    fi

    local arch_id="e2e-arch-t5"
    _inject_archived_ticket "$tracker_dir" "$arch_id" "Archived target for E2E test 5" "t5arch"

    # Add a LINK event on the archived ticket: arch_id depends_on open_dep_id
    python3 - "$tracker_dir/$arch_id" "$arch_id" "$open_dep_id" <<'PYEOF'
import json, time, sys
base, src_id, tgt_id = sys.argv[1], sys.argv[2], sys.argv[3]
ts = int(time.time())
link_event = {
    "timestamp": ts + 2,
    "uuid": "t5arch-LINK-0003",
    "event_type": "LINK",
    "env_id": "test-env",
    "author": "test-author",
    "data": {
        "source_id": src_id,
        "target_id": tgt_id,
        "relation": "depends_on",
        "link_uuid": "t5arch-link-uuid-0001"
    }
}
with open(f"{base}/0000000003-t5arch-LINK.json", "w") as f:
    json.dump(link_event, f)
PYEOF
    git -C "$tracker_dir" add "$arch_id/" 2>/dev/null
    git -C "$tracker_dir" commit -q -m "test: add depends_on link on archived ticket" 2>/dev/null || true

    local deps_output exit_code=0
    deps_output=$(cd "$repo" && bash "$TICKET_SCRIPT" deps --include-archived "$arch_id" 2>/dev/null) || exit_code=$?

    assert_eq "test 5: ticket deps --include-archived exits 0" "0" "$exit_code"

    local check_result
    check_result=$(python3 - "$deps_output" "$arch_id" "$open_dep_id" <<'PYEOF'
import json, sys
try:
    data = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)
arch_id, open_dep_id = sys.argv[2], sys.argv[3]
errors = []
if data.get("ticket_id") != arch_id:
    errors.append(f"ticket_id mismatch: expected {arch_id!r}, got {data.get('ticket_id')!r}")
# deps is a list of objects with target_id or bare strings — check both forms
raw_deps = data.get("deps", [])
dep_ids = []
for d in raw_deps:
    if isinstance(d, dict):
        dep_ids.append(d.get("target_id", ""))
    elif isinstance(d, str):
        dep_ids.append(d)
if open_dep_id not in dep_ids:
    errors.append(f"dep {open_dep_id!r} missing from deps list: {raw_deps!r}")
print("ERRORS:" + "; ".join(errors) if errors else "OK")
PYEOF
    ) || true

    assert_eq "test 5: --include-archived on archived ticket returns full graph with deps" "OK" "$check_result"
    assert_pass_if_clean "test_e2e_deps_include_archived_on_archived_ticket"
}
test_e2e_deps_include_archived_on_archived_ticket

# ── Test 6: deps traversal through archived mid-chain ticket ──────────────────
# Verifies that cycle detection (build_dep_graph internals) still works when an
# archived ticket sits in the middle of a dependency chain. The archived ticket
# should be invisible in default traversal, but downstream tickets should still
# resolve correctly.
echo "Test 6: deps traversal through archived mid-chain — downstream ticket resolves correctly"
test_e2e_deps_traversal_through_archived_midchain() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    # Chain: upstream_id → (arch_mid) → downstream_id
    # upstream blocks arch_mid, arch_mid blocks downstream
    # When querying downstream without --include-archived, only upstream (via
    # archived mid-chain) should be invisible; downstream should still resolve.

    local upstream_id downstream_id
    upstream_id=$(_create_ticket "$repo" task "E2E upstream open ticket")
    downstream_id=$(_create_ticket "$repo" task "E2E downstream open ticket")

    if [ -z "$upstream_id" ] || [ -z "$downstream_id" ]; then
        assert_eq "upstream and downstream tickets created for test 6" "non-empty" "empty"
        assert_pass_if_clean "test_e2e_deps_traversal_through_archived_midchain"
        return
    fi

    # Inject archived mid-chain ticket
    local arch_mid_id="e2e-arch-mid"
    _inject_archived_ticket "$tracker_dir" "$arch_mid_id" "Archived mid-chain for E2E test 6" "t6arch"

    # Link: upstream blocks arch_mid
    (cd "$repo" && bash "$TICKET_SCRIPT" link "$upstream_id" "$arch_mid_id" blocks >/dev/null 2>/dev/null) || true

    # Link: arch_mid blocks downstream (inject LINK event on archived ticket)
    python3 - "$tracker_dir/$arch_mid_id" "$arch_mid_id" "$downstream_id" <<'PYEOF'
import json, time, sys
base, src_id, tgt_id = sys.argv[1], sys.argv[2], sys.argv[3]
ts = int(time.time())
link_event = {
    "timestamp": ts + 3,
    "uuid": "t6arch-LINK-0004",
    "event_type": "LINK",
    "env_id": "test-env",
    "author": "test-author",
    "data": {
        "source_id": src_id,
        "target_id": tgt_id,
        "relation": "blocks",
        "link_uuid": "t6arch-link-uuid-0002"
    }
}
with open(f"{base}/0000000004-t6arch-LINK.json", "w") as f:
    json.dump(link_event, f)
PYEOF
    git -C "$tracker_dir" add "$arch_mid_id/" 2>/dev/null
    git -C "$tracker_dir" commit -q -m "test: add mid-chain archived blocks link" 2>/dev/null || true

    # Query downstream without --include-archived — should succeed (archived mid
    # is invisible but the command should not crash)
    local deps_output exit_code=0
    deps_output=$(cd "$repo" && bash "$TICKET_SCRIPT" deps "$downstream_id" 2>/dev/null) || exit_code=$?

    assert_eq "test 6: ticket deps downstream exits 0 with archived mid-chain" "0" "$exit_code"

    # Downstream should have no visible blockers (archived mid is excluded)
    local check_result
    check_result=$(python3 - "$deps_output" "$downstream_id" "$arch_mid_id" <<'PYEOF'
import json, sys
try:
    data = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)
downstream_id, arch_mid_id = sys.argv[2], sys.argv[3]
errors = []
if data.get("ticket_id") != downstream_id:
    errors.append(f"ticket_id mismatch: expected {downstream_id!r}, got {data.get('ticket_id')!r}")
blockers = data.get("blockers", [])
if arch_mid_id in blockers:
    errors.append(f"archived mid-chain ticket {arch_mid_id!r} visible in blockers (should be excluded)")
# ready_to_work should be True since mid-chain is archived and thus invisible
if data.get("ready_to_work") is not True:
    errors.append(f"ready_to_work should be True (archived mid-chain excluded), got {data.get('ready_to_work')!r}")
print("ERRORS:" + "; ".join(errors) if errors else "OK")
PYEOF
    ) || true

    assert_eq "test 6: downstream deps resolve correctly with archived mid-chain excluded" "OK" "$check_result"

    # Also verify with --include-archived: archived mid should be visible as a blocker
    local deps_full_output full_exit_code=0
    deps_full_output=$(cd "$repo" && bash "$TICKET_SCRIPT" deps --include-archived "$downstream_id" 2>/dev/null) || full_exit_code=$?

    assert_eq "test 6: ticket deps --include-archived downstream exits 0" "0" "$full_exit_code"

    local full_check
    full_check=$(python3 - "$deps_full_output" "$arch_mid_id" <<'PYEOF'
import json, sys
try:
    data = json.loads(sys.argv[1])
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)
arch_mid_id = sys.argv[2]
blockers = data.get("blockers", [])
if arch_mid_id not in blockers:
    print(f"archived mid {arch_mid_id!r} missing from blockers with --include-archived: {blockers!r}")
else:
    print("OK")
PYEOF
    ) || true

    assert_eq "test 6: --include-archived reveals archived mid-chain as blocker" "OK" "$full_check"

    assert_pass_if_clean "test_e2e_deps_traversal_through_archived_midchain"
}
test_e2e_deps_traversal_through_archived_midchain

# ── Summary ───────────────────────────────────────────────────────────────────
print_summary
