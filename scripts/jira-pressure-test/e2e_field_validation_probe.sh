#!/usr/bin/env bash
# E2E Field Validation Probe — systematically tests bidirectional CRUD for
# every field across 10 test tickets against live Jira.
#
# REFERENCE / MANUAL PRESSURE-TEST TOOLING — see scripts/jira-pressure-test/README.md.
# This script is NOT part of the automated test suite and is NOT shipped in the
# published wheel. It hits LIVE Jira and is run by hand to harden / pressure-test
# the reconciler's Jira field sync. Do not wire it into CI.
#
# Phases:
#   0. Pre-flight (env check, get_myself, save snapshot)
#   1. Create 10 local tickets → sync outbound → verify all fields in Jira
#   2. Edit fields locally → sync outbound → verify Jira updated
#   3. Edit fields in Jira → sync inbound → verify local updated
#   4. Status outbound negative test (gated/stub)
#   5. Delete behavior negative test (excluded by design)
#   6. Idempotency — 3 no-op passes → verify 0 mutations each
#   7. Reconciliation check — verify 0 discrepancies for probe keys
#   8. Cleanup — delete all Jira issues + local tickets, restore snapshot
#
# Usage: run manually from the repo root.
# Requires: JIRA_URL, JIRA_USER, JIRA_API_TOKEN env vars.
# Requires: REBAR_FIELD_VALIDATION_PROBE=1 to opt in (prevents accidental
#           inclusion in generic test-discovery sweeps).
# Working directory: repo root.

set -euo pipefail

# Explicit opt-in gate — prevents accidental invocation by find/glob test
# discovery. The probe creates real Jira issues against the configured
# instance, so it must only run when the caller explicitly intends to.
if [ "${REBAR_FIELD_VALIDATION_PROBE:-0}" != "1" ]; then
    echo "SKIP: e2e_field_validation_probe.sh requires REBAR_FIELD_VALIDATION_PROBE=1" >&2
    echo "      (this probe creates real Jira issues against the configured instance)" >&2
    exit 0
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
# This reference probe lives under scripts/jira-pressure-test/, so the rebar
# engine (dispatcher, reconciler package, rebar_reconciler/acli.py) is anchored at
# the repo's src/rebar/_engine tree rather than a sibling of this script.
_SCRIPTS_DIR="${REBAR_ENGINE_DIR:-${REPO_ROOT}/src/rebar/_engine}"
TICKET_CLI="${REBAR_TICKET_CLI:-${_SCRIPTS_DIR}/rebar}"
RECONCILER_DIR="$_SCRIPTS_DIR"
JIRA_PROJECT="${JIRA_PROJECT:-DIG}"
PROBE_TS="$(date +%s)"
PROBE_TAG="field-probe-${PROBE_TS}"

PASSED=0
FAILED=0
SKIPPED=0
ASSIGNEE_SKIP=false
PROBE_USER=""

# Arrays for tracking ticket IDs and Jira keys
declare -a LOCAL_IDS=()
declare -a JIRA_KEYS=()

# Matrix results: associative array keyed by "field:direction:operation"
declare -A MATRIX=()

TRACKER_DIR="${REPO_ROOT}/.tickets-tracker"  # tickets-boundary-ok
PREV_SNAPSHOT="${TRACKER_DIR}/.bridge_state/prev_snapshot.json"
PREV_SNAPSHOT_BACKUP=""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

pass_test() {
    local name="$1"
    echo "PASS: $name"
    PASSED=$((PASSED + 1))
}

fail_test() {
    local name="$1"
    local detail="${2:-}"
    echo "FAIL: $name${detail:+ — $detail}"
    FAILED=$((FAILED + 1))
    # Bug b859 (Part 0a): dump the most recent reconciler output's last 60
    # lines so unmatched stderr (including Python tracebacks) is visible.
    # The probe's main log captures this dump verbatim, eliminating the
    # observability gap that hid Phase 4's silent failure.
    if [ -n "${LAST_RECONCILER_LOG:-}" ] && [ -s "$LAST_RECONCILER_LOG" ]; then
        echo "=== last reconciler output (tail 60) ==="
        tail -60 "$LAST_RECONCILER_LOG"
        echo "=== end last reconciler output ==="
    fi
}

skip_test() {
    local name="$1"
    local reason="${2:-}"
    echo "SKIP: $name${reason:+ — $reason}"
    SKIPPED=$((SKIPPED + 1))
}

matrix_set() {
    local field="$1" direction="$2" operation="$3" result="$4"
    MATRIX["${field}:${direction}:${operation}"]="$result"
}

# shellcheck disable=SC2329 # invoked via trap
restore_snapshot() {
    if [ -n "$PREV_SNAPSHOT_BACKUP" ] && [ -f "$PREV_SNAPSHOT_BACKUP" ]; then
        cp "$PREV_SNAPSHOT_BACKUP" "$PREV_SNAPSHOT"
        rm -f "$PREV_SNAPSHOT_BACKUP"
        echo "Restored prev_snapshot.json from backup."
    fi
}

# Restore snapshot on any exit (crash safety).
trap restore_snapshot EXIT

# Bug b859 (Part 0a): the prior implementation captured reconciler output
# only to the local `output` var, and the caller piped it through a grep
# filter `^(FILTERED|filter:|OK:|ERROR:)` that silently dropped Python
# tracebacks. When the reconciler aborted pre-FILTERED PASS, operators saw
# nothing between "Running reconciler..." and the verify FAIL.
# Now: write every reconciler invocation's full unfiltered output to a
# side-car file at $LAST_RECONCILER_LOG, and expose $LAST_RECONCILER_LOG
# for fail_test to dump when an assertion fails. The function still echoes
# the output to stdout so existing callers see the same lines they always
# did.
LAST_RECONCILER_LOG=""
run_reconciler() {
    local output
    # Template form (not `-t`): the -t flag has divergent macOS/GNU semantics
    # and is prohibited by AGENTS.md rule:mktemp-tmp. Production/orchestrator
    # context, so a literal /tmp template is fine (no per-test TMPDIR contract).
    # The XXXXXX run MUST be trailing: BSD/macOS mkstemp fails on a ".log" suffix
    # after the X's (GNU tolerates it, BSD does not), so omit the suffix.
    LAST_RECONCILER_LOG=$(mktemp "/tmp/recon-probe.XXXXXX")
    output=$(cd "$RECONCILER_DIR" && python -m rebar_reconciler "$@" 2>&1) || true
    printf '%s\n' "$output" > "$LAST_RECONCILER_LOG"
    echo "$output"
}

run_filtered_reconciler() {
    local filter_ids="$1"
    shift
    run_reconciler --mode bootstrap-throttle --filter-local-ids "$filter_ids" --repo-root "$REPO_ROOT" "$@"
}

get_jira_field() {
    local key="$1"
    local field="$2"
    cd "$RECONCILER_DIR"
    python3 -c "
import importlib.util, sys, json, os
from rebar_reconciler import acli as mod
adf_spec = importlib.util.spec_from_file_location('adf', '${_SCRIPTS_DIR}/rebar_reconciler/adf.py')
adf_mod = importlib.util.module_from_spec(adf_spec)
adf_spec.loader.exec_module(adf_mod)
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
)
issue = client.get_issue_by_rest('${key}')
fields = issue.get('fields', issue)
val = fields.get('${field}', '')
# Description is returned as an ADF document, not a string. Decode via adf_to_text
# so the probe asserts against canonical plain text (bug 85a1 — the probe's prior
# raw.get('name', ...) returned '' for ADF, producing false-negative description
# verification failures).
if '${field}' == 'description' and isinstance(val, dict):
    val = adf_mod.adf_to_text(val)
elif isinstance(val, dict):
    val = val.get('name', val.get('displayName', ''))
if isinstance(val, list):
    print(json.dumps(val))
else:
    print(val)
"
}

get_jira_labels() {
    local key="$1"
    get_jira_field "$key" "labels"
}

get_jira_comments() {
    local key="$1"
    cd "$RECONCILER_DIR"
    python3 -c "
import importlib.util, json, os
from rebar_reconciler import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
)
comments = client.get_comments('${key}')
for c in comments:
    body = c.get('body', '') if isinstance(c, dict) else str(c)
    print(body)
"
}

get_local_field() {
    local ticket_id="$1"
    local field="$2"
    "$TICKET_CLI" show "$ticket_id" 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
val = data.get('${field}', '')
if isinstance(val, list):
    print(json.dumps(val))
else:
    print(val)
"
}

check_binding() {
    local local_id="$1"
    local bindings_file="${TRACKER_DIR}/.bridge_state/bindings.json"
    if [ ! -f "$bindings_file" ]; then
        echo "no-bindings-file"
        return
    fi
    python3 -c "
import json, sys
data = json.load(open('${bindings_file}'))
local_id = sys.argv[1]
entry = data.get('bindings', {}).get(local_id)
if entry is None:
    print('unbound')
elif entry.get('state') == 'confirmed':
    print('confirmed:' + (entry.get('jira_key') or 'none'))
else:
    print(entry.get('state', 'unknown'))
" "$local_id"
}

# is_valid_ticket_id: returns 0 (true) if the string looks like a probe-issued
# UUID (4-segment hex, e.g. 7f6e-e5de-4613-473c), 1 (false) otherwise.
# Used to guard Phase 2c create steps from propagating error strings as IDs.
is_valid_ticket_id() {
    local id="$1"
    [[ "$id" =~ ^[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}$ ]]
}

# restore_bindings_if_corrupt: if bindings.json fails to parse, restore it
# from the tickets branch using git show.  Called before each Phase-2b
# reconciler sub-cycle to guard against corruption left by a prior push
# failure (see probe run notes: the priority-1 pass succeeded but the
# subsequent git push to origin/tickets failed, leaving the local tickets
# branch ahead of origin; the next reconciler call found bindings.json
# in a partially-merged / truncated state).
restore_bindings_if_corrupt() {
    local bindings_file="${TRACKER_DIR}/.bridge_state/bindings.json"
    if python3 -c "import json; json.load(open('${bindings_file}'))" 2>/dev/null; then
        return 0  # healthy — nothing to do
    fi
    echo "restore_bindings_if_corrupt: bindings.json unparseable — restoring from tickets branch..." >&2
    if git -C "$REPO_ROOT" show "tickets:.bridge_state/bindings.json" \
            > "${bindings_file}.probe-restore.tmp" 2>/dev/null; then  # tickets-boundary-ok
        if python3 -c "import json; json.load(open('${bindings_file}.probe-restore.tmp'))" 2>/dev/null; then
            mv "${bindings_file}.probe-restore.tmp" "${bindings_file}"
            echo "restore_bindings_if_corrupt: restored successfully." >&2
            return 0
        else
            rm -f "${bindings_file}.probe-restore.tmp"
            echo "restore_bindings_if_corrupt: tickets-branch copy also unparseable — cannot restore." >&2
            return 1
        fi
    else
        rm -f "${bindings_file}.probe-restore.tmp"
        echo "restore_bindings_if_corrupt: git show failed — cannot restore." >&2
        return 1
    fi
}

