#!/usr/bin/env bash
# tests/scripts/test-ticket-reopen.sh
# Tests for the `rebar reopen` dispatcher arm.
#
# Contract:
#   - `rebar reopen` with NO ticket id prints the reopen usage and exits 1
#     (the sibling-command Usage contract), WITHOUT a raw bash 'unbound
#     variable' diagnostic. This also holds when only flags are supplied
#     (e.g. `rebar reopen --output json`), since the arity guard runs after
#     output-flag stripping.
#   - `rebar reopen <closed-id>` still reopens a closed ticket (happy path).
#
# Usage: bash tests/scripts/test-ticket-reopen.sh

# NOTE: -e intentionally omitted — assertions return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-reopen.sh ==="

_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Test 1: `rebar reopen` (no args) prints usage / exit 1 / no bash diag ─────
echo "Test 1: rebar reopen (no args) prints usage and exits 1 with no bash diagnostic"
test_reopen_no_args() {
    local out exit_code=0 repo
    # Run inside an isolated ticket repo: `reopen` with no args still resolves the
    # tracker (auto-init) before the usage check, so without this the tracker lands
    # in REPO_ROOT (the `.tickets-tracker` leak the repo-root guard fails on).
    repo=$(_make_test_repo)
    out=$(cd "$repo" && bash "$TICKET_SCRIPT" reopen 2>&1) || exit_code=$?
    assert_eq "reopen no-args exits 1" "1" "$exit_code"
    assert_contains "reopen no-args shows usage" "Usage: rebar reopen" "$out"
    assert_not_contains "reopen no-args has no bash unbound-variable diagnostic" "unbound variable" "$out"
}
test_reopen_no_args

# ── Test 2: `rebar reopen --output json` (flags only) also guarded ────────────
echo "Test 2: rebar reopen --output json (flags only) prints usage and exits 1"
test_reopen_flags_only() {
    local out exit_code=0 repo
    repo=$(_make_test_repo)
    out=$(cd "$repo" && bash "$TICKET_SCRIPT" reopen --output json 2>&1) || exit_code=$?
    assert_eq "reopen flags-only exits 1" "1" "$exit_code"
    assert_contains "reopen flags-only shows usage" "Usage: rebar reopen" "$out"
    assert_not_contains "reopen flags-only has no bash unbound-variable diagnostic" "unbound variable" "$out"
}
test_reopen_flags_only

# ── Test 3: happy path — create -> close -> reopen ────────────────────────────
echo "Test 3: rebar reopen <closed-id> reopens a closed ticket"
test_reopen_happy_path() {
    local repo id status exit_code=0
    repo=$(_make_test_repo)
    id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "reopen me" 2>/dev/null | tail -1)
    assert_ne "created an id" "" "$id"

    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$id" open closed >/dev/null 2>&1)
    status=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$id" 2>/dev/null \
        | python3 -c "import json,sys;print(json.load(sys.stdin).get('status',''))")
    assert_eq "ticket is closed before reopen" "closed" "$status"

    out=$(cd "$repo" && bash "$TICKET_SCRIPT" reopen "$id" 2>&1) || exit_code=$?
    assert_eq "reopen happy path exits 0" "0" "$exit_code"
    status=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$id" 2>/dev/null \
        | python3 -c "import json,sys;print(json.load(sys.stdin).get('status',''))")
    assert_eq "ticket is open after reopen" "open" "$status"
}
test_reopen_happy_path

print_summary
