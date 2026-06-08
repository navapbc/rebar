#!/usr/bin/env bash
# tests/perf/ticket-list-timing.sh
# Performance regression guard for ticket-list (exclude_archived=true) with
# the fast-skip optimization.
#
# What this tests:
#   Provisions a TICKETS_TRACKER_DIR with 500 synthetic tickets
#   (400 with .archived marker, 100 active), times 'ticket list'
#   (exclude_archived=true) 3 times WITH markers (fast path) and 3 times
#   WITHOUT markers (slow path), then asserts the fast path is at least
#   --threshold percent faster than the slow path.
#

# implemented in reduce_all_tickets(), and pass after.
#
# Usage:
#   bash tests/perf/ticket-list-timing.sh [--threshold=<percent>]
#
# Exit: 0 = PASS, non-zero = FAIL

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

TICKET_LIST_SH="$REPO_ROOT/plugins/dso/scripts/ticket-list.sh"

# ── Parse arguments ───────────────────────────────────────────────────────────
threshold_pct=20
for arg in "$@"; do
    case "$arg" in
        --threshold=*)
            threshold_pct="${arg#--threshold=}"
            ;;
        --help|-h)
            echo "Usage: $0 [--threshold=<percent>]"
            echo "  --threshold=N  Minimum wall-clock reduction percentage required (default: 20)"
            exit 0
            ;;
        *)
            echo "Error: unknown argument '$arg'" >&2
            exit 1
            ;;
    esac
done

# ── Validate dependencies ─────────────────────────────────────────────────────
if [ ! -x "$TICKET_LIST_SH" ]; then
    echo "FAIL: ticket-list.sh not found or not executable: $TICKET_LIST_SH" >&2
    exit 1
fi

# ── Provision synthetic tracker ───────────────────────────────────────────────
TDIR=$(mktemp -d)
trap 'rm -rf "$TDIR"' EXIT

echo "Provisioning 500 synthetic tickets (400 archived, 100 active)..."

python3 - "$TDIR" <<'PYEOF'
import json
import os
import sys

tracker_dir = sys.argv[1]
base_ts = 1_000_000_000

for i in range(500):
    ticket_id = f"perf-ticket-{i:04d}"
    ticket_dir = os.path.join(tracker_dir, ticket_id)
    os.makedirs(ticket_dir, exist_ok=True)

    # CREATE event
    create_event = {
        "timestamp": base_ts + i,
        "uuid": f"create-{i:04d}-aaaa-bbbb-cccc-ddddeeee{i:04d}",
        "event_type": "CREATE",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "perf-test",
        "data": {
            "ticket_type": "task",
            "title": f"Synthetic perf ticket {i}",
            "priority": 3,
        },
    }
    create_file = os.path.join(
        ticket_dir,
        f"{base_ts + i}-create-{i:04d}-CREATE.json",
    )
    with open(create_file, "w") as f:
        json.dump(create_event, f)

    if i < 400:
        # Archived: write 10 COMMENT events (making reduce_ticket expensive),
        # then STATUS closed + ARCHIVED event + .archived marker.
        # The extra COMMENT events ensure reduce_ticket() is meaningfully
        # more expensive than _is_net_archived() (which only scans for
        # ARCHIVED/REVERT events), making the fast-skip measurably faster.
        for j in range(10):
            comment_ts = base_ts + i + j
            comment_event = {
                "timestamp": comment_ts,
                "uuid": f"cmt-{i:04d}-{j:02d}-aaaa-bbbb-cccc-dddd{i:04d}",
                "event_type": "COMMENT",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "perf-test",
                "data": {"body": f"Comment {j} on ticket {i} — padding to make reduce_ticket non-trivial"},
            }
            comment_file = os.path.join(
                ticket_dir,
                f"{comment_ts}-cmt-{i:04d}-{j:02d}-COMMENT.json",
            )
            with open(comment_file, "w") as f:
                json.dump(comment_event, f)

        status_ts = base_ts + i + 10
        status_event = {
            "timestamp": status_ts,
            "uuid": f"status-{i:04d}-aaaa-bbbb-cccc-ddddeeee{i:04d}",
            "event_type": "STATUS",
            "env_id": "00000000-0000-4000-8000-000000000001",
            "author": "perf-test",
            "data": {"status": "closed"},
        }
        status_file = os.path.join(
            ticket_dir,
            f"{status_ts}-status-{i:04d}-STATUS.json",
        )
        with open(status_file, "w") as f:
            json.dump(status_event, f)

        archived_ts = base_ts + i + 11
        archived_event = {
            "timestamp": archived_ts,
            "uuid": f"arch-{i:04d}-aaaa-bbbb-cccc-ddddeeee{i:04d}",
            "event_type": "ARCHIVED",
            "env_id": "00000000-0000-4000-8000-000000000001",
            "author": "perf-test",
            "data": {},
        }
        archived_file = os.path.join(
            ticket_dir,
            f"{archived_ts}-arch-{i:04d}-ARCHIVED.json",
        )
        with open(archived_file, "w") as f:
            json.dump(archived_event, f)

        # Drop the .archived marker file (fast-skip optimization reads this)
        marker_file = os.path.join(ticket_dir, ".archived")
        open(marker_file, "w").close()

