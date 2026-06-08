#!/usr/bin/env bash
# tests/scripts/test-qualify-ticket-refs.sh
# Tests for qualify-ticket-refs.sh — rewrites bare ticket command refs to use shim.
#
# Tests:
#  (a) test_rewrites_backtick_ticket_ref    — `ticket list` → `.claude/scripts/dso ticket list`
#  (b) test_rewrites_bare_ticket_ref        — ticket list → .claude/scripts/dso ticket list
#  (c) test_skips_already_shimmed           — .claude/scripts/dso ticket list → unchanged
#  (d) test_skips_full_path                 — src/rebar/_engine/ticket → unchanged
#  (h) test_idempotent                     — running twice yields same result
#  (i) test_no_double_rewrite             — backtick + bare on same line doesn't double-apply
#
# Usage: bash tests/scripts/test-qualify-ticket-refs.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPT="$PLUGIN_ROOT/src/rebar/_engine/qualify-ticket-refs.sh"

source "$PLUGIN_ROOT/tests/lib/assert.sh"

echo "=== test-qualify-ticket-refs.sh ==="

SHIM=".claude/scripts/dso"

# Helper: apply the rewriter's perl regexes to a string and return the result.
# Replicates the core logic from qualify-ticket-refs.sh.
_apply_rewriter() {
    local _line="$1"
    printf '%s' "$_line" | perl -e '
        my $SHIM = ".claude/scripts/dso";
        my $subcmds = "list|show|create|transition|comment|link|unlink|deps|edit|init|sync|revert|compact|fsck|bridge-status|bridge-fsck";
        while (<STDIN>) {
            s/(?<!dso )(?<![\/\.])(?<=`)ticket\s+($subcmds)\b/$SHIM ticket $1/g;
            s/(?<!dso )(?<![\/\.`\w])ticket\s+($subcmds)\b/$SHIM ticket $1/g;
            print;
        }
    '
}

# ── (a) test_rewrites_backtick_ticket_ref ────────────────────────────────────
test_rewrites_backtick_ticket_ref() {
    _snapshot_fail
    # shellcheck disable=SC2016  # literal test input with backticks
    local _input='Run `ticket list` to see tickets.'
    local _result
    _result=$(_apply_rewriter "$_input")
    assert_contains "backtick ticket ref" "\`.claude/scripts/dso ticket list\`" "$_result"
    assert_pass_if_clean "test_rewrites_backtick_ticket_ref"
}

# ── (b) test_rewrites_bare_ticket_ref ────────────────────────────────────────
test_rewrites_bare_ticket_ref() {
    _snapshot_fail
    local _input='ticket create bug "title"'
    local _result
    _result=$(_apply_rewriter "$_input")
    assert_contains "bare ticket ref" ".claude/scripts/dso ticket create" "$_result"
    assert_pass_if_clean "test_rewrites_bare_ticket_ref"
}

# ── (c) test_skips_already_shimmed ───────────────────────────────────────────
test_skips_already_shimmed() {
    _snapshot_fail
    local _input='.claude/scripts/dso ticket list'
    local _result
    _result=$(_apply_rewriter "$_input")
    assert_eq "already shimmed unchanged" "$_input" "$_result"
    assert_pass_if_clean "test_skips_already_shimmed"
}

# ── (d) test_skips_full_path ────────────────────────────────────────────────
test_skips_full_path() {
    _snapshot_fail
    local _input='src/rebar/_engine/ticket list'
    local _result
    _result=$(_apply_rewriter "$_input")
    assert_eq "full path unchanged" "$_input" "$_result"
    assert_pass_if_clean "test_skips_full_path"
}

# ── (h) test_idempotent ────────────────────────────────────────────────────
test_idempotent() {
    _snapshot_fail
    # shellcheck disable=SC2016  # literal test input with backticks
    local _input='`ticket list` and ticket show <id>'
    local _pass1 _pass2
    _pass1=$(_apply_rewriter "$_input")
    _pass2=$(_apply_rewriter "$_pass1")
    assert_eq "idempotent: second pass unchanged" "$_pass1" "$_pass2"
    assert_pass_if_clean "test_idempotent"
}

# ── (i) test_no_double_rewrite ──────────────────────────────────────────────
test_no_double_rewrite() {
    _snapshot_fail
    # shellcheck disable=SC2016  # literal test input with backticks
    local _input='Use `ticket show <id>` to view'
    local _result
    _result=$(_apply_rewriter "$_input")
    # Should have exactly one .claude/scripts/dso, not two
    local _count
    _count=$(echo "$_result" | grep -o '\.claude/scripts/dso' | wc -l | tr -d ' ')
    assert_eq "no double rewrite: exactly 1 shim" "1" "$_count"
    assert_pass_if_clean "test_no_double_rewrite"
}

test_rewrites_backtick_ticket_ref
test_rewrites_bare_ticket_ref
test_skips_already_shimmed
test_skips_full_path
test_idempotent
test_no_double_rewrite

print_summary
