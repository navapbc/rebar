#!/usr/bin/env bash
# tests/scripts/test-ticket-link-python-push.sh
# RED integration test: Python-path `ticket link` must push the LINK event to origin.
#
# Bug 9b17-369f: _write_link_event in ticket_graph/_links.py commits locally
# but never calls git push, unlike bash write_commit_event which calls
# _push_tickets_branch after every commit.
#
# Test: after `ticket link A B blocks` via the Python path, the LINK event commit
# must be present on the remote tickets branch.
#
# RED STATE: test fails before _write_link_event adds a best-effort push.
# GREEN STATE: test passes after the push is added.
#
# Usage: bash tests/scripts/test-ticket-link-python-push.sh
# Returns: exit 0 if all tests pass, exit 1 if any fail

# NOTE: -e intentionally omitted — assertions return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
DISPATCHER="$REPO_ROOT/src/rebar/_engine/ticket"

source "$SCRIPT_DIR/../lib/assert.sh"
source "$SCRIPT_DIR/../lib/git-fixtures.sh"

echo "=== test-ticket-link-python-push.sh ==="

# ── Fixture helper ─────────────────────────────────────────────────────────────
# Creates a ticket repo AND a bare remote, wires them together.
# Prints the repo path.
_make_repo_with_remote() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"

    local tracker_dir="$tmp/repo/.tickets-tracker"
    local bare_dir="$tmp/tickets-remote.git"

    # Create bare remote and seed it with the current tickets branch state.
    git init --bare "$bare_dir" -q 2>/dev/null
    git -C "$tracker_dir" remote add origin "$bare_dir" 2>/dev/null
    git -C "$tracker_dir" push origin tickets -q 2>/dev/null

    echo "$tmp/repo"
}

_create_ticket() {
    local repo="$1" ticket_type="${2:-task}" title="${3:-Test}"
    local out
    out=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$DISPATCHER" create "$ticket_type" "$title" 2>/dev/null) || true
    echo "$out" | tail -1
}

_remote_has_link_commit_for() {
    local bare_dir="$1" pattern="$2" log_out
    [ -z "$bare_dir" ] && { echo "no"; return; }
    # Capture first to avoid pipefail+grep-q SIGPIPE false negative.
    log_out=$(git -C "$bare_dir" log tickets --oneline 2>/dev/null) || true
    if echo "$log_out" | grep -q "$pattern"; then
        echo "yes"
    else
        echo "no"
    fi
}

# ── Test 1: ticket link pushes LINK event commit to remote ────────────────────
echo "Test 1: 'ticket link A B blocks' pushes LINK event to remote tickets branch"
test_python_link_pushes_to_remote() {
    local repo t1 t2 before_count after_count

    repo=$(_make_repo_with_remote)
    t1=$(_create_ticket "$repo" task "Source ticket")
    t2=$(_create_ticket "$repo" task "Target ticket")

    if [ -z "$t1" ] || [ -z "$t2" ]; then
        assert_ne "tickets created (non-empty ids)" "" "$t1"
        return
    fi

    local bare_dir bare_dir_raw
    bare_dir_raw=$(git -C "$repo/.tickets-tracker" remote get-url origin 2>/dev/null) || true
    # Resolve symlinks (macOS /var/folders -> /private/var/folders) so git -C works
    bare_dir=$(cd "$bare_dir_raw" 2>/dev/null && pwd -P) || bare_dir="$bare_dir_raw"

    # Run ticket link WITHOUT _TICKET_TEST_NO_SYNC so push is attempted.
    local exit_code=0
    (cd "$repo" && unset _TICKET_TEST_NO_SYNC && bash "$DISPATCHER" link "$t1" "$t2" blocks) || exit_code=$?
    assert_eq "ticket link exits 0" "0" "$exit_code"

    local after_pushed
    after_pushed=$(_remote_has_link_commit_for "$bare_dir" "link $t1")

    assert_eq "LINK event commit pushed to remote after ticket link" "yes" "$after_pushed"
}
test_python_link_pushes_to_remote

print_summary
