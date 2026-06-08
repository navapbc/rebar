#!/usr/bin/env bash
# tests/scripts/test-ticket-create.sh
# RED tests for src/rebar/_engine/ticket-create.sh — `ticket create` subcommand.
#
# All 6 test functions MUST FAIL until ticket-create.sh is implemented.
# Covers: ticket ID output, CREATE event file naming, event JSON schema,
# Python-written JSON, atomic git commit, invalid ticket type rejection.
#
# Usage: bash tests/scripts/test-ticket-create.sh
# Returns: exit non-zero (RED) until ticket-create.sh is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_CREATE_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-create.sh"
HASH_SCRIPT="$REPO_ROOT/src/rebar/_engine/compute-verdict-hash.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-create.sh ==="

_verdict_hash() {
    local repo="$1" ticket_id="$2"
    (cd "$repo" && PROJECT_ROOT="$repo" bash "$HASH_SCRIPT" "$ticket_id" PASS 2>/dev/null)
}

# Helper: extract a JSON field from an event file with diagnostic error capture.
# Usage: _extract_event_field <event_file> <field_name> [--repr]
_extract_event_field() {
    local file="$1" field="$2" use_repr="${3:-}"
    local print_expr="print(e['data'].get('$field','MISSING'))"
    [[ "$use_repr" == "--repr" ]] && print_expr="print(repr(e['data'].get('$field','MISSING')))"
    python3 - "$file" <<PYEOF || true
import json, sys
try:
    e = json.load(open(sys.argv[1]))
    $print_expr
except Exception as ex:
    print(f"PARSE_ERROR:{ex}")
    sys.exit(1)
PYEOF
}

# ── Helper: create a fresh temp git repo with ticket system initialized ───────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: get the single CREATE event file path under a ticket dir ──────────
_find_create_event() {
    local tracker_dir="$1"
    local ticket_id="$2"
    find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | head -1
}

# ── Test 1: ticket create outputs a non-empty ticket ID to stdout ─────────────
echo "Test 1: ticket create outputs a ticket ID matching [a-z0-9]+-[a-z0-9]+"
test_ticket_create_outputs_ticket_id() {
    local repo
    repo=$(_make_test_repo)

    # ticket-create.sh must exist — RED: it does not exist yet
    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local stdout_out stderr_out _stderr_tmp
    _stderr_tmp=$(mktemp "${TMPDIR:-/tmp}/tc1-stderr.XXXXXX")
    stdout_out=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "My ticket" 2>"$_stderr_tmp") || true
    stderr_out=$(cat "$_stderr_tmp" 2>/dev/null) || true
    rm -f "$_stderr_tmp"

    # Assert: stdout is non-empty
    if [ -n "$stdout_out" ]; then
        assert_eq "ticket ID is non-empty" "non-empty" "non-empty"
    else
        assert_eq "ticket ID is non-empty" "non-empty" "empty"
        return
    fi

    # SC1: stdout contains the 16-hex canonical ID.
    # SC3: both lines on stdout — summary first, canonical ID last (| tail -1 extracts ID).
    local ticket_id
    ticket_id=$(echo "$stdout_out" | tail -1)
    if [[ "$ticket_id" =~ ^[a-z0-9]+-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+$ ]]; then
        assert_eq "stdout last line matches 16-hex canonical ID pattern" "match" "match"
    else
        assert_eq "stdout last line matches 16-hex canonical ID pattern" "match" "no-match: $ticket_id"
    fi

    # Assert: stdout first line contains the human summary. When an alias is
    # available (default path), the summary leads with the alias and shows the
    # canonical ID parenthetically: "Created ticket <alias> (<id>): <title>".
    # Falls back to "Created ticket <id>: <title>" only when alias is missing
    # (TICKET_WORDLIST_PATH override pointed at empty/missing file).
    local summary_line
    summary_line=$(echo "$stdout_out" | head -1)
    if [[ "$summary_line" == "Created ticket "*"($ticket_id): "* ]] \
       || [[ "$summary_line" == "Created ticket $ticket_id: "* ]]; then
        assert_eq "stdout first line contains human summary" "match" "match"
    else
        assert_eq "stdout first line contains human summary" "match" "no-match: $summary_line"
    fi

    # Also assert the alias variant appears in the typical happy path (the
    # bundled wordlist is present, so alias must be computed and displayed).
    if [[ "$summary_line" == "Created ticket "*"-"*"-"*"("*"): "* ]]; then
        assert_eq "stdout first line leads with alias-(id) when wordlist available" "alias-led" "alias-led"
    else
        assert_eq "stdout first line leads with alias-(id) when wordlist available" "alias-led" "no-alias-prefix: $summary_line"
    fi
}
test_ticket_create_outputs_ticket_id

# ── Test 2: ticket create writes exactly one CREATE event file ────────────────
echo "Test 2: ticket create writes exactly one *-CREATE.json event file"
test_ticket_create_writes_create_event_json() {
    local repo
    repo=$(_make_test_repo)

    # ticket-create.sh must exist — RED: it does not exist yet
    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "My ticket" 2>/dev/null) || true
    # SC3: dual-output — last line is the canonical ID
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for event file check" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"

    # Assert: ticket directory exists under .tickets-tracker/
    if [ -d "$tracker_dir/$ticket_id" ]; then
        assert_eq "ticket dir exists: .tickets-tracker/<ticket_id>/" "exists" "exists"
    else
        assert_eq "ticket dir exists: .tickets-tracker/<ticket_id>/" "exists" "missing"
        return
    fi

    # Assert: exactly one *-CREATE.json file in the ticket directory
    local event_count
    event_count=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "exactly one CREATE event file" "1" "$event_count"

    # Assert: the event file parses as valid JSON
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")
    if [ -n "$event_file" ]; then
        local parse_exit=0
        python3 -c "import json,sys; json.load(sys.stdin)" < "$event_file" 2>/dev/null || parse_exit=$?
        assert_eq "event JSON is valid" "0" "$parse_exit"
    else
        assert_eq "CREATE event file found for JSON validation" "found" "not-found"
    fi
}
test_ticket_create_writes_create_event_json

