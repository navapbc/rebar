#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-e2e.sh
# End-to-end integration test for the ticket init → create → show workflow.
#
# Exercises the full CLI lifecycle across all three subcommands using isolated
# temp git repos. Validates cross-component contracts (not covered by unit tests
# for individual components).
#
# Usage: bash tests/scripts/suites/test-ticket-e2e.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e is intentionally omitted — test functions return non-zero by design.
# -e would abort the runner on expected assertion mismatches.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-e2e.sh ==="

# ── Helper: create a fresh temp git repo ─────────────────────────────────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_test_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: find a CREATE event file for a ticket ────────────────────────────
_find_create_event() {
    local tracker_dir="$1"
    local ticket_id="$2"
    find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | head -1
}

# ── Test 1: test_full_workflow_init_create_show ───────────────────────────────
echo "Test 1: full workflow init → create → show with commit count"
test_full_workflow_init_create_show() {
    _snapshot_fail
    local repo
    repo=$(_make_test_repo)

    # 1a. Run ticket init
    local init_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || init_exit=$?
    assert_eq "init exits 0" "0" "$init_exit"
    if [ "$init_exit" -ne 0 ]; then return; fi

    # 1a. .tickets-tracker/ exists
    if [ -d "$repo/.tickets-tracker" ]; then
        assert_eq "init: .tickets-tracker/ exists" "exists" "exists"
    else
        assert_eq "init: .tickets-tracker/ exists" "exists" "missing"
        return
    fi

    # 1b. Run ticket create task "My first ticket"
    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "My first ticket" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "create: returned non-empty ticket ID" "non-empty" "empty"
        return
    else
        assert_eq "create: returned non-empty ticket ID" "non-empty" "non-empty"
    fi

    # 1c. Run ticket show <ticket_id>
    local show_exit=0
    local show_out
    show_out=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || show_exit=$?
    assert_eq "show exits 0" "0" "$show_exit"

    # Assert: output JSON has title = "My first ticket"
    local title_check
    title_check=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    print(data.get('title', ''))
except Exception:
    print('')
" "$show_out" 2>/dev/null) || true

    assert_eq "show: title is 'My first ticket'" "My first ticket" "$title_check"

    # Assert: output JSON has ticket_type = "task"
    local type_check
    type_check=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    print(data.get('ticket_type', ''))
except Exception:
    print('')
" "$show_out" 2>/dev/null) || true

    assert_eq "show: ticket_type is 'task'" "task" "$type_check"

    # 1d. Assert tickets branch has exactly 4 commits after init + 1 create.
    # ticket-init.sh creates 3 commits:
    #   1. "chore: initialize ticket tracker" (orphan empty commit)
    #   2. "chore: add .gitignore for env-id and state-cache"
    #   3. "chore: add no-op .pre-commit-config.yaml (bug 27d8-b230)"
    # ticket-create.sh adds 1 more commit = 4 total.
    local commits_after_create
    commits_after_create=$(git -C "$repo/.tickets-tracker" log --oneline 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "tickets branch has exactly 4 commits (3 init + 1 create)" "4" "$commits_after_create"

    # Additionally verify the latest commit message references the ticket ID
    local latest_msg
    latest_msg=$(git -C "$repo/.tickets-tracker" log --oneline -1 2>/dev/null)
    if [[ "$latest_msg" == *"$ticket_id"* ]]; then
        assert_eq "latest commit on tickets branch references ticket ID" "referenced" "referenced"
    else
        assert_eq "latest commit on tickets branch references ticket ID" "referenced" "not-referenced: $latest_msg"
    fi

    assert_pass_if_clean "test_full_workflow_init_create_show"
}
test_full_workflow_init_create_show

# ── Test 2: test_workflow_with_special_chars_in_title ────────────────────────
echo "Test 2: special characters in title (quotes and apostrophes) do not break JSON"
test_workflow_with_special_chars_in_title() {
    _snapshot_fail
    local repo
    repo=$(_make_test_repo)

    local init_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" init >/dev/null 2>/dev/null) || init_exit=$?
    assert_eq "special-chars: init exits 0" "0" "$init_exit"
    if [ "$init_exit" -ne 0 ]; then return; fi

    # Title with apostrophe and double-quotes
    local special_title="It's a \"test\" title"
    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "$special_title" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "special-chars: create returns non-empty ticket ID" "non-empty" "empty"
        return
    else
        assert_eq "special-chars: create returns non-empty ticket ID" "non-empty" "non-empty"
    fi

    local show_exit=0
    local show_out
    show_out=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || show_exit=$?
    assert_eq "special-chars: show exits 0" "0" "$show_exit"

    # Assert: title round-trips correctly through JSON
    local title_roundtrip
    title_roundtrip=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    print(data.get('title', ''))
except Exception as e:
    print(f'ERROR:{e}')
" "$show_out" 2>/dev/null) || true

    assert_eq "special-chars: title round-trips correctly" "$special_title" "$title_roundtrip"

    # Assert: the event file is valid JSON (not broken by special chars)
    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -n "$event_file" ]; then
        local parse_exit=0
        python3 -c "import json,sys; json.load(sys.stdin)" < "$event_file" 2>/dev/null || parse_exit=$?
        assert_eq "special-chars: event file is valid JSON" "0" "$parse_exit"
    else
        assert_eq "special-chars: CREATE event file found" "found" "not-found"
    fi

    assert_pass_if_clean "test_workflow_with_special_chars_in_title"
}
test_workflow_with_special_chars_in_title

