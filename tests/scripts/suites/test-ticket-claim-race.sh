#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-claim-race.sh
#
# WS5c: atomic `claim`. Two agents concurrently claiming the SAME open ticket
# must resolve via the single locked critical section: EXACTLY ONE wins (exit 0,
# ticket -> in_progress with that claimer's assignee) and the other is rejected
# with EXIT 10 (ConcurrencyError). The store ends with exactly one applied
# STATUS(in_progress) transition.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET="$REPO_ROOT/src/rebar/_engine/ticket"
source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-claim-race.sh ==="
PASSED=0; FAILED=0

_status_count() { find "$1/$2" -maxdepth 1 -name '*-STATUS.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' '; }

echo "Test 1: happy-path claim sets in_progress + assignee atomically"
test_claim_happy() {
    local repo; repo=$(mktemp -d); _CLEANUP_DIRS+=("$repo")
    clone_ticket_repo "$repo/r" >/dev/null 2>&1 || { echo "  SKIP: fixture"; return; }
    repo="$repo/r"; export _TICKET_TEST_NO_SYNC=1
    local id; id=$(cd "$repo" && bash "$TICKET" create task "claimable" 2>/dev/null | tail -1)
    local rc=0; (cd "$repo" && bash "$TICKET" claim "$id" --assignee=alice >/dev/null 2>/tmp/cl.err) || rc=$?
    local st; st=$(cd "$repo" && bash "$TICKET" show "$id" 2>/dev/null | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('status'),d.get('assignee'))" 2>/dev/null)
    if [ "$rc" -eq 0 ] && [ "$st" = "in_progress alice" ]; then
        echo "  PASS: claim -> in_progress + assignee=alice"; PASSED=$((PASSED+1))
    else
        echo "  FAIL: claim rc=$rc state='$st' ($(cat /tmp/cl.err))"; FAILED=$((FAILED+1))
    fi
}
test_claim_happy

echo "Test 2: two concurrent claims — exactly one wins, other exits 10, one transition"
test_claim_race() {
    local rounds=3 round all_ok=1
    for round in $(seq 1 "$rounds"); do
        local repo; repo=$(mktemp -d); _CLEANUP_DIRS+=("$repo")
        clone_ticket_repo "$repo/r" >/dev/null 2>&1 || { echo "  SKIP: fixture"; return; }
        repo="$repo/r"; export _TICKET_TEST_NO_SYNC=1
        local id; id=$(cd "$repo" && bash "$TICKET" create task "race $round" 2>/dev/null | tail -1)
        local tracker; tracker=$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$repo/.tickets-tracker")
        local out; out=$(mktemp -d); _CLEANUP_DIRS+=("$out")
        local e1=0 e2=0
        (cd "$repo" && bash "$TICKET" claim "$id" --assignee=alice >/dev/null 2>"$out/e1") & local p1=$!
        (cd "$repo" && bash "$TICKET" claim "$id" --assignee=bob   >/dev/null 2>"$out/e2") & local p2=$!
        wait "$p1" || e1=$?; wait "$p2" || e2=$?
        local winners=0 losers=0 other=0
        for e in "$e1" "$e2"; do
            case "$e" in 0) winners=$((winners+1));; 10) losers=$((losers+1));; *) other=$((other+1));; esac
        done
        local nstatus; nstatus=$(_status_count "$tracker" "$id")
        local st; st=$(cd "$repo" && bash "$TICKET" show "$id" 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin).get('status'))" 2>/dev/null)
        if [ "$winners" -ne 1 ] || [ "$losers" -ne 1 ] || [ "$other" -ne 0 ]; then
            echo "  FAIL (round $round): exits e1=$e1 e2=$e2 (want one 0 + one 10)"; all_ok=0
        elif [ "$nstatus" -ne 1 ]; then
            echo "  FAIL (round $round): expected 1 STATUS event, found $nstatus"; all_ok=0
        elif [ "$st" != "in_progress" ]; then
            echo "  FAIL (round $round): final status '$st' != in_progress"; all_ok=0
        fi
    done
    if [ "$all_ok" -eq 1 ]; then
        echo "  PASS: exactly-one-winner + loser-exit-10 + single-transition over $rounds rounds"; PASSED=$((PASSED+1))
    else FAILED=$((FAILED+1)); fi
}
test_claim_race

echo ""
printf "PASSED: %d  FAILED: %d\n" "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
