#!/usr/bin/env bash
# tests/scripts/suites/test-ticket-subprocess-count.sh
# RED structural tests: verify ticket-list.sh, ticket-show.sh, and ticket-transition.sh
# use at most one python3 subprocess per logical pipeline branch after consolidation.
#
# These are static source-structure tests — they grep/awk the script files and count
# python3 invocations in specific sections. They do NOT intercept live subprocesses.
#
# Current counts (before S2-T4 consolidation):
#   ticket-list.sh LLM branch:         2 python3 calls (filter + importlib dance)
#   ticket-list.sh default branch:      2 python3 calls (filter + heredoc inline)
#   ticket-show.sh LLM pathway:         4 python3 calls (reducer + llm-fmt + pretty + bridge)
#   ticket-transition.sh epic-close:    2 python3 calls (reducer + type extraction)
#
# After S2-T4 consolidation all of the above must be ≤1.
#
# Tests 5 (flock section) verifies the invariant is maintained at exactly 1.
#
# Usage: bash tests/scripts/suites/test-ticket-subprocess-count.sh
# Returns: exit non-zero (RED) until consolidation is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"

TRANSITION_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-transition.sh"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-ticket-subprocess-count.sh ==="

# NOTE (story 23d2-e0f3): the former Tests 1-3 counted python3 spawns inside the
# ticket-list.sh / ticket-show.sh read-shim heredocs. Those shims were deleted
# when the dual read path was collapsed into a single in-process implementation
# (ticket-reads.py) — reads now run in ONE python3 process by construction, so
# the per-heredoc spawn-count assertions no longer have a subject. The write-path
# subprocess-count guards below (transition + write_commit_event) are unaffected
# and remain.

