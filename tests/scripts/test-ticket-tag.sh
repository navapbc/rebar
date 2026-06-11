#!/usr/bin/env bash
# tests/scripts/test-ticket-tag.sh
# Tags are free-form in rebar. In particular, `brainstorm:complete` carries NO
# special gate (the DSO Planning-Intelligence-Log gate was removed when rebar was
# decoupled from the plugin). This test pins that brainstorm:complete is an
# ordinary tag and that tag/untag round-trip works through the dispatcher.
#
# Usage: bash tests/scripts/test-ticket-tag.sh
# Returns: exit 0 if all tests pass, exit non-zero if any fail

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REBAR="$PLUGIN_ROOT/src/rebar/_engine/rebar"

source "$SCRIPT_DIR/../lib/assert.sh"

echo "=== test-ticket-tag.sh ==="

_CLEANUP_DIRS=()
_cleanup() { for d in "${_CLEANUP_DIRS[@]:-}"; do rm -rf "$d" 2>/dev/null || true; done; }
trap _cleanup EXIT

# _new_repo — initialized rebar repo in a temp git dir; echoes the path.
_new_repo() {
    local root; root="$(mktemp -d)"
    _CLEANUP_DIRS+=("$root")
    ( cd "$root" && git init -q && git config user.email t@t.com && git config user.name t )
    _rebar "$root" init >/dev/null 2>&1
    printf '%s' "$root"
}

# _rebar <repo_root> <args...> — run the dispatcher inside the repo (cwd-relative
# git ops) with REBAR_ROOT pinned, exactly as the CLI wrapper does.
_rebar() {
    local root="$1"; shift
    ( cd "$root" && REBAR_ROOT="$root" bash "$REBAR" "$@" )
}

# ── test_brainstorm_complete_is_plain_tag ────────────────────────────────────
# An epic with no Planning Intelligence Log can still be tagged brainstorm:complete
# (no gate): tags are freeform and ungated.
test_brainstorm_complete_is_plain_tag() {
    _snapshot_fail
    local root eid rc
    root="$(_new_repo)"
    eid=$(_rebar "$root" create epic "E" 2>/dev/null | tail -1)

    rc=0
    _rebar "$root" tag "$eid" brainstorm:complete >/dev/null 2>&1 || rc=$?
    assert_eq "tag brainstorm:complete succeeds (no PIL gate)" "0" "$rc"

    local tags
    tags=$(_rebar "$root" show "$eid" 2>/dev/null)
    assert_contains "brainstorm:complete present after tagging" "brainstorm:complete" "$tags"
    assert_pass_if_clean "test_brainstorm_complete_is_plain_tag"
}

# ── test_ordinary_tag_round_trip ─────────────────────────────────────────────
test_ordinary_tag_round_trip() {
    _snapshot_fail
    local root tid rc
    root="$(_new_repo)"
    tid=$(_rebar "$root" create task "T" 2>/dev/null | tail -1)

    rc=0; _rebar "$root" tag "$tid" area:api >/dev/null 2>&1 || rc=$?
    assert_eq "tag area:api succeeds" "0" "$rc"
    assert_contains "area:api present" "area:api" "$(_rebar "$root" show "$tid" 2>/dev/null)"

    rc=0; _rebar "$root" untag "$tid" area:api >/dev/null 2>&1 || rc=$?
    assert_eq "untag area:api succeeds" "0" "$rc"
    assert_pass_if_clean "test_ordinary_tag_round_trip"
}

# ── GAP-6: adding the same tag twice is idempotent ───────────────────────────
# Tagging a ticket with the same tag twice must succeed (exit 0) and the tag
# must appear exactly once in the compiled state.
test_tag_twice_is_idempotent() {
    _snapshot_fail
    local root tid rc
    root="$(_new_repo)"
    tid=$(_rebar "$root" create task "T" 2>/dev/null | tail -1)

    rc=0; _rebar "$root" tag "$tid" area:api >/dev/null 2>&1 || rc=$?
    assert_eq "gap6: first tag succeeds" "0" "$rc"

    # Second identical tag must also succeed (idempotent, not an error).
    rc=0; _rebar "$root" tag "$tid" area:api >/dev/null 2>&1 || rc=$?
    assert_eq "gap6: second identical tag exits 0 (idempotent)" "0" "$rc"

    # Tag must appear exactly once in the compiled tags array.
    local show_json count
    show_json=$(_rebar "$root" show "$tid" 2>/dev/null)
    count=$(SHOW_JSON="$show_json" python3 -c '
import json, os
d = json.loads(os.environ["SHOW_JSON"])
print(sum(1 for t in d.get("tags", []) if t == "area:api"))
' 2>/dev/null) || count="ERR"
    assert_eq "gap6: tag appears exactly once after double-tag" "1" "$count"
    assert_pass_if_clean "test_tag_twice_is_idempotent"
}

# ── GAP-7: untag of a tag the ticket does not have is graceful ────────────────
# Untagging a tag that was never applied must exit 0 without error (graceful
# no-op), not fail.
test_untag_absent_tag_is_graceful() {
    _snapshot_fail
    local root tid rc
    root="$(_new_repo)"
    tid=$(_rebar "$root" create task "T" 2>/dev/null | tail -1)

    # Ticket has no tags; untag a tag it never had.
    rc=0; _rebar "$root" untag "$tid" never:applied >/dev/null 2>&1 || rc=$?
    assert_eq "gap7: untag of absent tag exits 0 (graceful)" "0" "$rc"
    assert_pass_if_clean "test_untag_absent_tag_is_graceful"
}

test_brainstorm_complete_is_plain_tag
test_ordinary_tag_round_trip
test_tag_twice_is_idempotent
test_untag_absent_tag_is_graceful

print_summary
