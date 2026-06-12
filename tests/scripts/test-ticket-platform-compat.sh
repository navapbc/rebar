#!/usr/bin/env bash
# tests/scripts/test-ticket-platform-compat.sh
# RED tests for platform-compatibility features of ticket-lib-api.sh:
#   1. Shell-special-character round-trips through write and read ops
#   2. _ticketlib_has_flock detection variable set correctly at source-time
#   3. Flock fallback: forced _ticketlib_has_flock=0 produces identical output
#
# RED phase: Section A tests (_ticketlib_has_flock detection) fail before
# ticket-lib-api.sh implements the detection variable. Sections B and C pass
# against the existing library and become guards once the fallback is wired in.
#
# Usage: bash tests/scripts/test-ticket-platform-compat.sh
# Returns: exit non-zero (RED) before implementation

# NOTE: -e intentionally omitted — test functions return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# GIT_DISCOVERY_ACROSS_FILESYSTEM=1 lets git cross Docker volume mount points
# (needed in GitHub Actions Alpine containers where the workspace is a mounted
# filesystem and git otherwise stops at the mount boundary).
REPO_ROOT="$(GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"
REPO_ROOT="${REPO_ROOT:-${GITHUB_WORKSPACE:-$(cd "$SCRIPT_DIR/../.." && pwd)}}"

TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_LIB_API="$REPO_ROOT/src/rebar/_engine/ticket-lib-api.sh"

# shellcheck source=/dev/null
source "$REPO_ROOT/tests/lib/assert.sh"
# shellcheck source=/dev/null
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-platform-compat.sh ==="

# ── Helper: create a fresh temp git repo with ticket system initialized ────────
# _CLEANUP_DIRS is initialized and an EXIT trap registered by git-fixtures.sh (line 42).
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: source ticket-lib-api.sh in an isolated subshell, run an op ──────
# Returns stdout from op; exits non-zero on source failure or op failure.
_invoke_lib_op() {
    # Tier B retired the bash ticket_* leaf functions (now in rebar._commands).
    # Characterize the live command path through the dispatcher (Python impl).
    local op="$1"
    shift
    local sub="${op#ticket_}"
    sub="${sub//_/-}"
    bash "$TICKET_SCRIPT" "$sub" "$@"
}

# ── Helper: extract field from ticket JSON output via python3 ─────────────────
_extract_json_field() {
    local json="$1"
    local field="$2"
    echo "$json" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('$field', ''))
except Exception as e:
    print('JSON_ERROR:' + str(e))
" 2>/dev/null || echo "EXTRACT_ERROR"
}

# ── Helper: extract string from ticket JSON comments list ─────────────────────
_extract_comment_body() {
    local json="$1"
    echo "$json" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    comments = d.get('comments', [])
    for c in comments:
        body = c.get('body', '') if isinstance(c, dict) else str(c)
        print(body)
except Exception as e:
    print('JSON_ERROR:' + str(e))
" 2>/dev/null || echo "EXTRACT_ERROR"
}

# ════════════════════════════════════════════════════════════════════════════════
# Section A: _ticketlib_has_flock detection (RED)
# ════════════════════════════════════════════════════════════════════════════════

echo ""
echo "=== Section A: _ticketlib_has_flock detection ==="

