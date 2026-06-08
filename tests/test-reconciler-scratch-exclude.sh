#!/usr/bin/env bash
# tests/scratch/test-reconciler-scratch-exclude.sh
#
# GREEN test — asserts that the Jira reconciler payload-builder (health.py
# iterdir walkers + __main__ --dry-run-enumerate) NEVER enumerates files
# under .tickets-tracker/.scratch/.
#
# Test matrix:
#   A. --dry-run-enumerate output excludes .scratch/ entry
#   B. --dry-run-enumerate output includes valid (non-scratch) ticket dir
#   C. health.count_open_by_type() excludes scratch subdirectory
#   D. health.capture_baseline() excludes scratch subdirectory
#
# Testing Mode: GREEN
# Usage: bash tests/scratch/test-reconciler-scratch-exclude.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
RECONCILER_DIR="$REPO_ROOT/src/rebar/_engine/dso_reconciler"
# Parent of dso_reconciler/ so `python3 -m dso_reconciler` resolves the package
RECONCILER_PARENT="$REPO_ROOT/src/rebar/_engine"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-reconciler-scratch-exclude.sh: reconciler excludes .scratch/ ==="

# ── Cleanup tracking ──────────────────────────────────────────────────────────
_CLEANUP_DIRS=()
_cleanup() {
    for d in "${_CLEANUP_DIRS[@]:-}"; do
        [ -n "$d" ] && rm -rf "$d"
    done
}
trap _cleanup EXIT

# ── Guard: reconciler must exist ─────────────────────────────────────────────
if [ ! -f "$RECONCILER_DIR/__main__.py" ]; then
    echo "FATAL: __main__.py not found at $RECONCILER_DIR/__main__.py" >&2
    exit 1
fi
if [ ! -f "$RECONCILER_DIR/health.py" ]; then
    echo "FATAL: health.py not found at $RECONCILER_DIR/health.py" >&2
    exit 1
fi

# ── Fixture factory ───────────────────────────────────────────────────────────
# Creates a minimal fake tickets-tracker under a temp root:
#   <root>/.tickets-tracker/aaaa-bbbb-cccc-dddd/<ts>-create.json  (real ticket)
#   <root>/.tickets-tracker/.scratch/aaaa-bbbb-cccc-dddd/plan.json (scratch data)
_make_fixture_root() {
    local tmpdir
    tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/test-reconciler-scratch.XXXXXX")
    _CLEANUP_DIRS+=("$tmpdir")

    local tracker="$tmpdir/.tickets-tracker"
    local valid_ticket_dir="$tracker/aaaa-bbbb-cccc-dddd"
    local scratch_dir="$tracker/.scratch/aaaa-bbbb-cccc-dddd"

    mkdir -p "$valid_ticket_dir"
    mkdir -p "$scratch_dir"

    # Write a minimal CREATE event so health.py counts this ticket as open
    cat > "$valid_ticket_dir/1000000000-create.json" <<'EOF'
{
  "event_type": "CREATE",
  "data": {
    "ticket_type": "task",
    "title": "Fixture ticket"
  }
}
EOF

    # Write a scratch data file that must NOT be read as a ticket event
    cat > "$scratch_dir/plan.json" <<'EOF'
{"scratch": true, "note": "agent planning data — not a ticket event"}
EOF

    echo "$tmpdir"
}

# ══════════════════════════════════════════════════════════════════════════════
# Test A: --dry-run-enumerate output does NOT include any .scratch/ path
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "── Test A: --dry-run-enumerate output excludes .scratch/ ──"

test_dry_run_enumerate_excludes_scratch() {
    local root
    root=$(_make_fixture_root)

    local output exit_code=0
    output=$(PYTHONPATH="$RECONCILER_PARENT" python3 -m dso_reconciler \
        --repo-root "$root" --dry-run-enumerate 2>/dev/null) || exit_code=$?

    assert_eq "--dry-run-enumerate exits 0" "0" "$exit_code"

    # .scratch must not appear anywhere in the output
    local scratch_lines
    scratch_lines=$(echo "$output" | grep -c "\.scratch" 2>/dev/null || echo "0")
    # Normalize BSD grep -c double-output quirk (BSD exits 1 on 0 matches)
    scratch_lines="${scratch_lines%%$'\n'*}"
    assert_eq ".scratch/ NOT in --dry-run-enumerate output" "0" "$scratch_lines"
}
test_dry_run_enumerate_excludes_scratch

