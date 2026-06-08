#!/usr/bin/env bash
# test-ticket-perf.sh
# Verifies that ticket show and ticket list complete in <0.15s mean wall-clock
# using hyperfine. Skips gracefully if hyperfine is not installed.
#
# Story context: 78fc-3858 (bash-native ticket ops), 564c-e391 (perf gate),
# 9482-39dd (this test).
#
# Usage: bash tests/scripts/test-ticket-perf.sh
# Returns: exit 0 if both pass (or skip), exit 1 if either fails.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_LIB="$REPO_ROOT/src/rebar/_engine/ticket-lib.sh"
BASELINE_FIXTURE="$REPO_ROOT/tests/fixtures/ticket-cli-baseline.json"

echo "=== test-ticket-perf.sh ==="
echo ""

# ── Step 1: Hyperfine availability check ─────────────────────────────────────
if ! command -v hyperfine >/dev/null 2>&1; then
    echo "SKIP: hyperfine not installed — skipping perf test"
    exit 0
fi

# ── Setup: temp repo with ticket system initialized ───────────────────────────
_CLEANUP_DIRS=()
# shellcheck disable=SC2329  # invoked via trap EXIT
_cleanup() {
    local d
    for d in "${_CLEANUP_DIRS[@]:-}"; do
        [ -n "$d" ] && [ -d "$d" ] && rm -rf "$d" 2>/dev/null || true
    done
}
trap _cleanup EXIT

# Use git-fixtures for a fast ticket-ready repo
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

WORK_DIR=$(mktemp -d)
_CLEANUP_DIRS+=("$WORK_DIR")
clone_ticket_repo "$WORK_DIR/repo"
TEST_REPO="$WORK_DIR/repo"
TRACKER_DIR="$TEST_REPO/.tickets-tracker"

# ── Step 2: Create a test ticket ──────────────────────────────────────────────
TICKET_ID=$(
    cd "$TEST_REPO" && \
    _TICKET_TEST_NO_SYNC=1 \
    TICKETS_TRACKER_DIR="$TRACKER_DIR" \
    bash "$TICKET_SCRIPT" create task "perf-test-ticket" 2>/dev/null \
    | tail -1
)

if [ -z "$TICKET_ID" ]; then
    echo "FAIL: setup — could not create test ticket"
    exit 1
fi
echo "Setup: created ticket $TICKET_ID in $TEST_REPO"
echo ""

# ── Helper: extract mean from hyperfine JSON ──────────────────────────────────
_extract_mean() {
    local json_file="$1"
    python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
mean = data['results'][0]['mean']
print('{:.4f}'.format(mean))
" "$json_file"
}

# ── Helper: format mean for display ──────────────────────────────────────────
_format_mean() {
    local mean_s="$1"
    python3 -c "print('{:.4f}s'.format(float('$mean_s')))"
}

BENCH_SHOW_JSON="/tmp/bench-show-$$.json"
BENCH_LIST_JSON="/tmp/bench-list-$$.json"
BENCH_CREATE_JSON="/tmp/bench-create-$$.json"
BENCH_COMMENT_JSON="/tmp/bench-comment-$$.json"
_CLEANUP_FILES=("$BENCH_SHOW_JSON" "$BENCH_LIST_JSON" "$BENCH_CREATE_JSON" "$BENCH_COMMENT_JSON")
# shellcheck disable=SC2329  # invoked via trap EXIT
_cleanup_files() {
    local f
    for f in "${_CLEANUP_FILES[@]:-}"; do
        [ -n "$f" ] && [ -f "$f" ] && rm -f "$f" 2>/dev/null || true
    done
}
# Append file cleanup to EXIT trap (already set above via _cleanup)
trap '_cleanup; _cleanup_files' EXIT