# restore_prev_snapshot_if_corrupt: if prev_snapshot.json fails to parse,
# restore it from the probe's own startup backup (PREV_SNAPSHOT_BACKUP).
# If the backup is unavailable, delete the file to force a full re-fetch on
# the next reconciler pass (the reconciler treats a missing prev_snapshot.json
# as a clean-start signal).  Called before each sub-cycle reconcile that may
# follow a git-push failure that can leave prev_snapshot.json in a
# partially-written / conflict-marker state.
restore_prev_snapshot_if_corrupt() {
    if python3 -c "import json; json.load(open('${PREV_SNAPSHOT}'))" 2>/dev/null; then
        return 0  # healthy — nothing to do
    fi
    echo "restore_prev_snapshot_if_corrupt: prev_snapshot.json unparseable — attempting restore..." >&2
    if [ -n "$PREV_SNAPSHOT_BACKUP" ] && [ -f "$PREV_SNAPSHOT_BACKUP" ]; then
        if python3 -c "import json; json.load(open('${PREV_SNAPSHOT_BACKUP}'))" 2>/dev/null; then
            cp "$PREV_SNAPSHOT_BACKUP" "$PREV_SNAPSHOT"
            echo "restore_prev_snapshot_if_corrupt: restored from probe startup backup." >&2
            return 0
        fi
    fi
    # Backup unavailable or also corrupt: delete to force full re-fetch.
    echo "restore_prev_snapshot_if_corrupt: no usable backup — deleting prev_snapshot.json to force full re-fetch." >&2
    rm -f "$PREV_SNAPSHOT"
    return 0
}

edit_ticket_field() {
    # Edit a single ticket field via the ticket CLI's edit subcommand.
    # The local ticket store is event-sourced — no ticket.json to mutate —
    # so writes must go through the CLI which emits an EDIT event.
    local ticket_id="$1"
    local field="$2"
    local value="$3"
    "$TICKET_CLI" edit "$ticket_id" "--${field}=${value}" 2>&1 | tail -1
}

jira_update_issue() {
    local key="$1"
    shift
    cd "$RECONCILER_DIR"
    local kwargs_json
    kwargs_json=$(python3 -c "
import json, sys
kwargs = {}
for arg in sys.argv[1:]:
    k, v = arg.split('=', 1)
    kwargs[k] = v
print(json.dumps(kwargs))
" "$@")
    python3 -c "
import importlib.util, os, json, sys
from rebar_reconciler import acli as mod
kwargs = json.loads(sys.argv[1])
mod.update_issue('${key}', **kwargs)
" "$kwargs_json"
}

jira_update_priority() {
    local key="$1"
    local priority_name="$2"
    cd "$RECONCILER_DIR"
    python3 -c "
import importlib.util, os
from rebar_reconciler import acli as mod
mod.update_priority('${key}', '${priority_name}')"
}

jira_update_issuetype() {
    local key="$1"
    local type_name="$2"
    cd "$RECONCILER_DIR"
    python3 -c "
import importlib.util, os
from rebar_reconciler import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
)
client.update_issuetype('${key}', '${type_name}')"
}

jira_transition() {
    local key="$1"
    local status_name="$2"
    cd "$RECONCILER_DIR"
    python3 -c "
import importlib.util, os
from rebar_reconciler import acli as mod
mod.transition_issue('${key}', '${status_name}')"
}

jira_add_label() {
    local key="$1"
    local label="$2"
    cd "$RECONCILER_DIR"
    python3 -c "
import importlib.util, os
from rebar_reconciler import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
)
client.add_label('${key}', '${label}')"
}

jira_remove_label() {
    local key="$1"
    local label="$2"
    cd "$RECONCILER_DIR"
    python3 -c "
import importlib.util, os
from rebar_reconciler import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
)
client.remove_label('${key}', '${label}')"
}

jira_add_comment() {
    local key="$1"
    local body="$2"
    cd "$RECONCILER_DIR"
    python3 -c "
import importlib.util, os
from rebar_reconciler import acli as mod
mod.add_comment('${key}', '${body}')"
}

jira_delete_issue() {
    local key="$1"
    cd "$RECONCILER_DIR"
    python3 -c "
import importlib.util, os
from rebar_reconciler import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
)
client.delete_issue('${key}')" 2>&1 || true
}

build_filter_ids() {
    local ids=""
    for id in "$@"; do
        if [ -n "$ids" ]; then
            ids="${ids},${id}"
        else
            ids="$id"
        fi
    done
    echo "$ids"
}

fallback_cleanup() {
    echo "Running fallback cleanup — searching Jira for label ${PROBE_TAG}..."
    cd "$RECONCILER_DIR"
    python3 -c "
import importlib.util, os, json
from rebar_reconciler import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
)
results = client.search_issues('project = ${JIRA_PROJECT} AND labels = \"${PROBE_TAG}\"')
issues = results if isinstance(results, list) else results.get('issues', [])
for issue in issues:
    key = issue.get('key', '')
    if key:
        print(f'Deleting orphaned probe issue {key}...')
        try:
            client.delete_issue(key)
        except Exception as e:
            print(f'  Warning: {e}')
print(f'Fallback cleanup complete: {len(issues)} issues processed.')
" 2>&1 || true
}

# ===========================================================================
# PHASE 0: Pre-flight
# ===========================================================================

echo ""
echo "==========================================="
echo "E2E FIELD VALIDATION PROBE — ${PROBE_TAG}"
echo "==========================================="
echo "Expected runtime: ~5-10 minutes (7+ Jira fetches)"
echo ""

echo "=== PHASE 0: Pre-flight ==="
echo ""

# Env check
for var in JIRA_URL JIRA_USER JIRA_API_TOKEN; do
    if [ -z "${!var:-}" ]; then
        echo "FATAL: ${var} is not set."
        exit 2
    fi
done
pass_test "Phase0.env-vars"

# Probe get_myself for assignee testing
PROBE_USER=$(cd "$RECONCILER_DIR" && python3 -c "
import importlib.util, os, json
from rebar_reconciler import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
)
myself = client.get_myself()
name = myself.get('displayName', '')
if not name:
    print('')
else:
    print(name)
" 2>/dev/null) || true

if [ -z "$PROBE_USER" ]; then
    ASSIGNEE_SKIP=true
    skip_test "Phase0.get-myself" "get_myself returned no displayName — assignee tests will be skipped"
else
    pass_test "Phase0.get-myself (${PROBE_USER})"
fi

# Save snapshot (unique temp file to avoid collisions with concurrent probes)
if [ -f "$PREV_SNAPSHOT" ]; then
    PREV_SNAPSHOT_BACKUP=$(mktemp "${TRACKER_DIR}/.bridge_state/prev_snapshot.json.probe-backup.XXXXXX")
    cp "$PREV_SNAPSHOT" "$PREV_SNAPSHOT_BACKUP"
    pass_test "Phase0.snapshot-backup"
else
    pass_test "Phase0.snapshot-backup (no prev snapshot — clean start)"
fi

# ===========================================================================
# PHASE 1: Create 10 tickets + outbound create sync
# ===========================================================================

echo ""
echo "=== PHASE 1: Create 10 local tickets and sync outbound ==="
echo ""

create_ticket() {
    local idx="$1" type="$2" title="$3" desc="$4" priority="$5" extra_tags="${6:-}"
    local tags="${PROBE_TAG}"
    if [ -n "$extra_tags" ]; then
        tags="${tags},${extra_tags}"
    fi
    # `ticket create` defaults assignee to unassigned (ticket-create.sh
    # change in this branch). Ticket 5 sets a real assignee in Phase 1
    # via `ticket edit --assignee=$PROBE_USER` for the assignee test.
    local output
    output=$("$TICKET_CLI" create "$type" "$title" -d "$desc" --priority "$priority" --tags "$tags" 2>&1)
    local id
    id=$(echo "$output" | tail -1)
    if [ -z "$id" ]; then
        # FATAL: index-aligned arrays (LOCAL_IDS, JIRA_KEYS) cannot tolerate
        # a gap. Abort the probe immediately rather than corrupt subsequent
        # phases that iterate by index.
        fail_test "Phase1.create-ticket-${idx}" "ticket create returned no ID: ${output}"
        echo "FATAL: cannot proceed with gap in LOCAL_IDS — aborting." >&2
        exit 1
    fi
    LOCAL_IDS+=("$id")
    pass_test "Phase1.create-ticket-${idx} (${id})"
    return 0
}

# Ticket 1: title + description baseline
create_ticket 1 task "FIELD-PROBE-1: title baseline ${PROBE_TS}" "Baseline description for probe" 2
# Ticket 2: bug type + priority highest
create_ticket 2 bug "FIELD-PROBE-2: bug type ${PROBE_TS}" "Bug type mapping test" 0
# Ticket 3: story type + priority lowest
create_ticket 3 story "FIELD-PROBE-3: priority low ${PROBE_TS}" "Priority mapping test" 4
# Ticket 4: multiline description
create_ticket 4 task "FIELD-PROBE-4: desc test ${PROBE_TS}" "Line1
Line2
Line3" 2
# Ticket 5: assignee testing
create_ticket 5 task "FIELD-PROBE-5: assignee ${PROBE_TS}" "Assignee test" 2
# Ticket 6: label testing
create_ticket 6 task "FIELD-PROBE-6: labels ${PROBE_TS}" "Label test" 2 "label-a,label-b,label-c"
# Ticket 7: issuetype asymmetry
create_ticket 7 task "FIELD-PROBE-7: issuetype ${PROBE_TS}" "Issuetype asymmetry test" 2
# Ticket 8: comment testing
create_ticket 8 task "FIELD-PROBE-8: comments ${PROBE_TS}" "Comment test" 2
# Ticket 9: status inbound testing
create_ticket 9 task "FIELD-PROBE-9: status ${PROBE_TS}" "Status inbound test" 2
# Ticket 10: delete behavior testing
create_ticket 10 task "FIELD-PROBE-10: delete ${PROBE_TS}" "Delete behavior test" 2

if [ "${#LOCAL_IDS[@]}" -lt 10 ]; then
    echo "FATAL: only created ${#LOCAL_IDS[@]} of 10 tickets."
    exit 1
fi

# Set assignee on ticket 5 via direct JSON edit
if [ "$ASSIGNEE_SKIP" = false ]; then
    edit_ticket_field "${LOCAL_IDS[4]}" "assignee" "${PROBE_USER}"
    pass_test "Phase1.set-assignee-ticket-5"
fi

# Add comment on ticket 8
"$TICKET_CLI" comment "${LOCAL_IDS[7]}" "Probe outbound comment" 2>/dev/null || true
pass_test "Phase1.add-comment-ticket-8"