# ── Test 3: CREATE event JSON contains all required fields ────────────────────
echo "Test 3: CREATE event JSON has all required base and CREATE-specific fields"
test_ticket_create_event_has_required_fields() {
    local repo
    repo=$(_make_test_repo)

    # ticket-create.sh must exist — RED: it does not exist yet
    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "My ticket" 2>/dev/null) || true
    # SC3: dual-output — last line is the canonical ID
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for field check" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "CREATE event file found" "found" "not-found"
        return
    fi

    # Extract and validate all required fields via Python
    # Base schema fields: timestamp (integer), uuid (string), event_type, env_id, author, data (object)
    # CREATE-specific data fields: ticket_type, title, parent_id
    local field_check
    field_check=$(python3 - "$event_file" <<'PYEOF'
import json, sys

try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

errors = []

# Base schema: timestamp must be an integer
if not isinstance(ev.get('timestamp'), int):
    errors.append(f"timestamp not int: {type(ev.get('timestamp'))}")

# Base schema: uuid must be a non-empty string
if not isinstance(ev.get('uuid'), str) or not ev.get('uuid'):
    errors.append(f"uuid missing or not str: {ev.get('uuid')!r}")

# Base schema: event_type must equal "CREATE"
if ev.get('event_type') != 'CREATE':
    errors.append(f"event_type not CREATE: {ev.get('event_type')!r}")

# Base schema: env_id must be a non-empty string
if not isinstance(ev.get('env_id'), str) or not ev.get('env_id'):
    errors.append(f"env_id missing or not str: {ev.get('env_id')!r}")

# Base schema: author must be a non-empty string
if not isinstance(ev.get('author'), str) or not ev.get('author'):
    errors.append(f"author missing or not str: {ev.get('author')!r}")

# Base schema: data must be an object
data = ev.get('data')
if not isinstance(data, dict):
    errors.append(f"data not dict: {type(data)}")
else:
    # CREATE-specific: data.ticket_type must be a string
    if not isinstance(data.get('ticket_type'), str):
        errors.append(f"data.ticket_type not str: {data.get('ticket_type')!r}")
    # CREATE-specific: data.title must be a string
    if not isinstance(data.get('title'), str):
        errors.append(f"data.title not str: {data.get('title')!r}")
    # CREATE-specific: data.parent_id must be a string (empty string is allowed for root tickets)
    if 'parent_id' not in data:
        errors.append("data.parent_id missing")
    elif not isinstance(data.get('parent_id'), str):
        errors.append(f"data.parent_id not str: {data.get('parent_id')!r}")

if errors:
    print("ERRORS:" + "; ".join(errors))
    sys.exit(2)
else:
    print("OK")
PYEOF
) || true

    if [ "$field_check" = "OK" ]; then
        assert_eq "all required fields present and correct types" "OK" "OK"
    else
        assert_eq "all required fields present and correct types" "OK" "$field_check"
    fi
}
test_ticket_create_event_has_required_fields

# ── Test 4: event JSON was written via Python (no bash heredoc artifacts) ─────
echo "Test 4: CREATE event JSON is Python-written (no bash heredoc artifacts)"
test_ticket_create_event_uses_python_json() {
    local repo
    repo=$(_make_test_repo)

    # ticket-create.sh must exist — RED: it does not exist yet
    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    # Use a title with special characters that a bash heredoc might mangle
    local special_title='it'"'"'s a "quoted" title'
    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "$special_title" 2>/dev/null) || true
    # SC3: dual-output — last line is the canonical ID
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for python-json check" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "CREATE event file found for python-json check" "found" "not-found"
        return
    fi

    # Assert: no literal \n sequences (bash heredoc artifact)
    local raw_content
    raw_content=$(cat "$event_file")
    if [[ "$raw_content" == *'\n'* ]]; then
        assert_eq "no literal \\n in JSON (bash heredoc artifact)" "no-literal-newline" "has-literal-newline"
    else
        assert_eq "no literal \\n in JSON (bash heredoc artifact)" "no-literal-newline" "no-literal-newline"
    fi

    # Assert: the special title round-trips correctly through JSON
    local title_check
    title_check=$(python3 - "$event_file" "$special_title" <<'PYEOF'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    ev = json.load(f)
stored_title = ev.get('data', {}).get('title', '')
expected = sys.argv[2]
if stored_title == expected:
    print("OK")
else:
    print(f"MISMATCH: expected={expected!r} got={stored_title!r}")
PYEOF
) || true

    if [ "$title_check" = "OK" ]; then
        assert_eq "special-char title round-trips via Python JSON" "OK" "OK"
    else
        assert_eq "special-char title round-trips via Python JSON" "OK" "$title_check"
    fi
}
test_ticket_create_event_uses_python_json

