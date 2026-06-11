#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-cache-gitignored.sh
#
# WS5/I3a: the per-ticket reducer cache (.cache.json) is local + rebuildable and
# MUST never be committed — a committed cache would create cross-client merge
# conflicts (violating the Concurrency Doctrine §0 I3/I6). This asserts:
#   1. .cache.json is in the committed tracker .gitignore.
#   2. `git add -A` in the tracker never stages a .cache.json (even one sitting
#      inside a ticket directory alongside real event files).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-cache-gitignored.sh ==="
PASSED=0
FAILED=0

repo=$(mktemp -d); _CLEANUP_DIRS+=("$repo")
clone_ticket_repo "$repo/r" >/dev/null 2>&1 || { echo "  SKIP: fixture failed"; exit 0; }
repo="$repo/r"
tracker=$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$repo/.tickets-tracker")

# Create a ticket so there is a real ticket dir with event files.
tid=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "cache gitignore test" 2>/dev/null | tail -1)

# Test 1: .gitignore on the tickets branch lists .cache.json
if git -C "$tracker" show tickets:.gitignore 2>/dev/null | grep -qFx '.cache.json'; then
    echo "  PASS: .cache.json present in committed tracker .gitignore"; PASSED=$((PASSED+1))
else
    echo "  FAIL: .cache.json NOT in committed tracker .gitignore"; FAILED=$((FAILED+1))
fi

# Test 2: a stray .cache.json (root and inside a ticket dir) is never staged by git add -A
echo '{"stale":true}' > "$tracker/.cache.json"
if [ -n "$tid" ] && [ -d "$tracker/$tid" ]; then
    echo '{"stale":true}' > "$tracker/$tid/.cache.json"
fi
git -C "$tracker" add -A 2>/dev/null || true
staged=$(git -C "$tracker" diff --cached --name-only 2>/dev/null | grep -F '.cache.json' || true)
if [ -z "$staged" ]; then
    echo "  PASS: git add -A did not stage any .cache.json"; PASSED=$((PASSED+1))
else
    echo "  FAIL: git add -A staged: $staged"; FAILED=$((FAILED+1))
fi
git -C "$tracker" reset -q 2>/dev/null || true

echo ""
printf "PASSED: %d  FAILED: %d\n" "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