# ── Test A1: _ticketlib_has_flock is set after sourcing ticket-lib-api.sh ──────
# RED: the variable does not exist in the current implementation.
# After sourcing, ${_ticketlib_has_flock:-UNSET} must be "0" or "1".
# Currently it is "UNSET" → assertion fails.
echo ""
echo "Test A1: _ticketlib_has_flock is set to 0 or 1 after source"
test_flock_detection_var_is_set() {
    local result
    result=$(TICKET_LIB_API="$TICKET_LIB_API" bash -c '
        # shellcheck source=/dev/null
        source "$TICKET_LIB_API" 2>/dev/null || { echo "SOURCE_FAILED"; exit 0; }
        echo "${_ticketlib_has_flock:-UNSET}"
    ' 2>/dev/null) || result="SUBSHELL_ERROR"

    # Must be exactly "0" or "1" — not empty, not UNSET
    if [[ "$result" == "0" || "$result" == "1" ]]; then
        assert_eq "_ticketlib_has_flock is 0 or 1" "ok" "ok"
    else
        assert_eq "_ticketlib_has_flock is 0 or 1" "0_or_1" "$result"
    fi
}
test_flock_detection_var_is_set

# ── Test A2: when flock binary is available, _ticketlib_has_flock is 1 ─────────
# RED: not implemented yet.
echo ""
echo "Test A2: _ticketlib_has_flock=1 when flock binary available"
test_flock_detection_var_is_1_when_flock_available() {
    # Only run this test if flock is available on this host
    if ! command -v flock >/dev/null 2>&1; then
        # flock not available on this host — skip with a neutral pass
        assert_eq "flock binary not available on this host (skip A2)" "skip" "skip"
        return
    fi

    local result
    result=$(TICKET_LIB_API="$TICKET_LIB_API" bash -c '
        # shellcheck source=/dev/null
        source "$TICKET_LIB_API" 2>/dev/null || { echo "SOURCE_FAILED"; exit 0; }
        echo "${_ticketlib_has_flock:-UNSET}"
    ' 2>/dev/null) || result="SUBSHELL_ERROR"

    assert_eq "_ticketlib_has_flock=1 when flock is in PATH" "1" "$result"
}
test_flock_detection_var_is_1_when_flock_available

# ── Test A3: when flock binary is absent, _ticketlib_has_flock is 0 ────────────
# Simulates a flock-free environment by prepending a fake PATH.
# RED: not implemented yet.
echo ""
echo "Test A3: _ticketlib_has_flock=0 when flock binary absent"
test_flock_detection_var_is_0_when_flock_absent() {
    # Build a temp dir containing no `flock` binary and prepend it to PATH
    local fake_bin
    fake_bin=$(mktemp -d)
    _CLEANUP_DIRS+=("$fake_bin")

    # Create dummy stubs for every command EXCEPT flock so the subshell works
    # (do not stub flock — that is the point: it should be absent)

    local result
    result=$(TICKET_LIB_API="$TICKET_LIB_API" PATH="$fake_bin:${PATH}" bash -c '
        # Strip flock from PATH by using a PATH without it.
        # Verify flock is not callable:
        if command -v flock >/dev/null 2>&1; then
            # flock somehow still visible — report and skip
            echo "FLOCK_STILL_VISIBLE"
            exit 0
        fi
        # shellcheck source=/dev/null
        source "$TICKET_LIB_API" 2>/dev/null || { echo "SOURCE_FAILED"; exit 0; }
        echo "${_ticketlib_has_flock:-UNSET}"
    ' 2>/dev/null) || result="SUBSHELL_ERROR"

    # If flock is baked into the PATH in a way we cannot shadow, skip gracefully
    if [[ "$result" == "FLOCK_STILL_VISIBLE" ]]; then
        # On Alpine/BusyBox, flock may be a builtin; treat as skip
        assert_eq "flock_absent simulation: flock not shadowable (skip A3)" "skip" "skip"
        return
    fi

    assert_eq "_ticketlib_has_flock=0 when flock absent from PATH" "0" "$result"
}
test_flock_detection_var_is_0_when_flock_absent

# ════════════════════════════════════════════════════════════════════════════════
# Section B: Flock fallback (forced _ticketlib_has_flock=0)
# ════════════════════════════════════════════════════════════════════════════════

echo ""
echo "=== Section B: Flock fallback path ==="

# ── Test B1: ticket_comment succeeds when _ticketlib_has_flock forced to 0 ─────
# RED: the fallback path does not exist yet.
echo ""
echo "Test B1: ticket_comment succeeds with _ticketlib_has_flock=0 forced"
test_flock_fallback_comment_succeeds() {
    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "fallback comment test" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "created ticket for B1" "non-empty" "empty"
        return
    fi

    # Source the library, then override _ticketlib_has_flock=0 before calling ticket_comment.
    # If the fallback is implemented, the comment write succeeds.
    # If not implemented, the write either fails (exits non-zero) or uses the flock
    # binary directly and the test checks that the result is identical.
    # REBAR_FORCE_MKDIR_LOCK=1 forces the mkdir-fallback write core (the seam's
    # _flock_stage_commit honors it) — the dispatcher equivalent of the retired
    # _ticketlib_has_flock=0 override, against the live (python) command path.
    local exit_code=0
    (
        cd "$repo" || exit 1
        _TICKET_TEST_NO_SYNC=1 TICKETS_TRACKER_DIR="$repo/.tickets-tracker" \
            REBAR_FORCE_MKDIR_LOCK=1 bash "$TICKET_SCRIPT" comment "$ticket_id" "fallback test body"
    ) >/dev/null 2>&1 || exit_code=$?

    # After a successful write, ticket_show must include the comment body.
    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    local found_body
    found_body=$(echo "$show_output" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    comments = d.get('comments', [])
    found = any('fallback test body' in (c.get('body','') if isinstance(c, dict) else str(c)) for c in comments)
    print('yes' if found else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")

    assert_eq "flock fallback: comment body round-trips" "yes" "$found_body"
}
test_flock_fallback_comment_succeeds

# ── Test B2: ticket_create succeeds when _ticketlib_has_flock forced to 0 ──────
# RED: fallback not implemented.
echo ""
echo "Test B2: ticket_create succeeds with _ticketlib_has_flock=0 forced"
test_flock_fallback_create_succeeds() {
    local repo
    repo=$(_make_test_repo)

    local created_id
    created_id=$(
        cd "$repo" || exit 1
        _TICKET_TEST_NO_SYNC=1 TICKETS_TRACKER_DIR="$repo/.tickets-tracker" \
            REBAR_FORCE_MKDIR_LOCK=1 bash "$TICKET_SCRIPT" create task "flock fallback create test" 2>/dev/null | tail -1
    ) || true

    if [ -z "$created_id" ]; then
        assert_eq "flock fallback: ticket_create returned id" "non-empty" "empty"
        return
    fi

    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$created_id" 2>/dev/null) || true

    local title
    title=$(echo "$show_output" | python3 -c "import json,sys; print(json.load(sys.stdin).get('title',''))" 2>/dev/null || echo "")

    assert_eq "flock fallback: created ticket title preserved" "flock fallback create test" "$title"
}
test_flock_fallback_create_succeeds

# ── Test B3: ticket_tag succeeds when _ticketlib_has_flock forced to 0 ─────────
# RED: fallback not implemented.
echo ""
echo "Test B3: ticket_tag succeeds with _ticketlib_has_flock=0 forced"
test_flock_fallback_tag_succeeds() {
    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "fallback tag test" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "created ticket for B3" "non-empty" "empty"
        return
    fi

    (
        cd "$repo" || exit 1
        _TICKET_TEST_NO_SYNC=1 TICKETS_TRACKER_DIR="$repo/.tickets-tracker" \
            REBAR_FORCE_MKDIR_LOCK=1 bash "$TICKET_SCRIPT" tag "$ticket_id" flock-fallback-label
    ) >/dev/null 2>&1 || true

    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    local has_tag
    has_tag=$(echo "$show_output" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print('yes' if 'flock-fallback-label' in d.get('tags', []) else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")

    assert_eq "flock fallback: tag round-trips" "yes" "$has_tag"
}
test_flock_fallback_tag_succeeds

# ── Test B4: fallback output identical to flock-enabled path ──────────────────
# Force _ticketlib_has_flock=0 for one op and =1 for another; compare ticket_show output.
# RED: fallback not implemented, so the forced=0 path will either fail or deviate.
echo ""
echo "Test B4: flock fallback produces identical ticket_show output as flock=1 path"
test_flock_fallback_output_identical() {
    # Only run when flock is available (otherwise both paths are python3 → trivially equal)
    if ! command -v flock >/dev/null 2>&1; then
        assert_eq "flock binary not available on this host (skip B4)" "skip" "skip"
        return
    fi

    local repo_a repo_b
    repo_a=$(_make_test_repo)
    repo_b=$(_make_test_repo)

    # Path A: normal (flock=1)
    local id_a
    id_a=$(
        cd "$repo_a" || exit 1
        _TICKET_TEST_NO_SYNC=1 TICKETS_TRACKER_DIR="$repo_a/.tickets-tracker" \
            bash "$TICKET_SCRIPT" create task "compat test ticket" 2>/dev/null | tail -1
    ) || true

    # Path B: forced mkdir fallback (REBAR_FORCE_MKDIR_LOCK=1, was flock=0)
    local id_b
    id_b=$(
        cd "$repo_b" || exit 1
        _TICKET_TEST_NO_SYNC=1 TICKETS_TRACKER_DIR="$repo_b/.tickets-tracker" \
            REBAR_FORCE_MKDIR_LOCK=1 bash "$TICKET_SCRIPT" create task "compat test ticket" 2>/dev/null | tail -1
    ) || true

    if [ -z "$id_a" ] || [ -z "$id_b" ]; then
        assert_eq "flock compat: both tickets created" "non-empty" "${id_a:-empty}/${id_b:-empty}"
        return
    fi

    local show_a show_b title_a title_b status_a status_b type_a type_b
    show_a=$(cd "$repo_a" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$id_a" 2>/dev/null) || true
    show_b=$(cd "$repo_b" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$id_b" 2>/dev/null) || true

    title_a=$(echo "$show_a" | python3 -c "import json,sys; print(json.load(sys.stdin).get('title',''))" 2>/dev/null || echo "")
    title_b=$(echo "$show_b" | python3 -c "import json,sys; print(json.load(sys.stdin).get('title',''))" 2>/dev/null || echo "")
    status_a=$(echo "$show_a" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
    status_b=$(echo "$show_b" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
    type_a=$(echo "$show_a" | python3 -c "import json,sys; print(json.load(sys.stdin).get('ticket_type',''))" 2>/dev/null || echo "")
    type_b=$(echo "$show_b" | python3 -c "import json,sys; print(json.load(sys.stdin).get('ticket_type',''))" 2>/dev/null || echo "")

    assert_eq "flock compat: title identical (flock=1 vs flock=0)" "$title_a" "$title_b"
    assert_eq "flock compat: status identical" "$status_a" "$status_b"
    assert_eq "flock compat: ticket_type identical" "$type_a" "$type_b"
}
test_flock_fallback_output_identical

# ════════════════════════════════════════════════════════════════════════════════
# Section C: Special-character round-trips
# ════════════════════════════════════════════════════════════════════════════════

echo ""
echo "=== Section C: Special-character round-trips ==="

# ── Test C1: single quotes in ticket title round-trip via ticket_create ─────────
echo ""
echo "Test C1: single quotes in title round-trip"
test_special_char_single_quotes_title() {
    local repo
    repo=$(_make_test_repo)

    local title_in="it's a test: 'quoted' value"
    local created_id
    created_id=$(
        cd "$repo" || exit 1
        # shellcheck disable=SC2030,SC2031
        export _TICKET_TEST_NO_SYNC=1
        # shellcheck disable=SC2030,SC2031
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_create task "$title_in" 2>/dev/null | tail -1
    ) || true

    if [ -z "$created_id" ]; then
        assert_eq "C1: ticket_create returned id" "non-empty" "empty"
        return
    fi

    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$created_id" 2>/dev/null) || true

    local title_out
    title_out=$(echo "$show_output" | python3 -c "import json,sys; print(json.load(sys.stdin).get('title',''))" 2>/dev/null || echo "")

    assert_eq "C1: single quotes round-trip in title" "$title_in" "$title_out"
}
test_special_char_single_quotes_title

# ── Test C2: double quotes in ticket title round-trip ─────────────────────────
echo ""
echo "Test C2: double quotes in title round-trip"
test_special_char_double_quotes_title() {
    local repo
    repo=$(_make_test_repo)

    local title_in='He said "hello world" to them'
    local created_id
    created_id=$(
        cd "$repo" || exit 1
        # shellcheck disable=SC2030,SC2031
        export _TICKET_TEST_NO_SYNC=1
        # shellcheck disable=SC2030,SC2031
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_create task "$title_in" 2>/dev/null | tail -1
    ) || true

    if [ -z "$created_id" ]; then
        assert_eq "C2: ticket_create returned id" "non-empty" "empty"
        return
    fi

    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$created_id" 2>/dev/null) || true

    local title_out
    title_out=$(echo "$show_output" | python3 -c "import json,sys; print(json.load(sys.stdin).get('title',''))" 2>/dev/null || echo "")

    assert_eq "C2: double quotes round-trip in title" "$title_in" "$title_out"
}
test_special_char_double_quotes_title

# ── Test C3: backslashes in ticket comment round-trip ─────────────────────────
echo ""
echo "Test C3: backslashes in comment body round-trip"
test_special_char_backslashes_comment() {
    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "backslash test" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "C3: created ticket" "non-empty" "empty"
        return
    fi

    # Use a literal backslash body
    # shellcheck disable=SC1003
    local body_in='path is C:\Users\test\file.txt and also \\double\\'

    (
        cd "$repo" || exit 1
        # shellcheck disable=SC2030,SC2031
        export _TICKET_TEST_NO_SYNC=1
        # shellcheck disable=SC2030,SC2031
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_comment "$ticket_id" "$body_in" >/dev/null 2>&1
    ) || true

    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    local body_out
    body_out=$(_extract_comment_body "$show_output")

    assert_eq "C3: backslashes round-trip in comment" "$body_in" "$body_out"
}
test_special_char_backslashes_comment

# ── Test C4: embedded newlines in ticket comment round-trip ───────────────────
echo ""
echo "Test C4: embedded newlines in comment body round-trip"
test_special_char_newlines_comment() {
    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "newline test" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "C4: created ticket" "non-empty" "empty"
        return
    fi

    # Body with embedded newlines
    local body_in
    body_in="line one
line two
line three"

    (
        cd "$repo" || exit 1
        # shellcheck disable=SC2030,SC2031
        export _TICKET_TEST_NO_SYNC=1
        # shellcheck disable=SC2030,SC2031
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_comment "$ticket_id" "$body_in" >/dev/null 2>&1
    ) || true

    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    local body_out
    body_out=$(_extract_comment_body "$show_output")

    assert_eq "C4: embedded newlines round-trip in comment" "$body_in" "$body_out"
}
test_special_char_newlines_comment

# ── Test C5: unicode characters in ticket title round-trip ────────────────────
echo ""
echo "Test C5: unicode in title round-trip"
test_special_char_unicode_title() {
    local repo
    repo=$(_make_test_repo)

    # Unicode: emoji, CJK, Arabic, combining diacritics
    local title_in="Ticket: 🎉 café naïve résumé 日本語 العربية"
    local created_id
    created_id=$(
        cd "$repo" || exit 1
        # shellcheck disable=SC2030,SC2031
        export _TICKET_TEST_NO_SYNC=1
        # shellcheck disable=SC2030,SC2031
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_create task "$title_in" 2>/dev/null | tail -1
    ) || true

    if [ -z "$created_id" ]; then
        assert_eq "C5: ticket_create returned id" "non-empty" "empty"
        return
    fi

    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$created_id" 2>/dev/null) || true

    local title_out
    title_out=$(echo "$show_output" | python3 -c "import json,sys; print(json.load(sys.stdin).get('title',''))" 2>/dev/null || echo "")

    assert_eq "C5: unicode round-trips in title" "$title_in" "$title_out"
}
test_special_char_unicode_title

# ── Test C6: unicode in comment body round-trip ───────────────────────────────
echo ""
echo "Test C6: unicode in comment body round-trip"
test_special_char_unicode_comment() {
    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "unicode comment test" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "C6: created ticket" "non-empty" "empty"
        return
    fi

    local body_in="Unicode: éàü 中文 한국어 🚀 ☃"
    # Use printf to produce actual unicode bytes
    local body_actual
    body_actual=$(printf '%b' "Unicode: \xc3\xa9\xc3\xa0\xc3\xbc \xe4\xb8\xad\xe6\x96\x87 \xed\x95\x9c\xea\xb5\xad\xec\x96\xb4 \xf0\x9f\x9a\x80 \xe2\x98\x83")

    (
        cd "$repo" || exit 1
        # shellcheck disable=SC2030,SC2031
        export _TICKET_TEST_NO_SYNC=1
        # shellcheck disable=SC2030,SC2031
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_comment "$ticket_id" "$body_actual" >/dev/null 2>&1
    ) || true

    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    local body_out
    body_out=$(_extract_comment_body "$show_output")

    assert_eq "C6: unicode round-trips in comment" "$body_actual" "$body_out"
}
test_special_char_unicode_comment

# ── Test C7: single quotes and backslashes in ticket_edit title round-trip ────
echo ""
echo "Test C7: single quotes + backslashes in ticket_edit title round-trip"
test_special_char_edit_title_mixed() {
    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "original title" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "C7: created ticket" "non-empty" "empty"
        return
    fi

    local new_title="It's a 'path' like C:\\Users\\test"

    (
        cd "$repo" || exit 1
        # shellcheck disable=SC2030,SC2031
        export _TICKET_TEST_NO_SYNC=1
        # shellcheck disable=SC2030,SC2031
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_edit "$ticket_id" --title "$new_title" >/dev/null 2>&1
    ) || true

    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    local title_out
    title_out=$(echo "$show_output" | python3 -c "import json,sys; print(json.load(sys.stdin).get('title',''))" 2>/dev/null || echo "")

    assert_eq "C7: mixed quotes+backslashes round-trip in edit" "$new_title" "$title_out"
}
test_special_char_edit_title_mixed

# ── Test C8: special chars in ticket_tag label round-trip via ticket_show tags ─
echo ""
echo "Test C8: URL-safe special chars in tag round-trip"
test_special_char_tag_label() {
    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "tag special char test" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "C8: created ticket" "non-empty" "empty"
        return
    fi

    # Tag labels must be URL-safe (no spaces/quotes) — use hyphens and colons
    local tag_in="env:prod-2.0_alpha"

    (
        cd "$repo" || exit 1
        # shellcheck disable=SC2030,SC2031
        export _TICKET_TEST_NO_SYNC=1
        # shellcheck disable=SC2030,SC2031
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_tag "$ticket_id" "$tag_in" >/dev/null 2>&1
    ) || true

    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    local has_tag
    has_tag=$(echo "$show_output" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print('yes' if '$tag_in' in d.get('tags', []) else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")

    assert_eq "C8: URL-safe tag round-trips" "yes" "$has_tag"
}
test_special_char_tag_label

# ── Test C9: special chars in ticket_list output round-trip ───────────────────
echo ""
echo "Test C9: special chars in ticket_list output round-trip"
test_special_char_ticket_list_title() {
    local repo
    repo=$(_make_test_repo)

    local title_in='list-test: quotes "double" & backslash \ here'
    local created_id
    created_id=$(
        cd "$repo" || exit 1
        # shellcheck disable=SC2030,SC2031
        export _TICKET_TEST_NO_SYNC=1
        # shellcheck disable=SC2030,SC2031
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_create task "$title_in" 2>/dev/null | tail -1
    ) || true

    if [ -z "$created_id" ]; then
        assert_eq "C9: ticket_create returned id" "non-empty" "empty"
        return
    fi

    local list_output
    list_output=$(
        cd "$repo" || exit 1
        # shellcheck disable=SC2030,SC2031
        export _TICKET_TEST_NO_SYNC=1
        # shellcheck disable=SC2030,SC2031
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_list 2>/dev/null
    ) || true

    # Find title in the list output for the created ticket
    local title_out
    title_out=$(echo "$list_output" | python3 -c "
import json, sys
try:
    tickets = json.load(sys.stdin)
    for t in tickets:
        if t.get('ticket_id') == '$created_id':
            print(t.get('title', ''))
            sys.exit(0)
    print('NOT_FOUND')
except Exception as e:
    print('JSON_ERROR:' + str(e))
" 2>/dev/null || echo "EXTRACT_ERROR")

    assert_eq "C9: special chars in ticket_list title" "$title_in" "$title_out"
}
test_special_char_ticket_list_title

# ── Test C10: special chars in ticket_transition (comment body) round-trip ────
echo ""
echo "Test C10: special chars in ticket_transition preserve existing comment round-trip"
test_special_char_transition_status() {
    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "transition compat test" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "C10: created ticket" "non-empty" "empty"
        return
    fi

    # Add a comment with special chars first
    local comment_body="pre-transition comment: it's \"complex\" with C:\\paths\\"

    (
        cd "$repo" || exit 1
        # shellcheck disable=SC2030,SC2031
        export _TICKET_TEST_NO_SYNC=1
        # shellcheck disable=SC2030,SC2031
        export TICKETS_TRACKER_DIR="$repo/.tickets-tracker"
        _invoke_lib_op ticket_comment "$ticket_id" "$comment_body" >/dev/null 2>&1
    ) || true

    # Transition the ticket
    (
        cd "$repo" || exit 1
        # shellcheck disable=SC2030,SC2031
        export _TICKET_TEST_NO_SYNC=1
        _invoke_lib_op ticket_transition "$ticket_id" open in_progress >/dev/null 2>&1
    ) || true

    local show_output
    show_output=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    # Status must have changed
    local status_out
    status_out=$(echo "$show_output" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")

    assert_eq "C10: transition status changed" "in_progress" "$status_out"

    # Comment body must still be intact
    local body_out
    body_out=$(_extract_comment_body "$show_output")

    assert_eq "C10: special chars preserved after transition" "$comment_body" "$body_out"
}
test_special_char_transition_status

print_summary
