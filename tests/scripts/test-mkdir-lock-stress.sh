#!/usr/bin/env bash
# Stress the no-flock mkdir-lock fallback (ticket hip-rod-graze, risk R9).
#
# On platforms without util-linux flock (default macOS), _flock_stage_commit
# serializes the stage+commit critical section with an atomic mkdir lock whose
# starvation behaviour under many concurrent local agents was unmeasured. This
# forces that path (REBAR_FORCE_MKDIR_LOCK=1) and asserts that N concurrent
# writers (a) lose no events and (b) finish within a bounded wait (no starvation).
set -euo pipefail

N="${MKDIR_LOCK_STRESS_N:-15}"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
git init -q
git config user.email t@t.co
git config user.name t
export REBAR_ROOT="$tmp" PROJECT_ROOT="$tmp"
export REBAR_FORCE_MKDIR_LOCK=1   # force the macOS no-flock path
export REBAR_PUSH=off             # no remote; keep the test deterministic + fast
rebar init --silent >/dev/null 2>&1

fail=0
start=$(date +%s)
pids=()
for i in $(seq 1 "$N"); do
    ( rebar create task "stress $i" >/dev/null 2>&1 ) &
    pids+=($!)
done
for p in "${pids[@]}"; do wait "$p" || true; done
elapsed=$(( $(date +%s) - start ))

count=$(rebar list 2>/dev/null | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)
if [ "$count" -eq "$N" ]; then
    echo "PASS: all $N concurrent mkdir-lock writes landed (no lost events)"
else
    echo "FAIL: expected $N tickets, found $count — events lost under mkdir-lock contention"
    fail=1
fi

# Bounded wait: N serialized commits must finish well under a generous ceiling.
# (Starvation would show up as a writer never acquiring the lock -> a lost event
# above, or an unbounded blow-up here.)
ceiling=$(( N * 8 ))
if [ "$elapsed" -le "$ceiling" ]; then
    echo "PASS: completed in ${elapsed}s (<= ${ceiling}s bounded wait)"
else
    echo "FAIL: ${elapsed}s exceeds the ${ceiling}s bounded-wait ceiling (possible starvation)"
    fail=1
fi

exit "$fail"
