#!/usr/bin/env bash
# tests/scripts/test-ticket-set-get-file-impact.sh
# RED integration tests for `ticket set-file-impact` and `ticket get-file-impact` subcommands.
#
# All tests MUST FAIL until set-file-impact and get-file-impact are implemented.
# Covers:
#   1. set-file-impact exits 0 and writes a FILE_IMPACT event file in the ticket dir
#   2. get-file-impact outputs a JSON array containing the set entry
#   3. ticket show includes a non-empty file_impact field after set-file-impact
#   4. set-file-impact with invalid JSON exits non-zero
#   5. set-file-impact with a JSON object (not array) exits non-zero
#   6. set-file-impact with [] exits 0 and get-file-impact returns []
#
# Usage: bash tests/scripts/test-ticket-set-get-file-impact.sh
# Returns: exit non-zero (RED) until both subcommands are implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-set-get-file-impact.sh ==="

# ── Helper: create a fresh temp git repo with ticket system initialized ────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: create a ticket and return its ID ─────────────────────────────────
_create_ticket() {
    local repo="$1"
    local ticket_type="${2:-task}"
    local title="${3:-Test ticket}"
    local out
    out=$(cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 \
        bash "$TICKET_SCRIPT" create "$ticket_type" "$title" 2>/dev/null) || true
    echo "$out" | tail -1
}

# ── Helper: count FILE_IMPACT event files in a ticket directory ───────────────
_count_file_impact_events() {
    local tracker_dir="$1"
    local ticket_id="$2"
    find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-FILE_IMPACT.json' ! -name '.*' \
        2>/dev/null | wc -l | tr -d ' '
}

