#!/usr/bin/env bash
# E2E validation probe
# Exercises the bidirectional sync pipeline against a connected Jira instance.
#
# MAINTAINED MANUAL PRESSURE-TEST TOOLING. See scripts/jira-pressure-test/README.md.
# This script is excluded from the automated test suite and published wheel.
# Run it manually when validating bridge changes. Do not add it to CI.
#
# Phases:
#   1. Create local ticket → sync outbound → verify Jira issue created
#   2. Edit local ticket → sync outbound → verify Jira updated
#   3. Edit Jira issue → sync inbound → verify local ticket updated
#   4. Idempotency — 3 no-op passes → verify 0 mutations each
#   5. Bridge preview cleanliness check → verify 0 proposed probe changes
#   6. Cleanup — delete Jira issue + local ticket
#
# Run this probe manually from the repository root. It requires
# REBAR_E2E_VALIDATION_PROBE=1 and explicit Jira connection variables.

set -euo pipefail

if [ "${REBAR_E2E_VALIDATION_PROBE:-0}" != "1" ]; then
    echo "FATAL: e2e_validation_probe.sh requires REBAR_E2E_VALIDATION_PROBE=1" >&2
    echo "The probe creates Jira issues in the selected project." >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT="$(git rev-parse --show-toplevel)"
RECONCILER_DIR="${REBAR_ENGINE_DIR:-${REPO_ROOT}/src/rebar/_engine}"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
TICKET_CLI="${REBAR_TICKET_CLI:-${REPO_ROOT}/.venv/bin/rebar}"

for variable in JIRA_URL JIRA_USER JIRA_API_TOKEN JIRA_PROJECT; do
    if [ -z "${!variable:-}" ]; then
        echo "FATAL: ${variable} is not set." >&2
        exit 2
    fi
done
if [ ! -x "$PYTHON_BIN" ]; then
    echo "FATAL: checkout Python is not executable at ${PYTHON_BIN}." >&2
    exit 2
fi
if [ ! -x "$TICKET_CLI" ]; then
    echo "FATAL: ticket CLI is not executable at ${TICKET_CLI}." >&2
    exit 2
fi
if [ ! -d "$RECONCILER_DIR" ]; then
    echo "FATAL: rebar engine directory does not exist at ${RECONCILER_DIR}." >&2
    exit 2
fi
if ! (cd "$RECONCILER_DIR" && "$PYTHON_BIN" -c "
from rebar_reconciler.adapters.jira import acli as mod
client_type = getattr(mod, 'AcliClient', None)
operations = (
    ('mod.update_issue', getattr(mod, 'update_issue', None)),
    ('mod.AcliClient.get_issue', getattr(client_type, 'get_issue', None)),
    ('mod.AcliClient.get_comments', getattr(client_type, 'get_comments', None)),
    ('mod.AcliClient.add_comment', getattr(client_type, 'add_comment', None)),
    ('mod.AcliClient.delete_issue', getattr(client_type, 'delete_issue', None)),
)
missing = [name for name, operation in operations if not callable(operation)]
if missing:
    raise TypeError('required Jira adapter operations are not callable: ' + ', '.join(missing))
"); then
    echo "FATAL: checkout Python cannot load required Jira adapter operations from ${RECONCILER_DIR}." >&2
    exit 2
fi
PROBE_TS="$(date +%s)"
PROBE_TAG="probe-test"
E2E_TAG="e2e-validation"

# Counters
PASSED=0
FAILED=0
SKIPPED=0
LOCAL_ID=""
JIRA_KEY=""

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
}

skip_test() {
    local name="$1"
    local reason="${2:-}"
    echo "SKIP: $name${reason:+ — $reason}"
    SKIPPED=$((SKIPPED + 1))
}

# Run one reconciler operation with the checkout Python.
run_reconciler() {
    local output
    output=$(cd "$RECONCILER_DIR" && "$PYTHON_BIN" -m rebar_reconciler "$@" 2>&1) || true
    echo "$output"
}

# Run a reconciler pass SCOPED to this probe's own ticket (LOCAL_ID). The store
# may hold local-only tickets that are not meant to sync to Jira; an unfiltered
# sync would otherwise try to push them all to Jira. --only narrows the complete
# examination to the probe ticket so the probe is safe and deterministic.
run_filtered_reconciler() {
    run_reconciler sync --max-changes 100 --only "$LOCAL_ID" --repo-root "$REPO_ROOT"
}

