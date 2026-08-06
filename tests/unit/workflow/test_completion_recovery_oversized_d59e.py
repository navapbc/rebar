"""A large, legitimate ticket reaches a verdict on its FULL context (bug d59e).

Bounded completion recovery exists to survive an exhausted aggregate verifier (ticket 9a08,
whose Impact calls the failure mode "a fail-closed close-gate availability defect: correct,
merged work cannot transition to `closed`"). Before this change it did the opposite for a
ticket that was merely large: ``_validate_recovery_inputs`` refused any context over 24,000
chars, and the remedy it printed ("shorten the ticket's description/comments") could not be
carried out — measured on the real ticket 9fd4-a94c-156e-4a56 (34,282 chars, reproducing its
live failure byte-for-byte): description 10,281 + 9 comments 23,617, so deleting the ENTIRE
description still leaves 24,001 chars, and comments cannot be deleted at all because the store
is append-only (``_lib_writes`` exposes ``comment()`` with no delete/redact counterpart).

**Why the budget is raised rather than made elastic.** An earlier attempt compacted an
over-budget context by dropping comment history oldest-first. That was WITHDRAWN as a
signed-false-PASS vector: on an epic the gate assembles one block PER TICKET
(``operations.assemble_context(graph=True)``), each with its own ``#### Comments`` heading, so
dropping "comment history" silently deleted whole CHILD tickets — including their unmet
acceptance criteria — while reporting only that comments were removed. Elision is dangerous in
both directions (dropping evidence a criterion IS met causes a false FAIL; dropping evidence it
is NOT causes a false PASS), and this gate SIGNS its verdict. So: nothing is ever elided, the
budget is large enough for real tickets, and anything above it is REFUSED visibly.

What this file pins:

* a realistic large ticket is verified on its full context, byte-identical — not "within
  budget", not summarized, not truncated;
* the criteria budget can never exceed the context budget (criteria ⊂ description ⊂ context),
  the incoherence that made the top of the criteria budget unreachable;
* an over-budget payload still fails fast with ZERO billable recovery calls, so raising the
  budget did not delete the cost guard.
"""

from __future__ import annotations

import json

import pytest

from rebar.llm.config import VERIFIER_DEFAULT_MODEL, LLMConfig
from rebar.llm.errors import CompletionRecoveryError, UnretryableOutputError
from rebar.llm.workflow import completion_recovery as _cr
from rebar.llm.workflow.completion_recovery import CompletionAgentStep
from rebar.llm.workflow.executor import StepContext

pytestmark = pytest.mark.unit

# The two real tickets this defect blocked, used as fixture sizes so the test tracks the
# motivating cases rather than an invented number.
_NINE_FD4_CHARS = 34_282
_TWO_NINE_THREE_TWO_CHARS = 41_595

# The flat `_MAX_CONTEXT_CHARS` constant is retired (bug 8eb3): the physical context ceiling
# is now derived from the resolved verifier model's own window. These tests consume the same
# accessor the code does, evaluated for the default verifier model, so they track the
# window-derived bound instead of a hard-coded number.
_DEFAULT_CONTEXT_CEILING = _cr.physical_context_ceiling(VERIFIER_DEFAULT_MODEL)


class _RecoverableRunner:
    """Primary call truncates (the only door into recovery); recovery then succeeds."""

    name = "recoverable"

    def __init__(self) -> None:
        self.requests: list = []

    def preflight(self) -> None:
        return None

    def run(self, req):
        self.requests.append(req)
        if len(self.requests) == 1:
            raise UnretryableOutputError("finish_reason=length")
        if req.execution_mode == "agentic":
            return {"text": "Observed implementation evidence at src/example.py:10."}
        payload = json.loads(req.instructions)
        criteria = [
            {
                "criterion": criterion,
                "met": True,
                "citation": {"kind": "source", "description": "src/example.py:10"},
                "kind": "codebase-verifiable",
            }
            for criterion in payload["expected_criteria"]
        ]
        return {"verdict": "PASS", "findings": [], "criteria": criteria}


def _ticket() -> dict:
    criteria = "\n".join(f"- [ ] criterion {index}" for index in range(1, 7))
    return {
        "ticket_id": "T-1",
        "title": "bounded completion",
        "ticket_type": "task",
        "description": f"## Acceptance Criteria\n{criteria}",
    }


def _ctx(context: str) -> StepContext:
    return StepContext(
        run_id="run-1",
        step_id="verify",
        kind="agent",
        step={
            "id": "verify",
            "prompt": "completion-verifier",
            "mode": "structured",
            "output_schema": "completion_verdict",
        },
        inputs={"ticket_id": "T-1", "context": context},
        workflow={"name": "completion-verification"},
        target_ticket="T-1",
        repo_root=None,
    )