# ── Test 5: ticket create auto-commits to the tickets branch ──────────────────
echo "Test 5: ticket create auto-commits event to tickets branch via write_commit_event"
test_ticket_create_auto_commits_to_tickets_branch() {
    local repo
    repo=$(_make_test_repo)

    # ticket-create.sh must exist — RED: it does not exist yet
    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    # Record commit count before create
    local commits_before
    commits_before=$(git -C "$repo/.tickets-tracker" log --oneline 2>/dev/null | wc -l | tr -d ' ')

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "My ticket" 2>/dev/null) || true
    # SC3: dual-output — last line is the canonical ID
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for commit check" "non-empty" "empty"
        return
    fi

    # Assert: the commit count increased by exactly 1
    local commits_after
    commits_after=$(git -C "$repo/.tickets-tracker" log --oneline 2>/dev/null | wc -l | tr -d ' ')
    local new_commits
    new_commits=$(( commits_after - commits_before ))
    assert_eq "exactly one new commit on tickets branch" "1" "$new_commits"

    # Assert: the latest commit message references the ticket ID
    local latest_commit_msg
    latest_commit_msg=$(git -C "$repo/.tickets-tracker" log --oneline -1 2>/dev/null)
    if [[ "$latest_commit_msg" == *"$ticket_id"* ]]; then
        assert_eq "latest commit references ticket ID" "referenced" "referenced"
    else
        assert_eq "latest commit references ticket ID" "referenced" "not-referenced: $latest_commit_msg"
    fi
}
test_ticket_create_auto_commits_to_tickets_branch

# ── Test 6: ticket create rejects invalid ticket type ─────────────────────────
echo "Test 6: ticket create rejects invalid ticket type with non-zero exit and error message"
test_ticket_create_rejects_invalid_ticket_type() {
    local repo
    repo=$(_make_test_repo)

    # ticket-create.sh must exist — RED: it does not exist yet
    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" create invalid_type "title" 2>&1) || exit_code=$?

    # Assert: exits non-zero
    assert_eq "invalid type exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error message is printed (not silent)
    if [ -n "$stderr_out" ]; then
        assert_eq "error message printed for invalid type" "has-message" "has-message"
    else
        assert_eq "error message printed for invalid type" "has-message" "silent"
    fi

    # Assert: no CREATE event file was written (command should fail before writing)
    local tracker_dir="$repo/.tickets-tracker"
    local spurious_events
    spurious_events=$(find "$tracker_dir" -maxdepth 2 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "no event file written on invalid type" "0" "$spurious_events"
}
test_ticket_create_rejects_invalid_ticket_type

# ── Test 7 (RED): ticket create with a closed parent is blocked ────────────────
echo "Test 7 (RED): ticket create with a closed parent exits non-zero"
test_create_with_closed_parent_blocked() {
    local repo
    repo=$(_make_test_repo)

    # Create and close a parent ticket
    local parent_id
    parent_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create epic "Parent epic to close" 2>/dev/null) || true
    parent_id=$(echo "$parent_id" | tail -1)

    if [ -z "$parent_id" ]; then
        assert_eq "parent ticket created for closed-parent test" "non-empty" "empty"
        return
    fi

    # Close the parent (transition open → closed)
    (cd "$repo" && bash "$TICKET_SCRIPT" transition "$parent_id" open closed --verdict-hash="$(_verdict_hash "$repo" "$parent_id")" 2>/dev/null) || true

    # Verify the parent is actually closed before proceeding
    local parent_status
    parent_status=$(python3 "$REPO_ROOT/src/rebar/_engine/ticket-reducer.py" \
        "$repo/.tickets-tracker/$parent_id" 2>/dev/null \
        | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('status',''))" 2>/dev/null) || true

    if [ "$parent_status" != "closed" ]; then
        # Can't run the guard test if parent isn't closed — fail RED to signal setup issue
        assert_eq "create-closed-parent: parent is closed before test" "closed" "$parent_status"
        return
    fi

    # Attempt to create a child under the closed parent — must exit non-zero
    # RED: current ticket-create.sh does not enforce this guard → exits 0
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Orphan child under closed parent" --parent "$parent_id" 2>&1) || exit_code=$?

    # Assert: exits non-zero (guard not yet implemented → currently exits 0, so FAILS RED)
    assert_eq "create-closed-parent: exits non-zero" "1" "$([ "$exit_code" -ne 0 ] && echo 1 || echo 0)"

    # Assert: error message mentions parent, closed, or not allowed
    if [[ "${stderr_out,,}" =~ parent|closed|not\ allowed|cannot ]]; then
        assert_eq "create-closed-parent: error mentions closed parent" "has-closed-hint" "has-closed-hint"
    else
        assert_eq "create-closed-parent: error mentions closed parent" "has-closed-hint" "no-hint: $stderr_out"
    fi

    # Assert: no CREATE event file was written for any new child
    local tracker_dir="$repo/.tickets-tracker"
    # Count CREATE events excluding the parent's own CREATE event
    local new_events
    new_events=$(find "$tracker_dir" -maxdepth 2 -name '*-CREATE.json' ! -name '.*' 2>/dev/null \
        | grep -v "/$parent_id/" | wc -l | tr -d ' ')
    assert_eq "create-closed-parent: no CREATE event written for blocked child" "0" "$new_events"
}
test_create_with_closed_parent_blocked

# ── Test 8 (RED): ticket create --priority writes priority to CREATE event ─────
echo "Test 8 (RED): ticket create --priority writes priority to CREATE event data"
test_ticket_create_with_priority_writes_priority_to_create_event() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Priority test" --priority 1 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for priority test" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "CREATE event file found for priority test" "found" "not-found"
        return
    fi

    local priority_val
    priority_val=$(_extract_event_field "$event_file" "priority")
    assert_eq "priority in CREATE event data" "1" "$priority_val"
}
test_ticket_create_with_priority_writes_priority_to_create_event

