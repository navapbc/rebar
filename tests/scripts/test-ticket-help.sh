#!/usr/bin/env bash
# tests/scripts/test-ticket-help.sh
# Tests for the `--help` / `-h` / `help` interception in the rebar dispatcher.
#
# Contract:
#   - `rebar <sub> --help` (or `-h`) prints that subcommand's usage and exits 0
#     WITHOUT executing the command (no side effects, no _ensure_initialized).
#   - `rebar --help` / `rebar -h` / `rebar help` print the subcommand overview.
#   - `rebar help <sub>` prints that subcommand's usage.
#   - Help is intercepted ONLY when the flag is the FIRST arg after the
#     subcommand; `--help`/`-h`/`help` appearing inside free-text parameters
#     (title, body, search query, ...) must NOT be intercepted.
#
# Usage: bash tests/scripts/test-ticket-help.sh

# NOTE: -e intentionally omitted — assertions return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-help.sh ==="

_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Test 1: `init --help` shows usage and does NOT execute init ───────────────
echo "Test 1: rebar init --help prints usage and does not execute init"
test_init_help_does_not_execute() {
    local out exit_code=0
    out=$(bash "$TICKET_SCRIPT" init --help 2>&1) || exit_code=$?
    assert_eq "init --help exits 0" "0" "$exit_code"
    assert_contains "init --help shows usage" "Usage: rebar init" "$out"
    # The init script prints "initialized" on execution — must be absent.
    assert_not_contains "init --help did not run init" "initialized" "$out"
}
test_init_help_does_not_execute

# ── Test 2: `<sub> --help` and `-h` print that subcommand's usage (exit 0) ────
echo "Test 2: rebar <sub> --help / -h prints subcommand usage"
test_subcommand_help_flag() {
    local out exit_code=0
    out=$(bash "$TICKET_SCRIPT" create --help 2>&1) || exit_code=$?
    assert_eq "create --help exits 0" "0" "$exit_code"
    assert_contains "create --help shows usage" "Usage: rebar create" "$out"

    exit_code=0
    out=$(bash "$TICKET_SCRIPT" transition -h 2>&1) || exit_code=$?
    assert_eq "transition -h exits 0" "0" "$exit_code"
    assert_contains "transition -h shows usage" "Usage: rebar transition" "$out"
}
test_subcommand_help_flag

# ── Test 3: top-level help forms print the overview ──────────────────────────
echo "Test 3: rebar --help / -h / help print the subcommand overview"
test_top_level_help() {
    local out exit_code
    for form in "--help" "-h" "help"; do
        exit_code=0
        out=$(bash "$TICKET_SCRIPT" $form 2>&1) || exit_code=$?
        assert_eq "rebar $form exits 0" "0" "$exit_code"
        assert_contains "rebar $form lists subcommands" "Subcommands:" "$out"
    done
}
test_top_level_help

# ── Test 4: `help <sub>` prints that subcommand's usage ──────────────────────
echo "Test 4: rebar help <sub> prints the subcommand usage"
test_help_subcommand_word() {
    local out exit_code=0
    out=$(bash "$TICKET_SCRIPT" help link 2>&1) || exit_code=$?
    assert_eq "help link exits 0" "0" "$exit_code"
    assert_contains "help link shows link usage" "Usage: rebar link" "$out"
}
test_help_subcommand_word

# ── Test 5: unknown subcommand help is an error (non-zero) ────────────────────
echo "Test 5: rebar <unknown> --help reports unknown subcommand (non-zero)"
test_unknown_subcommand_help() {
    local out exit_code=0
    out=$(bash "$TICKET_SCRIPT" frobnicate --help 2>&1) || exit_code=$?
    assert_ne "unknown --help exits non-zero" "0" "$exit_code"
    assert_contains "unknown --help names the bad subcommand" "unknown subcommand" "$out"
}
test_unknown_subcommand_help

# ── Test 6: --help inside a FREE-TEXT title is NOT intercepted ────────────────
# `create <type> <title>`: the title is at position 2, so a title containing
# "--help" must be created normally, not trigger help.
echo "Test 6: --help inside a ticket title is not intercepted (ticket is created)"
test_help_in_title_not_intercepted() {
    local repo
    repo=$(_make_test_repo)

    local before after id
    before=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null | python3 -c "import json,sys;print(len(json.load(sys.stdin)))")
    id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "please pass --help to the parser" 2>/dev/null | tail -1)
    after=$(cd "$repo" && bash "$TICKET_SCRIPT" list 2>/dev/null | python3 -c "import json,sys;print(len(json.load(sys.stdin)))")

    assert_eq "ticket created despite --help in title" "$((before + 1))" "$after"
    # And the stored title retains the literal --help text.
    local title
    title=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$id" 2>/dev/null \
        | python3 -c "import json,sys;print(json.load(sys.stdin).get('title',''))")
    assert_contains "stored title keeps --help text" "--help" "$title"
}
test_help_in_title_not_intercepted

# ── Test 7: `-h`/`help` as a free-text value (position >= 2) is not intercepted ─
# `create task <title>` with the title being literally "help" must create a
# ticket titled "help" (the bare word `help` is only honored at top level).
echo "Test 7: a bare 'help' title is created, not treated as a help request"
test_help_word_as_title_not_intercepted() {
    local repo
    repo=$(_make_test_repo)
    local id title exit_code=0
    id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "help" 2>/dev/null | tail -1) || exit_code=$?
    assert_ne "create task 'help' produced an id" "" "$id"
    title=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$id" 2>/dev/null \
        | python3 -c "import json,sys;print(json.load(sys.stdin).get('title',''))")
    assert_eq "title is the literal word 'help'" "help" "$title"
}
test_help_word_as_title_not_intercepted

# ── Test 8: transition help names the opt-in signature gate config key ────────
# BUG f406-49ba: the help wording overstated the story/epic close requirement as
# unconditional. The gate is OPT-IN (off by default) and only fires when
# verify.require_signature_for_close=true in .rebar/config.conf (the close gate
# now uses signatures, not the deprecated verdict hash). The help must NAME the key.
echo "Test 8: rebar help transition names verify.require_signature_for_close (opt-in gate)"
test_transition_help_names_verdict_config_key() {
    local out exit_code=0
    out=$(bash "$TICKET_SCRIPT" help transition 2>&1) || exit_code=$?
    assert_eq "help transition exits 0" "0" "$exit_code"
    assert_contains "help transition names the config key" "require_signature_for_close" "$out"
}
test_transition_help_names_verdict_config_key

print_summary
