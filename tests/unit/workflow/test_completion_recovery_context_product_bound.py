"""The two-ceiling recovery bound: physical (window-derived) + economic (bug 8eb3).

`d59e` raised a FLAT per-context char bound (24,000 → 100,000) and c9f7 outgrew it
within weeks (121,147 chars > 100,000), refused forever because the store is append-only
so context can only grow. A flat bound chases a monotonically growing tail; the fix is to
derive the bound from the capacity it actually protects.

`_validate_recovery_inputs` now expresses TWO distinct ceilings, evaluated against the
resolved verifier model:

* **Physical** — each evidence run must fit ONE model window. Derived from the resolved
  model's OWN context window (`own_window_tokens`), NOT the plan-review escalation max
  (`largest_window_tokens`, which over-admits because plan-review escalates up the ladder
  and completion does not). ceiling = own_window_tokens × 2 chars/token, deliberately
  conservative (English prose ≈ 4 chars/token) so half the window is left for the system
  prompt, criteria, tool traffic, and output.
* **Economic** — recovery re-sends the context once PER criterion, so spend scales with
  `len(context) × len(criteria)`. A single flat product ceiling (3,200,000 = the
  previously-ratified worst case, _MAX_CRITERIA 32 × 100k) replaces the arbitrary
  per-axis split, allocating the ratified worst case where real tickets need it.

This file is the held-out oracle for both ceilings and for the own-window accessor's
own-vs-escalation semantics.
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

_HAIKU = "claude-haiku-4-5"
_SONNET = "claude-sonnet-4-6"
_UNKNOWN = "openai:gpt-4o"

# The live c9f7 shape that the flat 100,000 bound refuses forever.
_C9F7_CONTEXT_CHARS = 121_147
_C9F7_CRITERIA = 22


# --------------------------------------------------------------------------- #
# The own-window accessor: own window, NOT the escalation max.
# --------------------------------------------------------------------------- #


def test_own_window_accessor_returns_the_matched_models_own_window() -> None:
    """AC: accessor(haiku rung) == 200,000 while largest_window_tokens(haiku) == 1,000,000.

    Completion does not escalate models, so reusing plan-review's escalation accessor
    would admit haiku contexts up to the LADDER MAX (1M tokens) instead of haiku's own
    200k — over-admitting context that cannot fit one window. The two accessors must
    disagree exactly here, side by side.
    """
    from rebar.llm.model_classes import own_window_tokens
    from rebar.llm.plan_review.sizing import largest_window_tokens

    assert own_window_tokens(_HAIKU) == 200_000
    assert largest_window_tokens(_HAIKU) == 1_000_000
    assert own_window_tokens(_SONNET) == 1_000_000


def test_own_window_accessor_falls_back_to_the_ladder_minimum_for_unknown_models() -> None:
    """An unrecognised model → the ladder MINIMUM (bug 48b3's conservative default).

    Under-admitting is loud and recoverable (a large ticket refuses visibly); over-admitting
    fails mid-run. The rung lookup is a substring match, so any family the ladder cannot
    locate must inherit the smallest window, never the largest.
    """
    from rebar.llm.model_classes import MODEL_WINDOW_LADDER, own_window_tokens

    ladder_min = min(window for _name, window in MODEL_WINDOW_LADDER)
    assert own_window_tokens(_UNKNOWN) == ladder_min
    assert own_window_tokens("") == ladder_min


# --------------------------------------------------------------------------- #
# The physical ceiling: window-derived, 2 chars/token.
# --------------------------------------------------------------------------- #


def test_physical_ceiling_is_window_derived_two_chars_per_token() -> None:
    """AC: sonnet → 2,000,000 chars; haiku → 400,000 chars; unknown → ladder-min × 2."""
    from rebar.llm.model_classes import MODEL_WINDOW_LADDER

    assert _cr.physical_context_ceiling(_SONNET) == 2_000_000
    assert _cr.physical_context_ceiling(_HAIKU) == 400_000
    ladder_min = min(window for _name, window in MODEL_WINDOW_LADDER)
    assert _cr.physical_context_ceiling(_UNKNOWN) == ladder_min * 2


# --------------------------------------------------------------------------- #
# The recovery-input validation: c9f7 accepted; physical + economic refusals.
# --------------------------------------------------------------------------- #


def test_c9f7_shape_proceeds_past_preflight_with_zero_runner_calls() -> None:
    """THE BUG: the live c9f7 shape (121,147 chars × 22 criteria) under the default
    sonnet verifier must pass preflight — 121,147 ≪ 2,000,000 physical and
    121,147 × 22 = 2,665,234 < 3,200,000 economic — buying zero runner calls."""
    criteria = [f"criterion {i}" for i in range(_C9F7_CRITERIA)]
    context = "x" * _C9F7_CONTEXT_CHARS
    # Must not raise under the resolved default verifier model.
    _cr._validate_recovery_inputs(criteria, context, VERIFIER_DEFAULT_MODEL)


def test_physical_ceiling_refuses_an_oversized_haiku_context() -> None:
    """AC: with a haiku-rung model a context over 400,000 chars is refused with a
    CompletionRecoveryError carrying {context_chars, context_char_limit}; the default
    sonnet model admits a 400,000-char context (its ceiling is 2,000,000)."""
    # One criterion so the economic product (400,001) stays far under 3,200,000 —
    # isolating the PHYSICAL ceiling as the sole cause of refusal.
    over = "x" * (400_000 + 1)
    with pytest.raises(CompletionRecoveryError) as caught:
        _cr._validate_recovery_inputs(["c"], over, _HAIKU)
    diag = caught.value.diagnostic
    assert diag["context_chars"] == 400_001
    assert diag["context_char_limit"] == 400_000

    # The same 400,001-char context is admitted under the default sonnet model.
    _cr._validate_recovery_inputs(["c"], "x" * 400_000, _SONNET)


def test_economic_ceiling_refuses_an_oversized_product() -> None:
    """AC: a context × criteria product over 3,200,000 is refused with a
    CompletionRecoveryError carrying {context_chars, criteria_total, recovery_input_chars,
    recovery_input_char_limit}, before any runner call — even when each axis alone fits
    the physical ceiling."""
    # 200,000 chars × 20 criteria = 4,000,000 > 3,200,000; 200,000 ≪ 2,000,000 physical.
    context = "x" * 200_000
    criteria = [f"criterion {i}" for i in range(20)]
    with pytest.raises(CompletionRecoveryError) as caught:
        _cr._validate_recovery_inputs(criteria, context, _SONNET)
    diag = caught.value.diagnostic
    assert diag["context_chars"] == 200_000
    assert diag["criteria_total"] == 20
    assert diag["recovery_input_chars"] == 200_000 * 20
    assert diag["recovery_input_char_limit"] == 3_200_000


def test_a_shape_under_both_ceilings_is_admitted() -> None:
    """Negative control: a context that fits the physical ceiling AND whose product fits
    the economic ceiling must be admitted (no false refusal)."""
    context = "x" * 100_000
    criteria = [f"criterion {i}" for i in range(10)]  # product 1,000,000 < 3,200,000
    _cr._validate_recovery_inputs(criteria, context, _SONNET)


# --------------------------------------------------------------------------- #
# End-to-end: the c9f7 shape reaches a verdict through the full recovery step.
# --------------------------------------------------------------------------- #


class _RecoverableRunner:
    """Primary call truncates (the door into recovery); recovery then succeeds."""

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


def test_a_context_over_the_old_flat_bound_now_reaches_a_verdict(monkeypatch) -> None:
    """END-TO-END: a 121,147-char context (over the retired 100,000 flat bound) reaches a
    real verdict through CompletionAgentStep under the default verifier, instead of being
    refused at preflight."""
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *a, **k: _ticket())
    context = "y" * _C9F7_CONTEXT_CHARS
    runner = _RecoverableRunner()
    step = CompletionAgentStep(runner=runner, repo_root=None, config=LLMConfig(runner="fake"))

    result = step.run(_ctx(context)).outputs

    assert result.get("verdict") in {"PASS", "FAIL"}, (
        f"a {_C9F7_CONTEXT_CHARS:,}-char context must reach a real verdict, not a "
        f"fail-closed refusal. Got: {result!r}"
    )
