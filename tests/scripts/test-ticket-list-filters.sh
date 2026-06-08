#!/usr/bin/env bash
# tests/scripts/test-ticket-list-filters.sh
# Behavioral tests for `ticket list` criteria filters: --priority and --without-tag,
# their comma-OR semantics, the has/without interaction, --help flag parity, and
# cross-implementation equivalence between the standalone script (ticket-list.sh)
# and the sourceable in-process path (ticket-lib-api.sh:ticket_list).
#
# `ticket list` must accept a uniform filter set so an agent can express a
# criteria-based request (e.g. "open P0 epics without the brainstorm:complete tag")
# in ONE command instead of pulling a broad list and filtering in-context. Both
# implementations must behave identically and both --help strings must advertise
# every flag (discoverability warm-path).
#
# Semantics under test:
#   --priority=<n[,n...]>     match tickets whose explicit priority == one of the
#                             values (comma = OR within the dimension; exact int
#                             match; tickets with no explicit priority never match).
#   --without-tag=<t[,t...]>  exclude a ticket if it has ANY of the listed tags.
#   --has-tag + --without-tag intersect the has-tag matches, then exclude.
#   Filters AND across dimensions.
#
# 16-hex-style canonical IDs are used so no git worktree / alias resolution is
# needed; tickets are built by hand as CREATE events.
#
# Usage: bash tests/scripts/test-ticket-list-filters.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
LIST_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-list.sh"
LIB_API="$REPO_ROOT/src/rebar/_engine/ticket-lib-api.sh"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-ticket-list-filters.sh ==="

# Source the in-process library once; ticket_list wraps its body in a subshell,
# so per-call env overrides (TICKETS_TRACKER_DIR) stay isolated.
# shellcheck source=/dev/null
source "$LIB_API"

# Distinct 16-hex IDs.
E0="aaaa-aaaa-aaaa-aaaa"   # epic   P0  tags=[rev]
E0B="bbbb-bbbb-bbbb-bbbb"  # epic   P0  tags=[rev, brainstorm:complete]
E1="cccc-cccc-cccc-cccc"   # epic   P1  tags=[]
E2="dddd-dddd-dddd-dddd"   # epic   P2  tags=[foo]
T0="eeee-eeee-eeee-eeee"   # task   P0  tags=[]

_CLEANUP_DIRS=()
cleanup() { for d in "${_CLEANUP_DIRS[@]:-}"; do [ -n "$d" ] && rm -rf "$d"; done; }
trap cleanup EXIT

# ── Build a tracker dir with CREATE events carrying priority + tags ──────────
_write_create() {
    local tracker="$1" id="$2" ttype="$3" priority="$4" tags_csv="$5"
    mkdir -p "$tracker/$id"
    python3 - "$tracker/$id" "$id" "$ttype" "$priority" "$tags_csv" <<'PY'
import json, sys
d, tid, ttype, priority, tags_csv = sys.argv[1:6]
tags = [t for t in tags_csv.split(',') if t]
data = {"ticket_type": ttype, "title": f"Ticket {tid}", "parent_id": "", "tags": tags}
if priority != "":
    data["priority"] = int(priority)
ev = {"event_type": "CREATE", "uuid": f"create-{tid}", "timestamp": 1000,
      "author": "Test", "env_id": "00000000-0000-4000-8000-000000000001",
      "data": data}
open(f"{d}/1000-create-{tid}-CREATE.json", "w").write(json.dumps(ev))
PY
}

_make_tracker() {
    local tracker
    tracker=$(mktemp -d "${TMPDIR:-/tmp}/list-filters.XXXXXX")
    _CLEANUP_DIRS+=("$tracker")
    _write_create "$tracker" "$E0"  epic 0 "rev"
    _write_create "$tracker" "$E0B" epic 0 "rev,brainstorm:complete"
    _write_create "$tracker" "$E1"  epic 1 ""
    _write_create "$tracker" "$E2"  epic 2 "foo"
    _write_create "$tracker" "$T0"  task 0 ""
    echo "$tracker"
}

# Run `ticket list` through a chosen implementation.
#   $1 = impl: "script" (ticket-list.sh) | "inproc" (lib-api ticket_list)
#   $2 = tracker dir; rest = args
_run_list() {
    local impl="$1" tracker="$2"; shift 2
    if [ "$impl" = "script" ]; then
        TICKETS_TRACKER_DIR="$tracker" bash "$LIST_SCRIPT" "$@"
    else
        TICKETS_TRACKER_DIR="$tracker" ticket_list "$@"
    fi
}

# Extract the set of ticket IDs from --format=llm output (one JSON obj per line).
# Fail loud (exit 2) on a malformed line or a missing 'id' key so a broken parser
# is never misattributed to broken filter logic — the cause is reported on stderr.
_ids_of() {
    python3 -c "
import sys, json
ids = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        sys.stderr.write('PARSE ERROR: non-JSON llm line %r: %s\n' % (line, exc))
        sys.exit(2)
    if 'id' not in obj:
        sys.stderr.write('PARSE ERROR: llm line missing expected \'id\' key: %r\n' % line)
        sys.exit(2)
    ids.append(obj['id'])
print(' '.join(sorted(ids)))
"
}