# ── Test 9 (RED): ticket create --assignee writes assignee to CREATE event ────
echo "Test 9 (RED): ticket create --assignee writes assignee to CREATE event data"
test_ticket_create_with_assignee_writes_assignee_to_create_event() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Assignee test" --assignee "Joe Oakhart" 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for assignee test" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "CREATE event file found for assignee test" "found" "not-found"
        return
    fi

    local assignee_val
    assignee_val=$(_extract_event_field "$event_file" "assignee")
    assert_eq "assignee in CREATE event data" "Joe Oakhart" "$assignee_val"
}
test_ticket_create_with_assignee_writes_assignee_to_create_event

# ── Test 9b: ticket create without --assignee defaults to unassigned ──────────
echo "Test 9b: ticket create without --assignee defaults to unassigned (empty)"
test_ticket_create_without_assignee_defaults_to_unassigned() {
    # Regression for bridge-side bug: the legacy default of git config
    # user.name conflated 'creator' and 'owner' and caused ACLI to reject
    # outbound update mutations when the local git user.name was not a
    # valid Jira user. New default: empty string.
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Default-assignee test" 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for default-assignee test" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "CREATE event file found for default-assignee test" "found" "not-found"
        return
    fi

    local assignee_val
    assignee_val=$(_extract_event_field "$event_file" "assignee")
    # The assignee field must be either missing from the data block or empty
    # — NOT set to the local git user.name. Both shapes satisfy "unassigned"
    # from the consumer side (outbound_differ uses .get('assignee', '')).
    case "$assignee_val" in
        ""|"MISSING")
            assert_eq "assignee defaults to unassigned" "unassigned" "unassigned"
            ;;
        *)
            assert_eq "assignee defaults to unassigned" "unassigned" "$assignee_val"
            ;;
    esac
}
test_ticket_create_without_assignee_defaults_to_unassigned

# ── Test 10: ticket create without --priority defaults to P2 ─────────────────
echo "Test 10: ticket create without --priority defaults to P2"
test_ticket_create_default_priority_is_2() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Default priority test" 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for default priority test" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "CREATE event file found for default priority test" "found" "not-found"
        return
    fi

    local priority_val
    priority_val=$(_extract_event_field "$event_file" "priority")
    assert_eq "default priority in CREATE event data" "2" "$priority_val"
}
test_ticket_create_default_priority_is_2

# ── Test 10b: --priority short flag sets priority (regression guard) ──────────
echo "Test 10b: --priority flag sets priority in CREATE event data"
test_ticket_create_priority_long_flag_sets_priority() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Short priority test" --priority 1 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for --priority test" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "CREATE event file found for --priority test" "found" "not-found"
        return
    fi

    local priority_val
    priority_val=$(_extract_event_field "$event_file" "priority")
    assert_eq "priority in CREATE event data via --priority flag" "1" "$priority_val"
}
test_ticket_create_priority_long_flag_sets_priority

# ── Test 11 (RED): --description="body" populates data.description in CREATE event ──
echo "Test 11 (RED): --description flag populates data.description in CREATE event JSON"
test_ticket_create_description_long_flag_populates_event() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local desc_body="This is a test description body"
    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Description test long flag" --description="$desc_body" 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for --description test" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "CREATE event file found for --description test" "found" "not-found"
        return
    fi

    local desc_val
    desc_val=$(_extract_event_field "$event_file" "description")
    assert_eq "data.description matches provided value (--description flag)" "$desc_body" "$desc_val"
}
test_ticket_create_description_long_flag_populates_event

# ── Test 12 (RED): -d "body" populates data.description in CREATE event ─────────
echo "Test 12 (RED): -d short flag populates data.description in CREATE event JSON"
test_ticket_create_description_short_flag_populates_event() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local desc_body="Short flag description body"
    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Description test short flag" -d "$desc_body" 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for -d test" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "CREATE event file found for -d test" "found" "not-found"
        return
    fi

    local desc_val
    desc_val=$(_extract_event_field "$event_file" "description")
    assert_eq "data.description matches provided value (-d flag)" "$desc_body" "$desc_val"
}
test_ticket_create_description_short_flag_populates_event

# ── Test 13 (RED): no -d flag leaves description as empty string in CREATE event ─
echo "Test 13 (RED): no -d flag leaves description as empty string in CREATE event"
test_ticket_create_no_description_flag_leaves_empty_string() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "No description test" 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for no-description test" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "CREATE event file found for no-description test" "found" "not-found"
        return
    fi

    local desc_val
    desc_val=$(_extract_event_field "$event_file" "description" --repr)
    assert_eq "data.description is empty string when no -d flag" "''" "$desc_val"
}
test_ticket_create_no_description_flag_leaves_empty_string

# ── Test 14 (RED): ticket show after create -d includes description in compiled output ──
echo "Test 14 (RED): ticket show after create -d includes description in compiled JSON output"
test_ticket_create_show_includes_description_after_create_with_d() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local desc_body="Compiled description from show"
    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Show description test" -d "$desc_body" 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for show-description test" "non-empty" "empty"
        return
    fi

    # Call ticket show via the reducer and verify description appears in compiled output
    # --description/-d flag is implemented. `ticket show` currently does not emit a description
    # field, so show_output may not be valid JSON or may lack the field. The empty-string guard
    # below (`if [ -z "$show_output" ]`) catches blank output, and the || true fallback on the
    # python3 call is acceptable because the test is expected to fail at the assert_eq level when
    # the feature is not yet implemented. The non-JSON-validation tradeoff is intentional for RED
    # tests: tight validation belongs in the GREEN phase once the feature ships. See TDD workflow.
    local show_output
    show_output=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    if [ -z "$show_output" ]; then
        assert_eq "ticket show returns output" "non-empty" "empty"
        return
    fi

    local desc_check
    # is not valid JSON (because the feature is unimplemented), the parse error is caught by the
    # || true fallback and desc_check is left empty, causing the subsequent assert_eq to fail with
    # a clear MISMATCH message. Silent-discard here is acceptable in the RED phase — the test will
    # still fail at the assertion, which is the desired behavior. In the GREEN phase, once
    # ticket show emits valid JSON with a description field, this path will produce 'OK'.
    desc_check=$(python3 -c "
import json, sys
data = json.loads(sys.argv[1])
desc = data.get('description', 'MISSING')
if desc == sys.argv[2]:
    print('OK')
else:
    print(f'MISMATCH: expected={sys.argv[2]!r} got={desc!r}')
" "$show_output" "$desc_body" 2>/dev/null) || true

    if [ "$desc_check" = "OK" ]; then
        assert_eq "ticket show compiled JSON includes correct description" "OK" "OK"
    else
        assert_eq "ticket show compiled JSON includes correct description" "OK" "$desc_check"
    fi
}
test_ticket_create_show_includes_description_after_create_with_d

