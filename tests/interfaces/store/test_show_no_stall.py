"""Require ``rebar show`` to return local state promptly under a held write lock.

Its throttled ``ensure_fresh`` reconverge uses a short read ledge rather than the
15-second writer timeout, preventing empty or incomplete consumer reads while a
background push owns the lock.
"""

from __future__ import annotations

import json
import threading
import time

from sync_contention_harness import _clear_sync_throttle, _rebar_cli

import rebar
from rebar._engine_support import reads
from rebar._store import lock as _lock


def test_ensure_fresh_does_not_stall_on_held_write_lock(repo_with_origin_tickets):
    _repo, tracker, tid = repo_with_origin_tickets
    _clear_sync_throttle(tracker)

    acquired = threading.Event()
    release = threading.Event()

    def _hold_lock():
        # Hold the write lock the whole time the read tries to reconverge — exactly
        # what a concurrent background push does during its commit window.
        handle = _lock.acquire(str(tracker), timeout=30, attempts=1)
        acquired.set()
        release.wait(timeout=30)
        handle.release()

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    try:
        assert acquired.wait(timeout=10), "could not pre-acquire the lock"
        t0 = time.monotonic()
        reads.ensure_fresh(str(tracker))  # the read-path freshness step
        elapsed = time.monotonic() - t0
        # Before the fix this blocked ~15s on the held lock; a read must not stall.
        # timing: hang-guard — stall detector; 8s dwarfs the ms-scale read, pre-fix hang was ~15s
        assert elapsed < 8.0, f"ensure_fresh stalled on the held lock: {elapsed:.1f}s"
    finally:
        release.set()
        holder.join(timeout=30)

    # And the record reads back complete (a read is always consistent locally).
    state = reads.show_state(tid, str(tracker))
    assert state["title"] == "no-stall target"


def test_cli_show_complete_or_erroring_under_write_burst(repo_with_origin_tickets, monkeypatch):
    """AC regression (slim-fetch-ledge): the burst-of-writes-then-`show` pattern via
    the real CLI under REBAR_SYNC_PUSH=always (background pushes contend) — every
    `rebar show` must be COMPLETE-or-ERRORING: never empty stdout with a zero exit.
    Exercises the consumer-facing path the bug broke (pipe `show` into a parser)."""
    repo, _tracker, tid = repo_with_origin_tickets
    monkeypatch.delenv("REBAR_SYNC_PUSH", raising=False)  # let the CLI helper set =always

    # A few more tickets so the burst is real.
    ids = [tid] + [
        rebar.create_ticket("task", f"burst target {i}", repo_root=str(repo)) for i in range(3)
    ]

    # One round is the measured detection floor. F3 and F6 fail on ticket zero, and the full
    # F1 through F7 matrix reproduces the six-round detection set without five redundant rounds.
    for round_no in range(1):
        # Burst of writes (each spawns a background push to origin under =always).
        for i, t in enumerate(ids):
            _rebar_cli(
                "edit", t, "--description", f"round {round_no} edit {i}", repo=repo, push="always"
            )
        # Immediately read each back through the CLI — the contention window.
        for t in ids:
            cp = _rebar_cli("show", t, repo=repo, push="always")
            empty_and_ok = cp.stdout.strip() == "" and cp.returncode == 0
            assert not empty_and_ok, (
                f"`rebar show {t}` returned EMPTY stdout with exit 0 "
                f"(round {round_no}); stderr={cp.stderr!r}"
            )
            if cp.returncode == 0:
                # A success exit must carry the complete record (parseable JSON).
                doc = json.loads(cp.stdout)
                assert doc.get("ticket_id"), f"incomplete show payload: {cp.stdout[:200]!r}"
