"""Thundering-herd hardening of the Anthropic transport backoff (ticket 254e-1770-854b-47a2).

Held-out RED->GREEN oracle for the two operator-approved parts of the fix, asserted on the
OBSERVABLE computed wait value returned by the `_build_retry_wait` strategy (never a private
name):

  Vector A -- no `Retry-After`: the fallback must apply Equal Jitter `uniform(cap/2, cap)` so
  N co-throttled clients de-correlate, instead of pydantic-ai's default NON-jittered
  `wait_exponential` (which returns the identical value to every client -> lockstep).

  Vector B -- a zero / negative integer `Retry-After`: it must be treated as ABSENT and fall
  through to the jittered fallback, NOT honored verbatim as an immediate (0s) synchronized
  replay.

A positive in-window `Retry-After` is still honored (regression guard). The strategy's RNG is
injected here for determinism; production binds the process-global `random` module.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("tenacity")

from rebar.llm.anthropic_model import _build_retry_wait

pytestmark = pytest.mark.unit


class _Outcome:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def exception(self) -> BaseException:
        return self._exc


class _State:
    """The minimal duck-typed tenacity `RetryCallState` the wait strategy reads."""

    def __init__(self, exc: BaseException, attempt_number: int = 1) -> None:
        self.outcome = _Outcome(exc)
        self.attempt_number = attempt_number


def _http_status_error(retry_after: str | None) -> httpx.HTTPStatusError:
    headers = {} if retry_after is None else {"retry-after": retry_after}
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, headers=headers, request=request)
    return httpx.HTTPStatusError("429 rate limited", request=request, response=response)


def _cap(attempt_number: int, max_wait: float) -> float:
    return min(2.0 ** (attempt_number - 1), max_wait)


# ── Vector A: no Retry-After → Equal-Jitter fallback that de-correlates ────────
def test_no_retry_after_fallback_is_decorrelated_equal_jitter():
    """Two clients throttled at the SAME attempt with NO `Retry-After` must draw DIFFERENT
    sleeps (de-correlation), each within the Equal-Jitter window `[cap/2, cap]`."""
    max_wait, attempt = 60.0, 1
    cap = _cap(attempt, max_wait)  # == 1.0
    state = _State(_http_status_error(retry_after=None), attempt_number=attempt)

    wait_a = _build_retry_wait(max_wait=max_wait, rng=random.Random(1))
    wait_b = _build_retry_wait(max_wait=max_wait, rng=random.Random(2))
    va, vb = wait_a(state), wait_b(state)

    # De-correlated: independent RNG streams yield different sleeps.
    assert va != vb
    # Both land in the Equal-Jitter window [cap/2, cap].
    assert cap / 2 <= va <= cap
    assert cap / 2 <= vb <= cap
    # Contrast control: the NON-jittered exponential the fix replaces gives the IDENTICAL
    # value to every client — the lockstep this test exists to prevent.
    from tenacity.wait import wait_exponential

    nonjittered = wait_exponential(multiplier=1, max=60)
    assert nonjittered(state) == nonjittered(state)


def test_fallback_jitter_window_scales_with_attempt_and_is_capped():
    """The Equal-Jitter window tracks the exponential cap and is bounded by `max_wait`."""
    rng = random.Random(7)
    # Deep attempt, generous max_wait: cap == 2**4 == 16 → window [8, 16].
    wait = _build_retry_wait(max_wait=60.0, rng=rng)
    state = _State(_http_status_error(retry_after=None), attempt_number=5)
    for _ in range(50):
        v = wait(state)
        assert 8.0 <= v <= 16.0
    # A small max_wait clamps the window: cap == min(16, 5) == 5 → window [2.5, 5].
    wait_small = _build_retry_wait(max_wait=5.0, rng=random.Random(7))
    state_small = _State(_http_status_error(retry_after=None), attempt_number=5)
    for _ in range(50):
        v = wait_small(state_small)
        assert 2.5 <= v <= 5.0


def test_zero_max_wait_collapses_the_jittered_fallback_to_zero():
    """With ``max_wait <= 0`` the exponential cap collapses to 0, so the jittered fallback is
    exactly ``0.0`` (the zero-backoff config the re-pointed mechanics tests in
    ``test_transport_retry.py`` rely on to stay instant under the guard)."""
    wait = _build_retry_wait(max_wait=0.0, rng=random.Random(0))
    # No Retry-After → fallback path collapses to zero regardless of attempt.
    assert wait(_State(_http_status_error(retry_after=None), attempt_number=1)) == 0.0
    assert wait(_State(_http_status_error(retry_after=None), attempt_number=4)) == 0.0
    # A guarded zero/negative Retry-After also collapses to 0.0 (still not a raw replay path).
    assert wait(_State(_http_status_error(retry_after="0"), attempt_number=3)) == 0.0


# ── Vector B: zero / negative Retry-After is guarded, not an immediate replay ──
@pytest.mark.parametrize("retry_after", ["0", "-5"])
def test_nonpositive_retry_after_is_guarded_to_jittered_fallback(retry_after):
    """A `Retry-After: 0` (or negative) must NOT collapse the backoff to an immediate replay;
    it is treated as absent → jittered fallback, so the sleep is strictly positive."""
    max_wait, attempt = 60.0, 1
    cap = _cap(attempt, max_wait)
    wait = _build_retry_wait(max_wait=max_wait, rng=random.Random(3))
    state = _State(_http_status_error(retry_after=retry_after), attempt_number=attempt)
    v = wait(state)
    assert v > 0.0
    assert cap / 2 <= v <= cap


def test_expired_http_date_retry_after_falls_to_jittered_fallback():
    """An already-expired HTTP-date `Retry-After` also yields a positive jittered wait."""
    past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=120), usegmt=True)
    max_wait, attempt = 60.0, 1
    cap = _cap(attempt, max_wait)
    wait = _build_retry_wait(max_wait=max_wait, rng=random.Random(4))
    state = _State(_http_status_error(retry_after=past), attempt_number=attempt)
    v = wait(state)
    assert v > 0.0
    assert cap / 2 <= v <= cap


# ── Regression: a positive in-window Retry-After is still honored (and capped) ─
def test_positive_retry_after_is_honored_and_capped():
    wait = _build_retry_wait(max_wait=60.0, rng=random.Random(0))
    honored = _State(_http_status_error(retry_after="5"), attempt_number=1)
    assert wait(honored) == 5.0
    # Above max_wait → capped at max_wait (never the raw header value).
    capped = _State(_http_status_error(retry_after="999"), attempt_number=1)
    assert wait(capped) == 60.0