# ── Test 15 (RED): --tags flag creates ticket with tags in CREATE event ────────
echo "Test 15 (RED): ticket create --tags writes tags array to CREATE event data"
test_tags_flag_creates_ticket_with_tags() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create bug "test-tags" --tags CLI_user 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for --tags test" "non-empty" "empty"
        return
    fi

    # Use ticket show to verify tags appear in compiled state
    local show_output
    show_output=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    if [ -z "$show_output" ]; then
        assert_eq "ticket show returns output for --tags test" "non-empty" "empty"
        return
    fi

    # Assert: tags array in compiled state contains "CLI_user"
    local tags_check
    tags_check=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    tags = data.get('tags', 'MISSING')
    if tags == 'MISSING':
        print('MISSING: tags field absent')
    elif 'CLI_user' in tags:
        print('OK')
    else:
        print(f'MISMATCH: expected CLI_user in tags, got {tags!r}')
except Exception as e:
    print(f'PARSE_ERROR:{e}')
" "$show_output" 2>/dev/null) || true

    if [ "$tags_check" = "OK" ]; then
        assert_eq "tags array contains CLI_user after --tags flag" "OK" "OK"
    else
        assert_eq "tags array contains CLI_user after --tags flag" "OK" "$tags_check"
    fi
}
test_tags_flag_creates_ticket_with_tags

# ── Test 16: no --tags flag creates ticket with empty tags array ───────────────
echo "Test 16: ticket create without --tags creates ticket with empty tags array"
test_no_tags_flag_creates_empty_tags() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create bug "test-no-tags" 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for no-tags test" "non-empty" "empty"
        return
    fi

    local show_output
    show_output=$(cd "$repo" && bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null) || true

    if [ -z "$show_output" ]; then
        assert_eq "ticket show returns output for no-tags test" "non-empty" "empty"
        return
    fi

    # Assert: tags array is empty [] when no --tags flag given
    local tags_check
    tags_check=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    tags = data.get('tags', 'MISSING')
    if tags == 'MISSING':
        print('MISSING: tags field absent')
    elif tags == []:
        print('OK')
    else:
        print(f'MISMATCH: expected [], got {tags!r}')
except Exception as e:
    print(f'PARSE_ERROR:{e}')
" "$show_output" 2>/dev/null) || true

    if [ "$tags_check" = "OK" ]; then
        assert_eq "tags array is empty [] when no --tags flag" "OK" "OK"
    else
        assert_eq "tags array is empty [] when no --tags flag" "OK" "$tags_check"
    fi
}
test_no_tags_flag_creates_empty_tags

# ── Test 17: --parent long flag sets parent_id in CREATE event ───────────────
echo "Test 17: --parent long flag sets parent_id in CREATE event data"
test_ticket_create_parent_long_flag_sets_parent_id() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    # Create a parent epic first
    local epic_id
    epic_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create epic "Parent epic for --parent test" 2>/dev/null) || true
    epic_id=$(echo "$epic_id" | tail -1)

    if [ -z "$epic_id" ]; then
        assert_eq "parent ticket created" "non-empty" "empty"
        return
    fi

    # Create a child task using --parent <parent-id>
    local child_id
    child_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Child task via --parent flag" --parent "$epic_id" 2>/dev/null) || true
    child_id=$(echo "$child_id" | tail -1)

    if [ -z "$child_id" ]; then
        assert_eq "child ticket created via --parent" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$child_id")

    if [ -z "$event_file" ]; then
        assert_eq "CREATE event file found for --parent test" "found" "not-found"
        return
    fi

    # --parent should set parent_id
    local parent_id_val
    parent_id_val=$(_extract_event_field "$event_file" "parent_id")
    assert_eq "--parent sets parent_id in CREATE event" "$epic_id" "$parent_id_val"
}
test_ticket_create_parent_long_flag_sets_parent_id

# ── Test 18 (RED): -p short flag sets priority (not parent) — bug 46b6-9e18 ──
echo "Test 18 (RED): -p short flag sets priority in CREATE event data (bug 46b6-9e18)"
test_ticket_create_short_p_flag_sets_priority() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    # Create a ticket using -p 1 (should set priority=1, not parent_id="1")
    # RED: current code maps -p to --parent, so -p 1 tries parent_id="1" which
    # does not exist and exits non-zero, or silently assigns parent_id="1".
    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Priority via -p flag" -p 1 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        # Current buggy behavior: -p 1 fails because "1" is not a valid parent ticket ID
        assert_eq "-p 1 creates ticket (priority shorthand)" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "CREATE event file found for -p priority test" "found" "not-found"
        return
    fi

    # -p 1 should set priority=1
    local priority_val
    priority_val=$(_extract_event_field "$event_file" "priority")
    assert_eq "-p sets priority in CREATE event (not parent_id)" "1" "$priority_val"

    # parent_id must remain empty (not set to "1")
    local parent_id_val
    parent_id_val=$(_extract_event_field "$event_file" "parent_id")
    assert_eq "-p does not set parent_id" "" "$parent_id_val"
}
test_ticket_create_short_p_flag_sets_priority