# ── Test 3: test_create_and_show_multiple_tickets ────────────────────────────
echo "Test 3: create 3 tickets and verify each has unique ID and correct state"
test_create_and_show_multiple_tickets() {
    _snapshot_fail
    local repo
    repo=$(_make_test_repo)

    local init_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" init >/dev/null 2>/dev/null) || init_exit=$?
    assert_eq "multiple: init exits 0" "0" "$init_exit"
    if [ "$init_exit" -ne 0 ]; then return; fi

    local id1 id2 id3
    id1=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "First ticket" 2>/dev/null | tail -1) || true
    id2=$(cd "$repo" && bash "$TICKET_SCRIPT" create bug "Second ticket" 2>/dev/null | tail -1) || true
    id3=$(cd "$repo" && bash "$TICKET_SCRIPT" create story "Third ticket" 2>/dev/null | tail -1) || true

    # Assert: all IDs are non-empty
    if [ -n "$id1" ] && [ -n "$id2" ] && [ -n "$id3" ]; then
        assert_eq "multiple: all three ticket IDs are non-empty" "all-non-empty" "all-non-empty"
    else
        assert_eq "multiple: all three ticket IDs are non-empty" "all-non-empty" "some-empty: '${id1:-}' '${id2:-}' '${id3:-}'"
        return
    fi

    # Assert: all IDs are unique
    if [ "$id1" != "$id2" ] && [ "$id1" != "$id3" ] && [ "$id2" != "$id3" ]; then
        assert_eq "multiple: all three ticket IDs are unique" "unique" "unique"
    else
        assert_eq "multiple: all three ticket IDs are unique" "unique" "collision: $id1 $id2 $id3"
    fi

    # Assert: show returns correct state for each ticket
    local show1 show2 show3
    show1=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$id1" 2>/dev/null) || true
    show2=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$id2" 2>/dev/null) || true
    show3=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$id3" 2>/dev/null) || true

    # Verify ticket 1: title and type
    local t1_title t1_type
    t1_title=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('title',''))" "$show1" 2>/dev/null) || true
    t1_type=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('ticket_type',''))" "$show1" 2>/dev/null) || true
    assert_eq "multiple: ticket1 title" "First ticket" "$t1_title"
    assert_eq "multiple: ticket1 type" "task" "$t1_type"

    # Verify ticket 2: title and type
    local t2_title t2_type
    t2_title=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('title',''))" "$show2" 2>/dev/null) || true
    t2_type=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('ticket_type',''))" "$show2" 2>/dev/null) || true
    assert_eq "multiple: ticket2 title" "Second ticket" "$t2_title"
    assert_eq "multiple: ticket2 type" "bug" "$t2_type"

    # Verify ticket 3: title and type
    local t3_title t3_type
    t3_title=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('title',''))" "$show3" 2>/dev/null) || true
    t3_type=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('ticket_type',''))" "$show3" 2>/dev/null) || true
    assert_eq "multiple: ticket3 title" "Third ticket" "$t3_title"
    assert_eq "multiple: ticket3 type" "story" "$t3_type"

    assert_pass_if_clean "test_create_and_show_multiple_tickets"
}
test_create_and_show_multiple_tickets

