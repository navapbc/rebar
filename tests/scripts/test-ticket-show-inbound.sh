#!/usr/bin/env bash
# tests/scripts/test-ticket-show-inbound.sh
# Behavioral tests for `ticket show` inbound-relationship augmentation.
#
# `ticket show` must surface the COMPLETE relationship picture for a ticket:
# not only the outgoing links stored in its own directory (deps) and its
# parent_id, but also the incoming relationships derived from other tickets:
#   - inbound_links: other tickets whose net-active LINK targets this ticket
#   - children:      tickets whose parent_id == this ticket
#
# These are derived read-only at show time (no events written), so the test
# builds a tracker directory by hand and invokes ticket-show.sh directly.
# 16-hex-style canonical IDs are used so resolve_ticket_id resolves via a
# plain directory check (no git worktree required).
#
# Usage: bash tests/scripts/test-ticket-show-inbound.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
SHOW_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket-show.sh"

source "$REPO_ROOT/tests/lib/assert.sh"

echo "=== test-ticket-show-inbound.sh ==="

ID_A="aaaa-aaaa-aaaa-aaaa"
ID_B="bbbb-bbbb-bbbb-bbbb"
ID_C="cccc-cccc-cccc-cccc"

_CLEANUP_DIRS=()
cleanup() { for d in "${_CLEANUP_DIRS[@]:-}"; do [ -n "$d" ] && rm -rf "$d"; done; }
trap cleanup EXIT

# ── Build a tracker dir with CREATE / LINK events by hand ────────────────────
_write_create() {
    local tracker="$1" id="$2" parent="$3" ttype="${4:-task}"
    mkdir -p "$tracker/$id"
    python3 - "$tracker/$id" "$id" "$parent" "$ttype" <<'PY'
import json, sys
d, tid, parent, ttype = sys.argv[1:5]
ev = {"event_type": "CREATE", "uuid": f"create-{tid}", "timestamp": 1000,
      "author": "Test", "env_id": "00000000-0000-4000-8000-000000000001",
      "data": {"ticket_type": ttype, "title": f"Ticket {tid}",
               "parent_id": parent or None}}
open(f"{d}/1000-create-{tid}-CREATE.json", "w").write(json.dumps(ev))
PY
}

_write_link() {
    local tracker="$1" src="$2" tgt="$3" rel="$4" ts="${5:-1500}"
    mkdir -p "$tracker/$src"
    python3 - "$tracker/$src" "$src" "$tgt" "$rel" "$ts" <<'PY'
import json, sys
d, src, tgt, rel, ts = sys.argv[1:6]
uuid = f"link-{src}-{rel}-{tgt}"
ev = {"event_type": "LINK", "uuid": uuid, "timestamp": int(ts),
      "author": "Test", "env_id": "00000000-0000-4000-8000-000000000001",
      "data": {"target_id": tgt, "relation": rel}}
open(f"{d}/{ts}-{uuid}-LINK.json", "w").write(json.dumps(ev))
PY
}

_make_tracker() {
    local tracker
    tracker=$(mktemp -d "${TMPDIR:-/tmp}/show-inbound.XXXXXX")
    _CLEANUP_DIRS+=("$tracker")
    echo "$tracker"
}

# ── Test 1: default format shows an inbound 'blocks' link ────────────────────
test_inbound_blocks_default() {
    local tracker; tracker=$(_make_tracker)
    _write_create "$tracker" "$ID_A" ""
    _write_create "$tracker" "$ID_B" ""
    _write_link "$tracker" "$ID_A" "$ID_B" "blocks"

    local out
    out=$(TICKETS_TRACKER_DIR="$tracker" bash "$SHOW_SCRIPT" "$ID_B" 2>/dev/null)

    assert_contains "default output has inbound_links key" '"inbound_links"' "$out"
    assert_contains "inbound_links names source A" "$ID_A" "$out"
    assert_contains "inbound_links carries relation blocks" '"blocks"' "$out"
}

# ── Test 2: default format lists children via parent_id ──────────────────────
test_children_default() {
    local tracker; tracker=$(_make_tracker)
    _write_create "$tracker" "$ID_A" "" "epic"
    _write_create "$tracker" "$ID_B" "$ID_A"
    _write_create "$tracker" "$ID_C" "$ID_A"

    local out
    out=$(TICKETS_TRACKER_DIR="$tracker" bash "$SHOW_SCRIPT" "$ID_A" 2>/dev/null)

    assert_contains "default output has children key" '"children"' "$out"
    assert_contains "children include B" "$ID_B" "$out"
    assert_contains "children include C" "$ID_C" "$out"
}

# ── Test 3: LLM format uses abbreviated inbound key (ibl / f / r) ────────────
test_inbound_llm_keys() {
    local tracker; tracker=$(_make_tracker)
    _write_create "$tracker" "$ID_A" ""
    _write_create "$tracker" "$ID_B" ""
    _write_link "$tracker" "$ID_A" "$ID_B" "blocks"

    local out
    out=$(TICKETS_TRACKER_DIR="$tracker" bash "$SHOW_SCRIPT" --output llm "$ID_B" 2>/dev/null)

    assert_contains "llm output uses ibl key" '"ibl"' "$out"
    assert_contains "llm inbound entry uses abbreviated from_id key f" '"f":"'"$ID_A"'"' "$out"
}

# ── Test 4: an isolated ticket has empty inbound, not a spurious source ──────
test_isolated_no_inbound() {
    local tracker; tracker=$(_make_tracker)
    _write_create "$tracker" "$ID_A" ""
    _write_create "$tracker" "$ID_B" ""  # unrelated

    local out
    out=$(TICKETS_TRACKER_DIR="$tracker" bash "$SHOW_SCRIPT" "$ID_A" 2>/dev/null)

    assert_contains "isolated ticket still emits inbound_links key" '"inbound_links"' "$out"
    assert_contains "isolated ticket still emits children key" '"children"' "$out"
    assert_not_contains "isolated A does not list B as inbound" "$ID_B" "$out"
}

# ── Test 5: inbound search keys off the canonical ID, not the alias supplied ──
# The stated goal is to scan for tickets referencing the *authoritative* ID,
# "not necessarily the ticket ID provided ... which may be an alias". Links are
# stored as the resolved canonical ID, so when `ticket show` is invoked with a
# non-canonical form (here an 8-hex short ID), the inbound scan must still
# resolve to the canonical ID first and find the link keyed on it. Without
# canonical resolution the substring needle would be the short form and miss
# every stored link (false-negative the whole feature).
test_inbound_resolved_via_short_id() {
    local tracker; tracker=$(_make_tracker)
    _write_create "$tracker" "$ID_A" ""
    _write_create "$tracker" "$ID_B" ""
    _write_link "$tracker" "$ID_A" "$ID_B" "blocks"

    # Show B by its 8-hex short ID (first group pair) rather than the full ID.
    local short_id="${ID_B:0:9}"  # "bbbb-bbbb"
    local out
    out=$(TICKETS_TRACKER_DIR="$tracker" bash "$SHOW_SCRIPT" "$short_id" 2>/dev/null)

    assert_contains "short-id show resolves to canonical ticket_id" '"'"$ID_B"'"' "$out"
    assert_contains "short-id show still surfaces inbound source A" "$ID_A" "$out"
    assert_contains "short-id show carries relation blocks" '"blocks"' "$out"
}

test_inbound_blocks_default
test_children_default
test_inbound_llm_keys
test_isolated_no_inbound
test_inbound_resolved_via_short_id

print_summary