# Run reconciler
FILTER_IDS=$(build_filter_ids "${LOCAL_IDS[@]}")
echo "Running reconciler (bootstrap-throttle, filtered to ${#LOCAL_IDS[@]} IDs)..."
reconciler_output=$(run_filtered_reconciler "$FILTER_IDS")
echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true

# Verify bindings and extract Jira keys.  The reconciler saves the binding
# store at the end of a pass — if the pass partially failed (e.g. HeadDrift
# or DirectionMismatch), the save may be skipped.  As a workaround, poll
# until the binding is confirmed or a ~120s budget is exhausted, using
# adaptive backoff (2s for the first 5 attempts, then 5s per attempt).
# On success the function returns immediately — no fixed worst-case wait.
# Bug 0877-2d0a-3c29-4292: the prior 3×2s (~6s) budget was too short for
# reconciler passes that include Jira REST roundtrips; all Phase-2
# outbound-UPDATE rows showed N/A because JIRA_KEYS were never populated.
check_binding_with_retry() {
    local local_id="$1"
    local state
    local elapsed=0
    local attempt=0
    local sleep_secs
    local budget=120

    # Guard: if local_id is an error string (not a valid ticket UUID), return
    # immediately rather than interpolating it into Python and producing a
    # SyntaxError that gets silently swallowed and loops for 120s.
    if ! is_valid_ticket_id "$local_id"; then
        echo "check_binding_with_retry: invalid ticket ID '${local_id}' — skipping" >&2
        echo "invalid-id"
        return
    fi

    while [[ $elapsed -lt $budget ]]; do
        state=$(check_binding "$local_id")
        if [[ "$state" == confirmed:* ]]; then
            echo "$state"
            return
        fi
        attempt=$(( attempt + 1 ))
        # Adaptive backoff: 2s for first 5 attempts, then 5s per attempt.
        if [[ $attempt -le 5 ]]; then
            sleep_secs=2
        else
            sleep_secs=5
        fi
        echo "check_binding_with_retry: attempt ${attempt}, state=${state}, sleeping ${sleep_secs}s (elapsed=${elapsed}s / budget=${budget}s)" >&2
        sleep "$sleep_secs"
        elapsed=$(( elapsed + sleep_secs ))
    done

    # Budget exhausted — return the last observed state.
    echo "check_binding_with_retry: budget exhausted after ${elapsed}s (${attempt} attempts), last state=${state}" >&2
    echo "$state"
}

for i in $(seq 0 9); do
    binding_state=$(check_binding_with_retry "${LOCAL_IDS[$i]}")
    if [[ "$binding_state" == confirmed:* ]]; then
        JIRA_KEYS+=("${binding_state#confirmed:}")
        pass_test "Phase1.binding-${i} (${LOCAL_IDS[$i]} → ${JIRA_KEYS[$i]})"
    else
        JIRA_KEYS+=("")
        fail_test "Phase1.binding-${i}" "expected confirmed, got: ${binding_state}"
    fi
done

# Verify outbound create fields
# Title (all tickets)
for i in $(seq 0 9); do
    [ -z "${JIRA_KEYS[$i]}" ] && continue
    jira_summary=$(get_jira_field "${JIRA_KEYS[$i]}" "summary")
    if [[ "$jira_summary" == *"FIELD-PROBE-$((i+1)):"* ]]; then
        pass_test "Phase1.verify-title-${i}"
    else
        fail_test "Phase1.verify-title-${i}" "got: ${jira_summary}"
    fi
done
matrix_set "title" "outbound" "create" "TESTED"

# Issuetype
for idx_type in "1:Bug" "2:Story" "0:Task" "3:Task"; do
    idx="${idx_type%%:*}"
    expected="${idx_type#*:}"
    [ -z "${JIRA_KEYS[$idx]}" ] && continue
    jira_type=$(get_jira_field "${JIRA_KEYS[$idx]}" "issuetype")
    if [ "$jira_type" = "$expected" ]; then
        pass_test "Phase1.verify-issuetype-${idx} (${expected})"
    else
        fail_test "Phase1.verify-issuetype-${idx}" "expected ${expected}, got: ${jira_type}"
    fi
done
matrix_set "issuetype" "outbound" "create" "TESTED"

# Priority
for idx_pri in "1:Highest" "2:Lowest" "0:Medium"; do
    idx="${idx_pri%%:*}"
    expected="${idx_pri#*:}"
    [ -z "${JIRA_KEYS[$idx]}" ] && continue
    jira_priority=$(get_jira_field "${JIRA_KEYS[$idx]}" "priority")
    if [ "$jira_priority" = "$expected" ]; then
        pass_test "Phase1.verify-priority-${idx} (${expected})"
    else
        fail_test "Phase1.verify-priority-${idx}" "expected ${expected}, got: ${jira_priority}"
    fi
done
matrix_set "priority" "outbound" "create" "TESTED"

# Description (ticket 4 multiline)
if [ -n "${JIRA_KEYS[3]}" ]; then
    jira_desc=$(get_jira_field "${JIRA_KEYS[3]}" "description")
    if [[ "$jira_desc" == *"Line1"* ]]; then
        pass_test "Phase1.verify-description-multiline"
    else
        fail_test "Phase1.verify-description-multiline" "got: ${jira_desc}"
    fi
fi
matrix_set "description" "outbound" "create" "TESTED"

# Assignee (ticket 5)
if [ "$ASSIGNEE_SKIP" = false ] && [ -n "${JIRA_KEYS[4]}" ]; then
    jira_assignee=$(get_jira_field "${JIRA_KEYS[4]}" "assignee")
    if [[ "${jira_assignee,,}" == "${PROBE_USER,,}" ]]; then
        pass_test "Phase1.verify-assignee"
        matrix_set "assignee" "outbound" "create" "PASS"
    else
        fail_test "Phase1.verify-assignee" "expected '${PROBE_USER}', got: '${jira_assignee}'"
        matrix_set "assignee" "outbound" "create" "FAIL"
    fi
else
    skip_test "Phase1.verify-assignee" "ASSIGNEE_SKIP"
    matrix_set "assignee" "outbound" "create" "SKIP"
fi

# Labels (ticket 6)
if [ -n "${JIRA_KEYS[5]}" ]; then
    jira_labels=$(get_jira_labels "${JIRA_KEYS[5]}")
    labels_ok=true
    for lbl in label-a label-b label-c "$PROBE_TAG"; do
        if ! echo "$jira_labels" | grep -q "$lbl"; then
            fail_test "Phase1.verify-label-${lbl}" "not in: ${jira_labels}"
            labels_ok=false
        fi
    done
    if [ "$labels_ok" = true ]; then
        pass_test "Phase1.verify-labels-ticket-6"
    fi
fi
matrix_set "labels" "outbound" "create" "TESTED"

# rebar-id binding label (spot check ticket 1)
if [ -n "${JIRA_KEYS[0]}" ]; then
    jira_labels=$(get_jira_labels "${JIRA_KEYS[0]}")
    if echo "$jira_labels" | grep -q "rebar-id"; then
        pass_test "Phase1.verify-rebar-id-label"
    else
        fail_test "Phase1.verify-rebar-id-label" "no rebar-id label: ${jira_labels}"
    fi
fi

# Comment (ticket 8)
if [ -n "${JIRA_KEYS[7]}" ]; then
    jira_comments=$(get_jira_comments "${JIRA_KEYS[7]}")
    if echo "$jira_comments" | grep -q "Probe outbound comment"; then
        pass_test "Phase1.verify-comment-outbound"
        matrix_set "comments" "outbound" "create" "PASS"
    else
        fail_test "Phase1.verify-comment-outbound" "comment not found"
        matrix_set "comments" "outbound" "create" "FAIL"
    fi
fi

# ===========================================================================
# PHASE 2: Outbound update sync
# ===========================================================================

echo ""
echo "=== PHASE 2: Edit locally and sync outbound ==="
echo ""

# Ticket 1: change title
edit_ticket_field "${LOCAL_IDS[0]}" "title" "FIELD-PROBE-1: UPDATED title ${PROBE_TS}"
pass_test "Phase2.edit-title"

# Ticket 1: change description
edit_ticket_field "${LOCAL_IDS[0]}" "description" "Updated description"
pass_test "Phase2.edit-description"

# Ticket 2: change priority from 0 to 3 (Highest → Low)
edit_ticket_field "${LOCAL_IDS[1]}" "priority" "3"
pass_test "Phase2.edit-priority"

# Ticket 5: unassign (set assignee to "")
if [ "$ASSIGNEE_SKIP" = false ]; then
    edit_ticket_field "${LOCAL_IDS[4]}" "assignee" ""
    pass_test "Phase2.edit-assignee-unassign"
fi

# Ticket 6: add label-d, remove label-a
"$TICKET_CLI" tag "${LOCAL_IDS[5]}" "label-d" 2>/dev/null || true
"$TICKET_CLI" untag "${LOCAL_IDS[5]}" "label-a" 2>/dev/null || true
pass_test "Phase2.edit-labels"

# Ticket 7: change ticket_type from task to bug (asymmetry test)
edit_ticket_field "${LOCAL_IDS[6]}" "ticket_type" "bug"
pass_test "Phase2.edit-issuetype-local"

# Ticket 8: add second comment
"$TICKET_CLI" comment "${LOCAL_IDS[7]}" "Second probe comment" 2>/dev/null || true
pass_test "Phase2.add-second-comment"

# Sync
echo "Running reconciler for outbound updates..."
reconciler_output=$(run_filtered_reconciler "$FILTER_IDS")
echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true

# Verify outbound updates
# Title (ticket 1)
if [ -n "${JIRA_KEYS[0]}" ]; then
    jira_summary=$(get_jira_field "${JIRA_KEYS[0]}" "summary")
    if [[ "$jira_summary" == *"UPDATED title"* ]]; then
        pass_test "Phase2.verify-title-updated"
        matrix_set "title" "outbound" "update" "PASS"
    else
        fail_test "Phase2.verify-title-updated" "got: ${jira_summary}"
        matrix_set "title" "outbound" "update" "FAIL"
    fi
fi

# Description (ticket 1)
if [ -n "${JIRA_KEYS[0]}" ]; then
    jira_desc=$(get_jira_field "${JIRA_KEYS[0]}" "description")
    if [[ "$jira_desc" == *"Updated description"* ]]; then
        pass_test "Phase2.verify-description-updated"
        matrix_set "description" "outbound" "update" "PASS"
    else
        fail_test "Phase2.verify-description-updated" "got: ${jira_desc}"
        matrix_set "description" "outbound" "update" "FAIL"
    fi
fi