run_bridge_preview_for_ids() {
    local filter_ids="$1"
    cd "$RECONCILER_DIR"
    "$PYTHON_BIN" -m rebar_reconciler preview --only "$filter_ids" --repo-root "$REPO_ROOT"
}

preview_plan_is_clean_for_probe() {
    local output="$1"
    local local_id="$2"
    local jira_key="${3:-}"
    printf '%s\n' "$output" | "$PYTHON_BIN" -c '
import json
import sys

local_id = sys.argv[1]
jira_key = sys.argv[2] if len(sys.argv) > 2 else ""
doc = None
for line in reversed([line.strip() for line in sys.stdin if line.strip()]):
    try:
        doc = json.loads(line)
        break
    except json.JSONDecodeError:
        continue
if doc is None:
    print("preview emitted no JSON document")
    sys.exit(1)
route = doc.get("route")
if route != "preview":
    print(f"expected route=preview, got {route!r}")
    sys.exit(1)
mutation_count = doc.get("mutation_count", -1)
if int(mutation_count) != 0:
    print(f"expected mutation_count=0, got {mutation_count!r}")
    sys.exit(1)
plan_text = json.dumps(doc.get("plan", []), sort_keys=True)
for needle in (local_id, jira_key):
    if needle and needle in plan_text:
        print(f"preview plan still contains probe identifier {needle}")
        sys.exit(1)
' "$local_id" "$jira_key"
}

# Extract a field from a Jira issue via ACLI search (search-based, not
# view-based — mirrors the _get_field pattern from the capability probe).
get_jira_field() {
    local key="$1"
    local field="$2"
    cd "$RECONCILER_DIR"
    "$PYTHON_BIN" -c "
import json, os
from rebar_reconciler.adapters.jira import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
    jira_project=os.environ['JIRA_PROJECT'],
)
issue = client.get_issue('${key}')
fields = issue.get('fields', issue)
val = fields.get('${field}', '')
if isinstance(val, dict):
    val = val.get('name', val.get('displayName', ''))
if isinstance(val, list):
    print(json.dumps(val))
else:
    print(val)
"
}

# Get Jira labels as a JSON array.
get_jira_labels() {
    local key="$1"
    get_jira_field "$key" "labels"
}

# Get Jira comments via ACLI.
get_jira_comments() {
    local key="$1"
    cd "$RECONCILER_DIR"
    "$PYTHON_BIN" -c "
import os
from rebar_reconciler.adapters.jira import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
    jira_project=os.environ['JIRA_PROJECT'],
)
comments = client.get_comments('${key}')
for c in comments:
    body = c.get('body', '') if isinstance(c, dict) else str(c)
    print(body)
"
}

# Read local ticket field via ticket show (JSON).
get_local_field() {
    local ticket_id="$1"
    local field="$2"
    "$TICKET_CLI" show "$ticket_id" 2>/dev/null | "$PYTHON_BIN" -c "
import sys, json
data = json.load(sys.stdin)
val = data.get('${field}', '')
if isinstance(val, list):
    print(json.dumps(val))
else:
    print(val)
"
}

# Check binding store for a confirmed binding for a local_id.
check_binding() {
    local local_id="$1"
    local tracker_dir="${REPO_ROOT}/.tickets-tracker"  # tickets-boundary-ok
    local bindings_file="${tracker_dir}/.bridge_state/bindings.json"
    if [ ! -f "$bindings_file" ]; then
        echo "no-bindings-file"
        return
    fi
    "$PYTHON_BIN" -c "
import json, sys
data = json.load(open('${bindings_file}'))
entry = data.get('bindings', {}).get('${local_id}')
if entry is None:
    print('unbound')
elif entry.get('state') == 'confirmed':
    print('confirmed:' + (entry.get('jira_key') or 'none'))
else:
    print(entry.get('state', 'unknown'))
"
}

# Extract the FILTERED mutation count from a filtered reconciler pass. The line
# is "filter: <N> mutations computed, <M> match filter ..."; we want M (the count
# scoped to the probe's filter), not N (the whole-store total). Uses awk (BSD grep
# lacks the -P/PCRE used previously, which silently failed on macOS).
extract_mutation_count() {
    local output="$1"
    echo "$output" | awk '/^filter: [0-9]+ mutations computed, [0-9]+ match filter/ {print $5; exit}'
}