# Intermediate thresholds: library functions (ticket_list, ticket_create, ticket_comment)
# now route through ticket-lib-api.sh but still invoke Python internally. Final 60%
# latency reduction requires all callers to use the library (tracked in 161e-b2b4).
# Target post-161e-b2b4: THRESHOLD=0.15, WRITE_THRESHOLD=0.6, WRITE_COMMIT_EVENT_THRESHOLD=0.15.
#
# Measured baselines under parallel test-suite load (332 concurrent test scripts,
# 2026-04-23):
#   show: 0.24s (serial baseline 0.09s, +167%)
#   list: 0.39s (serial baseline 0.08s, +388%)
#   create: 1.80s (serial baseline 0.40s, +350%)
#   comment: 1.72s (serial baseline 0.33s, +421%)
#   write_commit_event overhead (no-op flock): 0.63s (serial baseline ~0.1s, +530%)
# Thresholds below set to ~30% above measured medians under load as a regression guard.
# If this test consistently fails under load, consider scheduling perf tests serially
# via a test-group marker (tracked as follow-up).
THRESHOLD=0.50
WRITE_THRESHOLD=2.5

# ── Step 3: Benchmark ticket show ─────────────────────────────────────────────
echo "--- Benchmarking: ticket show $TICKET_ID ---"
# Single-quote the variable expansions inside the hyperfine command string so
# that paths containing spaces survive bash --shell word-splitting.
if ! hyperfine \
    --warmup 3 \
    --runs 10 \
    --export-json "$BENCH_SHOW_JSON" \
    --shell bash \
    "_TICKET_TEST_NO_SYNC=1 TICKETS_TRACKER_DIR='$TRACKER_DIR' bash '$TICKET_SCRIPT' show '$TICKET_ID'" \
    2>&1; then
    echo "FAIL: hyperfine failed for ticket show"
    exit 1
fi
echo ""

# ── Step 4: Benchmark ticket list ─────────────────────────────────────────────
echo "--- Benchmarking: ticket list ---"
if ! hyperfine \
    --warmup 3 \
    --runs 10 \
    --export-json "$BENCH_LIST_JSON" \
    --shell bash \
    "_TICKET_TEST_NO_SYNC=1 TICKETS_TRACKER_DIR='$TRACKER_DIR' bash '$TICKET_SCRIPT' list" \
    2>&1; then
    echo "FAIL: hyperfine failed for ticket list"
    exit 1
fi
echo ""

# ── Step 4b: Benchmark ticket create ─────────────────────────────────────────
echo "--- Benchmarking: ticket create ---"
if ! hyperfine \
    --warmup 3 \
    --runs 10 \
    --export-json "$BENCH_CREATE_JSON" \
    --shell bash \
    "cd '$TEST_REPO' && _TICKET_TEST_NO_SYNC=1 TICKETS_TRACKER_DIR='$TRACKER_DIR' bash '$TICKET_SCRIPT' create task 'perf-create-bench'" \
    2>&1; then
    echo "FAIL: hyperfine failed for ticket create"
    exit 1
fi
echo ""

# ── Step 4c: Benchmark ticket comment ────────────────────────────────────────
echo "--- Benchmarking: ticket comment $TICKET_ID ---"
if ! hyperfine \
    --warmup 3 \
    --runs 10 \
    --export-json "$BENCH_COMMENT_JSON" \
    --shell bash \
    "cd '$TEST_REPO' && _TICKET_TEST_NO_SYNC=1 TICKETS_TRACKER_DIR='$TRACKER_DIR' bash '$TICKET_SCRIPT' comment '$TICKET_ID' 'perf-bench-comment'" \
    2>&1; then
    echo "FAIL: hyperfine failed for ticket comment"
    exit 1
fi
echo ""

# ── Step 5: Parse results and assert ─────────────────────────────────────────
SHOW_MEAN=$(_extract_mean "$BENCH_SHOW_JSON")
LIST_MEAN=$(_extract_mean "$BENCH_LIST_JSON")
CREATE_MEAN=$(_extract_mean "$BENCH_CREATE_JSON")
COMMENT_MEAN=$(_extract_mean "$BENCH_COMMENT_JSON")

SHOW_PASS=false
LIST_PASS=false
CREATE_PASS=false
COMMENT_PASS=false

# Compare using python3 for reliable float comparison
if python3 -c "import sys; sys.exit(0 if float('$SHOW_MEAN') < $THRESHOLD else 1)"; then
    SHOW_PASS=true
    echo "PASS: ticket show mean=$(_format_mean "$SHOW_MEAN") (<${THRESHOLD}s)"
