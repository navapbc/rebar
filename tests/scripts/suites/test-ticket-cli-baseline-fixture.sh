#!/usr/bin/env bash
# test-ticket-cli-baseline-fixture.sh
#
# Verifies that tests/fixtures/ticket-cli-baseline.json exists and contains:
#   - All 9 expected ops: show, list, comment, create, tag, untag, edit, transition, link
#   - Per-op fields: mean_s, p50_s, p95_s, stddev_s
#   - Top-level metadata: codebase_ref (non-empty), captured_at (non-empty)
#
# RED before the fixture exists; GREEN once capture-baseline.sh has been run
# and the result committed.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
FIXTURE="$REPO_ROOT/tests/fixtures/ticket-cli-baseline.json"

PASS=0
FAIL=0

pass() { echo "  PASS: $*"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $*"; FAIL=$((FAIL + 1)); }

echo "=== test-ticket-cli-baseline-fixture ==="
echo

# ----- 1. Fixture file exists -----------------------------------------------
if [[ -f "$FIXTURE" ]]; then
  pass "fixture file exists at tests/fixtures/ticket-cli-baseline.json"
else
  fail "fixture file missing: tests/fixtures/ticket-cli-baseline.json (run tests/scripts/capture-baseline.sh to generate it)"
  echo
  echo "TOTAL: $PASS passed, $FAIL failed"
  exit 1
fi

# ----- 2. Fixture is valid JSON ---------------------------------------------
if python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$FIXTURE" 2>/dev/null; then
  pass "fixture is valid JSON"
else
  fail "fixture is NOT valid JSON"
  echo
  echo "TOTAL: $PASS passed, $FAIL failed"
  exit 1
fi

# ----- 3. Top-level metadata fields -----------------------------------------
CODEBASE_REF="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('codebase_ref',''))" "$FIXTURE")"
CAPTURED_AT="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('captured_at',''))" "$FIXTURE")"

if [[ -n "$CODEBASE_REF" ]]; then
  pass "codebase_ref is non-empty: $CODEBASE_REF"
else
  fail "codebase_ref is empty or missing"
fi

if [[ -n "$CAPTURED_AT" ]]; then
  pass "captured_at is non-empty: $CAPTURED_AT"
else
  fail "captured_at is empty or missing"
fi

# ----- 4. All 9 ops present with required fields ----------------------------
EXPECTED_OPS="show list comment create tag untag edit transition link"

for op in $EXPECTED_OPS; do
  result="$(python3 - "$FIXTURE" "$op" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
op   = sys.argv[2]
ops  = data.get("ops", {})
if op not in ops:
    print(f"MISSING_OP:{op}")
    sys.exit(0)
m = ops[op]
missing = [f for f in ("mean_s", "p50_s", "p95_s", "stddev_s") if f not in m]
if missing:
    print(f"MISSING_FIELDS:{op}:{','.join(missing)}")
else:
    vals = {f: m[f] for f in ("mean_s", "p50_s", "p95_s", "stddev_s")}
    print(f"OK:{op}:{vals}")
PYEOF
)"
  case "$result" in
    MISSING_OP:*)
      fail "op '$op' missing from fixture"
      ;;
    MISSING_FIELDS:*)
      missing_info="${result#MISSING_FIELDS:}"
      fail "op '$op' missing fields: $missing_info"
      ;;
    OK:*)
      detail="${result#OK:"$op":}"
      pass "op '$op' has mean_s/p50_s/p95_s/stddev_s — $detail"
      ;;
    *)
      fail "unexpected result parsing op '$op': $result"
      ;;
  esac
done

# ----- Summary ---------------------------------------------------------------
echo
echo "TOTAL: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