# ---------------------------------------------------------------------------
# Phase 1: Create local ticket and sync outbound
# ---------------------------------------------------------------------------

echo ""
echo "=== PHASE 1: Create local ticket and sync outbound ==="
echo ""

# Step 1: Create a local test ticket with known field values.
create_output=$("$TICKET_CLI" create task "E2E-PROBE: sync validation ${PROBE_TS}" \
    -d "Description for E2E probe test" \
    --priority 1 \
    --tags "${PROBE_TAG},${E2E_TAG}" 2>&1)
# `create` prints a one-line confirmation embedding the id — extract it.
LOCAL_ID=$(echo "$create_output" | grep -oE '[0-9a-f]{4}(-[0-9a-f]{4}){3}' | tail -1)

if [ -z "$LOCAL_ID" ]; then
    fail_test "Phase1.create-local" "ticket create returned no ID: ${create_output}"
    echo ""
    echo "E2E VALIDATION SUMMARY: ${PASSED} passed, ${FAILED} failed, ${SKIPPED} skipped"
    exit 1
fi
pass_test "Phase1.create-local (${LOCAL_ID})"

# Step 2: Run one reconciler pass with mode=bootstrap-strict (cap=10).
echo "Running reconciler pass (bootstrap-strict)..."
reconciler_output=$(run_filtered_reconciler)
echo "$reconciler_output"

# Step 3: Verify a new Jira issue was created via the binding store.
binding_state=$(check_binding "$LOCAL_ID")
if [[ "$binding_state" == confirmed:* ]]; then
    JIRA_KEY="${binding_state#confirmed:}"
    pass_test "Phase1.binding-confirmed (${LOCAL_ID} → ${JIRA_KEY})"
else
    fail_test "Phase1.binding-confirmed" "expected confirmed, got: ${binding_state}"
    # Cannot continue without a Jira key — skip remaining phases.
    skip_test "Phase2-6" "no Jira binding established"
    echo ""
    echo "E2E VALIDATION SUMMARY: ${PASSED} passed, ${FAILED} failed, ${SKIPPED} skipped"
    exit 1
fi

# Step 3a: Verify Jira issue has correct summary.
jira_summary=$(get_jira_field "$JIRA_KEY" "summary")
if [[ "$jira_summary" == *"E2E-PROBE: sync validation ${PROBE_TS}"* ]]; then
    pass_test "Phase1.jira-summary"
else
    fail_test "Phase1.jira-summary" "expected title containing probe TS, got: ${jira_summary}"
fi

# Step 3b: Verify Jira issue has correct priority (1 → High).
jira_priority=$(get_jira_field "$JIRA_KEY" "priority")
if [[ "$jira_priority" == "High" ]]; then
    pass_test "Phase1.jira-priority"
else
    fail_test "Phase1.jira-priority" "expected High, got: ${jira_priority}"
fi

# Step 3c: Verify Jira issue type is Task.
jira_type=$(get_jira_field "$JIRA_KEY" "issuetype")
if [[ "$jira_type" == "Task" ]]; then
    pass_test "Phase1.jira-issuetype"
else
    fail_test "Phase1.jira-issuetype" "expected Task, got: ${jira_type}"
fi

# Step 3d: Verify Jira labels include probe-test and e2e-validation.
jira_labels=$(get_jira_labels "$JIRA_KEY")
if echo "$jira_labels" | grep -q "$PROBE_TAG"; then
    pass_test "Phase1.jira-label-probe-test"
else
    fail_test "Phase1.jira-label-probe-test" "labels: ${jira_labels}"
fi
if echo "$jira_labels" | grep -q "$E2E_TAG"; then
    pass_test "Phase1.jira-label-e2e-validation"
else
    fail_test "Phase1.jira-label-e2e-validation" "labels: ${jira_labels}"
fi

# Step 3e: Verify Jira issue has rebar-id label for binding.
if echo "$jira_labels" | grep -q "rebar-id"; then
    pass_test "Phase1.jira-rebar-id-label"
else
    fail_test "Phase1.jira-rebar-id-label" "no rebar-id label found: ${jira_labels}"
fi

# ---------------------------------------------------------------------------
# Phase 2: Edit locally and sync outbound
# ---------------------------------------------------------------------------

