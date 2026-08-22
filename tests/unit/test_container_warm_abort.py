"""The container stage's warm-call abort-vs-degrade ruling (ticket shaggy-crass-bull).

``container_stage._run_container`` runs the container criteria (G3/G4) warm-then-fan-out:
one pairing runs serially first to write the shared cache prefix, then the rest fan out.
A SYSTEMIC failure on that warm call (``LLMUnavailableError``, or — bug 43d4 —
``LLMInputRejectedError``, which rejects identically for every remaining pairing) must
ABORT the whole stage rather than fan out N-1 doomed calls; any other warm failure must
DEGRADE to a direct fan-out of ALL pairings (never hang, never abort). The module had zero
referencing tests, so both halves of that ruling were unpinned — reverting the
``isinstance`` arm to its pre-43d4 form left the suite green.

These tests drive the REAL ``_run_container`` with ``passes.pass1_container`` (the
model-call boundary ``_timed_pairing`` invokes) monkeypatched, over a fixture sized so the
warm gate (``parent_tokens >= CACHE_MIN_PREFIX_TOKENS and len(pairings) >= 2``) is met.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMInputRejectedError, LLMUnavailableError
from rebar.llm.plan_review import container_stage, passes
from rebar.llm.plan_review.det_floor import PlanContext

pytestmark = pytest.mark.unit

# Sizing (all via the chars/4 estimate): budget = 50_000 * 0.9 - 32_000 = 13_000 tokens.
# Parent plan ~5_000 tokens (>= the 4_096 cache floor); each child ~7_000 tokens, so
# parent + one child = ~12_000 <= budget but parent + both = ~19_000 > budget ->
# pack_container_bins yields TWO single-child bins -> the warm gate is met.
_WINDOW_TOKENS = 50_000
_PLAN_DETAIL = "plan detail. " * 1540  # ~20_000 chars ≈ 5_000 tokens
_CHILD_DETAIL = "child detail. " * 2000  # ~28_000 chars ≈ 7_000 tokens


def _ctx() -> PlanContext:
    return PlanContext(
        ticket_id="P-1",
        ticket_type="epic",
        title="A container parent",
        description=_PLAN_DETAIL,
        children=[
            {"ticket_id": "C-1", "title": "child one", "description": _CHILD_DETAIL},
            {"ticket_id": "C-2", "title": "child two", "description": _CHILD_DETAIL},
        ],
        largest_window_tokens=_WINDOW_TOKENS,
    )


_CRITERIA = [{"id": "G3"}, {"id": "G4"}]


class _ScriptedPass1:
    """Replaces ``passes.pass1_container`` at the boundary ``_timed_pairing`` calls:
    raises the scripted exception on the FIRST (warm) call, succeeds afterwards."""

    def __init__(self, fail_first: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self._fail_first = fail_first

    def __call__(self, runner: Any, cfg: Any, *, parent_plan, children, criteria, sibling_roster):
        self.calls.append([c.get("ticket_id") for c in children])
        if len(self.calls) == 1 and self._fail_first is not None:
            raise self._fail_first
        return [], {"requests": 1, "input_tokens": 10, "output_tokens": 1}


def _run(monkeypatch, scripted: _ScriptedPass1) -> tuple[list, dict]:
    monkeypatch.setattr(passes, "pass1_container", scripted)
    coverage: dict[str, Any] = {}
    findings, _call_records = container_stage._run_container(
        _ctx(), LLMConfig(runner="fake"), object(), _CRITERIA, coverage
    )
    return findings, coverage


def test_fixture_reaches_the_warm_path(monkeypatch) -> None:
    """Sanity pin for the fixture itself: two in-budget single-child bins, warm gate met —
    the warm call completes first, then the remaining pairing fans out (2 calls total)."""
    scripted = _ScriptedPass1()
    _, coverage = _run(monkeypatch, scripted)
    cov = coverage["container"]
    assert cov["bins"] == 2
    assert cov["warmed"] is True
    assert scripted.calls == [["C-1"], ["C-2"]]  # warm serially first, then the pool


@pytest.mark.parametrize(
    "exc",
    [LLMUnavailableError("provider down"), LLMInputRejectedError("prompt rejected as too large")],
    ids=["unavailable", "input-rejected"],
)
def test_warm_call_systemic_failure_aborts_the_fanout(monkeypatch, exc) -> None:
    """A SYSTEMIC failure on the warming call aborts the stage: the exception re-raises
    (run_review turns it into an INDETERMINATE, unsigned verdict) and NO further pairing is
    dispatched — never fan out N-1 doomed calls. Narrowing the ``isinstance`` arm back to
    ``LLMUnavailableError`` only (the pre-43d4 form) makes the input-rejected case degrade
    to a fan-out instead, turning that parametrization RED."""
    scripted = _ScriptedPass1(fail_first=exc)
    monkeypatch.setattr(passes, "pass1_container", scripted)
    with pytest.raises(type(exc)):
        container_stage._run_container(_ctx(), LLMConfig(runner="fake"), object(), _CRITERIA, {})
    assert scripted.calls == [["C-1"]]  # exactly the warm call — the fan-out never ran


def test_warm_call_nonsystemic_failure_degrades_to_direct_fanout(monkeypatch) -> None:
    """The negative control: a NON-systemic warm failure must NOT abort — the stage
    degrades to a direct fan-out of ALL pairings (the failed pairing re-runs in the pool,
    so nothing is silently dropped) and reports ``warmed=False``."""
    scripted = _ScriptedPass1(fail_first=ValueError("a transient, pairing-local fault"))
    _, coverage = _run(monkeypatch, scripted)
    cov = coverage["container"]
    assert cov["warmed"] is False
    # 3 calls: the failed warm attempt, then BOTH pairings re-fanned directly.
    assert len(scripted.calls) == 3
    assert scripted.calls[0] == ["C-1"]
    assert sorted(tuple(c) for c in scripted.calls[1:]) == [("C-1",), ("C-2",)]
    assert cov["pairings_evaluated"] == 2  # both pairings completed despite the warm failure