# ── Test 4: test_env_id_embedded_in_events ───────────────────────────────────
echo "Test 4: env_id from .env-id is embedded in CREATE event JSON"
test_env_id_embedded_in_events() {
    _snapshot_fail
    local repo
    repo=$(_make_test_repo)

    local init_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" init >/dev/null 2>/dev/null) || init_exit=$?
    assert_eq "env_id: init exits 0" "0" "$init_exit"
    if [ "$init_exit" -ne 0 ]; then return; fi

    # Read the env_id from .env-id
    local env_id
    env_id=$(cat "$repo/.tickets-tracker/.env-id" 2>/dev/null | tr -d '[:space:]') || true

    if [ -z "$env_id" ]; then
        assert_eq "env_id: .env-id file exists and is non-empty" "non-empty" "empty"
        return
    else
        assert_eq "env_id: .env-id file exists and is non-empty" "non-empty" "non-empty"
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Env-id test ticket" 2>/dev/null | tail -1) || true

    if [ -z "$ticket_id" ]; then
        assert_eq "env_id: create returns ticket ID" "non-empty" "empty"
        return
    fi

    # Find the CREATE event file
    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "env_id: CREATE event file found" "found" "not-found"
        return
    fi

    # Assert: the env_id in the event JSON matches .env-id
    local event_env_id
    event_env_id=$(python3 -c "
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    ev = json.load(f)
print(ev.get('env_id', ''))
" "$event_file" 2>/dev/null) || true

    assert_eq "env_id: CREATE event env_id matches .env-id" "$env_id" "$event_env_id"

    # Additionally verify via ticket show output
    local show_out
    show_out=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    local show_env_id
    show_env_id=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    print(data.get('env_id', ''))
except Exception:
    print('')
" "$show_out" 2>/dev/null) || true

    assert_eq "env_id: show output env_id matches .env-id" "$env_id" "$show_env_id"

    assert_pass_if_clean "test_env_id_embedded_in_events"
}
test_env_id_embedded_in_events

# ── Test 5: test_concurrent_create_serialized_by_flock ───────────────────────
# Tests that the flock-based serialization in write_commit_event produces 3
# unique IDs with no lost writes.  Three creates are launched in parallel and
# then waited on individually so every exit code is captured and asserted.
# Note: true parallelism exposes git index.lock races that are orthogonal to
# the flock contract under test.  To eliminate non-determinism the test issues
# creates in staggered background processes (100 ms apart) — sufficient to
# exercise flock hand-off without risking git index collisions.
echo "Test 5: concurrent creates (3 staggered) complete without lost writes"
test_concurrent_create_serialized_by_flock() {
    _snapshot_fail
    local repo
    repo=$(_make_test_repo)

    local init_exit=0
    (cd "$repo" && bash "$TICKET_SCRIPT" init >/dev/null 2>/dev/null) || init_exit=$?
    assert_eq "concurrent: init exits 0" "0" "$init_exit"
    if [ "$init_exit" -ne 0 ]; then return; fi

    # Launch 3 background ticket create calls with a 100 ms stagger so that
    # git process setup is serialized at the OS level while flock hand-off is
    # still exercised.  Each PID is captured for individual wait + exit check.
    local tmp_id_dir tmp_id1 tmp_id2 tmp_id3
    tmp_id_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp_id_dir")
    tmp_id1="$tmp_id_dir/id1"
    tmp_id2="$tmp_id_dir/id2"
    tmp_id3="$tmp_id_dir/id3"

    (cd "$repo" && { _out=$(bash "$TICKET_SCRIPT" create task "Concurrent ticket 1" 2>/dev/null) || exit $?; printf '%s\n' "$_out" | tail -1 >"$tmp_id1"; }) &
    local pid1=$!
    sleep 0.1
    (cd "$repo" && { _out=$(bash "$TICKET_SCRIPT" create task "Concurrent ticket 2" 2>/dev/null) || exit $?; printf '%s\n' "$_out" | tail -1 >"$tmp_id2"; }) &
    local pid2=$!
    sleep 0.1
    (cd "$repo" && { _out=$(bash "$TICKET_SCRIPT" create task "Concurrent ticket 3" 2>/dev/null) || exit $?; printf '%s\n' "$_out" | tail -1 >"$tmp_id3"; }) &
    local pid3=$!

    # Wait for each individually and capture exit codes
    local exit1=0 exit2=0 exit3=0
    wait "$pid1" || exit1=$?
    wait "$pid2" || exit2=$?
    wait "$pid3" || exit3=$?

    # Assert: all 3 exited 0 (no errors)
    assert_eq "concurrent: create 1 exits 0" "0" "$exit1"
    assert_eq "concurrent: create 2 exits 0" "0" "$exit2"
    assert_eq "concurrent: create 3 exits 0" "0" "$exit3"

    local cid1 cid2 cid3
    cid1=$(cat "$tmp_id1" 2>/dev/null | tr -d '[:space:]') || true
    cid2=$(cat "$tmp_id2" 2>/dev/null | tr -d '[:space:]') || true
    cid3=$(cat "$tmp_id3" 2>/dev/null | tr -d '[:space:]') || true

    # Assert: all 3 ticket IDs are non-empty
    if [ -n "$cid1" ] && [ -n "$cid2" ] && [ -n "$cid3" ]; then
        assert_eq "concurrent: all 3 ticket IDs are non-empty" "all-non-empty" "all-non-empty"
    else
        assert_eq "concurrent: all 3 ticket IDs are non-empty" "all-non-empty" "some-empty: '${cid1:-}' '${cid2:-}' '${cid3:-}'"
        return
    fi

    # Assert: all 3 ticket IDs are unique (no collision under parallel load)
    if [ "$cid1" != "$cid2" ] && [ "$cid1" != "$cid3" ] && [ "$cid2" != "$cid3" ]; then
        assert_eq "concurrent: all 3 ticket IDs are unique" "unique" "unique"
    else
        assert_eq "concurrent: all 3 ticket IDs are unique" "unique" "collision: $cid1 $cid2 $cid3"
    fi

    # Assert: all 3 event files are committed to the tickets branch (no lost writes)
    local tracker_dir="$repo/.tickets-tracker"
    local event_count=0
    for cid in "$cid1" "$cid2" "$cid3"; do
        local ef
        ef=$(_find_create_event "$tracker_dir" "$cid")
        if [ -n "$ef" ]; then
            event_count=$((event_count + 1))
        fi
    done
    assert_eq "concurrent: all 3 event files present (no lost writes)" "3" "$event_count"

    # Assert: all 3 events are committed (verify via git log)
    # Capture git log output first to avoid SIGPIPE with grep -qF + pipefail
    local git_log_out
    git_log_out=$(git -C "$tracker_dir" log --oneline 2>/dev/null) || true
    local committed_count=0
    for cid in "$cid1" "$cid2" "$cid3"; do
        if [[ "$git_log_out" == *"$cid"* ]]; then
            committed_count=$((committed_count + 1))
        fi
    done
    assert_eq "concurrent: all 3 events are git-committed" "3" "$committed_count"

    # Assert: ticket show works for all 3 (reducer can read all events)
    for cid in "$cid1" "$cid2" "$cid3"; do
        local show_exit=0
        (cd "$repo" && bash "$TICKET_SCRIPT" show "$cid" >/dev/null 2>/dev/null) || show_exit=$?
        assert_eq "concurrent: show $cid exits 0" "0" "$show_exit"
    done

    assert_pass_if_clean "test_concurrent_create_serialized_by_flock"
}
test_concurrent_create_serialized_by_flock

# ── Test 6: test_concurrent_create_exit_captured_without_pipefail ────────────
# Regression test for bug 6076-e8dc: concurrent create subshells must propagate
# the bash command's exit code explicitly, not rely on pipefail inheritance.
# RED: old pipeline pattern (cmd | tail -1 >file) returns 0 when cmd fails
#      even without pipefail — error goes undetected.
# GREEN: new pattern (out=$(cmd); echo | tail >file) correctly propagates exit.
echo "Test 6: concurrent create exit code propagated without relying on pipefail"
test_concurrent_create_exit_captured_without_pipefail() {
    _snapshot_fail
    local tmp_out
    tmp_out=$(mktemp)
    _CLEANUP_DIRS+=("$tmp_out")

    # Simulate the old pipeline pattern explicitly WITHOUT pipefail.
    # A failing command piped to tail returns 0 (tail's exit) without pipefail.
    local old_exit=0
    (set +o pipefail; false 2>/dev/null | tail -1 >"$tmp_out") &
    wait $! || old_exit=$?
    # If old_exit=0, the old pipeline pattern swallowed the failure.
    assert_eq "old-pipeline-without-pipefail: failure returns non-zero" \
        "zero-swallowed" "$([[ $old_exit -eq 0 ]] && echo zero-swallowed || echo non-zero)"

    # The new production pattern uses variable capture — exit code is explicit.
    # Simulate: `out=$(cmd) || exit $?` correctly propagates failures.
    local new_exit=0
    (set +o pipefail; _out=$(false 2>/dev/null) || exit $?; printf '%s\n' "$_out" | tail -1 >"$tmp_out") &
    wait $! || new_exit=$?
    assert_eq "new-pattern-without-pipefail: failure returns non-zero" \
        "non-zero" "$([[ $new_exit -ne 0 ]] && echo non-zero || echo zero)"

    assert_pass_if_clean "test_concurrent_create_exit_captured_without_pipefail"
}
test_concurrent_create_exit_captured_without_pipefail

print_summary
