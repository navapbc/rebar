"""Held-out RED->GREEN oracle for paediatric-orchestral-anemone (ad7b-6928-49fe-475a).

Contract (cited: baldish-regainable-steed e4a6 merged `_cas_backoff` jitter, the Jira DC
transport jitter idiom `adapters/jira_datacenter/retry.py:182`, and this bug's AC): the
CAS-publication backoff in `last_pass.publish` must be JITTERED so two reconciler passes
that collide once do NOT wake in lockstep. Each delay must land in `[base, 1.25*base]`
(escalation preserved, always >= base), and two independent colliding passes must produce
DIFFERENT delay sequences. At zero contention (CAS wins first attempt) there is no sleep.

RED (current fixed `(0.1, 0.2)[attempt]` schedule): both passes sleep an identical
`[0.1, 0.2]`, so the two sequences are byte-identical -> lockstep. GREEN after jitter.
"""

from __future__ import annotations

import importlib
import random
import sys
from pathlib import Path

import pytest

_BASE_SCHEDULE = (0.1, 0.2)
_JITTER_CEILING = 1.25  # base * (1 + 0.25)


def _last_pass():
    engine_dir = Path(__file__).resolve().parents[2] / "src" / "rebar" / "_engine"
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
    return importlib.import_module("rebar_reconciler.last_pass")


class _CollidingRefLock:
    """A ref-lock whose CAS always loses, forcing `publish` to exhaust its retries."""

    def _hash_object(self, _repo_root, _raw):
        return "n" * 40

    def _ref_oid(self, _repo_root, _ref, remote=None):
        return "a" * 40

    def _cas_advance(self, _repo_root, _ref, *, new_oid, old_oid, remote):
        return False


class _WinningRefLock:
    """A ref-lock whose first CAS wins -> the zero-contention control (no backoff)."""

    def _hash_object(self, _repo_root, _raw):
        return "n" * 40

    def _ref_oid(self, _repo_root, _ref, remote=None):
        return "a" * 40

    def _cas_advance(self, _repo_root, _ref, *, new_oid, old_oid, remote):
        return True


def _drive_publish(monkeypatch, last_pass, ref_lock, label):
    """Run `publish` against `ref_lock`, returning the ordered backoff delays it slept."""
    delays: list[float] = []
    monkeypatch.setattr(last_pass, "_load_ref_lock", lambda: ref_lock)
    monkeypatch.setattr(last_pass, "_remote", lambda _repo_root: None)
    monkeypatch.setattr(
        last_pass, "resolve_environment_id", lambda _repo_root, explicit=None: "env"
    )
    monkeypatch.setattr(last_pass, "_write_detail", lambda *_a, **_k: None)
    try:
        last_pass.publish(
            Path("/nonexistent"),
            pass_id=label,
            outcome="success",
            sleep_fn=delays.append,
        )
    except last_pass.LastPassError:
        pass  # expected when the CAS always collides
    return delays


def test_colliding_publications_do_not_wake_in_lockstep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last_pass = _last_pass()
    random.seed(20260828)  # deterministic: the two passes draw distinct jitter values

    delays_a = _drive_publish(monkeypatch, last_pass, _CollidingRefLock(), "passA")
    delays_b = _drive_publish(monkeypatch, last_pass, _CollidingRefLock(), "passB")

    # Precondition: both passes exhausted the loop -> one sleep per non-final attempt.
    assert len(delays_a) == len(_BASE_SCHEDULE)
    assert len(delays_b) == len(_BASE_SCHEDULE)

    # Each delay is jittered within [base, 1.25*base]: >= base (escalation preserved) and
    # bounded below the next base so the schedule stays monotone.
    for delays in (delays_a, delays_b):
        for base, delay in zip(_BASE_SCHEDULE, delays, strict=True):
            assert base <= delay <= base * _JITTER_CEILING

    # The defect: an unjittered schedule makes the two colliding passes byte-identical, so
    # they re-collide in lockstep. Jitter must break that.
    assert delays_a != delays_b, (
        "colliding passes slept identical backoff durations (lockstep); "
        "the CAS-publish backoff is unjittered"
    )


def test_zero_contention_publication_takes_no_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last_pass = _last_pass()
    delays = _drive_publish(monkeypatch, last_pass, _WinningRefLock(), "solo")
    assert delays == [], "a first-attempt CAS win must not sleep any backoff"