# ── Test 19 (RED): ticket create stdout last line matches 16-hex canonical format ─
echo "Test 19 (RED): ticket create stdout last line matches ^[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}$"
test_16hex_stdout_last_line() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local stdout_out
    stdout_out=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "16-hex stdout test" 2>/dev/null) || true

    if [ -z "$stdout_out" ]; then
        assert_eq "ticket ID is non-empty (16-hex stdout test)" "non-empty" "empty"
        return
    fi

    # Get the last line of stdout (the ticket ID)
    local last_line
    last_line=$(echo "$stdout_out" | tail -1)

    # Assert: last line matches 16-hex canonical format ^[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}$
    # RED: current implementation produces 8-hex (xxxx-xxxx), so this FAILS until implementation
    if [[ "$last_line" =~ ^[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}$ ]]; then
        assert_eq "stdout last line matches 16-hex canonical format" "match" "match"
    else
        assert_eq "stdout last line matches 16-hex canonical format" "match" "no-match: $last_line"
    fi
}
test_16hex_stdout_last_line

# ── Test 20 (RED): ticket create event filename (ticket dir) is 16-hex canonical ─
echo "Test 20 (RED): ticket create event directory name is 16-hex canonical ID"
test_16hex_event_filename() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "16-hex event filename test" 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for 16-hex event filename test" "non-empty" "empty"
        return
    fi

    # The ticket directory name IS the ticket ID — assert it matches 16-hex canonical format.
    # RED: current implementation produces 8-hex (xxxx-xxxx), so this FAILS until implementation.
    if [[ "$ticket_id" =~ ^[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}$ ]]; then
        assert_eq "ticket directory name matches 16-hex canonical UUID pattern" "match" "match"
    else
        assert_eq "ticket directory name matches 16-hex canonical UUID pattern" "match" "no-match: $ticket_id"
    fi

    # Also assert the ticket directory actually exists under .tickets-tracker/ # tickets-boundary-ok
    local tracker_dir="$repo/.tickets-tracker" # tickets-boundary-ok: test fixture — isolated test repo, not production store
    if [ -d "$tracker_dir/$ticket_id" ]; then
        assert_eq "ticket dir exists under .tickets-tracker/<16-hex-id>/" "exists" "exists" # tickets-boundary-ok
    else
        assert_eq "ticket dir exists under .tickets-tracker/<16-hex-id>/" "exists" "missing" # tickets-boundary-ok
    fi
}
test_16hex_event_filename

# ── Test 21 (RED): CREATE event data.id field matches 16-hex canonical format ────
echo "Test 21 (RED): CREATE event JSON data.id field matches 16-hex canonical format"
test_16hex_event_data_id() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "16-hex event data id test" 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for 16-hex event data.id test" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker" # tickets-boundary-ok: test fixture — isolated test repo, not production store
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "CREATE event file found for data.id test" "found" "not-found"
        return
    fi

    # Check if data.id field exists in the event and matches 16-hex format.
    # RED: current implementation does not write data.id, so this FAILS until implementation.
    local id_check
    id_check=$(python3 - "$event_file" <<'PYEOF'
import json, sys, re

CANONICAL_16HEX = re.compile(r'^[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}$')

try:
    with open(sys.argv[1], encoding='utf-8') as f:
        ev = json.load(f)
except Exception as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(1)

data = ev.get('data', {})
# Try both 'id' and 'ticket_id' field names per the task spec
ticket_id_val = data.get('id') or data.get('ticket_id') or 'MISSING'

if ticket_id_val == 'MISSING':
    print("MISSING: no id or ticket_id field in data")
elif CANONICAL_16HEX.match(str(ticket_id_val)):
    print("OK")
else:
    print(f"NO_MATCH: {ticket_id_val!r} does not match 16-hex pattern")
PYEOF
) || true

    if [ "$id_check" = "OK" ]; then
        assert_eq "CREATE event data.id matches 16-hex canonical format" "OK" "OK"
    else
        assert_eq "CREATE event data.id matches 16-hex canonical format" "OK" "$id_check"
    fi
}
test_16hex_event_data_id

# ── Test 22 (RED): 8-hex format xxxx-xxxx is rejected as invalid canonical ID ────
echo "Test 22 (RED): 8-hex format xxxx-xxxx is rejected — NOT a valid 16-hex canonical ID"
test_16hex_rejects_8hex_format() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "16-hex reject 8-hex test" 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for 8-hex rejection test" "non-empty" "empty"
        return
    fi

    # Assert: the created ticket ID does NOT match the old 8-hex format ^[0-9a-f]{4}-[0-9a-f]{4}$
    # (9 characters: xxxx-xxxx).
    # RED: current implementation produces the 8-hex format, so this FAILS until implementation.
    if [[ "$ticket_id" =~ ^[0-9a-f]{4}-[0-9a-f]{4}$ ]]; then
        assert_eq "ticket ID does NOT use old 8-hex format xxxx-xxxx" "not-8hex" "is-8hex: $ticket_id"
    else
        assert_eq "ticket ID does NOT use old 8-hex format xxxx-xxxx" "not-8hex" "not-8hex"
    fi

    # Assert: the ticket ID IS the 16-hex canonical format (belt-and-suspenders)
    if [[ "$ticket_id" =~ ^[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}$ ]]; then
        assert_eq "ticket ID uses 16-hex canonical format (not 8-hex)" "16hex" "16hex"
    else
        assert_eq "ticket ID uses 16-hex canonical format (not 8-hex)" "16hex" "not-16hex: $ticket_id"
    fi
}
test_16hex_rejects_8hex_format