# Assert the llm-format result of a filter (against ONE impl) equals an expected ID set.
_assert_ids() {
    local label="$1" impl="$2" tracker="$3" expected="$4"; shift 4
    local got
    got=$(_run_list "$impl" "$tracker" --format=llm "$@" 2>/dev/null | _ids_of)
    assert_eq "$label [$impl]" "$expected" "$got"
}

# ── Tests run against BOTH implementations ───────────────────────────────────
for IMPL in script inproc; do
    TR=$(_make_tracker)

    # 1: --priority=0 matches every P0 ticket regardless of type.
    _assert_ids "priority=0 matches all P0" "$IMPL" "$TR" "$E0 $E0B $T0" --priority=0

    # 2: --priority=0,1 — comma = OR within the dimension.
    _assert_ids "priority=0,1 OR-matches P0 and P1" "$IMPL" "$TR" "$E0 $E0B $E1 $T0" --priority=0,1

    # 3: --without-tag excludes the tagged ticket only.
    _assert_ids "without-tag=brainstorm:complete excludes E0B" "$IMPL" "$TR" \
        "$E0 $E1 $E2 $T0" --without-tag=brainstorm:complete

    # 4: --without-tag=a,b excludes a ticket holding ANY listed tag.
    _assert_ids "without-tag=foo,brainstorm:complete excludes E0B and E2" "$IMPL" "$TR" \
        "$E0 $E1 $T0" --without-tag=foo,brainstorm:complete

    # 5: has-tag + without-tag = intersect then exclude.
    _assert_ids "has-tag=rev AND without-tag=brainstorm:complete -> E0 only" "$IMPL" "$TR" \
        "$E0" --has-tag=rev --without-tag=brainstorm:complete

    # 5b: --has-tag comma = OR within the dimension (single-value behavior unchanged).
    _assert_ids "has-tag=rev,foo OR-matches rev or foo" "$IMPL" "$TR" \
        "$E0 $E0B $E2" --has-tag=rev,foo

    # 6: the motivating exemplar, in one command.
    _assert_ids "exemplar: open P0 epics without brainstorm:complete -> E0" "$IMPL" "$TR" \
        "$E0" --type=epic --status=open --priority=0 --without-tag=brainstorm:complete

    # 7: default (no new flags) is unaffected — all five tickets returned.
    _assert_ids "default no-filter returns all five" "$IMPL" "$TR" \
        "$E0 $E0B $E1 $E2 $T0"

    # 8: --help advertises both new flags (discoverability parity).
    help_out=$(_run_list "$IMPL" "$TR" --help 2>&1)
    assert_contains "--help lists --priority [$IMPL]" "--priority" "$help_out"
    assert_contains "--help lists --without-tag [$IMPL]" "--without-tag" "$help_out"
    assert_contains "--help notes detected_by auto-intersect [$IMPL]" "detected_by" "$help_out"
done

# ── Test: --priority out-of-range yields a clear error, not a silent empty list ──
test_priority_out_of_range_errors() {
    local tracker; tracker=$(_make_tracker)
    local out rc
    out=$(_run_list script "$tracker" --priority=5 2>&1); rc=$?
    assert_eq "ticket-list.sh --priority=5 exits non-zero" "1" "$rc"
    assert_contains "ticket-list.sh --priority=5 reports out-of-range" "out of range" "$out"
    out=$(_run_list inproc "$tracker" --priority=5 2>&1); rc=$?
    assert_eq "lib-api --priority=5 exits non-zero" "1" "$rc"
    assert_contains "lib-api --priority=5 reports out-of-range" "out of range" "$out"
}
test_priority_out_of_range_errors

# ── Test: the priority field survives the reduce -> to_llm pipeline ───────────
# The --priority filter matches on the COMPILED ticket's priority. If
# reduce_all_tickets()/to_llm() dropped priority, every priority query would
# silently return empty. Assert positively that a P0 ticket's compiled llm
# output carries pr=0 — so a dropped-field regression fails HERE, distinct from
# the filter-logic assertions above.
test_priority_field_propagates() {
    local tracker; tracker=$(_make_tracker)
    local out
    out=$(_run_list script "$tracker" --type=epic --priority=0 --format=llm 2>/dev/null)
    assert_contains "compiled llm output preserves priority as pr=0" '"pr":0' "$out"
    out=$(_run_list inproc "$tracker" --type=epic --priority=0 --format=llm 2>/dev/null)
    assert_contains "in-process path also preserves pr=0" '"pr":0' "$out"
}
test_priority_field_propagates

# ── Test 9: cross-implementation equivalence on the exemplar ─────────────────
test_cross_impl_equivalence() {
    local tracker; tracker=$(_make_tracker)
    local a b
    a=$(_run_list script "$tracker" --format=llm --type=epic --status=open --priority=0 --without-tag=brainstorm:complete 2>/dev/null)
    b=$(_run_list inproc "$tracker" --format=llm --type=epic --status=open --priority=0 --without-tag=brainstorm:complete 2>/dev/null)
    assert_eq "ticket-list.sh and lib-api ticket_list agree on the exemplar" "$a" "$b"
}
test_cross_impl_equivalence

print_summary