# ── Test 5: ticket-transition.sh main flock pipeline uses exactly 1 python3 ───
# The flock section (lines ~222-352) must contain exactly 1 python3 -c invocation.
# This is an invariant test — it verifies the flock block was NOT accidentally
# split into multiple subprocesses during consolidation work.
echo "Test 5: ticket-transition.sh main flock section uses exactly 1 python3 subprocess"
test_transition_flock_section_exactly_one_python3() {
    if [ ! -f "$TRANSITION_SCRIPT" ]; then
        assert_eq "ticket-transition.sh exists" "exists" "missing"
        return
    fi

    # Count python3 invocations from the flock comment to the 'flock_exit=$?' capture.
    # Uses ^\s*python3 since the flock block's python3 is at the start of a line.
    local flock_count
    flock_count=$(awk '
        /# The entire read-verify-write is done inside python3/ { in_flock=1 }
        /flock_exit=\$\?/ && in_flock { in_flock=0 }
        in_flock && /^\s*python3/ { count++ }
        END { print count+0 }
    ' "$TRANSITION_SCRIPT")

    # Invariant: flock block must contain exactly 1 python3 invocation (already true).
    # This is a GREEN invariant test — it verifies the implementation did not regress.
    assert_eq "ticket-transition.sh flock section python3 count is exactly 1" "1" "$flock_count"
}
test_transition_flock_section_exactly_one_python3

# ── Test 6: write_commit_event (CREATE and COMMENT events) spawns zero python3 ──
# GREEN invariant: write_commit_event now uses jq (bash-native) instead of
# python3 for JSON serialization.  Calling write_commit_event for a CREATE event
# (the ticket create write path) and a COMMENT event (the ticket comment write
# path) must each produce zero python3 spawns.
#
# Uses the same sentinel/shim pattern as test-ticket-write-commit-event.sh
# (Test 1): a python3 wrapper in a temp PATH dir counts invocations and then
# execs the real python3 so correctness is preserved.
echo "Test 6: write_commit_event for CREATE and COMMENT events spawns zero python3"
test_write_op_python3_count_zero() {
    # Resolve constants needed inside the function.
    local _script_dir
    _script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local _repo_root
    _repo_root="$(git -C "$_script_dir" rev-parse --show-toplevel)"
    local _ticket_script="$_repo_root/src/rebar/_engine/ticket"
    local _ticket_lib="$_repo_root/src/rebar/_engine/ticket-lib.sh"

    # Resolve real python3 for the shim's exec delegation.
    local _real_python3
    _real_python3="$(command -v python3 2>/dev/null || true)"
    if [ -z "$_real_python3" ] || [ ! -x "$_real_python3" ]; then
        _real_python3="/usr/bin/python3"
    fi

    # ── Setup: fresh ticket repo ──────────────────────────────────────────────
    local _tmp
    _tmp=$(mktemp -d)
    git init -q -b main "$_tmp"
    git -C "$_tmp" config user.email "test@test.com"
    git -C "$_tmp" config user.name "Test"
    git -C "$_tmp" config commit.gpgsign false
    echo "initial" > "$_tmp/README.md"
    git -C "$_tmp" add -A
    git -C "$_tmp" commit -q -m "init"
    (cd "$_tmp" && _TICKET_TEST_NO_SYNC=1 bash "$_ticket_script" init >/dev/null 2>&1) || {
        assert_eq "write_op: ticket init succeeded" "0" "non-zero"
        rm -rf "$_tmp"
        return
    }
    # Guard: ticket init exits 0 but on macOS temp volumes gc.auto can fail if
    # .tickets-tracker isn't present yet; skip non-essential gc config safely.
    [[ -d "$_tmp/.tickets-tracker" ]] && git -C "$_tmp/.tickets-tracker" config gc.auto 0 || true

    # Create a ticket to use as a target for event writes.
    local _ticket_id
    _ticket_id=$(cd "$_tmp" && _TICKET_TEST_NO_SYNC=1 bash "$_ticket_script" create task "Subprocess count test" 2>/dev/null | tr -d '[:space:]')
    if [ -z "$_ticket_id" ]; then
        assert_eq "write_op: ticket create setup returned ID" "non-empty" "empty"
        rm -rf "$_tmp"
        return
    fi

    # ── Sentinel shim ─────────────────────────────────────────────────────────
    local _sentinel
    _sentinel=$(mktemp)
    rm -f "$_sentinel"
    local _shim_dir
    _shim_dir=$(mktemp -d)
    cat > "$_shim_dir/python3" <<SHIMEOF
#!/usr/bin/env bash
echo "CALLED" >> "$_sentinel"
exec "$_real_python3" "\$@"
SHIMEOF
    chmod +x "$_shim_dir/python3"

    # ── Helper: build a minimal CREATE event JSON for the given ticket ────────
    local _event_json_create
    _event_json_create=$(mktemp)
    "$_real_python3" - "$_event_json_create" "$_ticket_id" "Subprocess count test" <<'PYEOF'
import json, sys, uuid, datetime
out, tid, title = sys.argv[1], sys.argv[2], sys.argv[3]
ev = {"event_type": "CREATE",
      "timestamp": datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S%f") + "Z",
      "uuid": str(uuid.uuid4()).replace("-","")[:12],
      "data": {"ticket_id": tid, "title": title, "type": "task",
               "priority": 4, "status": "open", "tags": []}}
with open(out, "w", encoding="utf-8") as f:
    json.dump(ev, f, ensure_ascii=False)
PYEOF

    # ── Helper: build a COMMENT event JSON ────────────────────────────────────
    local _event_json_comment
    _event_json_comment=$(mktemp)
    "$_real_python3" - "$_event_json_comment" "$_ticket_id" "Test comment" <<'PYEOF'
import json, sys, uuid, datetime
out, tid, body = sys.argv[1], sys.argv[2], sys.argv[3]
ev = {"event_type": "COMMENT",
      "timestamp": datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S%f") + "Z",
      "uuid": str(uuid.uuid4()).replace("-","")[:12],
      "data": {"ticket_id": tid, "body": body}}
with open(out, "w", encoding="utf-8") as f:
    json.dump(ev, f, ensure_ascii=False)
PYEOF

    # ── Test 6a: write_commit_event for CREATE event (ticket create write path) ──
    rm -f "$_sentinel"
    local _create_exit=0
    (
        cd "$_tmp"
        PATH="$_shim_dir:$PATH" _TICKET_TEST_NO_SYNC=1 bash -c "
            source '$_ticket_lib'
            write_commit_event '$_ticket_id' '$_event_json_create'
        " 2>/dev/null
    ) || _create_exit=$?

    local _create_calls=0
    if [ -f "$_sentinel" ]; then
        _create_calls=$(wc -l < "$_sentinel" | tr -d ' ')
    fi
    rm -f "$_sentinel"

    assert_eq "write_op: write_commit_event(CREATE) exits 0" "0" "$_create_exit"
    assert_eq "write_op: write_commit_event(CREATE) python3 spawn count = 0" "0" "$_create_calls"

    # ── Test 6b: write_commit_event for COMMENT event (ticket comment write path) ──
    rm -f "$_sentinel"
    local _comment_exit=0
    (
        cd "$_tmp"
        PATH="$_shim_dir:$PATH" _TICKET_TEST_NO_SYNC=1 bash -c "
            source '$_ticket_lib'
            write_commit_event '$_ticket_id' '$_event_json_comment'
        " 2>/dev/null
    ) || _comment_exit=$?

    local _comment_calls=0
    if [ -f "$_sentinel" ]; then
        _comment_calls=$(wc -l < "$_sentinel" | tr -d ' ')
    fi
    rm -f "$_sentinel"

    assert_eq "write_op: write_commit_event(COMMENT) exits 0" "0" "$_comment_exit"
    assert_eq "write_op: write_commit_event(COMMENT) python3 spawn count = 0" "0" "$_comment_calls"

    rm -f "$_event_json_create" "$_event_json_comment"
    rm -rf "$_shim_dir" "$_tmp"
}
test_write_op_python3_count_zero

print_summary