# ── Test 1: set-file-impact exits 0 and writes FILE_IMPACT event ──────────────
echo "Test 1: ticket set-file-impact exits 0 and writes a FILE_IMPACT event file"
test_set_file_impact_happy_path() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "File impact test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for set-file-impact test" "non-empty" "empty"
        assert_pass_if_clean "test_set_file_impact_happy_path"
        return
    fi

    local before_count
    before_count=$(_count_file_impact_events "$tracker_dir" "$ticket_id")

    local exit_code=0
    (cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 \
        bash "$TICKET_SCRIPT" set-file-impact "$ticket_id" \
        '[{"path":"src/foo.py","reason":"modified"}]' 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "set-file-impact exits 0" "0" "$exit_code"

    # Assert: exactly one new FILE_IMPACT event file was written
    local after_count
    after_count=$(_count_file_impact_events "$tracker_dir" "$ticket_id")
    local new_events
    new_events=$(( after_count - before_count ))
    assert_eq "exactly one FILE_IMPACT event written" "1" "$new_events"

    # Assert: FILE_IMPACT event file contains correct data
    local fi_file
    fi_file=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-FILE_IMPACT.json' \
        ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ -z "$fi_file" ]; then
        assert_eq "FILE_IMPACT event file found" "found" "not-found"
        assert_pass_if_clean "test_set_file_impact_happy_path"
        return
    fi

    local schema_check
    schema_check=$(python3 - "$fi_file" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

errors = []

# Base schema: event_type must equal 'FILE_IMPACT'
if ev.get('event_type') != 'FILE_IMPACT':
    errors.append(f"event_type not FILE_IMPACT: {ev.get('event_type')!r}")

# Base schema: timestamp must be an integer
if not isinstance(ev.get('timestamp'), int):
    errors.append(f"timestamp not int: {type(ev.get('timestamp'))}")

# data.file_impact must be a list with the expected entry
data = ev.get('data', {})
if not isinstance(data, dict):
    errors.append(f"data not dict: {type(data)}")
else:
    fi = data.get('file_impact')
    if not isinstance(fi, list):
        errors.append(f"data.file_impact not list: {type(fi)}")
    elif len(fi) != 1:
        errors.append(f"data.file_impact has wrong length: {len(fi)}")
    else:
        entry = fi[0]
        if entry.get('path') != 'src/foo.py':
            errors.append(f"entry.path wrong: {entry.get('path')!r}")
        if entry.get('reason') != 'modified':
            errors.append(f"entry.reason wrong: {entry.get('reason')!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(2)
else:
    print("OK")
PYEOF
) || true

    if [ "$schema_check" = "OK" ]; then
        assert_eq "FILE_IMPACT event has correct schema and content" "OK" "OK"
    else
        assert_eq "FILE_IMPACT event has correct schema and content" "OK" "$schema_check"
    fi

    assert_pass_if_clean "test_set_file_impact_happy_path"
}
test_set_file_impact_happy_path

# ── Test 2: get-file-impact outputs JSON array containing the set entry ────────
echo "Test 2: ticket get-file-impact outputs JSON array containing the set entry"
test_get_file_impact_returns_set_value() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Get file impact test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for get-file-impact test" "non-empty" "empty"
        assert_pass_if_clean "test_get_file_impact_returns_set_value"
        return
    fi

    # First set a value
    (cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 \
        bash "$TICKET_SCRIPT" set-file-impact "$ticket_id" \
        '[{"path":"src/foo.py","reason":"modified"}]' 2>/dev/null) || true

    # Now get the value
    local exit_code=0
    local output
    output=$(cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 \
        bash "$TICKET_SCRIPT" get-file-impact "$ticket_id" 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "get-file-impact exits 0" "0" "$exit_code"

    # Assert: output is a JSON array containing the expected entry
    local content_check
    content_check=$(python3 - "$output" <<'PYEOF'
import json, sys

raw = sys.argv[1]
try:
    arr = json.loads(raw)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

if not isinstance(arr, list):
    print(f"NOT_LIST:{type(arr)}")
    sys.exit(2)

if len(arr) != 1:
    print(f"WRONG_LENGTH:{len(arr)}")
    sys.exit(3)

entry = arr[0]
errors = []
if entry.get('path') != 'src/foo.py':
    errors.append(f"path wrong: {entry.get('path')!r}")
if entry.get('reason') != 'modified':
    errors.append(f"reason wrong: {entry.get('reason')!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(4)
else:
    print("OK")
PYEOF
) || true

    if [ "$content_check" = "OK" ]; then
        assert_eq "get-file-impact output contains expected entry" "OK" "OK"
    else
        assert_eq "get-file-impact output contains expected entry" "OK" "$content_check"
    fi

    assert_pass_if_clean "test_get_file_impact_returns_set_value"
}
test_get_file_impact_returns_set_value

# ── Test 3: ticket show includes non-empty file_impact field ──────────────────
echo "Test 3: ticket show output includes a file_impact field with the matching array (not empty [])"
test_ticket_show_includes_file_impact() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Show file impact test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for show file_impact test" "non-empty" "empty"
        assert_pass_if_clean "test_ticket_show_includes_file_impact"
        return
    fi

    # Set file impact
    (cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 \
        bash "$TICKET_SCRIPT" set-file-impact "$ticket_id" \
        '[{"path":"src/foo.py","reason":"modified"}]' 2>/dev/null) || true

    # Run ticket show
    local exit_code=0
    local show_output
    show_output=$(cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 \
        bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || exit_code=$?

    # Assert: exits 0
    assert_eq "ticket show exits 0 after set-file-impact" "0" "$exit_code"

    # Assert: show output has file_impact with the expected array (not [])
    local fi_check
    fi_check=$(python3 - "$show_output" <<'PYEOF'
import json, sys

raw = sys.argv[1]
try:
    state = json.loads(raw)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

fi = state.get('file_impact')
if fi is None:
    print("MISSING_KEY:file_impact not in state")
    sys.exit(2)

if not isinstance(fi, list):
    print(f"NOT_LIST:{type(fi)}")
    sys.exit(3)

if len(fi) == 0:
    print("EMPTY_LIST:file_impact is []")
    sys.exit(4)

entry = fi[0]
errors = []
if entry.get('path') != 'src/foo.py':
    errors.append(f"path wrong: {entry.get('path')!r}")
if entry.get('reason') != 'modified':
    errors.append(f"reason wrong: {entry.get('reason')!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(5)
else:
    print("OK")
PYEOF
) || true

    if [ "$fi_check" = "OK" ]; then
        assert_eq "ticket show includes file_impact with expected entry" "OK" "OK"
    else
        assert_eq "ticket show includes file_impact with expected entry" "OK" "$fi_check"
    fi

    assert_pass_if_clean "test_ticket_show_includes_file_impact"
}
test_ticket_show_includes_file_impact

# ── Test 4: set-file-impact with invalid JSON exits non-zero ──────────────────
echo "Test 4: ticket set-file-impact with invalid JSON argument exits non-zero with validation error"
test_set_file_impact_rejects_invalid_json() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Validation test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for validation test" "non-empty" "empty"
        assert_pass_if_clean "test_set_file_impact_rejects_invalid_json"
        return
    fi

    local exit_code=0
    local combined_out
    combined_out=$(cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 \
        bash "$TICKET_SCRIPT" set-file-impact "$ticket_id" 'not-json' 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "set-file-impact invalid JSON: exits non-zero" "1" \
        "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: output does NOT contain the generic "Subcommands:" usage block
    # (which would mean the subcommand isn't registered yet).
    # A real implementation emits a specific validation error, not the dispatch help.
    if [[ "$combined_out" == *"Subcommands:"* ]]; then
        assert_eq "set-file-impact invalid JSON: subcommand recognized (no generic usage)" \
            "no-usage-block" "got-usage-block"
    else
        assert_eq "set-file-impact invalid JSON: subcommand recognized (no generic usage)" \
            "no-usage-block" "no-usage-block"
    fi

    assert_pass_if_clean "test_set_file_impact_rejects_invalid_json"
}
test_set_file_impact_rejects_invalid_json

# ── Test 5: set-file-impact with JSON object (not array) exits non-zero ───────
echo "Test 5: ticket set-file-impact with a JSON object (not array) exits non-zero with validation error"
test_set_file_impact_rejects_non_array() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Schema validation test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for schema validation test" "non-empty" "empty"
        assert_pass_if_clean "test_set_file_impact_rejects_non_array"
        return
    fi

    local exit_code=0
    local combined_out
    combined_out=$(cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 \
        bash "$TICKET_SCRIPT" set-file-impact "$ticket_id" '{"not":"an-array"}' 2>&1) || exit_code=$?

    # Assert: exits non-zero (must be an array, not an object)
    assert_eq "set-file-impact object input: exits non-zero" "1" \
        "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: output does NOT contain the generic "Subcommands:" usage block
    # (which would mean the subcommand isn't registered yet).
    # A real implementation emits a specific schema error, not the dispatch help.
    if [[ "$combined_out" == *"Subcommands:"* ]]; then
        assert_eq "set-file-impact object input: subcommand recognized (no generic usage)" \
            "no-usage-block" "got-usage-block"
    else
        assert_eq "set-file-impact object input: subcommand recognized (no generic usage)" \
            "no-usage-block" "no-usage-block"
    fi

    assert_pass_if_clean "test_set_file_impact_rejects_non_array"
}
test_set_file_impact_rejects_non_array

# ── Test 6: set-file-impact with [] exits 0 and get-file-impact returns [] ────
echo "Test 6: ticket set-file-impact with [] exits 0 and get-file-impact returns []"
test_set_file_impact_empty_array() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)
    local tracker_dir="$repo/.tickets-tracker"

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Empty array test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for empty array test" "non-empty" "empty"
        assert_pass_if_clean "test_set_file_impact_empty_array"
        return
    fi

    local before_count
    before_count=$(_count_file_impact_events "$tracker_dir" "$ticket_id")

    # Set empty array
    local exit_code=0
    (cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 \
        bash "$TICKET_SCRIPT" set-file-impact "$ticket_id" '[]' 2>/dev/null) || exit_code=$?

    # Assert: exits 0 (empty array is valid)
    assert_eq "set-file-impact []: exits 0" "0" "$exit_code"

    # Assert: a FILE_IMPACT event was written
    local after_count
    after_count=$(_count_file_impact_events "$tracker_dir" "$ticket_id")
    local new_events
    new_events=$(( after_count - before_count ))
    assert_eq "set-file-impact []: FILE_IMPACT event written" "1" "$new_events"

    # Assert: get-file-impact returns []
    local get_exit_code=0
    local get_output
    get_output=$(cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 \
        bash "$TICKET_SCRIPT" get-file-impact "$ticket_id" 2>/dev/null) || get_exit_code=$?

    # Assert: get exits 0
    assert_eq "get-file-impact after []: exits 0" "0" "$get_exit_code"

    # Assert: output is []
    local empty_check
    empty_check=$(python3 - "$get_output" <<'PYEOF'
import json, sys

raw = sys.argv[1].strip()
try:
    arr = json.loads(raw)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

if not isinstance(arr, list):
    print(f"NOT_LIST:{type(arr)}")
    sys.exit(2)

if len(arr) != 0:
    print(f"NOT_EMPTY:{arr}")
    sys.exit(3)

print("OK")
PYEOF
) || true

    if [ "$empty_check" = "OK" ]; then
        assert_eq "get-file-impact after [] returns []" "OK" "OK"
    else
        assert_eq "get-file-impact after [] returns []" "OK" "$empty_check"
    fi

    assert_pass_if_clean "test_set_file_impact_empty_array"
}
test_set_file_impact_empty_array

