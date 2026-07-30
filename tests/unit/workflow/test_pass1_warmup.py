"""Pass-1 warm-then-fan-out for the main chunk/agent pool (story 25fa-0865-8b61-498c).

The container path already warms the cache (one serial pairing, then fan-out); these
tests pin the same gate ported to the MAIN Pass-1 pool: when the plan-bearing shared
prefix is large enough to cache (>= ``CACHE_MIN_PREFIX_TOKENS``) and there are >= 2
Pass-1 calls, the first single-turn chunk runs serially to completion (writing the
cache prefix) before anything is submitted to the pool; small plans / single-call
reviews bypass the warm-up; a non-systemic warm failure degrades to direct fan-out
(the ladder drops that chunk's findings exactly as today, the remainder still fans
out, and the review completes); and in a stubbed-usage run the shared prefix
incurs ONE cache write and N-1 cache reads (observed via the per-call usage records
from the d52a instrumentation seam).

OFFLINE: a test-local ``RecordingRunner`` wraps ``FakeRunner``, records thread-safe
start/end call ordering, and simulates the provider cache — a call that STARTS after
some earlier call has COMPLETED reads the warmed prefix; otherwise it writes it.
"""

from __future__ import annotations

import threading

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMUnavailableError
from rebar.llm.plan_review import pass1, registry
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.runner import FakeRunner

_WRITE_USAGE = {
    "requests": 1,
    "input_tokens": 100,
    "output_tokens": 10,
    "cache_read_tokens": 0,
    "cache_write_tokens": 5000,
}
_READ_USAGE = {
    "requests": 1,
    "input_tokens": 100,
    "output_tokens": 10,
    "cache_read_tokens": 5000,
    "cache_write_tokens": 0,
}


class RecordingRunner:
    """Thread-safe recording runner: logs ``("start", n)`` / ``("end", n)`` events per
    call, simulates the provider prompt cache (a call that starts after ANY earlier
    call completed gets cache-READ usage; otherwise cache-WRITE — so a naive
    concurrent fan-out yields multiple writes, a warmed one exactly one), and can
    raise a scripted exception on the first call."""

    name = "recording"

    def __init__(self, fail_first: Exception | None = None) -> None:
        self.events: list[tuple[str, int]] = []
        self._lock = threading.Lock()
        self._n = 0
        self._completed = 0
        self._fail_first = fail_first

    def preflight(self) -> None:  # pragma: no cover - trivial
        pass

    def run(self, req):
        with self._lock:
            self._n += 1
            n = self._n
            warmed = self._completed > 0
            self.events.append(("start", n))
        try:
            if n == 1 and self._fail_first is not None:
                raise self._fail_first
            result = FakeRunner(structured={"analysis": "", "findings": []}).run(req)
            result["_usage"] = dict(_READ_USAGE if warmed else _WRITE_USAGE)
            return result
        finally:
            with self._lock:
                self._completed += 1
                self.events.append(("end", n))


def _cfg() -> LLMConfig:
    return LLMConfig(runner="fake")


# A description large enough that est_tokens(plan) >= CACHE_MIN_PREFIX_TOKENS (4096
# tokens ~= 16384 chars at 4 chars/token) — the cache-floor gate is MET.
_LARGE_DESCRIPTION = (
    "## Acceptance Criteria\n- [ ] the widget is observably correct\n\n## Context\n"
    + "plan detail. " * 2000
)
# Small: the same shape, far below the cache floor — the gate is NOT met.
_SMALL_DESCRIPTION = "## Acceptance Criteria\n- [ ] the widget is observably correct\n"


def _ctx(tmp_path, description: str) -> PlanContext:
    return PlanContext(
        ticket_id="25fa-0000-0000-0001",
        ticket_type="task",
        title="A task",
        description=description,
        repo_root=str(tmp_path),
    )


@pytest.fixture(autouse=True)
def _no_budget_shedding(monkeypatch):
    """Pin a high per-plan budget cap so the AGENT criteria these tests submit are
    never shed — shedding would change the call counts under assertion."""
    monkeypatch.setenv("REBAR_PLAN_REVIEW_BUDGET", "1000")


def _single() -> list[dict]:
    return [registry.by_id()["E2"], registry.by_id()["E5"]]  # packs into ONE chunk


