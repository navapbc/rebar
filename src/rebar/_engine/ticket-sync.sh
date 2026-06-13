#!/usr/bin/env bash
# ticket-sync.sh — cross-environment reconvergence shim (Tier D).
#
# The reconvergence implementation was ported to Python (rebar._store.sync) in
# Tier D; this file is now a thin shim that keeps the ONE bash caller working: the
# dispatcher's `_ensure_initialized` freshness path for write arms (create/comment/
# transition/…), which `source`s this file and calls `_reconverge_tickets`. Read
# arms reconverge in-process via rebar._engine_support.reads.ensure_fresh →
# rebar._store.sync.reconverge; this shim routes the bash write-arm path to the same
# single implementation. Retired with the dispatcher in Tier E.
#
# Best-effort: never fails the caller (a freshness fetch must not break a write).

_reconverge_tickets() {
    local tracker_dir="$1"
    [ -n "$tracker_dir" ] || return 0
    local _src
    _src="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    python3 -c "import sys; sys.path.insert(0, sys.argv[2]); from rebar._store import sync; sync.reconverge(sys.argv[1])" \
        "$tracker_dir" "$_src" >/dev/null 2>&1 || true
}