def _realistic_context(total: int) -> str:
    """A context shaped like a real ticket: a description plus many evidence comments.

    Deliberately NOT ``"x" * N`` — a degenerate blob is the hostile-input case covered
    separately, and conflating the two is what let a guard written for hostile input refuse
    legitimate work.
    """
    head = "Ticket: T-1\nDescription:\n## Acceptance Criteria\n" + "\n".join(
        f"- [ ] criterion {i}" for i in range(1, 7)
    )
    body: list[str] = []
    i = 0
    while len(head) + sum(map(len, body)) < total:
        i += 1
        body.append(
            f"\n\nComment {i}: evidence recorded per the verifier's own remediation "
            f"guidance — see src/example.py:{i} for criterion {i % 6 + 1}. " + "detail " * 20
        )
    return (head + "".join(body))[:total]


@pytest.mark.parametrize(
    ("label", "size"),
    [("9fd4", _NINE_FD4_CHARS), ("2932", _TWO_NINE_THREE_TWO_CHARS)],
)
def test_a_real_blocked_ticket_now_reaches_a_verdict(monkeypatch, label: str, size: int) -> None:
    """THE BUG: both tickets this defect blocked must now verify rather than be refused."""
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *a, **k: _ticket())
    context = _realistic_context(size)
    runner = _RecoverableRunner()
    step = CompletionAgentStep(runner=runner, repo_root=None, config=LLMConfig(runner="fake"))

    result = step.run(_ctx(context)).outputs

    assert result.get("verdict") in {"PASS", "FAIL"}, (
        f"ticket {label} ({size:,} chars) must reach a real verdict rather than a "
        f"fail-closed refusal. Got: {result!r}"
    )


def test_the_full_context_reaches_the_model_with_nothing_elided(monkeypatch) -> None:
    """The anti-elision guarantee, asserted byte-for-byte.

    "Within budget" is not good enough: a summarized or truncated context is exactly the
    signed-false-PASS vector this design withdrew. Every per-criterion call must carry the
    assembled context verbatim.
    """
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *a, **k: _ticket())
    context = _realistic_context(_NINE_FD4_CHARS)
    runner = _RecoverableRunner()
    step = CompletionAgentStep(runner=runner, repo_root=None, config=LLMConfig(runner="fake"))

    step.run(_ctx(context))

    evidence_calls = [r for r in runner.requests[1:] if r.execution_mode == "agentic"]
    assert evidence_calls, "recovery must have made per-criterion evidence calls"
    for index, req in enumerate(evidence_calls):
        assert context in req.instructions, (
            f"evidence call {index} did not carry the context VERBATIM — something elided or "
            "rewrote it, which is the false-PASS vector this design exists to avoid"
        )


def test_the_criteria_budget_never_exceeds_the_context_budget() -> None:
    """Criteria ⊂ description ⊂ context, so a criteria budget larger than the context budget
    advertises capacity that cannot be used — the 32,000-vs-24,000 incoherence that made the
    top quarter of the criteria budget structurally unreachable."""
    assert _cr._MAX_TOTAL_CRITERIA_CHARS <= _DEFAULT_CONTEXT_CEILING, (
        f"criteria budget ({_cr._MAX_TOTAL_CRITERIA_CHARS:,}) exceeds the context budget "
        f"({_DEFAULT_CONTEXT_CEILING:,}); every criteria set above the context budget would be "
        "accepted by one bound and refused by the other"
    )


def test_the_budget_covers_the_tickets_this_defect_blocked() -> None:
    """Regression floor: the budget must not drift back below the sizes that motivated it."""
    for label, size in (("9fd4", _NINE_FD4_CHARS), ("2932", _TWO_NINE_THREE_TWO_CHARS)):
        assert size <= _DEFAULT_CONTEXT_CEILING, (
            f"ticket {label} ({size:,} chars) would be refused again by a context budget of "
            f"{_DEFAULT_CONTEXT_CEILING:,}"
        )


def test_an_over_budget_payload_still_fails_before_any_billable_call(monkeypatch) -> None:
    """NEGATIVE CONTROL — raising the budget must not delete the cost guard.

    Recovery re-sends the context once per criterion, so an unbounded context is a real
    multiplier. A payload above the budget must still fail fast, buying only the single
    already-spent primary request.
    """
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *a, **k: _ticket())
    runner = _RecoverableRunner()
    cfg = LLMConfig(runner="fake")
    step = CompletionAgentStep(runner=runner, repo_root=None, config=cfg)

    # The step derives its physical ceiling from its own resolved model, so breach THAT
    # ceiling (not a hard-coded constant) by one char.
    over_budget = "x" * (_cr.physical_context_ceiling(cfg.model) + 1)
    with pytest.raises(CompletionRecoveryError, match=r"context.*bound"):
        step.run(_ctx(over_budget))

    assert len(runner.requests) == 1, (
        "an over-budget payload must not buy any billable recovery call; only the primary "
        f"(already-spent) request is allowed. Got {len(runner.requests)}."
    )
