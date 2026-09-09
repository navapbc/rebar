"""Require every store read to be complete or loud under sync contention.

``show``, ``list``, ``search``, and ``ready`` share ``ensure_fresh``: a held write lock
must yield the local snapshot after the two-second reconverge ledge, not the 15-second
writer timeout. Exit-zero payloads must match each surface's JSON shape and carry ticket
identity; nonzero is an acceptable loud failure. An in-process deadline detects ledge
regressions without interpreter-startup noise, while a separate subprocess limit guards
only against a true hang.
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

# Separate product and liveness bounds. The in-process ceiling sits between the two-second
# read ledge and 15-second writer timeout, detecting a regression without startup noise.
# The subprocess limit stays below the 120-second hold and guards only against a true hang.
_PRODUCT_LOCK_DEADLINE_S = 2  # mirrors reads._RECONVERGE_LOCK_TIMEOUT
_WRITER_LOCK_TIMEOUT_S = 15  # mirrors sync._SYNC_LOCK_TIMEOUT (pre-ledge stall signature)
_DEADLINE_ORACLE_CEILING_S = 8  # in-process product-deadline oracle: 2 < 8 < 15
_HOLD_RELEASE_S = 120  # background holder releases the write lock after this
_LIVENESS_TIMEOUT_S = 45  # subprocess liveness bound: fast path ~2s, hold 120s


def test_the_held_lock_oracle_discriminates_blocking_from_ambient_slowness() -> None:
    """Separate the two-second ledge from the 15-second stall and bound the 120-second hold."""
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
    """Hold the write lock while every real CLI surface reads.

    The in-process assertion measures reconverge without startup slack; subprocess calls
    use an independent liveness bound and must return the local snapshot rather than hang.
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

        # Measure the <=2s product deadline in-process, excluding interpreter startup. A
        # revert to the 15s writer timeout must cross the 8s ceiling.
        _clear_sync_throttle(tracker)
        _t0 = time.monotonic()
        reads.ensure_fresh(str(tracker))
        deadline_elapsed = time.monotonic() - _t0
        # Hang guard: 8s comfortably exceeds the read ledge yet remains below the regression.
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
