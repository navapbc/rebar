#!/usr/bin/env bash
# tests/scripts/test-ticket-migrate-file-impact-v1.sh
# Behavioral fixture test for src/rebar/_engine/ticket-migrate-file-impact-v1.sh
#
# These tests are RED — the migration script does not yet exist.
# Tests MUST FAIL until ticket-migrate-file-impact-v1.sh is implemented (task 361a-6c26).
#
# Test structure:
#   - 1 ticket with a well-formed File Impact COMMENT (two parseable paths)
#   - 1 ticket with a malformed File Impact COMMENT (no parseable paths)
#   - Idempotency: run once to migrate, run again, assert no duplication
#
# Assertions:
#   1. Well-formed fixture: exit 0, FILE_IMPACT event written, data.file_impact correct, stamp file exists
#   2. Malformed fixture: exit 0, ticket dir name in stderr, no FILE_IMPACT event, stamp file written
#   3. Idempotency: second run exits 0, no new FILE_IMPACT events, stamp file preserved
#
# Usage: bash tests/scripts/test-ticket-migrate-file-impact-v1.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e intentionally omitted — test functions may return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
MIGRATE_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-migrate-file-impact-v1.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-migrate-file-impact-v1.sh ==="

# ── Suite-runner guard: skip when migration script does not exist ─────────────
# RED tests fail by design (script not found). When auto-discovered by
# run-script-tests.sh, they would break `bash tests/run-all.sh`. Skip with
# exit 0 when ticket-migrate-file-impact-v1.sh is absent AND running under the suite runner.
if [ "${_RUN_ALL_ACTIVE:-0}" = "1" ] && [ ! -f "$MIGRATE_SCRIPT" ]; then
    echo "SKIP: ticket-migrate-file-impact-v1.sh not yet implemented (RED) — tests deferred"
    echo ""
    printf "PASSED: 0  FAILED: 0\n"
    exit 0
fi

# ── Cleanup tracking ──────────────────────────────────────────────────────────
_CLEANUP_DIRS=()
trap 'rm -rf "${_CLEANUP_DIRS[@]}" 2>/dev/null || true' EXIT

# ── Helper: create a fresh temp git repo with ticket system initialized ────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: write a COMMENT event file to a ticket dir ───────────────────────
# Usage: _write_comment_event <ticket_dir> <timestamp> <uuid> <body_text>
_write_comment_event() {
    local ticket_dir="$1"
    local timestamp="$2"
    local uuid="$3"
    local body_text="$4"
    local filename="${timestamp}-${uuid}-COMMENT.json"

    python3 -c "
import json, sys
payload = {
    'event_type': 'COMMENT',
    'timestamp': $timestamp,
    'uuid': '$uuid',
    'env_id': '00000000-0000-4000-8000-000000000001',
    'data': {
        'body': sys.argv[1]
    }
}
json.dump(payload, sys.stdout)
" "$body_text" > "$ticket_dir/$filename"
}

# ── Helper: write a CREATE event file to a ticket dir ────────────────────────
_write_create_event() {
    local ticket_dir="$1"
    local timestamp="$2"
    local uuid="$3"
    local ticket_id="$4"
    local filename="${timestamp}-${uuid}-CREATE.json"

    python3 -c "
import json, sys
payload = {
    'event_type': 'CREATE',
    'timestamp': $timestamp,
    'uuid': '$uuid',
    'env_id': '00000000-0000-4000-8000-000000000001',
    'data': {
        'ticket_id': '$ticket_id',
        'ticket_type': 'task',
        'title': 'Test ticket $ticket_id',
        'status': 'open',
        'description': 'Test description'
    }
}
json.dump(payload, sys.stdout)
" > "$ticket_dir/$filename"
}

# ── Helper: count FILE_IMPACT events in a ticket dir ─────────────────────────
_count_file_impact_events() {
    local ticket_dir="$1"
    find "$ticket_dir" -name '*-FILE_IMPACT.json' 2>/dev/null | wc -l | tr -d ' '
}

# ── Helper: read data.file_impact from the FILE_IMPACT event ─────────────────
# Returns normalized JSON (sorted keys, compact) for comparison
_get_file_impact_data() {
    local ticket_dir="$1"
    python3 - "$ticket_dir" <<'PYEOF'
import json, os, sys

ticket_dir = sys.argv[1]
for fname in sorted(os.listdir(ticket_dir)):
    if not fname.endswith('-FILE_IMPACT.json') or fname.startswith('.'):
        continue
    fpath = os.path.join(ticket_dir, fname)
    try:
        with open(fpath) as f:
            event = json.load(f)
        file_impact = event.get('data', {}).get('file_impact', None)
        if file_impact is not None:
            print(json.dumps(file_impact))
            sys.exit(0)
    except (json.JSONDecodeError, OSError):
        pass
print("null")
PYEOF
}

# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: Well-formed fixture — FILE_IMPACT event written with correct data
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 1: well-formed fixture produces FILE_IMPACT event"
test_migrate_file_impact_well_formed_fixture() {
    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local tracker_dir="$repo/.tickets-tracker"
    local ticket_id="ticket-wellformed-001"
    local ticket_dir="$tracker_dir/$ticket_id"
    mkdir -p "$ticket_dir"

    _write_create_event "$ticket_dir" "1700000000" "testuuid000cr001" "$ticket_id"
    _write_comment_event "$ticket_dir" "1700000001" "testuuid001cm001" \
        "## File Impact
- src/foo.py (modified)
- src/bar.py (deleted)"

    # Run migration
    local exit_code=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT" --target "$repo") >/dev/null 2>&1 || exit_code=$?
    assert_eq "well-formed: migration exits 0" "0" "$exit_code"

    # FILE_IMPACT event must exist in ticket dir
    local fi_count
    fi_count=$(_count_file_impact_events "$ticket_dir")
    if [ "$fi_count" -ge 1 ]; then
        assert_eq "well-formed: FILE_IMPACT event file exists" "yes" "yes"
    else
        assert_eq "well-formed: FILE_IMPACT event file exists" "yes" "no"
    fi

    # data.file_impact must contain the two paths with their reasons
    local actual_impact
    actual_impact=$(_get_file_impact_data "$ticket_dir")
    local expected_impact
    expected_impact='[{"path": "src/foo.py", "reason": "modified"}, {"path": "src/bar.py", "reason": "deleted"}]'
    assert_eq "well-formed: data.file_impact matches expected" "$expected_impact" "$actual_impact"

    # Stamp file must exist
    if [ -f "$repo/.claude/.file-impact-migration-v1" ]; then
        assert_eq "well-formed: stamp file written" "exists" "exists"
    else
        assert_eq "well-formed: stamp file written" "exists" "missing"
    fi
}
test_migrate_file_impact_well_formed_fixture

# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: Malformed fixture — no FILE_IMPACT event written, stderr contains ticket id
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 2: malformed fixture is skipped gracefully"
test_migrate_file_impact_malformed_fixture() {
    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local tracker_dir="$repo/.tickets-tracker"
    local ticket_id="ticket-malformed-002"
    local ticket_dir="$tracker_dir/$ticket_id"
    mkdir -p "$ticket_dir"

    _write_create_event "$ticket_dir" "1700001000" "testuuid002cr001" "$ticket_id"
    _write_comment_event "$ticket_dir" "1700001001" "testuuid002cm001" \
        "## File Impact
(no parseable paths here — malformed)"

    # Run migration, capture stderr
    local exit_code=0
    local stderr_out
    stderr_out=$(cd "$repo" && bash "$MIGRATE_SCRIPT" --target "$repo" 2>&1 >/dev/null) || exit_code=$?

    # Must exit 0 (not crash on malformed input)
    assert_eq "malformed: migration exits 0" "0" "$exit_code"

    # Stderr must mention the ticket directory name
    if printf '%s' "$stderr_out" | grep -q "$ticket_id"; then
        assert_eq "malformed: ticket id appears in stderr" "yes" "yes"
    else
        assert_eq "malformed: ticket id appears in stderr" "yes" "no"
    fi

    # No FILE_IMPACT event must be written for the malformed ticket
    local fi_count
    fi_count=$(_count_file_impact_events "$ticket_dir")
    assert_eq "malformed: no FILE_IMPACT event written" "0" "$fi_count"

    # Stamp file must still be written (whole migration run completed)
    if [ -f "$repo/.claude/.file-impact-migration-v1" ]; then
        assert_eq "malformed: stamp file still written" "exists" "exists"
    else
        assert_eq "malformed: stamp file still written" "exists" "missing"
    fi
}
test_migrate_file_impact_malformed_fixture

# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: Idempotency — second run writes no new FILE_IMPACT events
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 3: idempotency — second run is a no-op"
test_migrate_file_impact_idempotency() {
    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local tracker_dir="$repo/.tickets-tracker"
    local ticket_id="ticket-idempotency-003"
    local ticket_dir="$tracker_dir/$ticket_id"
    mkdir -p "$ticket_dir"

    _write_create_event "$ticket_dir" "1700002000" "testuuid003cr001" "$ticket_id"
    _write_comment_event "$ticket_dir" "1700002001" "testuuid003cm001" \
        "## File Impact
- src/baz.py (added)"

    # First run — performs migration
    (cd "$repo" && bash "$MIGRATE_SCRIPT" --target "$repo") >/dev/null 2>&1 || true

    # Count FILE_IMPACT events after first run
    local fi_count_first
    fi_count_first=$(_count_file_impact_events "$ticket_dir")

    # Second run — stamp file present; should be a no-op
    local exit_code=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT" --target "$repo") >/dev/null 2>&1 || exit_code=$?

    assert_eq "idempotency: second run exits 0" "0" "$exit_code"

    # FILE_IMPACT event count must be unchanged
    local fi_count_second
    fi_count_second=$(_count_file_impact_events "$ticket_dir")
    assert_eq "idempotency: no new FILE_IMPACT events written" "$fi_count_first" "$fi_count_second"

    # Stamp file must still be present
    if [ -f "$repo/.claude/.file-impact-migration-v1" ]; then
        assert_eq "idempotency: stamp file still present" "exists" "exists"
    else
        assert_eq "idempotency: stamp file still present" "exists" "missing"
    fi
}
test_migrate_file_impact_idempotency

# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: Git commit — events are committed to tracker branch after migration
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 4: events are committed to tracker git branch"
test_migrate_file_impact_git_commit() {
    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local tracker_dir="$repo/.tickets-tracker"
    local ticket_id="ticket-gitcommit-004"
    local ticket_dir="$tracker_dir/$ticket_id"
    mkdir -p "$ticket_dir"

    _write_create_event "$ticket_dir" "1700003000" "testuuid004cr001" "$ticket_id"
    _write_comment_event "$ticket_dir" "1700003001" "testuuid004cm001" \
        "## File Impact
- src/qux.py (modified)"

    # Stage event files so git knows about them (simulates pre-existing committed ticket)
    git -C "$tracker_dir" add -A
    git -C "$tracker_dir" commit -m "test: add fixture ticket" --no-verify >/dev/null 2>&1 || true

    # Run migration
    local exit_code=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT" --target "$repo") >/dev/null 2>&1 || exit_code=$?
    assert_eq "git-commit: migration exits 0" "0" "$exit_code"

    # The FILE_IMPACT event file must be committed (not just on disk)
    local untracked_count
    untracked_count=$(git -C "$tracker_dir" status --porcelain 2>/dev/null | grep '^??' | wc -l | tr -d ' ')
    assert_eq "git-commit: no uncommitted FILE_IMPACT events after migration" "0" "$untracked_count"

    # The commit message must mention file-impact-v1
    local last_commit_msg
    last_commit_msg=$(git -C "$tracker_dir" log -1 --pretty=%s 2>/dev/null || echo "")
    if printf '%s' "$last_commit_msg" | grep -q "file-impact-v1"; then
        assert_eq "git-commit: commit message mentions file-impact-v1" "yes" "yes"
    else
        assert_eq "git-commit: commit message mentions file-impact-v1" "yes" "no"
    fi
}
test_migrate_file_impact_git_commit

# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: Git commit failure — written event files are cleaned up and exit is 1
# ═══════════════════════════════════════════════════════════════════════════════
echo "Test 5: commit failure cleans up written events and exits 1"
test_migrate_file_impact_git_commit_failure_cleanup() {
    if [ ! -f "$MIGRATE_SCRIPT" ]; then
        assert_eq "migration script exists (prereq)" "exists" "missing"
        return
    fi

    local repo
    repo=$(_make_test_repo)

    local tracker_dir="$repo/.tickets-tracker"
    local ticket_id="ticket-commitfail-005"
    local ticket_dir="$tracker_dir/$ticket_id"
    mkdir -p "$ticket_dir"

    _write_create_event "$ticket_dir" "1700004000" "testuuid005cr001" "$ticket_id"
    _write_comment_event "$ticket_dir" "1700004001" "testuuid005cm001" \
        "## File Impact
- src/quux.py (added)"

    # Stage fixture so migration can write and git-add the FILE_IMPACT event
    git -C "$tracker_dir" add -A
    git -C "$tracker_dir" commit -m "test: add fixture ticket" --no-verify >/dev/null 2>&1 || true

    # Install a pre-commit hook in the main repo that always fails.
    # The tracker is a worktree sharing the main repo's hooks, so this blocks
    # git commit in the tracker without touching the tracker's gitlink file.
    # Fixture setup uses --no-verify, so only the migration's bare commit is blocked.
    local hooks_dir="$repo/.git/hooks"
    mkdir -p "$hooks_dir"
    local hook="$hooks_dir/pre-commit"
    printf '#!/bin/sh\nexit 1\n' > "$hook"
    chmod +x "$hook"

    # Run migration — should exit 1 due to commit failure
    local exit_code=0
    (cd "$repo" && bash "$MIGRATE_SCRIPT" --target "$repo") >/dev/null 2>&1 || exit_code=$?
    assert_eq "commit-failure: migration exits 1" "1" "$exit_code"

    # Event file must NOT remain on disk (cleanup removed it)
    local fi_count
    fi_count=$(_count_file_impact_events "$ticket_dir")
    assert_eq "commit-failure: FILE_IMPACT event file removed by cleanup" "0" "$fi_count"

    # Stamp file must NOT be written (migration did not complete)
    if [ -f "$repo/.claude/.file-impact-migration-v1" ]; then
        assert_eq "commit-failure: stamp file not written" "missing" "exists"
    else
        assert_eq "commit-failure: stamp file not written" "missing" "missing"
    fi
}
test_migrate_file_impact_git_commit_failure_cleanup

# ═══════════════════════════════════════════════════════════════════════════════
print_summary
