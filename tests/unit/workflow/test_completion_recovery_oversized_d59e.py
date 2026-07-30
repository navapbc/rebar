"""A large-but-legitimate ticket must still reach a completion verdict (bug d59e).

Bounded completion recovery exists to survive an exhausted aggregate verifier
(ticket 9a08, which states the intended remedy: the workflow "should bound/compact
evidence and tool output, scale the response budget when justified, or recover by
splitting the decision step" and "should not spend several minutes and terminate
without any verdict"). Before this fix it did the opposite for a ticket that was merely
large: ``_validate_recovery_inputs`` refused on ``len(context) > _MAX_CONTEXT_CHARS``
before any recovery work happened, so correct, merged work could not transition to
``closed`` without ``--force-close`` — which is 9a08's own definition of a fail-closed
close-gate availability defect.

Why the "just shorten the ticket" remedy is not an answer, measured on the real
ticket 9fd4-a94c-156e-4a56 (34,282 chars, matching its live failure byte-for-byte):

* description 10,281 chars; 9 comments totalling 23,617 chars (69% of the context);
* deleting the ENTIRE description still leaves 24,001 chars — one char over the limit;
* comments are the only sufficient lever, and the store is append-only: ``_lib_writes``
  exposes ``comment()`` and no delete/redact counterpart.

So the advertised remedy names material that cannot resolve the breach. Worse, shipped
ticket 19b1 makes ticket comments the *sanctioned* completion-evidence channel ("the
verifier reads the ticket's comments so documented evidence is incorporated on the next
verification"), so following the system's own guidance drives a ticket irreversibly
across the bound.

What this file pins:

* an oversized-but-legitimate context still yields a VERDICT (recovery compacts rather
  than refusing), and the payload actually put on the wire respects the budget;
* the declared criteria budget is reachable — criteria that pass their own bounds can
  never assemble into a context that is refused (d59e's untouched AC 4);
* a genuinely unbounded payload STILL fails fast with no billable recovery call, so the
  cost guard the bound was written for is preserved rather than deleted.
"""

from __future__ import annotations

import json

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.errors import CompletionRecoveryError, UnretryableOutputError
from rebar.llm.workflow import completion_recovery as _cr
from rebar.llm.workflow.completion_recovery import CompletionAgentStep
from rebar.llm.workflow.executor import StepContext

pytestmark = pytest.mark.unit


class _RecoverableRunner:
    """Primary call truncates (the only door into recovery); recovery then succeeds."""

    name = "recoverable"

    def __init__(self) -> None:
        self.requests: list = []

    def preflight(self) -> None:
        return None

    def run(self, req):  # noqa: ANN001, ANN201
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


def _ticket(n_criteria: int = 6) -> dict:
    criteria = "\n".join(f"- [ ] criterion {index}" for index in range(1, n_criteria + 1))
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


def _realistic_oversized_context(total: int | None = None) -> str:
    """A context shaped like the real 9fd4: a modest description plus many comments.

    Deliberately NOT ``"x" * N`` — a degenerate blob is the hostile-input case the
    existing bound test already covers, and conflating the two is what let a guard
    written for hostile input reject legitimate work.
    """
    # Relative to the module's DECLARED budget, never a hardcoded char count: a fixture
    # pinned to one value silently stops exercising the over-budget path the moment the
    # budget is retuned, which is exactly how this defect stayed invisible.
    if total is None:
        total = _cr._MAX_CONTEXT_CHARS * 3
    head = "Ticket: T-1\nDescription:\n## Acceptance Criteria\n" + "\n".join(
        f"- [ ] criterion {i}" for i in range(1, 7)
    )
    body = []
    i = 0
    while len(head) + sum(map(len, body)) < total:
        i += 1
        body.append(
            f"\n\nComment {i}: evidence recorded per the verifier's own remediation "
            f"guidance — see src/example.py:{i} and the CI run for criterion {i % 6 + 1}. "
            + "detail "
            * 20
        )
    return (head + "".join(body))[:total]


def test_oversized_but_legitimate_context_still_reaches_a_verdict(monkeypatch) -> None:
    """THE BUG. A large legitimate ticket must close without --force-close.

    Asserts the observable postcondition (a verdict is produced), not how recovery
    achieves it, so a compaction, a summarization, or a per-criterion slice all satisfy
    it — but a refusal does not.
    """
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *a, **k: _ticket())
    context = _realistic_oversized_context()
    assert len(context) > _cr._MAX_CONTEXT_CHARS, "fixture precondition: over the budget"

    runner = _RecoverableRunner()
    step = CompletionAgentStep(runner=runner, repo_root=None, config=LLMConfig(runner="fake"))

    result = step.run(_ctx(context)).outputs

    assert isinstance(result, dict), f"recovery must yield a verdict payload, got {result!r}"
    verdict = result
    assert verdict.get("verdict") in {"PASS", "FAIL"}, (
        "an oversized-but-legitimate ticket must reach a real verdict rather than a "
        f"fail-closed refusal. Got: {verdict!r}"
    )
    assert len(runner.requests) > 1, (
        "recovery must actually run (more than the single truncated primary call)"
    )