# Priority (ticket 2: 3 → Low)
if [ -n "${JIRA_KEYS[1]}" ]; then
    jira_priority=$(get_jira_field "${JIRA_KEYS[1]}" "priority")
    if [ "$jira_priority" = "Low" ]; then
        pass_test "Phase2.verify-priority-updated"
        matrix_set "priority" "outbound" "update" "PASS"
    else
        fail_test "Phase2.verify-priority-updated" "expected Low, got: ${jira_priority}"
        matrix_set "priority" "outbound" "update" "FAIL"
    fi
fi

# Assignee unassign (ticket 5)
if [ "$ASSIGNEE_SKIP" = false ] && [ -n "${JIRA_KEYS[4]}" ]; then
    jira_assignee=$(get_jira_field "${JIRA_KEYS[4]}" "assignee")
    if [ -z "$jira_assignee" ] || [ "$jira_assignee" = "None" ]; then
        pass_test "Phase2.verify-assignee-unassigned"
        matrix_set "assignee" "outbound" "update" "PASS"
    else
        fail_test "Phase2.verify-assignee-unassigned" "got: ${jira_assignee}"
        matrix_set "assignee" "outbound" "update" "FAIL"
    fi
else
    skip_test "Phase2.verify-assignee-unassigned" "ASSIGNEE_SKIP"
    matrix_set "assignee" "outbound" "update" "SKIP"
fi

# Labels (ticket 6: has label-b, label-c, label-d; does NOT have label-a)
if [ -n "${JIRA_KEYS[5]}" ]; then
    jira_labels=$(get_jira_labels "${JIRA_KEYS[5]}")
    label_update_ok=true
    for lbl in label-b label-c label-d; do
        if ! echo "$jira_labels" | grep -q "$lbl"; then
            fail_test "Phase2.verify-label-present-${lbl}" "not in: ${jira_labels}"
            label_update_ok=false
        fi
    done
    if echo "$jira_labels" | grep -q '"label-a"'; then
        fail_test "Phase2.verify-label-removed-a" "label-a still present: ${jira_labels}"
        label_update_ok=false
    fi
    if [ "$label_update_ok" = true ]; then
        pass_test "Phase2.verify-labels-updated"
        matrix_set "labels" "outbound" "update" "PASS"
    else
        matrix_set "labels" "outbound" "update" "FAIL"
    fi
fi

# Issuetype asymmetry (ticket 7: Jira should still be Task, NOT Bug)
if [ -n "${JIRA_KEYS[6]}" ]; then
    jira_type=$(get_jira_field "${JIRA_KEYS[6]}" "issuetype")
    if [ "$jira_type" = "Task" ]; then
        pass_test "Phase2.verify-issuetype-NOT-pushed (Task, not Bug)"
        matrix_set "issuetype" "outbound" "update" "BY_DESIGN"
    else
        fail_test "Phase2.verify-issuetype-NOT-pushed" "expected Task (blocked), got: ${jira_type}"
        matrix_set "issuetype" "outbound" "update" "FAIL"
    fi
fi

# Comment (ticket 8)
if [ -n "${JIRA_KEYS[7]}" ]; then
    jira_comments=$(get_jira_comments "${JIRA_KEYS[7]}")
    if echo "$jira_comments" | grep -q "Second probe comment"; then
        pass_test "Phase2.verify-comment-pushed"
        matrix_set "comments" "outbound" "update" "PASS"
    else
        fail_test "Phase2.verify-comment-pushed" "second comment not found"
        matrix_set "comments" "outbound" "update" "FAIL"
    fi
    # Check for duplicate first comment (dedup validation)
    dup_count=$(echo "$jira_comments" | grep -c "Probe outbound comment" || true)
    if [ "$dup_count" -le 1 ]; then
        pass_test "Phase2.verify-no-duplicate-comments"
    else
        fail_test "Phase2.verify-no-duplicate-comments" "found ${dup_count} copies"
    fi
fi

# ===========================================================================
# PHASE 2b: Priority enum full-cycle (values 1 and 3)
# ===========================================================================
#
# Existing tickets cover priorities 0 (Highest), 2 (Medium), 4 (Lowest) at
# create time, and Phase 2 updates ticket 2 from 0→3 (Low). This phase
# explicitly cycles ticket 3 (FIELD-PROBE-3, currently priority 4/Lowest)
# through local priority 1 (High) and then priority 3 (Low), verifying
# both outbound mappings.  Using ticket 3 (idx 2) keeps changes isolated
# from Phase 3's inbound tests on ticket 2 (idx 1).

echo ""
echo "=== PHASE 2b: Priority enum full-cycle (priorities 1 and 3) ==="
echo ""

# Sub-cycle A: local priority 4 → 1 (Lowest → High)
edit_ticket_field "${LOCAL_IDS[2]}" "priority" "1"
pass_test "Phase2b.edit-priority-to-1"

echo "Running reconciler for priority=1 outbound..."
reconciler_output=$(run_filtered_reconciler "$FILTER_IDS")
echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true

if [ -n "${JIRA_KEYS[2]}" ]; then
    jira_priority=$(get_jira_field "${JIRA_KEYS[2]}" "priority")
    if [ "$jira_priority" = "High" ]; then
        pass_test "Phase2b.verify-priority-1-outbound (High)"
        matrix_set "priority" "outbound" "update-p1" "PASS"
    else
        fail_test "Phase2b.verify-priority-1-outbound" "expected High, got: ${jira_priority}"
        matrix_set "priority" "outbound" "update-p1" "FAIL"
    fi
fi

# Sub-cycle B: local priority 1 → 3 (High → Low)
edit_ticket_field "${LOCAL_IDS[2]}" "priority" "3"
pass_test "Phase2b.edit-priority-to-3"

# Restore bindings.json and prev_snapshot.json before this sub-cycle's
# reconcile in case the priority-1 reconciler's git push left either file
# in a corrupt state (confirmed pattern: push failure after sub-cycle A
# corrupts both files, causing sub-cycle B reconcile to abort and leaving
# Jira at priority=1/High instead of priority=3/Low).
restore_bindings_if_corrupt || true
restore_prev_snapshot_if_corrupt || true

echo "Running reconciler for priority=3 outbound..."
reconciler_output=$(run_filtered_reconciler "$FILTER_IDS")
echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true

if [ -n "${JIRA_KEYS[2]}" ]; then
    jira_priority=$(get_jira_field "${JIRA_KEYS[2]}" "priority")
    if [ "$jira_priority" = "Low" ]; then
        pass_test "Phase2b.verify-priority-3-outbound (Low)"
        matrix_set "priority" "outbound" "update-p3" "PASS"
    else
        fail_test "Phase2b.verify-priority-3-outbound" "expected Low, got: ${jira_priority}"
        matrix_set "priority" "outbound" "update-p3" "FAIL"
    fi
fi

# ===========================================================================
# PHASE 2c: Epic type + parent/child hierarchy
# ===========================================================================
#
# Probes:
#   2c-1: Create a local epic ticket → reconcile → verify Jira issuetype=Epic
#   2c-2: Create a child task with --parent <epic-local-id> → reconcile →
#          verify Jira child carries parent.key == epic Jira key (outbound parent)
#   2c-3: Reparent child to a second parent (ticket 1 / LOCAL_IDS[0]) → reconcile →
#          verify Jira parent changed
#   2c-4: Create a third ticket; set its Jira parent to the epic key via REST
#          set_parent → reconcile inbound → verify local parent_id resolves
#
# Ordering note: 2c-2's outbound parent bind relies on the epic being bound
# first. A second reconciler pass is run before asserting the parent key to
# handle the unbound-parent grace path documented in outbound_differ.py L183.
#
# 36af exclusion: no issuetype UPDATE assertions here — only the CREATE path.

echo ""
echo "=== PHASE 2c: Epic type + parent/child hierarchy ==="
echo ""

# --- 2c-1: Epic CREATE outbound ---
EPIC_LOCAL_ID=""
EPIC_JIRA_KEY=""
EPIC_LOCAL_ID=$("$TICKET_CLI" create epic \
    "FIELD-PROBE-EPIC: hierarchy ${PROBE_TS}" \
    -d "Epic for parent/child probe" \
    --priority 2 \
    --tags "${PROBE_TAG}" \
    2>&1 | tail -1) || true

# Validate that the returned value is a real ticket UUID, not an error string.
# The ticket CLI prints errors to stderr and the ID to stdout; with 2>&1 both
# land in the variable, so we must check the ID format explicitly.
if ! is_valid_ticket_id "$EPIC_LOCAL_ID"; then
    fail_test "Phase2c.create-epic" "ticket create epic returned no valid ID (got: ${EPIC_LOCAL_ID})"
    EPIC_LOCAL_ID=""
else
    pass_test "Phase2c.create-epic (${EPIC_LOCAL_ID})"
fi

# Create the third ticket (used for inbound-parent test 2c-4) before
# reconciling so a single pass binds both the epic and this ticket.
THIRD_LOCAL_ID=""
THIRD_JIRA_KEY=""
if [ -n "$EPIC_LOCAL_ID" ]; then
    THIRD_LOCAL_ID=$("$TICKET_CLI" create task \
        "FIELD-PROBE-THIRD: inbound-parent ${PROBE_TS}" \
        -d "Inbound parent resolution test" \
        --priority 2 \
        --tags "${PROBE_TAG}" \
        2>&1 | tail -1) || true
    if ! is_valid_ticket_id "$THIRD_LOCAL_ID"; then
        fail_test "Phase2c.create-third" "ticket create task (inbound-parent) returned no valid ID (got: ${THIRD_LOCAL_ID})"
        THIRD_LOCAL_ID=""
    else
        pass_test "Phase2c.create-third (${THIRD_LOCAL_ID})"
    fi
fi

# Reconcile epic + third ticket
PARITY_FILTER="$FILTER_IDS"
if [ -n "$EPIC_LOCAL_ID" ]; then
    PARITY_FILTER="${PARITY_FILTER},${EPIC_LOCAL_ID}"
fi
if [ -n "$THIRD_LOCAL_ID" ]; then
    PARITY_FILTER="${PARITY_FILTER},${THIRD_LOCAL_ID}"
fi

# Restore bridge-state files if corrupt before reconciling — a prior push
# failure in Phase 2b can leave both bindings.json and prev_snapshot.json
# in an unparseable state, causing the reconciler to abort immediately.
restore_bindings_if_corrupt || true
restore_prev_snapshot_if_corrupt || true

echo "Running reconciler for epic create outbound..."
reconciler_output=$(run_filtered_reconciler "$PARITY_FILTER")
echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true

# Wait for binding
if [ -n "$EPIC_LOCAL_ID" ]; then
    epic_binding=$(check_binding_with_retry "$EPIC_LOCAL_ID")
    if [[ "$epic_binding" == confirmed:* ]]; then
        EPIC_JIRA_KEY="${epic_binding#confirmed:}"
        pass_test "Phase2c.epic-binding (${EPIC_LOCAL_ID} → ${EPIC_JIRA_KEY})"
    else
        fail_test "Phase2c.epic-binding" "expected confirmed, got: ${epic_binding}"
    fi