print(f"Done: created 500 ticket dirs in {tracker_dir}")
PYEOF

echo "Provisioning complete."

# ── time_run: clear caches then time one cold-cache ticket-list run ───────────
# Cache clearing ensures we measure cold-path work, not OS/process cache warmth.
# Without clearing, subsequent runs hit the in-process cache and appear much
# faster — masking the difference between fast-skip and slow-path.
time_run() {
    local start end
    # Clear per-ticket .cache.json files so every run is cold
    find "$TDIR" -name ".cache.json" -delete 2>/dev/null || true
    start=$(python3 -c "import time; print(int(time.monotonic() * 1000))")
    TICKETS_TRACKER_DIR="$TDIR" bash "$TICKET_LIST_SH" > /dev/null 2>&1
    end=$(python3 -c "import time; print(int(time.monotonic() * 1000))")
    echo $(( end - start ))
}

# ── Time 3 runs WITH markers (fast path) ─────────────────────────────────────
echo "Timing WITH .archived markers (fast path) — 3 cold-cache runs..."
f1=$(time_run)
f2=$(time_run)
f3=$(time_run)
echo "  Run 1: ${f1}ms"
echo "  Run 2: ${f2}ms"
echo "  Run 3: ${f3}ms"

optimized_ms=$(python3 - "$f1" "$f2" "$f3" <<'PYEOF'
import sys
vals = sorted(int(v) for v in sys.argv[1:])
print(vals[1])
PYEOF
)
echo "  Median (optimized): ${optimized_ms}ms"

# ── Remove .archived markers for baseline measurement ─────────────────────────
echo "Removing .archived markers for baseline (slow-path) measurement..."
find "$TDIR" -name ".archived" -delete

# ── Time 3 runs WITHOUT markers (slow path / baseline) ───────────────────────
echo "Timing WITHOUT .archived markers (slow path / baseline) — 3 cold-cache runs..."
b1=$(time_run)
b2=$(time_run)
b3=$(time_run)
echo "  Run 1: ${b1}ms"
echo "  Run 2: ${b2}ms"
echo "  Run 3: ${b3}ms"

baseline_ms=$(python3 - "$b1" "$b2" "$b3" <<'PYEOF'
import sys
vals = sorted(int(v) for v in sys.argv[1:])
print(vals[1])
PYEOF
)
echo "  Median (baseline):  ${baseline_ms}ms"

# ── Compute reduction percentage and assert ───────────────────────────────────
result=$(python3 - "$optimized_ms" "$baseline_ms" "$threshold_pct" <<'PYEOF'
import sys
optimized = int(sys.argv[1])
baseline  = int(sys.argv[2])
threshold = float(sys.argv[3])

if baseline == 0:
    print("ERROR: baseline_ms is 0 — cannot compute reduction percentage")
    sys.exit(2)

reduction_pct = (baseline - optimized) / baseline * 100
verdict = "PASS" if reduction_pct >= threshold else "FAIL"
print(f"{reduction_pct:.1f} {verdict}")
PYEOF
)

reduction_pct="${result%% *}"
verdict="${result##* }"

echo "  Reduction: ${reduction_pct}%  (threshold: ${threshold_pct}% reduction required)"

if [ "$verdict" = "PASS" ]; then
    echo "PASS: fast path ${reduction_pct}% faster than baseline (>= ${threshold_pct}% required)"
    exit 0
else
    echo "FAIL: fast path only ${reduction_pct}% faster than baseline (< ${threshold_pct}% required; fast-skip optimization may not be active)"
    exit 1
fi