# ══════════════════════════════════════════════════════════════════════════════
# Test B: --dry-run-enumerate output DOES include the valid ticket directory
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "── Test B: --dry-run-enumerate output includes valid ticket dir ──"

test_dry_run_enumerate_includes_valid_ticket() {
    local root
    root=$(_make_fixture_root)

    local output exit_code=0
    output=$(PYTHONPATH="$RECONCILER_PARENT" python3 -m dso_reconciler \
        --repo-root "$root" --dry-run-enumerate 2>/dev/null) || exit_code=$?

    assert_eq "--dry-run-enumerate exits 0 (test B)" "0" "$exit_code"

    local found_valid=0
    echo "$output" | grep -q "aaaa-bbbb-cccc-dddd" 2>/dev/null && found_valid=1 || true
    assert_eq "valid ticket dir present in --dry-run-enumerate output" "1" "$found_valid"
}
test_dry_run_enumerate_includes_valid_ticket

# ══════════════════════════════════════════════════════════════════════════════
# Test C: health.count_open_by_type() excludes .scratch/ from ticket count
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "── Test C: health.count_open_by_type() excludes .scratch/ ──"

test_count_open_excludes_scratch() {
    local root
    root=$(_make_fixture_root)

    local py_tmp
    py_tmp=$(mktemp "${TMPDIR:-/tmp}/test-reconciler-count.XXXXXX".py)
    cat > "$py_tmp" <<PYEOF
import sys, importlib.util, json
from pathlib import Path

repo_root_arg = Path(sys.argv[1])
reconciler_path = Path(sys.argv[2])

spec = importlib.util.spec_from_file_location("health", reconciler_path / "health.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

counts = mod.count_open_by_type(repo_root=repo_root_arg)
print(json.dumps(counts))
PYEOF

    local output exit_code=0
    output=$(python3 "$py_tmp" "$root" "$RECONCILER_DIR" 2>/dev/null) || exit_code=$?
    rm -f "$py_tmp"

    assert_eq "count_open_by_type exits 0" "0" "$exit_code"

    # The fixture has exactly 1 valid open task; scratch dir must not inflate count
    local task_count
    task_count=$(echo "$output" | python3 -c \
        "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('task', 0))" \
        2>/dev/null || echo "")
    assert_eq "count_open_by_type returns 1 task (valid ticket only)" "1" "$task_count"

    # .scratch must not appear as a ticket type key in the result dict
    local scratch_key_found=0
    echo "$output" | python3 -c \
        "import json,sys; d=json.loads(sys.stdin.read()); exit(1 if '.scratch' in d else 0)" \
        2>/dev/null && scratch_key_found=0 || scratch_key_found=1
    assert_eq ".scratch NOT a key in count_open_by_type result" "0" "$scratch_key_found"
}
test_count_open_excludes_scratch

# ══════════════════════════════════════════════════════════════════════════════
# Test D: health.capture_baseline() excludes .scratch/ from total count
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "── Test D: health.capture_baseline() excludes .scratch/ from total ──"

test_capture_baseline_excludes_scratch() {
    local root
    root=$(_make_fixture_root)

    local py_tmp
    py_tmp=$(mktemp "${TMPDIR:-/tmp}/test-reconciler-baseline.XXXXXX".py)
    cat > "$py_tmp" <<PYEOF
import sys, importlib.util, json
from pathlib import Path

repo_root_arg = Path(sys.argv[1])
reconciler_path = Path(sys.argv[2])

spec = importlib.util.spec_from_file_location("health", reconciler_path / "health.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

baseline_path = mod.capture_baseline("test-pass-001", repo_root=repo_root_arg)
record = json.loads(baseline_path.read_text())
print(json.dumps(record))
PYEOF

    local output exit_code=0
    output=$(python3 "$py_tmp" "$root" "$RECONCILER_DIR" 2>/dev/null) || exit_code=$?
    rm -f "$py_tmp"

    assert_eq "capture_baseline exits 0" "0" "$exit_code"

    # The baseline pre_pass_fsck_total should be exactly 1 (one valid ticket, scratch excluded)
    local pre_fsck
    pre_fsck=$(echo "$output" | python3 -c \
        "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('pre_pass_fsck_total', -1))" \
        2>/dev/null || echo "-1")
    assert_eq "capture_baseline pre_pass_fsck_total=1 (scratch excluded)" "1" "$pre_fsck"
}
test_capture_baseline_excludes_scratch

# ══════════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════════
echo ""
print_summary
