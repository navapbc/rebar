#!/usr/bin/env bash
# tests/scripts/test-ticket-compact-all.sh
# Behavioral tests for ticket-compact-all.sh — SNAPSHOT backfill utility.
#
# Tests:
#   1. Tickets without SNAPSHOTs are compacted
#   2. Tickets already with SNAPSHOTs are skipped (idempotent)
#   3. --dry-run writes nothing
#   4. --limit=N stops after N tickets
#   5. --no-commit skips git commit
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
COMPACT_ALL="$REPO_ROOT/src/rebar/_engine/ticket-compact-all.sh"
COMPACT="$REPO_ROOT/src/rebar/_engine/ticket-compact.sh"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-compact-all.sh ==="
echo ""

_CLEANUP_DIRS=()
_cleanup() { rm -rf "${_CLEANUP_DIRS[@]}" 2>/dev/null || true; }
trap _cleanup EXIT

_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

_create_ticket() {
    local repo="$1" title="${2:-Test bug}"
    cd "$repo" && bash "$TICKET_SCRIPT" create bug "$title" -d "test" 2>/dev/null | grep -o '[a-f0-9]\{4\}-[a-f0-9]\{4\}-[a-f0-9]\{4\}-[a-f0-9]\{4\}'
}

_snap_count() {
    local repo="$1"
    find "$repo/.tickets-tracker" -name '*-SNAPSHOT.json' 2>/dev/null | wc -l | tr -d ' '
}

# ===========================================================================
# test_compacts_tickets_without_snapshots
#
# Given: two tickets without SNAPSHOT files
# When: ticket-compact-all.sh runs
# Then: both tickets get a *-SNAPSHOT.json file
# ===========================================================================
test_compacts_tickets_without_snapshots() {
    local repo
    repo=$(_make_test_repo)

    local id1 id2
    id1=$(_create_ticket "$repo" "Bug one")
    id2=$(_create_ticket "$repo" "Bug two")

    assert_eq "before: no SNAPSHOTs" "0" "$(_snap_count "$repo")"

    cd "$repo" && TICKET_SYNC_CMD="true" bash "$COMPACT_ALL" --no-commit >/dev/null 2>&1

    assert_eq "test_compacts_tickets_without_snapshots: SNAPSHOTs created" "2" "$(_snap_count "$repo")"
}

# ===========================================================================
# test_skips_tickets_with_existing_snapshots
#
# Given: one ticket with SNAPSHOT (pre-compacted), one without
# When: ticket-compact-all.sh runs
# Then: total SNAPSHOT count reaches 2 (one new, one pre-existing)
# ===========================================================================
test_skips_tickets_with_existing_snapshots() {
    local repo
    repo=$(_make_test_repo)

    local id1 id2
    id1=$(_create_ticket "$repo" "Already compacted")
    id2=$(_create_ticket "$repo" "Needs compaction")

    # Pre-compact id1
    cd "$repo" && TICKET_SYNC_CMD="true" bash "$COMPACT" "$id1" \
        --threshold=0 --skip-sync >/dev/null 2>&1

    local snap_before
    snap_before=$(_snap_count "$repo")

    # Run backfill — should only add one more SNAPSHOT
    cd "$repo" && TICKET_SYNC_CMD="true" bash "$COMPACT_ALL" --no-commit >/dev/null 2>&1

    assert_eq "test_skips_tickets_with_existing_snapshots: total SNAPSHOTs = 2" "2" "$(_snap_count "$repo")"
}

# ===========================================================================
# test_dry_run_writes_nothing
#
# Given: two tickets without SNAPSHOTs
# When: --dry-run is passed
# Then: no SNAPSHOT files written, exit 0
# ===========================================================================
test_dry_run_writes_nothing() {
    local repo
    repo=$(_make_test_repo)

    _create_ticket "$repo" "Dry run one" >/dev/null
    _create_ticket "$repo" "Dry run two" >/dev/null

    cd "$repo" && TICKET_SYNC_CMD="true" bash "$COMPACT_ALL" --dry-run >/dev/null 2>&1

    assert_eq "test_dry_run_writes_nothing: no SNAPSHOTs written" "0" "$(_snap_count "$repo")"
}

# ===========================================================================
# test_limit_stops_early
#
# Given: three tickets without SNAPSHOTs
# When: --limit=2
# Then: exactly 2 tickets get SNAPSHOTs
# ===========================================================================
test_limit_stops_early() {
    local repo
    repo=$(_make_test_repo)

    _create_ticket "$repo" "Limit one"   >/dev/null
    _create_ticket "$repo" "Limit two"   >/dev/null
    _create_ticket "$repo" "Limit three" >/dev/null

    cd "$repo" && TICKET_SYNC_CMD="true" bash "$COMPACT_ALL" --limit=2 --no-commit >/dev/null 2>&1

    assert_eq "test_limit_stops_early: exactly 2 SNAPSHOTs" "2" "$(_snap_count "$repo")"
}

# ===========================================================================
# test_no_commit_skips_git_commit
#
# Given: one ticket without SNAPSHOT
# When: --no-commit
# Then: SNAPSHOT on disk; tracker has no new commits beyond before state
# ===========================================================================
test_no_commit_skips_git_commit() {
    local repo
    repo=$(_make_test_repo)

    _create_ticket "$repo" "No-commit test" >/dev/null

    local commits_before
    commits_before=$(git -C "$repo/.tickets-tracker" rev-list --count HEAD 2>/dev/null || echo 0)

    cd "$repo" && TICKET_SYNC_CMD="true" bash "$COMPACT_ALL" --no-commit >/dev/null 2>&1

    local commits_after
    commits_after=$(git -C "$repo/.tickets-tracker" rev-list --count HEAD 2>/dev/null || echo 0)

    assert_eq "test_no_commit_skips_git_commit: SNAPSHOT on disk" "1" "$(_snap_count "$repo")"
    assert_eq "test_no_commit_skips_git_commit: no new git commit" "$commits_before" "$commits_after"
}

# ── Run all tests ─────────────────────────────────────────────────────────────
test_compacts_tickets_without_snapshots
test_skips_tickets_with_existing_snapshots
test_dry_run_writes_nothing
test_limit_stops_early
test_no_commit_skips_git_commit

print_summary
