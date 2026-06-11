#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-read-unknown-options.sh
# Regression guard (bug witty-lath-trend): the read arms search/deps must REJECT
# unknown options (e.g. the removed legacy --json) instead of silently ignoring
# them — matching list/show/ready. Sibling of the ready --json fix.
#
# Usage: bash tests/scripts/suites/test-ticket-read-unknown-options.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET="$REPO_ROOT/src/rebar/_engine/ticket"
source "$SCRIPT_DIR/../lib/run_test.sh"

echo "=== test-ticket-read-unknown-options.sh ==="

# Fresh initialized repo + one ticket. Run everything with cwd INSIDE the repo so
# write (create) and read (deps/search) resolve the same tracker (avoid a cwd vs
# REBAR_ROOT split).
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"
git init -q && git config user.email t@e.st && git config user.name t \
  && git commit -q --allow-empty -m init
export _TICKET_TEST_NO_SYNC=1
"$TICKET" init >/dev/null 2>&1
ID=$("$TICKET" create task "unknown-option probe" 2>/dev/null | grep -oE '[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}' | head -1)

# search: removed legacy --json and a bogus flag are rejected (exit 2, message).
run_test "search rejects legacy --json"        2 "unknown option" "$TICKET" search probe --json
run_test "search rejects --bogus-flag"          2 "unknown option" "$TICKET" search probe --bogus-flag
# deps: likewise.
run_test "deps rejects legacy --json"           2 "unknown option" "$TICKET" deps "$ID" --json
run_test "deps rejects --bogus-flag"            2 "unknown option" "$TICKET" deps "$ID" --bogus-flag

# Valid invocations still succeed (no regression).
run_test "search <query> still works"           0 ""               "$TICKET" search probe
run_test "search --status= still works"         0 ""               "$TICKET" search probe --status=open
run_test "deps <id> still works"                0 '"ready_to_work"' "$TICKET" deps "$ID"
run_test "deps --include-archived still works"  0 '"ready_to_work"' "$TICKET" deps "$ID" --include-archived

print_results