fi

if [ -n "$THIRD_LOCAL_ID" ]; then
    third_binding=$(check_binding_with_retry "$THIRD_LOCAL_ID")
    if [[ "$third_binding" == confirmed:* ]]; then
        THIRD_JIRA_KEY="${third_binding#confirmed:}"
        pass_test "Phase2c.third-binding (${THIRD_LOCAL_ID} → ${THIRD_JIRA_KEY})"
    else
        fail_test "Phase2c.third-binding" "expected confirmed, got: ${third_binding}"
    fi
fi

# Verify epic issuetype in Jira (CREATE, no type-update assertion per 36af)
if [ -n "$EPIC_JIRA_KEY" ]; then
    jira_epic_type=$(get_jira_field "$EPIC_JIRA_KEY" "issuetype")
    if [ "$jira_epic_type" = "Epic" ]; then
        pass_test "Phase2c.verify-epic-issuetype-outbound (Epic)"
        matrix_set "epic" "outbound" "create" "PASS"
    else
        fail_test "Phase2c.verify-epic-issuetype-outbound" "expected Epic, got: ${jira_epic_type}"
        matrix_set "epic" "outbound" "create" "FAIL"
    fi
fi

# --- 2c-2: Child task CREATE with --parent <epic-local-id> ---
CHILD_LOCAL_ID=""
CHILD_JIRA_KEY=""
if [ -n "$EPIC_LOCAL_ID" ]; then
    CHILD_LOCAL_ID=$("$TICKET_CLI" create task \
        "FIELD-PROBE-CHILD: hierarchy ${PROBE_TS}" \
        -d "Child of epic for parent probe" \
        --priority 2 \
        --parent "$EPIC_LOCAL_ID" \
        --tags "${PROBE_TAG}" \
        2>&1 | tail -1) || true
    if ! is_valid_ticket_id "$CHILD_LOCAL_ID"; then
        fail_test "Phase2c.create-child" "ticket create task --parent returned no valid ID (got: ${CHILD_LOCAL_ID})"
        CHILD_LOCAL_ID=""
    else
        pass_test "Phase2c.create-child (${CHILD_LOCAL_ID})"
    fi
fi

# Update filter to include child
if [ -n "$CHILD_LOCAL_ID" ]; then
    PARITY_FILTER="${PARITY_FILTER},${CHILD_LOCAL_ID}"
fi

# First reconciler pass — epic may already be bound; child parent key may
# resolve immediately, or the pass may defer if the binding store lookup
# races the intra-pass bind.  A second pass (below) guarantees resolution
# (documented unbound-parent grace: outbound_differ.py L183).
echo "Running reconciler pass 1 for child create outbound..."
reconciler_output=$(run_filtered_reconciler "$PARITY_FILTER")
echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true

# Second pass to flush any unbound-parent deferral.
echo "Running reconciler pass 2 (unbound-parent grace flush) for child parent..."
reconciler_output=$(run_filtered_reconciler "$PARITY_FILTER")
echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true

if [ -n "$CHILD_LOCAL_ID" ]; then
    child_binding=$(check_binding_with_retry "$CHILD_LOCAL_ID")
    if [[ "$child_binding" == confirmed:* ]]; then
        CHILD_JIRA_KEY="${child_binding#confirmed:}"
        pass_test "Phase2c.child-binding (${CHILD_LOCAL_ID} → ${CHILD_JIRA_KEY})"
    else
        fail_test "Phase2c.child-binding" "expected confirmed, got: ${child_binding}"
    fi
fi

# Verify child's Jira parent == epic Jira key via get_parent_map
if [ -n "$CHILD_JIRA_KEY" ] && [ -n "$EPIC_JIRA_KEY" ]; then
    child_parent_key=$(cd "$RECONCILER_DIR" && python3 -c "
import importlib.util, os, json
from rebar_reconciler import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
)
parent_map = client.get_parent_map('${JIRA_PROJECT}', jql='key = ${CHILD_JIRA_KEY}')
print(parent_map.get('${CHILD_JIRA_KEY}') or '')
" 2>/dev/null) || true
    if [ "$child_parent_key" = "$EPIC_JIRA_KEY" ]; then
        pass_test "Phase2c.verify-child-parent-outbound (${CHILD_JIRA_KEY} → ${EPIC_JIRA_KEY})"
        matrix_set "parent" "outbound" "create" "PASS"
    else
        fail_test "Phase2c.verify-child-parent-outbound" "expected parent=${EPIC_JIRA_KEY}, got: ${child_parent_key}"
        matrix_set "parent" "outbound" "create" "FAIL"
    fi
fi

# --- 2c-3: Reparent child to a SECOND epic → verify Jira parent changed ---
# Jira's next-gen hierarchy permits ONLY an Epic as a parent (outbound_differ
# suppresses non-epic parents; the applier 400-skips them). A Task→Task
# reparent is therefore rejected by design — reparenting to a task would never
# land and is not a valid outbound-reparent assertion. We create + bind a
# second epic (EPIC2) and reparent the child epic→epic, which is the real
# supported outbound reparent path (live-proven, ticket 8b25).
EPIC2_LOCAL_ID=""
EPIC2_JIRA_KEY=""
if [ -n "$CHILD_LOCAL_ID" ] && [ -n "$EPIC_JIRA_KEY" ]; then
    EPIC2_LOCAL_ID=$("$TICKET_CLI" create epic \
        "FIELD-PROBE-EPIC2: reparent-target ${PROBE_TS}" \
        -d "Second epic for reparent probe" \
        --priority 2 \
        --tags "${PROBE_TAG}" \
        2>&1 | tail -1) || true
    if ! is_valid_ticket_id "$EPIC2_LOCAL_ID"; then
        fail_test "Phase2c.create-epic2" "ticket create epic2 returned no valid ID (got: ${EPIC2_LOCAL_ID})"
        EPIC2_LOCAL_ID=""
    else
        pass_test "Phase2c.create-epic2 (${EPIC2_LOCAL_ID})"
        PARITY_FILTER="${PARITY_FILTER},${EPIC2_LOCAL_ID}"
    fi
fi

if [ -n "$EPIC2_LOCAL_ID" ]; then
    # Bind EPIC2 first so the differ can resolve the child's new parent key.
    echo "Running reconciler to bind EPIC2..."
    reconciler_output=$(run_filtered_reconciler "$PARITY_FILTER")
    echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true
    epic2_binding=$(check_binding_with_retry "$EPIC2_LOCAL_ID")
    if [[ "$epic2_binding" == confirmed:* ]]; then
        EPIC2_JIRA_KEY="${epic2_binding#confirmed:}"
        pass_test "Phase2c.epic2-binding (${EPIC2_LOCAL_ID} → ${EPIC2_JIRA_KEY})"
    else
        fail_test "Phase2c.epic2-binding" "expected confirmed, got: ${epic2_binding}"
    fi
fi

if [ -n "$CHILD_LOCAL_ID" ] && [ -n "$EPIC2_LOCAL_ID" ]; then
    # Use "parent" (not "parent_id") — ticket edit's allowed fields are:
    # title priority assignee ticket_type description tags parent.
    edit_ticket_field "$CHILD_LOCAL_ID" "parent" "${EPIC2_LOCAL_ID}"
    pass_test "Phase2c.reparent-child-local"

    echo "Running reconciler for reparent outbound..."
    reconciler_output=$(run_filtered_reconciler "$PARITY_FILTER")
    echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true

    if [ -n "$CHILD_JIRA_KEY" ] && [ -n "$EPIC2_JIRA_KEY" ]; then
        new_parent_key=$(cd "$RECONCILER_DIR" && python3 -c "
import importlib.util, os
from rebar_reconciler import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
)
parent_map = client.get_parent_map('${JIRA_PROJECT}', jql='key = ${CHILD_JIRA_KEY}')
print(parent_map.get('${CHILD_JIRA_KEY}') or '')
" 2>/dev/null) || true
        if [ "$new_parent_key" = "$EPIC2_JIRA_KEY" ]; then
            pass_test "Phase2c.verify-reparent-outbound (${CHILD_JIRA_KEY} → ${EPIC2_JIRA_KEY})"
            matrix_set "parent" "outbound" "update" "PASS"
        else
            fail_test "Phase2c.verify-reparent-outbound" "expected ${EPIC2_JIRA_KEY}, got: ${new_parent_key}"
            matrix_set "parent" "outbound" "update" "FAIL"
        fi
    fi
fi

# --- 2c-4: Inbound parent resolution ---
# Set Jira parent on THIRD ticket to the epic's Jira key via REST set_parent.
# After reconcile inbound, assert local parent_id == EPIC_LOCAL_ID.
if [ -n "$THIRD_JIRA_KEY" ] && [ -n "$EPIC_JIRA_KEY" ]; then
    cd "$RECONCILER_DIR" && python3 -c "
import importlib.util, os
from rebar_reconciler import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
)
client.set_parent('${THIRD_JIRA_KEY}', '${EPIC_JIRA_KEY}')
" 2>&1 || true
    pass_test "Phase2c.jira-set-parent-third"

    sleep 2

    echo "Running reconciler for inbound parent resolution..."
    reconciler_output=$(run_filtered_reconciler "$PARITY_FILTER")
    echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true

    if [ -n "$THIRD_LOCAL_ID" ] && [ -n "$EPIC_LOCAL_ID" ]; then
        local_parent=$(get_local_field "$THIRD_LOCAL_ID" "parent_id")
        if [ "$local_parent" = "$EPIC_LOCAL_ID" ]; then
            pass_test "Phase2c.verify-parent-inbound (${THIRD_LOCAL_ID}.parent_id == ${EPIC_LOCAL_ID})"
            matrix_set "parent" "inbound" "update" "PASS"
        else
            fail_test "Phase2c.verify-parent-inbound" "expected ${EPIC_LOCAL_ID}, got: ${local_parent}"
            matrix_set "parent" "inbound" "update" "FAIL"
        fi
    fi
fi

# Inbound epic check: if the EPIC_JIRA_KEY ticket is visible on the inbound
# mirror path (i.e., the reconciler's inbound fetch includes it and it is
# locally typed as 'epic'), verify that.  Because the epic was locally created
# and is already bound, the inbound differ will not retype it (36af exclusion);
# the local ticket_type is already 'epic' — assert it directly.
if [ -n "$EPIC_LOCAL_ID" ]; then
    epic_local_type=$(get_local_field "$EPIC_LOCAL_ID" "ticket_type")
    if [ "$epic_local_type" = "epic" ]; then
        pass_test "Phase2c.verify-epic-local-type-preserved"
        matrix_set "epic" "inbound" "mirror" "PASS"
    else
        fail_test "Phase2c.verify-epic-local-type-preserved" "expected epic, got: ${epic_local_type}"
        matrix_set "epic" "inbound" "mirror" "FAIL"
    fi
