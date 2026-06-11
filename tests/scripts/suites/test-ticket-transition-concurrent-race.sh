#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-transition-concurrent-race.sh
#
# WS2 characterization test for the transition critical section — written BEFORE
# the heredoc→module extraction so the refactor is characterized against it.
#
# The optimistic-concurrency contract: when two clients concurrently transition
# the SAME open ticket to DIFFERENT targets, the single locked critical section
# (lock → reduce+verify → write → commit, all in one process) must guarantee:
#   * EXACTLY ONE winner (exit 0),
#   * the loser is rejected with EXIT 10 (ConcurrencyError), not a generic error,
#   * the store ends with EXACTLY ONE applied STATUS transition (no lost update,
#     no double-apply), and the final status is the winner's target.
#
# This is stronger than test-ticket-transition.sh Test 7 ("at most one
# succeeds") — it pins exit 10 for the loser and the single-transition store
# invariant, which is the property the WS2 extraction must preserve.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-transition-concurrent-race.sh ==="

PASSED=0
FAILED=0

_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# Count applied STATUS event files in a ticket dir (excludes dotfiles).
_status_event_count() {
    local repo="$1" ticket_id="$2"
    local tracker
    tracker="$repo/.tickets-tracker"
    find "$tracker/$ticket_id" -maxdepth 1 -name '*-STATUS.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' '
}

echo "Test 1: concurrent open->in_progress vs open->blocked — exactly one wins, loser exits 10, one transition applied"
test_concurrent_transition_exactly_one_winner() {
    local rounds=3 round
    local all_ok=1
    for round in $(seq 1 "$rounds"); do
        local repo ticket_id
        repo=$(_make_test_repo)
        ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "race round $round" 2>/dev/null | tail -1) || true
        if [ -z "$ticket_id" ]; then
            echo "  SKIP: ticket create failed (round $round)"
            return
        fi

        local outdir
        outdir=$(mktemp -d); _CLEANUP_DIRS+=("$outdir")
        local e1=0 e2=0

        # Launch two transitions from the same starting status concurrently.
        (cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" transition "$ticket_id" open in_progress \
            >"$outdir/o1" 2>"$outdir/e1") & local p1=$!
        (cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" transition "$ticket_id" open blocked \
            >"$outdir/o2" 2>"$outdir/e2") & local p2=$!
        wait "$p1" || e1=$?
        wait "$p2" || e2=$?

        # Exactly one winner (exit 0) and the loser exits 10.
        local winners=0 losers10=0 other=0
        for e in "$e1" "$e2"; do
            case "$e" in
                0) winners=$((winners+1)) ;;
                10) losers10=$((losers10+1)) ;;
                *) other=$((other+1)) ;;
            esac
        done

        local n_status
        n_status=$(_status_event_count "$repo" "$ticket_id")

        # Final status must be the winner's target (in_progress or blocked).
        local final_status
        final_status=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null \
            | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null) || final_status=""

        if [ "$winners" -ne 1 ] || [ "$losers10" -ne 1 ] || [ "$other" -ne 0 ]; then
            echo "  FAIL (round $round): exit codes e1=$e1 e2=$e2 (want exactly one 0 and one 10)"
            echo "    e1 stderr: $(cat "$outdir/e1" 2>/dev/null)"
            echo "    e2 stderr: $(cat "$outdir/e2" 2>/dev/null)"
            all_ok=0
        elif [ "$n_status" -ne 1 ]; then
            echo "  FAIL (round $round): expected exactly 1 applied STATUS event, found $n_status"
            all_ok=0
        elif [ "$final_status" != "in_progress" ] && [ "$final_status" != "blocked" ]; then
            echo "  FAIL (round $round): final status '$final_status' is not the winner's target"
            all_ok=0
        fi
    done

    if [ "$all_ok" -eq 1 ]; then
        echo "  PASS: exactly-one-winner + loser-exit-10 + single-transition held across $rounds rounds"
        PASSED=$((PASSED + 1))
    else
        FAILED=$((FAILED + 1))
    fi
}
test_concurrent_transition_exactly_one_winner

echo ""
printf "PASSED: %d  FAILED: %d\n" "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