# ── gate met: the warm call completes before any pool call starts ─────────────────
def test_warm_chunk_completes_before_pool_starts(tmp_path) -> None:
    runner = RecordingRunner()
    cov: dict = {}
    pass1.run_pass1(
        _ctx(tmp_path, _LARGE_DESCRIPTION),
        _cfg(),
        runner,
        _single(),
        [registry.by_id()["E4"]],
        cov,
    )
    # 1 chunk + 1 agent = 2 calls; the warm chunk (call 1) starts AND ends before any
    # other call starts.
    assert cov["batch_plan"]["warm"] is True
    assert cov["batch_plan"]["warmed"] is True
    assert len([e for e in runner.events if e[0] == "start"]) == 2
    assert runner.events[0] == ("start", 1)
    assert runner.events[1] == ("end", 1)


# ── bypasses: small plan; single call ─────────────────────────────────────────────
def test_small_plan_bypasses_warmup(tmp_path) -> None:
    runner = RecordingRunner()
    cov: dict = {}
    pass1.run_pass1(
        _ctx(tmp_path, _SMALL_DESCRIPTION),
        _cfg(),
        runner,
        _single(),
        [registry.by_id()["E4"]],
        cov,
    )
    assert cov["batch_plan"]["warm"] is False
    assert cov["batch_plan"]["warmed"] is False
    # Behavior unchanged: every call still runs (directly in the pool).
    assert len([e for e in runner.events if e[0] == "start"]) == 2


def test_single_call_review_bypasses_warmup(tmp_path) -> None:
    runner = RecordingRunner()
    cov: dict = {}
    pass1.run_pass1(_ctx(tmp_path, _LARGE_DESCRIPTION), _cfg(), runner, _single(), [], cov)
    # E2+E5 pack into ONE chunk and there are no agent criteria → a single Pass-1
    # call: nothing to amortize a warm over.
    assert cov["chunks"] == 1
    assert cov["batch_plan"]["warm"] is False
    assert cov["batch_plan"]["warmed"] is False
    assert len([e for e in runner.events if e[0] == "start"]) == 1


# ── warm failure: non-systemic degrades to direct fan-out; systemic aborts ────────
def test_warm_failure_degrades_to_direct_fanout_and_completes(tmp_path) -> None:
    runner = RecordingRunner(fail_first=RuntimeError("transient warm failure"))
    cov: dict = {}
    pass1.run_pass1(
        _ctx(tmp_path, _LARGE_DESCRIPTION),
        _cfg(),
        runner,
        _single(),
        [registry.by_id()["E4"]],
        cov,
    )
    assert cov["batch_plan"]["warm"] is True
    assert cov["batch_plan"]["warmed"] is False
    # Today's direct-fan-out behavior is preserved: the failed chunk's findings drop
    # (the ladder swallowed the non-systemic error; the chunk is not re-run), and the
    # remaining call still fans out — the review completes with its usage record.
    assert len([e for e in runner.events if e[0] == "start"]) == 2
    assert len(cov["usage"]["per_call"]) == 1
    assert cov["usage"]["per_call"][0]["criteria"] == ["E4"]


def test_systemic_warm_failure_aborts_fanout(tmp_path) -> None:
    runner = RecordingRunner(fail_first=LLMUnavailableError("tier down"))
    with pytest.raises(LLMUnavailableError):
        pass1.run_pass1(
            _ctx(tmp_path, _LARGE_DESCRIPTION),
            _cfg(),
            runner,
            _single(),
            [registry.by_id()["E4"]],
            {},
        )
    # Nothing was fanned out after the systemic warm failure.
    assert len([e for e in runner.events if e[0] == "start"]) == 1


# ── the payoff: one cache write, N-1 cache reads (per-call usage records) ─────────
def test_warmed_run_incurs_one_cache_write_then_reads(tmp_path) -> None:
    runner = RecordingRunner()
    cov: dict = {}
    pass1.run_pass1(
        _ctx(tmp_path, _LARGE_DESCRIPTION),
        _cfg(),
        runner,
        _single(),
        [registry.by_id()["E4"], registry.by_id()["A1"]],
        cov,
    )
    per_call = cov["usage"]["per_call"]
    assert len(per_call) == 3  # 1 chunk + 2 agent criteria
    writes = [c for c in per_call if c["cache_write_tokens"] > 0]
    reads = [c for c in per_call if c["cache_read_tokens"] > 0]
    assert len(writes) == 1  # exactly ONE call wrote the shared prefix…
    assert len(reads) == 2  # …and every other call read it
    assert cov["usage"]["totals"]["cache_write_tokens"] == 5000
    assert cov["usage"]["totals"]["cache_read_tokens"] == 10000