# ── Test 7: per-element schema validation (bug 100b-6146) ─────────────────────
# Only array-ness was validated; junk elements violating the {path,reason}
# contract were stored. set-file-impact must reject any element that is not an
# object with string keys "path" and "reason", naming the offending index.
echo "Test 7: ticket set-file-impact rejects malformed elements naming the bad index (RED before 100b fix)"
test_set_file_impact_rejects_bad_elements() {
    _snapshot_fail

    local repo
    repo=$(_make_test_repo)

    local ticket_id
    ticket_id=$(_create_ticket "$repo" task "Element validation test ticket")

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket created for element validation test" "non-empty" "empty"
        assert_pass_if_clean "test_set_file_impact_rejects_bad_elements"
        return
    fi

    # Case A: scalar elements (not objects) -> exit non-zero, names index 0
    local ec_a=0 out_a
    out_a=$(cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 \
        bash "$TICKET_SCRIPT" set-file-impact "$ticket_id" '[42,"string"]' 2>&1) || ec_a=$?
    assert_eq "scalar elements: exits non-zero" "1" \
        "$([ "$ec_a" -ne 0 ] && echo 1 || echo 0)"
    if [[ "$out_a" == *"file_impact[0]"* ]]; then
        assert_eq "scalar elements: error names index 0" "names-0" "names-0"
    else
        assert_eq "scalar elements: error names index 0" "names-0" "$out_a"
    fi

    # Case B: missing "path" key -> exit non-zero, names index 0
    local ec_b=0 out_b
    out_b=$(cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 \
        bash "$TICKET_SCRIPT" set-file-impact "$ticket_id" '[{"reason":"x"}]' 2>&1) || ec_b=$?
    assert_eq "missing path: exits non-zero" "1" \
        "$([ "$ec_b" -ne 0 ] && echo 1 || echo 0)"
    if [[ "$out_b" == *"file_impact[0]"* ]]; then
        assert_eq "missing path: error names index 0" "names-0" "names-0"
    else
        assert_eq "missing path: error names index 0" "names-0" "$out_b"
    fi

    # Case C: valid first, malformed second (path not string) -> names index 1
    local ec_c=0 out_c
    out_c=$(cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 \
        bash "$TICKET_SCRIPT" set-file-impact "$ticket_id" \
        '[{"path":"a","reason":"b"},{"path":7}]' 2>&1) || ec_c=$?
    assert_eq "bad second element: exits non-zero" "1" \
        "$([ "$ec_c" -ne 0 ] && echo 1 || echo 0)"
    if [[ "$out_c" == *"file_impact[1]"* ]]; then
        assert_eq "bad second element: error names index 1" "names-1" "names-1"
    else
        assert_eq "bad second element: error names index 1" "names-1" "$out_c"
    fi

    # Case D: fully valid array -> exit 0 (regression guard)
    local ec_d=0
    (cd "$repo" && \
        _TICKET_TEST_NO_SYNC=1 \
        bash "$TICKET_SCRIPT" set-file-impact "$ticket_id" \
        '[{"path":"src/foo.py","reason":"modified"}]' 2>/dev/null) || ec_d=$?
    assert_eq "valid array still exits 0" "0" "$ec_d"

    assert_pass_if_clean "test_set_file_impact_rejects_bad_elements"
}
test_set_file_impact_rejects_bad_elements

print_summary