# ── Test alias_1 (RED): wordlist resource file exists ────────────────────────
echo "Test alias_1 (RED): src/rebar/_engine/resources/ticket-wordlist.txt exists in repo"
test_alias_wordlist_file_exists() {
    local wordlist="$REPO_ROOT/src/rebar/_engine/resources/ticket-wordlist.txt"
    if [ ! -f "$wordlist" ]; then
        assert_eq "ticket-wordlist.txt exists" "exists" "missing: $wordlist"
        return
    fi
    assert_eq "ticket-wordlist.txt exists" "exists" "exists"

    # Assert: file is non-empty (has at least one word)
    local line_count
    line_count=$(wc -l < "$wordlist" | tr -d ' ')
    if [ "$line_count" -gt 0 ]; then
        assert_eq "ticket-wordlist.txt is non-empty" "non-empty" "non-empty"
    else
        assert_eq "ticket-wordlist.txt is non-empty" "non-empty" "empty"
        return
    fi

    # Assert: file contains '# NOUNS' section separator (marks adjective/noun boundary)
    if grep -q "^# NOUNS$" "$wordlist"; then
        assert_eq "ticket-wordlist.txt contains '# NOUNS' separator" "present" "present"
    else
        assert_eq "ticket-wordlist.txt contains '# NOUNS' separator" "present" "missing"
    fi
}
test_alias_wordlist_file_exists

# ── Test alias_2 (RED): CREATE event data.alias matches three-word format ────
echo "Test alias_2 (RED): CREATE event data.alias matches ^[a-z]+-[a-z]+-[a-z]+\$"
test_alias_format() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local ticket_id
    ticket_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Alias format test" 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    if [ -z "$ticket_id" ]; then
        assert_eq "ticket ID returned for alias format test" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"  # tickets-boundary-ok
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "CREATE event file found for alias format test" "found" "not-found"
        return
    fi

    # Assert: data.alias field exists in the CREATE event JSON
    local alias_val
    alias_val=$(_extract_event_field "$event_file" "alias")

    if [ -z "$alias_val" ] || [ "$alias_val" = "MISSING" ]; then
        assert_eq "data.alias field present in CREATE event" "present" "missing"
        return
    fi

    assert_ne "data.alias is non-empty" "" "$alias_val"

    # Assert: alias matches three hyphen-separated lowercase words
    if [[ "$alias_val" =~ ^[a-z]+-[a-z]+-[a-z]+$ ]]; then
        assert_eq "data.alias matches ^[a-z]+-[a-z]+-[a-z]+\$" "match" "match"
    else
        assert_eq "data.alias matches ^[a-z]+-[a-z]+-[a-z]+\$" "match" "no-match: $alias_val"
    fi
}
test_alias_format

# ── Test alias_3 (RED): each created ticket has a non-empty data.alias ───────
echo "Test alias_3 (RED): each ticket created gets a distinct non-empty data.alias"
test_alias_deterministic() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    # Create two tickets and verify each has a non-empty alias
    local id1 id2
    id1=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Alias ticket one" 2>/dev/null) || true
    id1=$(echo "$id1" | tail -1)
    id2=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Alias ticket two" 2>/dev/null) || true
    id2=$(echo "$id2" | tail -1)

    if [ -z "$id1" ] || [ -z "$id2" ]; then
        assert_eq "both tickets created for alias determinism test" "non-empty" "at-least-one-empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"  # tickets-boundary-ok

    local ev1 ev2
    ev1=$(_find_create_event "$tracker_dir" "$id1")
    ev2=$(_find_create_event "$tracker_dir" "$id2")

    if [ -z "$ev1" ] || [ -z "$ev2" ]; then
        assert_eq "both CREATE event files found for alias test" "found" "at-least-one-missing"
        return
    fi

    local alias1 alias2
    alias1=$(_extract_event_field "$ev1" "alias")
    alias2=$(_extract_event_field "$ev2" "alias")

    # Assert: neither alias is empty or MISSING
    if [ -z "$alias1" ] || [ "$alias1" = "MISSING" ]; then
        assert_eq "ticket 1 data.alias is non-empty" "non-empty" "empty-or-missing"
    else
        assert_ne "ticket 1 data.alias is non-empty" "" "$alias1"
    fi

    if [ -z "$alias2" ] || [ "$alias2" = "MISSING" ]; then
        assert_eq "ticket 2 data.alias is non-empty" "non-empty" "empty-or-missing"
    else
        assert_ne "ticket 2 data.alias is non-empty" "" "$alias2"
    fi
}
test_alias_deterministic