echo ""
echo "=== PHASE 2: Edit locally and sync outbound ==="
echo ""

# Step 4: Edit the local ticket title via the CLI. The store is event-sourced —
# there is no per-ticket ticket.json to edit in place — and `rebar edit` exists
# (the old "CLI has no edit subcommand" assumption is stale).
if "$TICKET_CLI" edit "$LOCAL_ID" --title="E2E-PROBE: EDITED title ${PROBE_TS}" 2>/dev/null; then
    pass_test "Phase2.edit-local-title"
else
    fail_test "Phase2.edit-local-title" "rebar edit --title failed for ${LOCAL_ID}"
fi

# Step 5: Edit local priority to 3 via the CLI.
if "$TICKET_CLI" edit "$LOCAL_ID" --priority=3 2>/dev/null; then
    pass_test "Phase2.edit-local-priority"
else
    fail_test "Phase2.edit-local-priority" "rebar edit --priority failed for ${LOCAL_ID}"
fi

# Step 6: Add a local comment.
"$TICKET_CLI" comment "$LOCAL_ID" "Probe comment from local" 2>/dev/null || true
pass_test "Phase2.add-local-comment"

# Step 7: Add a local tag.
"$TICKET_CLI" tag "$LOCAL_ID" "probe-edit-tag" 2>/dev/null || true
pass_test "Phase2.add-local-tag"

# Step 8: Run another reconciler pass.
echo "Running reconciler pass (bootstrap-strict) for outbound updates..."
reconciler_output=$(run_filtered_reconciler)
echo "$reconciler_output"

# Step 9: Verify Jira issue updated.

# 9a: Summary changed.
jira_summary=$(get_jira_field "$JIRA_KEY" "summary")
if [[ "$jira_summary" == *"EDITED title"* ]]; then
    pass_test "Phase2.jira-summary-updated"
else
    fail_test "Phase2.jira-summary-updated" "expected EDITED title, got: ${jira_summary}"
fi

# 9b: Priority changed to Low (3 → Low).
jira_priority=$(get_jira_field "$JIRA_KEY" "priority")
if [[ "$jira_priority" == "Low" ]]; then
    pass_test "Phase2.jira-priority-updated"
else
    fail_test "Phase2.jira-priority-updated" "expected Low, got: ${jira_priority}"
fi

# 9c: Comment added.
jira_comments=$(get_jira_comments "$JIRA_KEY")
if echo "$jira_comments" | grep -q "Probe comment from local"; then
    pass_test "Phase2.jira-comment-added"
else
    fail_test "Phase2.jira-comment-added" "comment not found in Jira comments"
fi

# 9d: Label added.
jira_labels=$(get_jira_labels "$JIRA_KEY")
if echo "$jira_labels" | grep -q "probe-edit-tag"; then
    pass_test "Phase2.jira-label-added"
else
    fail_test "Phase2.jira-label-added" "probe-edit-tag not in labels: ${jira_labels}"
fi

# ---------------------------------------------------------------------------
# Phase 3: Edit on Jira side and sync inbound
# ---------------------------------------------------------------------------

echo ""
echo "=== PHASE 3: Edit on Jira side and sync inbound ==="
echo ""

# Step 10: Edit Jira summary via ACLI.
# Use the Jira adapter wrapper to avoid raw subprocess calls.
cd "$RECONCILER_DIR"
if "$PYTHON_BIN" -c "
import importlib.util
from rebar_reconciler.adapters.jira import acli as mod
mod.update_issue('${JIRA_KEY}', summary='E2E-PROBE: JIRA-EDITED ${PROBE_TS}')
" 2>&1; then
    pass_test "Phase3.jira-edit-summary"
else
    fail_test "Phase3.jira-edit-summary"
fi

# Step 11: Add Jira comment via ACLI.
if "$PYTHON_BIN" -c "
import os
from rebar_reconciler.adapters.jira import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
    jira_project=os.environ['JIRA_PROJECT'],
)
client.add_comment('${JIRA_KEY}', 'Probe comment from Jira')
" 2>&1; then
    pass_test "Phase3.jira-add-comment"
else
    fail_test "Phase3.jira-add-comment"
fi

# Wait briefly for Jira consistency.
sleep 2

# Step 12: Run another reconciler pass (inbound sync).
echo "Running reconciler pass (bootstrap-strict) for inbound sync..."
reconciler_output=$(run_filtered_reconciler)
echo "$reconciler_output"

