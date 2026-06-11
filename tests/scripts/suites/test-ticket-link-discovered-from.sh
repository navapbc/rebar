#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-link-discovered-from.sh
#
# WS5b: `discovered_from` relation (emergent-work provenance, closes beads' gap).
# It is a canonical relation, DIRECTIONAL (no reciprocal LINK), and NEVER
# cycle-inducing (treated like relates_to). Surfaced in deps.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET="$REPO_ROOT/src/rebar/_engine/ticket"
source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-link-discovered-from.sh ==="
PASSED=0; FAILED=0
_ok(){ echo "  PASS: $1"; PASSED=$((PASSED+1)); }
_no(){ echo "  FAIL: $1"; FAILED=$((FAILED+1)); }

repo=$(mktemp -d); _CLEANUP_DIRS+=("$repo")
clone_ticket_repo "$repo/r" >/dev/null 2>&1 || { echo "  SKIP: fixture failed"; exit 0; }
repo="$repo/r"
export _TICKET_TEST_NO_SYNC=1
A=$(cd "$repo" && bash "$TICKET" create task "parent work" 2>/dev/null | tail -1)
B=$(cd "$repo" && bash "$TICKET" create bug "discovered during A" 2>/dev/null | tail -1)
tracker=$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$repo/.tickets-tracker")

# 1: link B discovered_from A succeeds
rc=0; (cd "$repo" && bash "$TICKET" link "$B" "$A" discovered_from >/dev/null 2>/tmp/df.err) || rc=$?
[ "$rc" -eq 0 ] && _ok "link discovered_from succeeds" || { _no "link discovered_from exit=$rc ($(cat /tmp/df.err))"; }

# 2: deps of B surfaces the discovered_from relation to A
deps=$(cd "$repo" && bash "$TICKET" deps "$B" 2>/dev/null)
if echo "$deps" | grep -q "discovered_from" && echo "$deps" | grep -q "$A"; then
    _ok "deps surfaces discovered_from → A"
else
    _no "deps did not surface discovered_from (got: $(echo "$deps" | head -c 200))"
fi

# 3: directional — no reciprocal LINK written in A's dir for discovered_from
recip=$(grep -l "discovered_from" "$tracker/$A"/*-LINK.json 2>/dev/null || true)
[ -z "$recip" ] && _ok "directional: no reciprocal discovered_from LINK in target dir" \
                 || _no "unexpected reciprocal discovered_from LINK in target dir"

# 4: never cycle-inducing — A depends_on B, then B discovered_from A must NOT error
C=$(cd "$repo" && bash "$TICKET" create task "c" 2>/dev/null | tail -1)
D=$(cd "$repo" && bash "$TICKET" create task "d" 2>/dev/null | tail -1)
(cd "$repo" && bash "$TICKET" link "$C" "$D" depends_on >/dev/null 2>&1) || true
rc=0; (cd "$repo" && bash "$TICKET" link "$D" "$C" discovered_from >/dev/null 2>/tmp/df2.err) || rc=$?
[ "$rc" -eq 0 ] && _ok "discovered_from never induces a cycle" \
               || _no "discovered_from wrongly blocked as cycle (exit=$rc: $(cat /tmp/df2.err))"

echo ""
printf "PASSED: %d  FAILED: %d\n" "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