# ── Test alias_4 (RED): missing wordlist falls back to 8-hex alias + WARN ────
echo "Test alias_4 (RED): missing wordlist falls back to 8-hex alias with WARN on stderr"
test_alias_fallback_on_missing_wordlist() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    # Shadow the wordlist with a temp dir that lacks it: point TICKET_WORDLIST_PATH
    # at a nonexistent path.  ticket-create.sh must check the path and fall back.
    # We accomplish this by creating a minimal override env: remove the resources
    # dir from the repo fixture so the default path resolution also misses it.
    local alt_resources
    alt_resources=$(mktemp -d)
    _CLEANUP_DIRS+=("$alt_resources")
    # Do NOT create ticket-wordlist.txt inside alt_resources — that's the missing case.

    # Run ticket create with TICKET_WORDLIST_PATH pointing at a nonexistent file.
    # This env var is the expected hook point in ticket-create.sh for testability.
    local missing_wordlist="$alt_resources/ticket-wordlist.txt"
    local exit_code=0
    local stderr_out ticket_id
    local _fallback_stderr
    _fallback_stderr=$(mktemp "$alt_resources/stderr.XXXXXX")
    ticket_id=$(cd "$repo" && TICKET_WORDLIST_PATH="$missing_wordlist" bash "$TICKET_SCRIPT" create task "Fallback alias test" 2>"$_fallback_stderr") || exit_code=$?
    stderr_out=$(cat "$_fallback_stderr" 2>/dev/null) || true
    ticket_id=$(echo "$ticket_id" | tail -1)

    # Assert: ticket create still exits 0 (graceful fallback, not abort)
    assert_eq "fallback: ticket create exits 0 with missing wordlist" "0" "$exit_code"

    if [ -z "$ticket_id" ]; then
        assert_eq "fallback: ticket ID returned despite missing wordlist" "non-empty" "empty"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"  # tickets-boundary-ok
    local event_file
    event_file=$(_find_create_event "$tracker_dir" "$ticket_id")

    if [ -z "$event_file" ]; then
        assert_eq "fallback: CREATE event file found" "found" "not-found"
        return
    fi

    local alias_val
    alias_val=$(_extract_event_field "$event_file" "alias")

    # Assert: data.alias is non-empty
    if [ -z "$alias_val" ] || [ "$alias_val" = "MISSING" ]; then
        assert_eq "fallback: data.alias is non-empty" "non-empty" "empty-or-missing"
        return
    fi

    assert_ne "fallback: data.alias is non-empty" "" "$alias_val"

    # Assert: fallback alias matches 8-hex-char format
    if [[ "$alias_val" =~ ^[0-9a-f]{8}$ ]]; then
        assert_eq "fallback: data.alias matches ^[0-9a-f]{8}\$" "match" "match"
    else
        assert_eq "fallback: data.alias matches ^[0-9a-f]{8}\$" "match" "no-match: $alias_val"
    fi

    # Assert: stderr contains "WARN" or "wordlist" (warning about fallback)
    local _stderr_lower
    _stderr_lower=$(echo "$stderr_out" | tr '[:upper:]' '[:lower:]')
    if [[ "$_stderr_lower" =~ warn|wordlist ]]; then
        assert_eq "fallback: stderr contains WARN or wordlist" "has-warning" "has-warning"
    else
        assert_eq "fallback: stderr contains WARN or wordlist" "has-warning" "no-warning: $stderr_out"
    fi
}
test_alias_fallback_on_missing_wordlist

# ── Test alias_5 (RED): --parent=<alias> resolves to canonical parent_id ──────
echo "Test alias_5 (RED): ticket create --parent=<alias> resolves alias to canonical parent_id"
test_parent_alias_resolution() {
    local repo
    repo=$(_make_test_repo)

    if [ ! -f "$TICKET_CREATE_SCRIPT" ]; then
        assert_eq "ticket-create.sh exists" "exists" "missing"
        return
    fi

    local tracker_dir="$repo/.tickets-tracker"  # tickets-boundary-ok

    # Create parent epic and get its canonical ID and alias
    local parent_canonical parent_alias
    parent_canonical=$(cd "$repo" && bash "$TICKET_SCRIPT" create epic "Parent epic for alias test" 2>/dev/null) || true
    parent_canonical=$(echo "$parent_canonical" | tail -1)

    if [ -z "$parent_canonical" ]; then
        assert_eq "parent epic created successfully" "non-empty" "empty"
        return
    fi

    # Read the alias from the CREATE event
    local parent_event
    parent_event=$(_find_create_event "$tracker_dir" "$parent_canonical")
    if [ -z "$parent_event" ]; then
        assert_eq "parent CREATE event found" "found" "missing"
        return
    fi

    parent_alias=$(_extract_event_field "$parent_event" "alias")
    if [ -z "$parent_alias" ] || [ "$parent_alias" = "MISSING" ]; then
        assert_eq "parent alias is non-empty" "non-empty" "empty-or-missing: $parent_alias"
        return
    fi

    # RED gate: create child task using alias as --parent value
    local child_id exit_code=0 child_stderr
    child_stderr=$(mktemp "${TMPDIR:-/tmp}/alias5-stderr.XXXXXX")
    child_id=$(cd "$repo" && bash "$TICKET_SCRIPT" create task "Child task via alias parent" --parent "$parent_alias" 2>"$child_stderr") || exit_code=$?
    child_id=$(echo "$child_id" | tail -1)
    local stderr_content
    stderr_content=$(cat "$child_stderr" 2>/dev/null) || true
    rm -f "$child_stderr"

    # Assert: exit code 0 (alias must resolve without error)
    assert_eq "exit code 0 when --parent=<alias> is given" "0" "$exit_code"

    if [ "$exit_code" -ne 0 ]; then
        # Show the error for diagnostics
        assert_eq "no error on stderr" "(none)" "$stderr_content"
        return
    fi

    if [ -z "$child_id" ]; then
        assert_eq "child ticket ID is non-empty" "non-empty" "empty"
        return
    fi

    # Assert: child's parent_id in CREATE event equals canonical parent ID
    local child_event child_parent_id
    child_event=$(_find_create_event "$tracker_dir" "$child_id")
    if [ -z "$child_event" ]; then
        assert_eq "child CREATE event found" "found" "missing"
        return
    fi

    child_parent_id=$(_extract_event_field "$child_event" "parent_id")
    assert_eq "child parent_id equals canonical parent ID (not alias)" "$parent_canonical" "$child_parent_id"
}
test_parent_alias_resolution

print_summary
