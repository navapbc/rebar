"""Bug baldish-regainable-steed (e4a6-545b-36af-456d): the CAS backoff must sleep BEFORE
re-fetching (so each retry is made against a fresher view, not a staler one) and must be
JITTERED (so writers that collide once do not wake in lockstep and re-collide).

Proven mechanism (Phase 1, runtime-confirmed): `_recover_non_fast_forward` sleeps AFTER the
merge and the outer loop re-pushes with no intervening fetch, so every retry pushes state
captured before the sleep; and `_CAS_BACKOFF_SECONDS` is a fixed unjittered schedule.

These oracles are held out from the fixer. They assert observable ordering/behaviour, never
private names, so they do not double as change-detectors.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from rebar import config
from rebar._store import compat, lock, push, push_classify

pytestmark = pytest.mark.unit

# The real "lost tickets race" rejection: a genuine CAS mismatch classified retriable.
_CANNOT_LOCK_REF_STDERR = (
    "! [remote rejected] HEAD -> tickets (cannot lock ref 'refs/heads/tickets': "
    "is at e45f61a9ef9f8a570e257079e51c9f39fa061240 but "
    "expected 9ecaaa40a28e992b060da61ef5969d425f94d1fe)"
)


def _completed(args: tuple[str, ...], rc: int = 0, out: str = "", err: str = ""):
    return subprocess.CompletedProcess(args, rc, out, err)


@contextmanager
def _open_lock(*_args: object, **_kwargs: object) -> Iterator[None]:
    yield


def _common(monkeypatch: pytest.MonkeyPatch, tracker: Path) -> None:
    tracker.mkdir(exist_ok=True)
    monkeypatch.setattr(push, "_push_mode", lambda _root=None: "always")
    monkeypatch.setattr(config, "tickets_branch", lambda _root=None: "tickets")
    monkeypatch.setattr(config, "tickets_remote", lambda _root=None: "origin")
    monkeypatch.setattr(lock, "write_lock", _open_lock)
    monkeypatch.setattr(lock, "check_no_rebase_in_progress", lambda _base: None)
    monkeypatch.setattr(
        compat, "store_epoch_merge_target", lambda _base, _ref: ("origin/tickets", None)
    )


def _racing_git(
    seq: list[str], push_results: list[int]
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """A fake git that records push/fetch/merge into ``seq`` in call order, driving a
    lost-CAS race whose push return codes come from ``push_results`` (1 = rejected)."""
    results = list(push_results)

    def fake(_base: str, *args: str, **_kwargs: object):
        if args[:2] == ("remote", "get-url"):
            return _completed(args, out="local-origin\n")
        if args and args[0] == "push":
            rc = results.pop(0) if results else 1
            seq.append(f"push(rc={rc})")
            return _completed(args, rc, err=_CANNOT_LOCK_REF_STDERR if rc else "")
        if args and args[0] == "fetch":
            seq.append("fetch")
            return _completed(args)
        if args and args[0] == "merge":
            seq.append("merge")
            return _completed(args)
        if args[:2] == ("rev-list", "--count"):
            return _completed(args, out="6\n")
        return _completed(args)

    return fake


def test_backoff_sleep_precedes_a_refetch_on_every_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: after the backoff sleep the retry RE-FETCHES before recomputing the merge.

    The observable contract is the interleaved order of git operations and backoff sleeps:
    every backoff SLEEP must be immediately followed by a ``fetch`` (a fresh read), and no
    SLEEP may sit between a ``merge`` and the ``push`` that follows it — otherwise the push
    is issued against state captured before the sleep (the proven staleness mechanism).
    """
    tracker = tmp_path / ".tickets-tracker"
    _common(monkeypatch, tracker)
    seq: list[str] = []
    monkeypatch.setattr(push, "_git", _racing_git(seq, [1, 1, 0]))

    def sleep_fn(delay: float) -> None:
        seq.append(f"SLEEP({delay})")

    assert push.push_tickets_branch(str(tracker), strict=True, sleep_fn=sleep_fn) is None

    # Anti-vacuity: the race actually collided (at least one CAS retry was taken).
    assert sum(1 for ev in seq if ev.startswith("SLEEP")) >= 1, seq
    # The recovery slept, and every sleep is a back-off-then-refetch, not merge-then-sleep.
    for i, ev in enumerate(seq):
        if ev.startswith("SLEEP"):
            following = seq[i + 1] if i + 1 < len(seq) else "<end>"
            assert following == "fetch", (
                f"after backoff at {i} the next git op is {following!r}, not a re-fetch; "
                f"the retry pushes stale state. sequence={seq}"
            )
    # And no backoff separates a merge from the push that consumes it.
    for i, ev in enumerate(seq):
        if ev == "merge":
            after = seq[i + 1] if i + 1 < len(seq) else "<end>"
            assert not after.startswith("SLEEP"), (
                f"a backoff sits between merge at {i} and the next push: {seq}"
            )


def test_zero_contention_is_unchanged_no_backoff_no_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control (AC): a write that lands first try neither backs off nor re-fetches."""
    tracker = tmp_path / ".tickets-tracker"
    _common(monkeypatch, tracker)
    seq: list[str] = []
    monkeypatch.setattr(push, "_git", _racing_git(seq, [0]))

    def sleep_fn(delay: float) -> None:
        seq.append(f"SLEEP({delay})")

    assert push.push_tickets_branch(str(tracker), strict=True, sleep_fn=sleep_fn) is None
    assert seq == ["push(rc=0)"], seq


def test_cas_backoff_is_jittered_not_lockstep() -> None:
    """AC2: repeated backoff at the SAME attempt yields varied (jittered) delays, never a
    single lockstep value, and each stays within an escalating, bounded window (>= base)."""
    samples = [_captured_delay(attempt=1) for _ in range(200)]
    base = push_classify._CAS_BACKOFF_SECONDS[0]
    # Teeth: an unjittered schedule returns the identical value every time.
    assert len(set(samples)) > 1, "backoff is not jittered — all samples identical (lockstep)"
    # Additive jitter: never shorter than the base delay, and bounded (not runaway).
    assert min(samples) >= base, (min(samples), base)
    assert max(samples) < base * 2.0, (max(samples), base)


def test_jitter_preserves_escalation_between_attempts() -> None:
    """The jittered windows for consecutive attempts stay ordered (escalating backoff)."""
    schedule = push_classify._CAS_BACKOFF_SECONDS
    for idx in range(len(schedule) - 1):
        lower = [_captured_delay(attempt=idx + 1) for _ in range(50)]
        upper = [_captured_delay(attempt=idx + 2) for _ in range(50)]
        assert max(lower) <= min(upper), (idx, max(lower), min(upper))


def test_best_effort_exhaustion_still_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4/B4: under sustained contention a best-effort (non-strict) write returns without
    raising even though delivery never landed — the delivery contract is unchanged."""
    tracker = tmp_path / ".tickets-tracker"
    _common(monkeypatch, tracker)
    seq: list[str] = []
    monkeypatch.setattr(push, "_git", _racing_git(seq, [1, 1, 1, 1, 1, 1]))
    # No raise, returns None; and collisions really occurred (anti-vacuity).
    assert push.push_tickets_branch(str(tracker), sleep_fn=lambda _d: None) is None
    assert sum(1 for ev in seq if ev == "fetch") >= 1, seq


def _captured_delay(*, attempt: int) -> float:
    captured: list[float] = []
    push_classify._cas_backoff(attempt, captured.append)
    assert len(captured) == 1
    return captured[0]