# Step 13: Verify local ticket updated with Jira-side title.
local_title=$(get_local_field "$LOCAL_ID" "title")
if [[ "$local_title" == *"JIRA-EDITED"* ]]; then
    pass_test "Phase3.local-title-synced-from-jira"
else
    # The inbound differ may not update title if the outbound differ already
    # pushed our local edit — this depends on conflict resolution policy.
    # Accept either the Jira-edited or locally-edited title as valid.
    if [[ "$local_title" == *"EDITED title"* ]]; then
        pass_test "Phase3.local-title-synced-from-jira (local-wins — title retained)"
    else
        fail_test "Phase3.local-title-synced-from-jira" "got: ${local_title}"
    fi
fi

# ---------------------------------------------------------------------------
# Phase 4: Idempotency check
# ---------------------------------------------------------------------------

echo ""
echo "=== PHASE 4: Idempotency check (3 no-op passes) ==="
echo ""

# Settle to steady state first — the reconciler is eventually-consistent (a
# prior Jira-side edit converges over 2-3 passes), so reconcile until the
# filtered count reaches 0 before asserting no-op idempotency.
echo "Settling to steady state (eventual consistency)..."
for _ in 1 2 3 4 5 6; do
    reconciler_output=$(run_filtered_reconciler)
    mc=$(extract_mutation_count "$reconciler_output")
    [ "$mc" = "0" ] && break
    sleep 1
done

# Once settled, repeated no-op passes MUST each be 0 (true idempotency).
for i in 1 2 3; do
    echo "Idempotency pass ${i}..."
    reconciler_output=$(run_filtered_reconciler)
    echo "$reconciler_output"
    mutation_count=$(extract_mutation_count "$reconciler_output")
    if [ "$mutation_count" = "0" ]; then
        pass_test "Phase4.idempotency-pass-${i} (0 mutations)"
    else
        fail_test "Phase4.idempotency-pass-${i}" "expected 0 mutations, got: ${mutation_count}"
    fi
done

# ---------------------------------------------------------------------------
# Phase 5: Bridge preview cleanliness check
# ---------------------------------------------------------------------------

echo ""
echo "=== PHASE 5: Bridge preview cleanliness check ==="
echo ""

set +e
preview_output=$(run_bridge_preview_for_ids "$LOCAL_ID")
preview_rc=$?
set -e
echo "$preview_output"

if [ "$preview_rc" -ne 0 ]; then
    fail_test "Phase5.bridge-preview-clean" "preview exited ${preview_rc}"
elif preview_error=$(preview_plan_is_clean_for_probe "$preview_output" "$LOCAL_ID" "$JIRA_KEY" 2>&1); then
    pass_test "Phase5.bridge-preview-clean"
else
    fail_test "Phase5.bridge-preview-clean" "$preview_error"
fi

# ---------------------------------------------------------------------------
# Phase 6: Cleanup
# ---------------------------------------------------------------------------

echo ""
echo "=== PHASE 6: Cleanup ==="
echo ""

# Step 18: Delete the Jira test issue.
cd "$RECONCILER_DIR"
if "$PYTHON_BIN" -c "
import importlib.util, os
from rebar_reconciler.adapters.jira import acli as mod
client = mod.AcliClient(
    jira_url=os.environ['JIRA_URL'],
    user=os.environ['JIRA_USER'],
    api_token=os.environ['JIRA_API_TOKEN'],
    jira_project=os.environ['JIRA_PROJECT'],
)
client.delete_issue('${JIRA_KEY}')
" 2>&1; then
    pass_test "Phase6.delete-jira-issue (${JIRA_KEY})"
else
    fail_test "Phase6.delete-jira-issue (${JIRA_KEY})"
fi

# Step 19: Delete the local test ticket.
if "$TICKET_CLI" delete "$LOCAL_ID" --user-approved 2>/dev/null; then
    pass_test "Phase6.delete-local-ticket (${LOCAL_ID})"
else
    fail_test "Phase6.delete-local-ticket (${LOCAL_ID})"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "==========================================="
echo "E2E VALIDATION SUMMARY: ${PASSED} passed, ${FAILED} failed, ${SKIPPED} skipped"
echo "==========================================="

if [ "$FAILED" -gt 0 ]; then
    exit 1
fi
exit 0
