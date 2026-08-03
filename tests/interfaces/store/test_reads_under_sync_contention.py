"""Generic read-integrity property under sync contention (ticket fa6e, ed2b family).

ed2b was fixed for `rebar show` (the slim-fetch-ledge: `_RECONVERGE_LOCK_TIMEOUT = 2`
in `rebar._engine_support.reads`) and pinned by `test_show_no_stall.py` — but every
read surface that routes through `ensure_fresh` (show/list/search/ready) shares that
reconverge path and would regress the same way. This module pins the property ONCE,
table-driven over the surfaces, instead of bespoke per-surface copies.

The property: a read must be COMPLETE-or-LOUD within bounded time. Exit 0 implies
stdout parses as the surface's JSON shape (valid-empty `[]` passes — distinguishable
from truncated-empty stdout); a nonzero exit passes (loud failure allowed); a stall
past the per-call deadline fails (that stall is exactly what consumers experienced
as truncated/empty pipes).

RED demonstration (AC): revert the ledge locally — set
`reads._RECONVERGE_LOCK_TIMEOUT` back to the 15s writer default — and
`test_reads_complete_promptly_while_write_lock_is_held` goes RED on every surface
(the held lock stalls each read past the deadline). Green on the fixed tree.
"""

from __future__ import annotations

import json
import subprocess
import threading

import pytest
from sync_contention_harness import _clear_sync_throttle, _rebar_cli

import rebar
from rebar._store import lock as _lock

# (surface, argv builder, JSON shape on a zero exit). `show` takes --output json and
# emits an object; `list`/`search` emit a JSON array by default (search has no
# --output flag at all); `ready` takes --output json and emits an array.
SURFACES = [
    ("show", lambda tid: ("show", tid, "--output", "json"), dict),
    ("list", lambda tid: ("list",), list),
    ("search", lambda tid: ("search", "burst"), list),
    ("ready", lambda tid: ("ready", "--output", "json"), list),
]

_READ_DEADLINE_SECS = 30


def _assert_complete_or_loud(name, expected_shape, cp, context):
    """The pinned invariant: zero exit ⇒ non-empty stdout parsing as the surface's
    documented JSON shape; nonzero exits are acceptable (loud beats silent)."""
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
        for name, argv, shape in SURFACES:
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
            _assert_complete_or_loud(name, shape, cp, f"round {round_no}")


def test_reads_complete_promptly_while_write_lock_is_held(repo_with_origin_tickets):
    """Deterministic contention: hold the tracker write lock (what a background push
    does during its commit window) and run every surface through the real CLI. With
    the ledge, each read waits <=2s for the lock then serves its local snapshot;
    reverting the ledge (15s writer default) stalls every surface past the deadline
    — the documented RED for this ticket."""
    repo, tracker, tid = repo_with_origin_tickets

    acquired = threading.Event()
    release = threading.Event()

    def _hold_lock():
        handle = _lock.acquire(str(tracker), timeout=30, attempts=1)
        acquired.set()
        release.wait(timeout=120)
        handle.release()

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    try:
        assert acquired.wait(timeout=10), "could not pre-acquire the lock"
        for name, argv, shape in SURFACES:
            _clear_sync_throttle(tracker)
            try:
                cp = _rebar_cli(*argv(tid), repo=repo, push="off", timeout=10)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    f"`rebar {name}` stalled >10s on the held write lock — "
                    "the read-path reconverge is not bounded (ed2b regression)"
                )
            _assert_complete_or_loud(name, shape, cp, "held write lock")
    finally:
        release.set()
        holder.join(timeout=120)