fi

# ===========================================================================
# PHASE 2d: Ticket-level dedup
# ===========================================================================
#
# Outbound dedup (2d-1): Run reconciler a second time after all creates.
#   For each original 10 probe tickets, assert that Jira search by their
#   rebar-id label returns EXACTLY 1 issue per ticket.
# Inbound dedup (2d-2): Assert that each probe Jira issue has exactly one
#   local jira-* mirror (or one confirmed binding) — no duplicate local
#   CREATE events across passes.

echo ""
echo "=== PHASE 2d: Ticket-level dedup (outbound + inbound) ==="
echo ""

echo "Running second reconciler pass for dedup check..."
reconciler_output=$(run_filtered_reconciler "$PARITY_FILTER")
echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true

# 2d-1: Outbound dedup — exactly 1 Jira issue per probe rebar-id label
dedup_outbound_ok=true
for i in $(seq 0 9); do
    [ -z "${JIRA_KEYS[$i]}" ] && continue
    local_id="${LOCAL_IDS[$i]}"
    # The rebar-id label takes the form "rebar-id-<short-id>" or "rebar-id-<full-id>".
    # Search by the exact rebar-id label present on the known Jira key.
    jira_labels_raw=$(get_jira_labels "${JIRA_KEYS[$i]}" 2>/dev/null) || true
    rebar_id_label=$(python3 -c "
import json, sys
labels = json.loads(sys.argv[1]) if sys.argv[1].startswith('[') else []
match = [l for l in labels if l.startswith('rebar-id')]
print(match[0] if match else '')
" "$jira_labels_raw" 2>/dev/null) || true

    if [ -z "$rebar_id_label" ]; then
        skip_test "Phase2d.outbound-dedup-${i}" "no rebar-id label found on ${JIRA_KEYS[$i]}"
        continue
    fi

    dup_count=$(cd "$RECONCILER_DIR" && python3 -c "
import importlib.util, os, json
from rebar_reconciler import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
)
results = client.search_issues('project = ${JIRA_PROJECT} AND labels = \"${rebar_id_label}\"')
print(len(results))
" 2>/dev/null) || true
    dup_count="${dup_count:-0}"
    if [ "$dup_count" = "1" ]; then
        pass_test "Phase2d.outbound-dedup-${i} (1 Jira issue for ${rebar_id_label})"
    else
        fail_test "Phase2d.outbound-dedup-${i}" "expected 1, got ${dup_count} for ${rebar_id_label}"
        dedup_outbound_ok=false
    fi
done

if [ "$dedup_outbound_ok" = true ]; then
    matrix_set "dedup" "outbound" "no-duplicate" "PASS"
else
    matrix_set "dedup" "outbound" "no-duplicate" "FAIL"
fi

# 2d-2: Inbound dedup — exactly one local confirmed binding per Jira key
dedup_inbound_ok=true
BINDINGS_FILE="${TRACKER_DIR}/.bridge_state/bindings.json"
for i in $(seq 0 9); do
    [ -z "${JIRA_KEYS[$i]}" ] && continue
    jira_key="${JIRA_KEYS[$i]}"
    # Count how many local IDs map to this Jira key in the bindings store
    binding_count=$(python3 -c "
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print(0)
    sys.exit()
bindings = data.get('bindings', {})
count = sum(1 for entry in bindings.values()
            if isinstance(entry, dict) and entry.get('jira_key') == sys.argv[2])
print(count)
" "$BINDINGS_FILE" "$jira_key" 2>/dev/null) || true
    binding_count="${binding_count:-0}"
    if [ "$binding_count" = "1" ]; then
        pass_test "Phase2d.inbound-dedup-${i} (1 local binding for ${jira_key})"
    else
        fail_test "Phase2d.inbound-dedup-${i}" "expected 1 local binding, got ${binding_count} for ${jira_key}"
        dedup_inbound_ok=false
    fi
done

if [ "$dedup_inbound_ok" = true ]; then
    matrix_set "dedup" "inbound" "no-duplicate" "PASS"
else
    matrix_set "dedup" "inbound" "no-duplicate" "FAIL"
fi

# ===========================================================================
# PHASE 3: Inbound update sync
# ===========================================================================

echo ""
echo "=== PHASE 3: Edit in Jira and sync inbound ==="
echo ""

# Ticket 1: edit summary in Jira
if [ -n "${JIRA_KEYS[0]}" ]; then
    jira_update_issue "${JIRA_KEYS[0]}" "summary=FIELD-PROBE-1: JIRA-EDITED ${PROBE_TS}" 2>&1 || true
    pass_test "Phase3.jira-edit-summary"
fi

# Ticket 1: edit description in Jira
if [ -n "${JIRA_KEYS[0]}" ]; then
    jira_update_issue "${JIRA_KEYS[0]}" "description=Jira-edited description" 2>&1 || true
    pass_test "Phase3.jira-edit-description"
fi

# Ticket 2: change priority to High (→ local 1)
if [ -n "${JIRA_KEYS[1]}" ]; then
    jira_update_priority "${JIRA_KEYS[1]}" "High" 2>&1 || true
    pass_test "Phase3.jira-edit-priority"
fi

# Ticket 5: re-assign from Jira side
if [ "$ASSIGNEE_SKIP" = false ] && [ -n "${JIRA_KEYS[4]}" ]; then
    jira_update_issue "${JIRA_KEYS[4]}" "assignee=${PROBE_USER}" 2>&1 || true
    pass_test "Phase3.jira-edit-assignee"
fi

# Ticket 6: add label-e from Jira side
if [ -n "${JIRA_KEYS[5]}" ]; then
    jira_add_label "${JIRA_KEYS[5]}" "label-e" 2>&1 || true
    pass_test "Phase3.jira-add-label"
fi

# Ticket 6: remove label-b from Jira side (inbound label removal test)
if [ -n "${JIRA_KEYS[5]}" ]; then
    jira_remove_label "${JIRA_KEYS[5]}" "label-b" 2>&1 || true
    pass_test "Phase3.jira-remove-label"
fi

# Ticket 7: change issuetype in Jira to Bug (should sync inbound)
if [ -n "${JIRA_KEYS[6]}" ]; then
    jira_update_issuetype "${JIRA_KEYS[6]}" "Bug" 2>&1 || true
    pass_test "Phase3.jira-edit-issuetype"
fi

# Ticket 8: add comment from Jira side (tests inbound comment gap)
if [ -n "${JIRA_KEYS[7]}" ]; then
    jira_add_comment "${JIRA_KEYS[7]}" "Jira-side comment" 2>&1 || true
    pass_test "Phase3.jira-add-comment"
fi

# Ticket 9: transition to In Progress
if [ -n "${JIRA_KEYS[8]}" ]; then
    jira_transition "${JIRA_KEYS[8]}" "In Progress" 2>&1 || true
    pass_test "Phase3.jira-transition-status"
fi

# Wait for Jira index consistency
sleep 3

# Sync
echo "Running reconciler for inbound sync..."
reconciler_output=$(run_filtered_reconciler "$FILTER_IDS")
echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true

# Verify inbound updates AGAINST THE DOCUMENTED CONFLICT-RESOLUTION POLICY
# (rebar_reconciler/conflict_resolver.py FIELD_CLASSES):
#   state  (title/priority/status/assignee/type) → resolve_state: LOCAL ALWAYS
#       WINS. A Jira-side edit does NOT sync inbound; local is pushed outbound.
#   additive (description/comments) → local content is never dropped.
#   set    (labels) → union: a Jira ADD propagates inbound; a Jira REMOVE does not.
# Tickets 1,2,5 were also edited locally in Phase 2; for STATE fields the outcome
# is identical whether or not local changed (resolve_state is unconditional).

# Title (ticket 1) — STATE → local-wins. Jira "JIRA-EDITED" must NOT land; local
# keeps its Phase-2 value and the reconciler reverts Jira outbound.
local_title=$(get_local_field "${LOCAL_IDS[0]}" "title")
if [[ "$local_title" == *"UPDATED title"* && "$local_title" != *"JIRA-EDITED"* ]]; then
    pass_test "Phase3.verify-title-inbound (state→local-wins; no inbound)"
    matrix_set "title" "inbound" "update" "LOCAL-WINS"
else
    fail_test "Phase3.verify-title-inbound" "state field must keep local 'UPDATED title'; got: ${local_title}"
    matrix_set "title" "inbound" "update" "FAIL"
fi

# Description (ticket 1) — ADDITIVE → local content is never dropped (local-first
# merge). Local keeps its Phase-2 edit; Jira receives the merge outbound.
local_desc=$(get_local_field "${LOCAL_IDS[0]}" "description")
if [[ "$local_desc" == *"Updated description"* ]]; then
    pass_test "Phase3.verify-description-inbound (additive: local content retained)"
    matrix_set "description" "inbound" "update" "ADDITIVE"
else
    fail_test "Phase3.verify-description-inbound" "additive must retain local 'Updated description'; got: ${local_desc}"
    matrix_set "description" "inbound" "update" "FAIL"
fi

# Priority (ticket 2) — STATE → local-wins. Local stays 3 (Phase-2 value); Jira
# "High" (→1) does NOT sync inbound.
local_priority=$(get_local_field "${LOCAL_IDS[1]}" "priority")
if [ "$local_priority" = "3" ]; then
    pass_test "Phase3.verify-priority-inbound (state→local-wins; stays 3)"
    matrix_set "priority" "inbound" "update" "LOCAL-WINS"
else
    fail_test "Phase3.verify-priority-inbound" "state field must keep local 3; got: ${local_priority}"
    matrix_set "priority" "inbound" "update" "FAIL"
fi

# Assignee (ticket 5) — STATE → local-wins. Local stays unassigned (Phase-2);
# Jira re-assignment does NOT sync inbound.
if [ "$ASSIGNEE_SKIP" = false ]; then
    local_assignee=$(get_local_field "${LOCAL_IDS[4]}" "assignee")
    if [ -z "$local_assignee" ] || [ "$local_assignee" = "None" ]; then
        pass_test "Phase3.verify-assignee-inbound (state→local-wins; stays unassigned)"
        matrix_set "assignee" "inbound" "update" "LOCAL-WINS"
    else
        fail_test "Phase3.verify-assignee-inbound" "state field must keep local unassigned; got: '${local_assignee}'"
        matrix_set "assignee" "inbound" "update" "FAIL"
    fi
else
    skip_test "Phase3.verify-assignee-inbound" "ASSIGNEE_SKIP"
    matrix_set "assignee" "inbound" "update" "SKIP"
fi

# Labels (ticket 6) — SET → union. A Jira ADD (label-e) propagates inbound; a
# Jira REMOVE (label-b) does NOT (union only adds, never removes).
local_tags=$(get_local_field "${LOCAL_IDS[5]}" "tags")
label_inbound_ok=true
if echo "$local_tags" | grep -q "label-e"; then
    pass_test "Phase3.verify-label-add-inbound (set union: Jira add propagates)"
else
    fail_test "Phase3.verify-label-add-inbound" "label-e (Jira add) must propagate inbound; got: ${local_tags}"
    label_inbound_ok=false
fi
if echo "$local_tags" | grep -q '"label-b"'; then
    pass_test "Phase3.verify-label-remove-inbound (set union: Jira remove NOT propagated; label-b kept)"
else
    fail_test "Phase3.verify-label-remove-inbound" "set union must NOT drop label-b on a Jira-side remove; got: ${local_tags}"
    label_inbound_ok=false
fi
if [ "$label_inbound_ok" = true ]; then
    matrix_set "labels" "inbound" "update" "UNION-ADD"
else
    matrix_set "labels" "inbound" "update" "FAIL"
fi

# Issuetype (ticket 7) — STATE (type) → local-wins. Local was set to "bug" in
# Phase 2; local keeps it (Jira "Bug" does not override inbound).
local_type=$(get_local_field "${LOCAL_IDS[6]}" "ticket_type")
if [ "$local_type" = "bug" ]; then
    pass_test "Phase3.verify-issuetype-inbound (state→local-wins; stays bug)"
    matrix_set "issuetype" "inbound" "update" "LOCAL-WINS"
else
    fail_test "Phase3.verify-issuetype-inbound" "state field must keep local 'bug'; got: ${local_type}"
    matrix_set "issuetype" "inbound" "update" "FAIL"
fi

# Comments (ticket 8) — additive class. INBOUND comment sync IS implemented
# (epic f89d closed bug 0ee6 — the inbound differ now reads the nested `comment`
# field): a Jira-side comment MUST flow to local. This guard was previously
# INVERTED — it FAILED when comments synced and PASSED on the gap (matrix
# NOT-SYNCED), locking the bug in as "expected". De-encoded here so the fix is
# detected and a regression FAILS the probe (story 822a).
local_comments=$("$TICKET_CLI" show "${LOCAL_IDS[7]}" 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
comments = data.get('comments', [])
for c in comments:
    body = c.get('body', '') if isinstance(c, dict) else str(c)
    print(body)
" 2>/dev/null) || true
if echo "$local_comments" | grep -q "Jira-side comment"; then
    pass_test "Phase3.verify-inbound-comments (inbound comment synced — bug 0ee6 closed)"
    matrix_set "comments" "inbound" "update" "SYNCED"
else
    fail_test "Phase3.verify-inbound-comments" "inbound comment did NOT sync to local — bug 0ee6 regressed"
    matrix_set "comments" "inbound" "update" "FAIL"
fi

# Status (ticket 9) — STATE → local-wins. Ticket 9 was NOT locally edited, yet a
# Jira transition still does NOT sync inbound (resolve_state is unconditional).
# Local stays "open".
local_status=$(get_local_field "${LOCAL_IDS[8]}" "status")
if [ "$local_status" = "open" ]; then
    pass_test "Phase3.verify-status-inbound (state→local-wins; stays open)"
    matrix_set "status" "inbound" "update" "LOCAL-WINS"
else
    fail_test "Phase3.verify-status-inbound" "state field must keep local 'open'; got: ${local_status}"
    matrix_set "status" "inbound" "update" "FAIL"
fi

# ===========================================================================
# PHASE 3a: GENUINE inbound on an UNTOUCHED ticket (positive + negative control)
# ===========================================================================
#
# Phase 2 does NOT touch ticket 4 (LOCAL_IDS[3]). Per the documented conflict
# policy the ONLY fields that flow inbound are SET (labels: union ADD) and
# additive; STATE fields (title/status/priority/assignee/type) are local-wins
# even when local is untouched (resolve_state is unconditional). So this phase
# runs both controls on the same untouched ticket:
#   POSITIVE — a Jira-side label ADD must land locally (set union).
#   NEGATIVE — a Jira-side title edit must be IGNORED locally (state local-wins).
# Together these pin the inbound sync boundary precisely.

echo ""
echo "=== PHASE 3a: Genuine inbound on untouched ticket (set-add lands; state ignored) ==="
echo ""

# POSITIVE control — Jira label add on the untouched ticket.
if [ -n "${JIRA_KEYS[3]}" ]; then
    jira_add_label "${JIRA_KEYS[3]}" "inbound-untouched-label" 2>&1 || true
    pass_test "Phase3a.jira-add-label-untouched"
fi
sleep 2
echo "Running reconciler for inbound on untouched ticket..."
reconciler_output=$(run_filtered_reconciler "$FILTER_IDS")
echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true

local_tags=$(get_local_field "${LOCAL_IDS[3]}" "tags")
if echo "$local_tags" | grep -q "inbound-untouched-label"; then
    pass_test "Phase3a.verify-untouched-label-inbound (set union add propagates)"
    matrix_set "labels" "inbound" "untouched-add" "PASS"
else
    fail_test "Phase3a.verify-untouched-label-inbound" "Jira label add must sync inbound on untouched ticket; got: ${local_tags}"
    matrix_set "labels" "inbound" "untouched-add" "FAIL"
fi

# NEGATIVE control — Jira title edit on the SAME untouched ticket must NOT land
# (title is STATE → local-wins even untouched).
if [ -n "${JIRA_KEYS[3]}" ]; then
    jira_update_issue "${JIRA_KEYS[3]}" "summary=FIELD-PROBE-4: JIRA-TITLE-IGNORED ${PROBE_TS}" 2>&1 || true
    sleep 2
    echo "Running reconciler (state local-wins negative control)..."
    reconciler_output=$(run_filtered_reconciler "$FILTER_IDS")
    echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true
    local_title=$(get_local_field "${LOCAL_IDS[3]}" "title")
    if [[ "$local_title" != *"JIRA-TITLE-IGNORED"* ]]; then
        pass_test "Phase3a.verify-untouched-title-local-wins (state ignores inbound)"
    else
        fail_test "Phase3a.verify-untouched-title-local-wins" "state title must stay local-wins even untouched; got: ${local_title}"
    fi
fi

# ===========================================================================
# PHASE 4: Status outbound negative test
# ===========================================================================

echo ""
echo "=== PHASE 4: Status outbound propagation ==="
echo ""

# Bug 85a1 (Gap 8): status outbound is now first-class — local status changes
# must propagate to Jira via REST POST /transitions. Previously this phase
# asserted BY_DESIGN no-propagation (gated behind REBAR_RECONCILER_STATUS_GATING);
# that gate was removed.
#
# Bug b859 (Part 1b, H4 fix): we transition LOCAL_IDS[2] (idx 2 — FIELD-PROBE-3
# priority low) which Phase 3 leaves untouched. Previously this phase used
# LOCAL_IDS[8] but Phase 3 jira_transition's it to In Progress, and Phase 3's
# local-wins outbound pass reverted Jira back to To Do — so Phase 4's
# transition open->in_progress could become a no-op (current-status drift) and
# the reconciler would emit no output. Using an untouched ticket guarantees a
# real local->Jira delta.
"$TICKET_CLI" transition "${LOCAL_IDS[2]}" open in_progress 2>/dev/null || true

echo "Running reconciler for status outbound test..."
reconciler_output=$(run_filtered_reconciler "$FILTER_IDS")
echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true

# Verify Jira status now reflects the local change.
if [ -n "${JIRA_KEYS[2]}" ]; then
    jira_status=$(get_jira_field "${JIRA_KEYS[2]}" "status")
    if [ "$jira_status" = "In Progress" ]; then
        pass_test "Phase4.verify-status-outbound-in-progress"
        matrix_set "status" "outbound" "update" "PASS"
    else
        fail_test "Phase4.verify-status-outbound-in-progress" "expected In Progress, got: ${jira_status}"
        matrix_set "status" "outbound" "update" "FAIL"
    fi
fi

# ===========================================================================
# PHASE 5: Delete behavior negative test
# ===========================================================================

echo ""
echo "=== PHASE 5: Delete behavior test ==="
echo ""

# Delete ticket 10 locally
"$TICKET_CLI" delete "${LOCAL_IDS[9]}" --user-approved 2>/dev/null || true
pass_test "Phase5.delete-local-ticket-10"

# Build filter with only 9 IDs (excluding ticket 10)
FILTER_IDS_9=$(build_filter_ids "${LOCAL_IDS[@]:0:9}")

echo "Running reconciler with 9-ticket filter..."
reconciler_output=$(run_filtered_reconciler "$FILTER_IDS_9")
echo "$reconciler_output" | grep -E "^(FILTERED|filter:|OK:|ERROR:)" || true

# Verify Jira issue for ticket 10 still exists
if [ -n "${JIRA_KEYS[9]}" ]; then
    jira_summary=$(get_jira_field "${JIRA_KEYS[9]}" "summary" 2>/dev/null) || true
    if [[ "$jira_summary" == *"FIELD-PROBE-10"* ]]; then
        pass_test "Phase5.verify-jira-NOT-deleted (ticket 10 still in Jira)"
        matrix_set "delete" "outbound" "exclusion" "BY_DESIGN"
    else
        fail_test "Phase5.verify-jira-NOT-deleted" "could not find ticket 10 in Jira"
        matrix_set "delete" "outbound" "exclusion" "FAIL"
    fi
fi

# ===========================================================================
# PHASE 6: Idempotency
# ===========================================================================

echo ""
echo "=== PHASE 6: Idempotency check (3 no-op passes) ==="
echo ""

for i in 1 2 3; do
    echo "Idempotency pass ${i}..."
    reconciler_output=$(run_filtered_reconciler "$FILTER_IDS_9")
    # Extract filtered mutation count from the filter: log line
    # awk is portable; grep -P / -oP are GNU-only and break on macOS BSD grep.
    # Line format: "filter: N mutations computed, M match filter (...)"
    filtered_count=$(echo "$reconciler_output" | awk '/^filter: [0-9]+ mutations computed, [0-9]+ match filter/ {print $5; exit}')
    filtered_count="${filtered_count:--1}"
    if [ "$filtered_count" = "0" ]; then
        pass_test "Phase6.idempotency-pass-${i} (0 filtered mutations)"
    else
        fail_test "Phase6.idempotency-pass-${i}" "expected 0 filtered mutations, got: ${filtered_count}"
    fi
done

# ===========================================================================
# PHASE 6a: Interleaved bidirectional idempotency (N=10 mixed passes)
# ===========================================================================
#
# Bug b859 (Part 4b): Phase 6 only checks no-op passes; doesn't exercise
# the convergence path under mixed local + Jira edits. Phase 6a alternates
# local and Jira edits on a single controlled ticket across 10 passes and
# asserts that each pass converges to its true delta (i.e., the diff is
# either 0 or precisely what was just edited — not phantom mutations).
#
# Uses LOCAL_IDS[3] (FIELD-PROBE-4 multiline desc; also used by Phase 3a
# inbound test). The ticket is tagged so any future orphan-mirror sweep
# preserves it.

echo ""
echo "=== PHASE 6a: Interleaved bidirectional idempotency (N=10) ==="
echo ""

if [ -n "${JIRA_KEYS[3]}" ] && [ -n "${LOCAL_IDS[3]}" ]; then
    # Tag the ticket so orphan sweeps skip it.
    "$TICKET_CLI" tag "${LOCAL_IDS[3]}" "probe:phase6a" 2>/dev/null || true

    # EVENTUAL idempotency: after a local OR Jira edit the reconciler converges
    # to steady state (0 pending mutations) over a SMALL number of passes — not
    # necessarily one. Empirically a Jira-side edit settles over 2-3 passes
    # (local-wins revert + live Jira search-index eventual consistency), so the
    # honest assertion is "reaches 0 within K passes", which still FAILS on a
    # genuine non-converging drift. (The earlier "<=2 in a single pass" budget
    # was a false premise — the reconciler is eventually-, not instantly-,
    # consistent.)
    PHASE6A_MAX_SETTLE_PASSES=6
    PHASE6A_FAIL=0
    for n in $(seq 1 10); do
        # Alternate: odd N edits LOCAL title; even N edits JIRA summary.
        if (( n % 2 == 1 )); then
            "$TICKET_CLI" edit "${LOCAL_IDS[3]}" --title "Phase6a-LOCAL-${n} ${PROBE_TS}" 2>/dev/null || true
        else
            jira_update_issue "${JIRA_KEYS[3]}" "summary=Phase6a-JIRA-${n} ${PROBE_TS}" >/dev/null 2>&1 || true
            sleep 1
        fi
        # Reconcile until the filtered store reaches steady state (0 mutations),
        # bounded by PHASE6A_MAX_SETTLE_PASSES.
        settled=false
        last_mut=-1
        settle_passes=0
        for _attempt in $(seq 1 "$PHASE6A_MAX_SETTLE_PASSES"); do
            settle_passes=$_attempt
            reconciler_output=$(run_filtered_reconciler "$FILTER_IDS")
            last_mut=$(echo "$reconciler_output" | awk '/^filter: [0-9]+ mutations computed, [0-9]+ match filter/ {print $5; exit}')
            last_mut="${last_mut:--1}"
            if [ "$last_mut" = "0" ]; then
                settled=true
                break
            fi
            sleep 1
        done
        if [ "$settled" = true ]; then
            pass_test "Phase6a.pass-${n} (converged to steady state in ${settle_passes} pass(es))"
        else
            fail_test "Phase6a.pass-${n}" "did NOT converge to 0 within ${PHASE6A_MAX_SETTLE_PASSES} passes (last filtered mutations=${last_mut})"
            PHASE6A_FAIL=$((PHASE6A_FAIL + 1))
        fi
    done
    if [ "$PHASE6A_FAIL" = "0" ]; then
        pass_test "Phase6a.summary (all 10 edits reached steady state — eventual idempotency holds)"
    else
        fail_test "Phase6a.summary" "${PHASE6A_FAIL} of 10 edits did not converge to steady state"
    fi
fi

# ===========================================================================
# PHASE 7: Reconciliation check
# ===========================================================================

echo ""
echo "=== PHASE 7: Reconciliation check ==="
echo ""

reconcile_check_output=$(run_reconciler --mode reconcile-check --repo-root "$REPO_ROOT")
echo "$reconcile_check_output" | head -20

# Check only our 9 probe Jira keys for discrepancies
probe_discrepancies=0
for i in $(seq 0 8); do
    [ -z "${JIRA_KEYS[$i]}" ] && continue
    if echo "$reconcile_check_output" | grep -q "discrepancy.*${JIRA_KEYS[$i]}\|${JIRA_KEYS[$i]}.*discrepancy\|${JIRA_KEYS[$i]}.*mismatch"; then
        fail_test "Phase7.reconcile-check-${i}" "discrepancy for ${JIRA_KEYS[$i]}"
        probe_discrepancies=$((probe_discrepancies + 1))
    fi
done
if [ "$probe_discrepancies" -eq 0 ]; then
    pass_test "Phase7.reconcile-check (0 discrepancies for probe keys)"
fi

# ===========================================================================
# PHASE 8: Cleanup
# ===========================================================================

echo ""
echo "=== PHASE 8: Cleanup ==="
echo ""

cleanup_failed=false

# Delete all 10 Jira issues
for i in $(seq 0 9); do
    if [ -n "${JIRA_KEYS[$i]}" ]; then
        if jira_delete_issue "${JIRA_KEYS[$i]}" 2>/dev/null; then
            pass_test "Phase8.delete-jira-${i} (${JIRA_KEYS[$i]})"
        else
            fail_test "Phase8.delete-jira-${i}" "${JIRA_KEYS[$i]}"
            cleanup_failed=true
        fi
    fi
done

# Delete Phase 2c Jira issues (child first, then third, then epic — ordering
# avoids Jira's "has children" constraint on Epic delete where applicable).
for parity_pair in "child:${CHILD_JIRA_KEY}" "third:${THIRD_JIRA_KEY}" "epic2:${EPIC2_JIRA_KEY}" "epic:${EPIC_JIRA_KEY}"; do
    parity_label="${parity_pair%%:*}"
    parity_key="${parity_pair#*:}"
    if [ -n "$parity_key" ]; then
        if jira_delete_issue "$parity_key" 2>/dev/null; then
            pass_test "Phase8.delete-jira-${parity_label} (${parity_key})"
        else
            fail_test "Phase8.delete-jira-${parity_label}" "${parity_key}"
            cleanup_failed=true
        fi
    fi
done

# Delete remaining 9 local tickets (ticket 10 already deleted)
for i in $(seq 0 8); do
    if "$TICKET_CLI" delete "${LOCAL_IDS[$i]}" --user-approved 2>/dev/null; then
        pass_test "Phase8.delete-local-${i} (${LOCAL_IDS[$i]})"
    else
        fail_test "Phase8.delete-local-${i}" "${LOCAL_IDS[$i]}"
        cleanup_failed=true
    fi
done

# Delete Phase 2c local tickets (child and third must be deleted before epic
# since open-children guard blocks epic closure).
for parity_pair in "child:${CHILD_LOCAL_ID}" "third:${THIRD_LOCAL_ID}" "epic2:${EPIC2_LOCAL_ID}" "epic:${EPIC_LOCAL_ID}"; do
    parity_label="${parity_pair%%:*}"
    parity_id="${parity_pair#*:}"
    if [ -n "$parity_id" ]; then
        if "$TICKET_CLI" delete "$parity_id" --user-approved 2>/dev/null; then
            pass_test "Phase8.delete-local-${parity_label} (${parity_id})"
        else
            fail_test "Phase8.delete-local-${parity_label}" "${parity_id}"
            cleanup_failed=true
        fi
    fi
done

# Snapshot is restored by the EXIT trap.

# Always run tag-based fallback cleanup — covers the case where bindings
# were never confirmed (JIRA_KEYS[i] empty), so the indexed loop above
# couldn't delete the Jira issues that DID get created by the reconciler.
echo "Running tag-based fallback cleanup to catch any orphaned Jira issues..."
fallback_cleanup

# ===========================================================================
# Report
# ===========================================================================

echo ""
echo "==================================================================================="
echo "FIELD SYNC MATRIX — what syncs between local tickets and Jira (${JIRA_PROJECT}) — ${PROBE_TAG}"
echo "==================================================================================="
echo ""
printf "%-12s %-9s %-13s %-16s %-22s\n" "Field" "Class" "Create L→J" "Update out L→J" "Update in J→L"
printf "%-12s %-9s %-13s %-16s %-22s\n" "------------" "---------" "-------------" "----------------" "----------------------"

# Documented sync policy per field (conflict_resolver.py FIELD_CLASSES).
declare -A FIELD_CLASS=(
    [title]=state [description]=additive [priority]=state [assignee]=state
    [issuetype]=state [status]=state [labels]=set [comments]=additive
    [parent]=hier [epic]=hier [delete]=ticket [dedup]=n/a
)

for field in title description priority assignee issuetype status labels comments parent epic delete dedup; do
    cls="${FIELD_CLASS[$field]:-?}"
    oc="${MATRIX["${field}:outbound:create"]:-N/A}"
    ou="${MATRIX["${field}:outbound:update"]:-N/A}"
    iu="${MATRIX["${field}:inbound:update"]:-N/A}"
    # Handle special cases
    case "$field" in
        status)  oc="SYNCED(→To Do)" ;;
        delete)  ou="—"; iu="—"; oc="${MATRIX["delete:outbound:exclusion"]:-N/A}" ;;
        epic)    ou="N/A (36af)"; iu="${MATRIX["epic:inbound:mirror"]:-N/A}" ;;
        parent)  oc="${MATRIX["parent:outbound:create"]:-N/A}"
                 ou="${MATRIX["parent:outbound:update"]:-N/A}"
                 iu="${MATRIX["parent:inbound:update"]:-N/A}" ;;
        dedup)   oc="—"
                 ou="${MATRIX["dedup:outbound:no-duplicate"]:-N/A}"
                 iu="${MATRIX["dedup:inbound:no-duplicate"]:-N/A}" ;;
    esac
    printf "%-12s %-9s %-13s %-16s %-22s\n" "$field" "$cls" "$oc" "$ou" "$iu"
done

cat <<'LEGEND'

Legend — Class is the conflict_resolver.py FIELD_CLASSES sync rule:
  state    → SYNCED outbound; inbound = LOCAL-WINS (Jira edits never overwrite local).
  additive → SYNCED outbound; inbound merges (local content never dropped). (comments:
             inbound SYNCED both ways — bug 0ee6 closed, epic f89d.)
  set      → SYNCED outbound; inbound = UNION-ADD (Jira ADDs land locally; Jira
             REMOVEs do NOT propagate).
  hier     → parent/epic hierarchy links (outbound).
  ticket   → ticket-level: a local delete leaves the Jira issue INTACT by design.
CRUD coverage: Create = Phase 1 (local→Jira on create); Read = every verify reads
both sides; Update out = Phase 2 (local edit→Jira); Update in = Phase 3/3a (Jira
edit→local, per Class); Delete = Phase 5 (local delete; Jira retained).
LEGEND

echo ""
echo "==========================================="
echo "E2E FIELD VALIDATION SUMMARY: ${PASSED} passed, ${FAILED} failed, ${SKIPPED} skipped"
echo "==========================================="

if [ "$FAILED" -gt 0 ]; then
    exit 1
fi
exit 0