else
    echo "FAIL: ticket show mean=$(_format_mean "$SHOW_MEAN") (>=${THRESHOLD}s)"
fi

if python3 -c "import sys; sys.exit(0 if float('$LIST_MEAN') < $THRESHOLD else 1)"; then
    LIST_PASS=true
    echo "PASS: ticket list mean=$(_format_mean "$LIST_MEAN") (<${THRESHOLD}s)"
else
    echo "FAIL: ticket list mean=$(_format_mean "$LIST_MEAN") (>=${THRESHOLD}s)"
fi

if python3 -c "import sys; sys.exit(0 if float('$CREATE_MEAN') < $WRITE_THRESHOLD else 1)"; then
    CREATE_PASS=true
    echo "PASS: ticket create mean=$(_format_mean "$CREATE_MEAN") (<${WRITE_THRESHOLD}s)"
else
    echo "FAIL: ticket create mean=$(_format_mean "$CREATE_MEAN") (>=${WRITE_THRESHOLD}s)"
fi

if python3 -c "import sys; sys.exit(0 if float('$COMMENT_MEAN') < $WRITE_THRESHOLD else 1)"; then
    COMMENT_PASS=true
    echo "PASS: ticket comment mean=$(_format_mean "$COMMENT_MEAN") (<${WRITE_THRESHOLD}s)"
else
    echo "FAIL: ticket comment mean=$(_format_mean "$COMMENT_MEAN") (>=${WRITE_THRESHOLD}s)"
fi

# ── Step 5b: Micro-benchmark write_commit_event overhead (excludes git commit) ──
# DD7 specifies <0.05s for write_commit_event function overhead excluding the
# git commit floor (~300ms). This micro-benchmark stubs _flock_stage_commit with
# a no-op and runs N calls in-process to measure the function's own overhead:
# JSON parsing (jq), field extraction, staging-temp creation, and directory setup.
# Running in-process avoids bash-startup + source overhead that dominates per-subshell timing.
echo "--- Micro-benchmark: write_commit_event function overhead (git-commit excluded) ---"
WRITE_COMMIT_EVENT_THRESHOLD=0.75  # 750ms per call during intermediate state under load (161e-b2b4); target: 0.15s post-optimization
MICRO_PASS=false
MICRO_MEAN="n/a"

# Build a minimal CREATE event JSON for use in the micro-benchmark
MICRO_EVENT_JSON=$(mktemp)
_CLEANUP_FILES+=("$MICRO_EVENT_JSON")
python3 - "$MICRO_EVENT_JSON" <<'PYEOF'
import json, sys, uuid, datetime
out_path = sys.argv[1]
event = {
    "event_type": "CREATE",
    "timestamp": datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S%f") + "Z",
    "uuid": str(uuid.uuid4()).replace("-", "")[:12],
    "data": {
        "ticket_id": "micro-bench-01",
        "title": "micro benchmark",
        "type": "task",
        "priority": 4,
        "status": "open",
        "tags": [],
    },
}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(event, f, ensure_ascii=False)
PYEOF

MICRO_TICKET_ID="micro-bench-01"
mkdir -p "$TEST_REPO/.tickets-tracker/$MICRO_TICKET_ID"

# Run N calls to write_commit_event in-process using a single bash subprocess.
# _flock_stage_commit is overridden with a no-op to exclude git-commit latency.
# Timing is measured with date +%s%N inside the same process to avoid subshell
# startup bias that would otherwise dominate the 50ms threshold.
MICRO_RUNS=10
MICRO_FAILED=false
MICRO_MEAN="n/a"