def test_recovery_never_puts_more_than_the_budget_on_the_wire(monkeypatch) -> None:
    """The cost intent of the bound survives: no single recovery call exceeds the budget.

    This is what makes the fix a *bound*, not a removal — the guard's purpose was to cap
    billable payload, and that must still hold once refusal becomes compaction.
    """
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *a, **k: _ticket())
    runner = _RecoverableRunner()
    step = CompletionAgentStep(runner=runner, repo_root=None, config=LLMConfig(runner="fake"))

    step.run(_ctx(_realistic_oversized_context()))

    oversized = [
        (i, len(r.instructions))
        for i, r in enumerate(runner.requests[1:], start=1)
        if len(r.instructions or "") > _cr._MAX_FINALIZER_INPUT_CHARS
    ]
    assert not oversized, f"recovery calls exceeded the payload budget: {oversized}"


def test_the_criteria_budget_never_exceeds_the_context_budget() -> None:
    """d59e AC 4, pinned as the INVARIANT rather than as a sampled behaviour.

    Criteria ⊂ description ⊂ context, so a criteria budget larger than the context
    budget advertises capacity that cannot be used — which is exactly the 32,000 vs
    24,000 incoherence this bug is about.

    This assertion exists because the behavioural form of AC 4 below became a tautology
    once refusal was replaced by compaction: with nothing rejecting an over-budget
    context, sampling criteria sizes can no longer detect a constants regression. This
    one can — it fails the moment the two budgets are set incoherently again.
    """
    assert _cr._MAX_TOTAL_CRITERIA_CHARS <= _cr._MAX_CONTEXT_CHARS, (
        "the declared criteria budget "
        f"({_cr._MAX_TOTAL_CRITERIA_CHARS:,}) exceeds the context budget "
        f"({_cr._MAX_CONTEXT_CHARS:,}). Criteria are extracted from the description and "
        "the description is embedded in the context, so every criteria set above the "
        "context budget is unusable and the advertised criteria capacity is a lie."
    )


@pytest.mark.parametrize("total", [24_100, 28_000, 31_900])
def test_criteria_that_pass_their_own_bounds_are_never_refused_by_the_context_bound(
    total: int,
) -> None:
    """d59e AC 4 — the declared criteria budget must be reachable.

    Criteria are extracted from the description and the description is embedded in the
    context, so criteria ⊂ context. With ``_MAX_TOTAL_CRITERIA_CHARS`` (32,000) larger
    than ``_MAX_CONTEXT_CHARS`` (24,000), every criteria set in the 24k–32k band passes
    its own three bounds and is then unconditionally refused by the context bound —
    i.e. the top quarter of the advertised criteria budget is unreachable.
    """
    n = 10
    # Size the filler so the PREFIX is counted too, otherwise the total overshoots
    # _MAX_TOTAL_CRITERIA_CHARS and the test would fail on its own fixture rather than
    # on the defect.
    prefix_len = len("criterion 00 ")
    each = total // n - prefix_len
    criteria = [f"criterion {i:02d} " + ("y" * each) for i in range(n)]
    context = (
        "Ticket: T\nDescription:\n## Acceptance Criteria\n"
        + "\n".join(f"- [ ] {c}" for c in criteria)
        + "\n"
    )

    assert sum(map(len, criteria)) <= _cr._MAX_TOTAL_CRITERIA_CHARS, "fixture precondition"
    assert all(len(c) <= _cr._MAX_CRITERION_CHARS for c in criteria), "fixture precondition"
    assert len(criteria) <= _cr._MAX_CRITERIA, "fixture precondition"

    try:
        _cr._validate_recovery_inputs(criteria, context)
    except CompletionRecoveryError as exc:
        pytest.fail(
            "criteria within every declared criteria bound were refused on the context "
            f"bound, making the advertised criteria budget unreachable: {exc}"
        )


def test_a_genuinely_unbounded_payload_still_fails_before_any_billable_call(
    monkeypatch,
) -> None:
    """NEGATIVE CONTROL — the hostile-input guard must not be deleted by this fix.

    The bound was written to "reject hostile/unbounded recovery work before the first
    recovery call". A degenerate multi-megabyte blob must still fail fast with only the
    single (already-spent) primary request, so "let legitimate tickets through" cannot
    be satisfied by removing the guard outright.
    """
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *a, **k: _ticket())
    runner = _RecoverableRunner()
    step = CompletionAgentStep(runner=runner, repo_root=None, config=LLMConfig(runner="fake"))

    with pytest.raises(CompletionRecoveryError):
        step.run(_ctx("x" * 5_000_000))

    assert len(runner.requests) == 1, (
        "a hostile payload must not buy any billable recovery call; only the primary "
        f"(already-spent) request is allowed. Got {len(runner.requests)}."
    )
