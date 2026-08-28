"""Generic read-integrity property under sync contention (ticket fa6e, ed2b family).

ed2b was fixed for `rebar show` (the slim-fetch-ledge: `_RECONVERGE_LOCK_TIMEOUT = 2`
in `rebar._engine_support.reads`) and pinned by `test_show_no_stall.py` — but every
read surface that routes through `ensure_fresh` (show/list/search/ready) shares that
reconverge path and would regress the same way. This module pins the property ONCE,
table-driven over the surfaces, instead of bespoke per-surface copies.

The property: a read must be COMPLETE-or-LOUD within bounded time. Exit 0 implies
stdout parses as the surface's JSON shape AND the payload carries identity — `show`
returns an object with a truthy `ticket_id`, and every element a list-shaped surface
returns carries one (valid-empty `[]` passes — distinguishable from truncated-empty
stdout); a nonzero exit passes (loud failure allowed); a stall past the per-call
deadline fails (that stall is exactly what consumers experienced as truncated/empty
pipes).

The completeness half closes the gap proven by fault-seeding under afa0-2e15: seeds
F3 (`show` emits `{}`, exit 0) and F6 (`show` emits a record without `ticket_id`,
exit 0) passed the shape-only property while the original regression caught both. A
shape-valid but content-hollow payload is truncation wearing valid JSON.

RED demonstration (AC): revert the ledge locally — set
`reads._RECONVERGE_LOCK_TIMEOUT` back to the 15s writer default — and
`test_reads_complete_promptly_while_write_lock_is_held` goes RED via its in-process
product-deadline assertion (the held lock stalls the shared reconverge ~15s, past the
8s ceiling). Green on the fixed tree. That in-process assertion — not the subprocess
liveness bound — is the discriminating oracle (ticket nauseating-asphalt-quail).
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

import pytest
from sync_contention_harness import _clear_sync_throttle, _rebar_cli

import rebar
from rebar._engine_support import reads
from rebar._store import lock as _lock


def _record_has_identity(doc):
    """A complete ticket record carries a truthy ``ticket_id``."""
    return isinstance(doc, dict) and bool(doc.get("ticket_id"))


def _all_records_have_identity(doc):
    """Every element present carries identity; a valid-empty ``[]`` passes
    (vacuously true), staying distinguishable from truncated output."""
    return all(_record_has_identity(el) for el in doc)


# (surface, argv builder, JSON shape on a zero exit, content-completeness predicate).
# `show` takes --output json and emits an object; `list`/`search` emit a JSON array by
# default (search has no --output flag at all); `ready` takes --output json and emits
# an array. All four surfaces emit ticket-state records, so identity (`ticket_id`) is
# the per-surface completeness witness.
SURFACES = [
    ("show", lambda tid: ("show", tid, "--output", "json"), dict, _record_has_identity),
    ("list", lambda tid: ("list",), list, _all_records_have_identity),
    ("search", lambda tid: ("search", "burst"), list, _all_records_have_identity),
    ("ready", lambda tid: ("ready", "--output", "json"), list, _all_records_have_identity),
]

_READ_DEADLINE_SECS = 30

# ── Held-lock oracle: two SEPARATED, anchored bounds (ticket nauseating-asphalt-quail) ──
# The read-path reconverge waits at most `_RECONVERGE_LOCK_TIMEOUT` (=2s, the documented
# product deadline in rebar._engine_support.reads) for the write lock, then serves the local
# snapshot. Reverting that ledge to the 15s writer default (sync._SYNC_LOCK_TIMEOUT) is the
# ed2b regression this test must catch.
#
# A SINGLE subprocess wall-clock bound cannot do both jobs, because the genuine-stall
# signature here is only ~15s, not the 120s hold: subprocess time is interpreter-startup +
# `import rebar` + reconverge(<=2s) + read, and the startup term is unbounded on a constrained
# CI runner (audit puppylike-emo-rasbora observed 9.43s, ~0.6s under the old 10s per-call
# ceiling). A bound low enough to catch a 15s stall therefore also fires on ambient slowness.
# So the two concerns are split:
#
#   * the PRODUCT DEADLINE (<=2s) is asserted IN-PROCESS, where startup slack is absent and the
#     2s-vs-15s gap is a clean, machine-independent lock-timeout ceiling (measured 2.09s vs
#     15.09s, ~0.09s overhead). An 8s ceiling sits safely between the two — the same ceiling
#     test_show_no_stall.py uses for the identical in-process reconverge.
#   * the SUBPROCESS bound becomes a generous LIVENESS bound only — far under the 120s hold so a
#     genuinely hung read still fails, but high enough (~20x the ~2s fast path) that CI startup
#     slack across the per-surface loop never crosses it. Cumulative loop exposure is thus moot:
#     each call is bounded independently and the deadline is one in-process measurement.
_PRODUCT_LOCK_DEADLINE_S = 2  # mirrors reads._RECONVERGE_LOCK_TIMEOUT
_WRITER_LOCK_TIMEOUT_S = 15  # mirrors sync._SYNC_LOCK_TIMEOUT (pre-ledge stall signature)
_DEADLINE_ORACLE_CEILING_S = 8  # in-process product-deadline oracle: 2 < 8 < 15
_HOLD_RELEASE_S = 120  # background holder releases the write lock after this
_LIVENESS_TIMEOUT_S = 45  # subprocess liveness bound: fast path ~2s, hold 120s


def test_the_held_lock_oracle_discriminates_blocking_from_ambient_slowness() -> None:
    """The held-lock oracle must catch a real stall AND tolerate a slow machine.

    Two separated bounds, each with a load-bearing direction:
    - the in-process product deadline must sit strictly between the 2s ledge and the 15s
      writer-default stall, so a revert to 15s (the ed2b regression) is caught while the 2s
      fast path passes even on a slow box;
    - the subprocess liveness bound must stay under the 120s hold, so a read that genuinely
      hangs on the lock still fails, yet be generous enough that CI startup slack across the
      per-surface loop never crosses it. Both directions are load-bearing, so both are asserted.
    """
    assert _PRODUCT_LOCK_DEADLINE_S < _DEADLINE_ORACLE_CEILING_S < _WRITER_LOCK_TIMEOUT_S, (
        f"the {_DEADLINE_ORACLE_CEILING_S}s in-process deadline oracle must sit between the "
        f"{_PRODUCT_LOCK_DEADLINE_S}s ledge and the {_WRITER_LOCK_TIMEOUT_S}s writer-default "
        "stall, or it cannot both tolerate the fast path and catch the ed2b regression"
    )
    assert _LIVENESS_TIMEOUT_S < _HOLD_RELEASE_S, (
        f"liveness bound {_LIVENESS_TIMEOUT_S}s must stay under the {_HOLD_RELEASE_S}s hold, "
        "or a read that genuinely hangs on the held lock would pass"
    )


def _assert_complete_or_loud(name, expected_shape, complete, cp, context):
    """The pinned invariant: zero exit ⇒ non-empty stdout parsing as the surface's
    documented JSON shape AND content-complete (every record carries identity);
    nonzero exits are acceptable (loud beats silent)."""
    if cp.returncode != 0:
        return
    out = cp.stdout.strip()
    assert out != "", (
        f"`rebar {name}` returned EMPTY stdout with exit 0 ({context}); stderr={cp.stderr!r}"
    )
    try:
        doc = json.loads(out)
    except ValueError as exc:
        pytest.fail(
            f"`rebar {name}` exit 0 with unparseable stdout ({context}): {exc}; head={out[:200]!r}"
        )
    assert isinstance(doc, expected_shape), (
        f"`rebar {name}` exit 0 with wrong JSON shape ({context}): "
        f"expected {expected_shape.__name__}, got {type(doc).__name__}"
    )
    assert complete(doc), (
        f"`rebar {name}` exit 0 with a shape-valid but content-hollow payload ({context}): "
        f"a record is missing a truthy `ticket_id` — truncation wearing valid JSON "
        f"(afa0-2e15 seeds F3/F6); head={out[:200]!r}"
    )


def test_reads_complete_or_error_under_write_burst(repo_with_origin_tickets, monkeypatch):
    """The ed2b storm generalized: bursts of `rebar edit` (each spawning a background
    push under REBAR_SYNC_PUSH=always) interleaved with every read surface. The
    throttle marker is cleared before each read so every invocation actually
    exercises the reconverge path instead of short-circuiting."""
    repo, tracker, tid = repo_with_origin_tickets
    monkeypatch.delenv("REBAR_SYNC_PUSH", raising=False)  # let the CLI helper set =always

    ids = [tid] + [
        rebar.create_ticket("task", f"burst target {i}", repo_root=str(repo)) for i in range(3)
    ]

    for round_no in range(3):
        for i, t in enumerate(ids):
            _rebar_cli(
                "edit", t, "--description", f"round {round_no} edit {i}", repo=repo, push="always"
            )
        for name, argv, shape, complete in SURFACES:
            _clear_sync_throttle(tracker)
            try:
                cp = _rebar_cli(
                    *argv(ids[round_no % len(ids)]),
                    repo=repo,
                    push="always",
                    timeout=_READ_DEADLINE_SECS,
                )
            except subprocess.TimeoutExpired:
                pytest.fail(
                    f"`rebar {name}` stalled past {_READ_DEADLINE_SECS}s under the write "
                    f"burst (round {round_no}) — the ed2b symptom"
                )
            _assert_complete_or_loud(name, shape, complete, cp, f"round {round_no}")


def test_reads_complete_promptly_while_write_lock_is_held(repo_with_origin_tickets):
    """Deterministic contention: hold the tracker write lock (what a background push
    does during its commit window) and run every surface through the real CLI.

    Two SEPARATED oracles, so neither conflates the product deadline with machine slack
    (ticket nauseating-asphalt-quail):

    * PRODUCT DEADLINE — asserted in-process: the shared reconverge path (`ensure_fresh`,
      which every surface below routes through) must give up on the held lock within the
      <=2s ledge, not the 15s writer default. Measured in-process (no subprocess startup
      slack) so the 2s-vs-15s gap is machine-independent; reverting the ledge takes this
      RED. This is the discriminating oracle for the ed2b regression.
    * LIVENESS — asserted via the subprocess bound: each surface must return its local
      snapshot rather than hang on the lock. The bound is generous (far under the 120s
      hold) because its only job is to catch a true hang, not to time the 2s deadline.
    """
    repo, tracker, tid = repo_with_origin_tickets

    acquired = threading.Event()
    release = threading.Event()

    def _hold_lock():
        handle = _lock.acquire(str(tracker), timeout=30, attempts=1)
        acquired.set()
        release.wait(timeout=_HOLD_RELEASE_S)
        handle.release()

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    try:
        assert acquired.wait(timeout=10), "could not pre-acquire the lock"

        # Product deadline (<=2s), asserted IN-PROCESS so interpreter/import startup slack
        # cannot inflate it: the shared reconverge must abandon the held lock within the
        # ledge and serve local state. Reverting reads._RECONVERGE_LOCK_TIMEOUT to the 15s
        # writer default stalls this ~15s and takes the test RED (the documented ed2b RED).
        _clear_sync_throttle(tracker)
        _t0 = time.monotonic()
        reads.ensure_fresh(str(tracker))
        deadline_elapsed = time.monotonic() - _t0
        # timing: hang-guard — the <=2s ledge is a lock-timeout CEILING, not a workload
        # (measured 2.09s vs 15.09s at the 15s writer default, ~0.09s overhead), so the 8s
        # ceiling dwarfs the machine-independent fast path and cannot flake under contention.
        assert deadline_elapsed < _DEADLINE_ORACLE_CEILING_S, (
            f"read-path reconverge stalled {deadline_elapsed:.1f}s on the held write lock — the "
            f"<=2s ledge (reads._RECONVERGE_LOCK_TIMEOUT) is not in force (ed2b regression)"
        )

        for name, argv, shape, complete in SURFACES:
            _clear_sync_throttle(tracker)
            try:
                cp = _rebar_cli(*argv(tid), repo=repo, push="off", timeout=_LIVENESS_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    f"`rebar {name}` did not complete within {_LIVENESS_TIMEOUT_S}s while the "
                    "write lock was held — the read hung on the lock instead of serving its "
                    "local snapshot (ed2b regression)"
                )
            _assert_complete_or_loud(name, shape, complete, cp, "held write lock")
    finally:
        release.set()
        holder.join(timeout=_HOLD_RELEASE_S)