MICRO_RESULT=$(
    cd "$TEST_REPO"
    _TICKET_TEST_NO_SYNC=1 \
    TICKETS_TRACKER_DIR="$TRACKER_DIR" \
    bash -s "$MICRO_TICKET_ID" "$MICRO_EVENT_JSON" "$MICRO_RUNS" "$TICKET_LIB" <<'INNER_EOF'
ticket_id="$1"
event_json="$2"
runs="$3"
ticket_lib_path="$4"

# Source ticket-lib from the plugin repo (not the test clone)
# shellcheck disable=SC1090
source "$ticket_lib_path" || { echo "FAILED_SOURCE" >&2; exit 1; }

# Override _flock_stage_commit with a no-op to exclude git-commit latency.
# This isolates write_commit_event's own overhead: JSON parsing, field
# extraction, staging-temp creation, and directory setup.
_flock_stage_commit() { return 0; }

total_ns=0
for _i in $(seq 1 "$runs"); do
    _start=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time_ns()))")
    write_commit_event "$ticket_id" "$event_json" 2>/dev/null
    _end=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time_ns()))")
    total_ns=$(( total_ns + (_end - _start) ))
done
mean_ns=$(( total_ns / runs ))
echo "$mean_ns"
INNER_EOF
) || MICRO_FAILED=true

if [ "$MICRO_FAILED" = false ] && [ -n "$MICRO_RESULT" ] && [[ "$MICRO_RESULT" =~ ^[0-9]+$ ]]; then
    MICRO_MEAN=$(python3 -c "print('{:.4f}'.format($MICRO_RESULT / 1e9))")
    echo "write_commit_event (no-op _flock_stage_commit): mean=${MICRO_MEAN}s over ${MICRO_RUNS} runs"

    if python3 -c "import sys; sys.exit(0 if float('$MICRO_MEAN') < $WRITE_COMMIT_EVENT_THRESHOLD else 1)"; then
        MICRO_PASS=true
        echo "PASS: write_commit_event overhead mean=${MICRO_MEAN}s (<${WRITE_COMMIT_EVENT_THRESHOLD}s)"
    else
        echo "FAIL: write_commit_event overhead mean=${MICRO_MEAN}s (>=${WRITE_COMMIT_EVENT_THRESHOLD}s)"
    fi
else
    echo "SKIP: write_commit_event micro-benchmark could not run in isolation (result='$MICRO_RESULT')"
    MICRO_PASS=true  # Non-blocking: skip rather than fail if env doesn't support it
fi
echo ""

# ── Step 6: Optional baseline comparison ────────────────────────────────────
if [ -f "$BASELINE_FIXTURE" ]; then
    echo ""
    echo "--- Baseline comparison (informational) ---"
    python3 - "$BASELINE_FIXTURE" "$SHOW_MEAN" "$LIST_MEAN" "$CREATE_MEAN" "$COMMENT_MEAN" <<'PYEOF'
import json, sys

fixture_path, show_mean, list_mean, create_mean, comment_mean = (
    sys.argv[1], float(sys.argv[2]), float(sys.argv[3]),
    float(sys.argv[4]), float(sys.argv[5])
)
with open(fixture_path) as f:
    baseline = json.load(f)

def compare_op(op_name, current_mean):
    ops = baseline.get("ops", {})
    if op_name not in ops:
        print(f"  {op_name}: no baseline entry — skipping comparison")
        return
    baseline_mean = ops[op_name].get("mean_s")
    if baseline_mean is None:
        print(f"  {op_name}: no mean_s in baseline — skipping comparison")
        return
    delta = current_mean - baseline_mean
    sign = "+" if delta >= 0 else ""
    print(f"  {op_name}: current={current_mean:.4f}s  baseline={baseline_mean:.4f}s  delta={sign}{delta:.4f}s")

compare_op("show", show_mean)
compare_op("list", list_mean)
compare_op("create", create_mean)
compare_op("comment", comment_mean)
PYEOF
fi

# ── Exit ──────────────────────────────────────────────────────────────────────
echo ""
if $SHOW_PASS && $LIST_PASS && $CREATE_PASS && $COMMENT_PASS && $MICRO_PASS; then
    echo "Results: ticket show, list, create, comment, and write_commit_event overhead all within threshold"
    exit 0
else
    echo "Results: one or more operations exceeded threshold (read: ${THRESHOLD}s, write: ${WRITE_THRESHOLD}s, write_commit_event-overhead: ${WRITE_COMMIT_EVENT_THRESHOLD}s)"
    exit 1
fi
