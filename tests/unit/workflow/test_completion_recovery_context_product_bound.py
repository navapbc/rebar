"""Verify completion recovery's physical and economic context bounds.

The physical ceiling uses twice the resolved verifier model's own token window. Unknown models use
the smallest ladder window. The economic ceiling bounds context length times criterion count.
"""

from __future__ import annotations

import json
import re

import pytest

from rebar.llm.config import VERIFIER_DEFAULT_MODEL, LLMConfig
from rebar.llm.errors import CompletionRecoveryError, UnretryableOutputError
from rebar.llm.workflow import completion_criteria as _cc
from rebar.llm.workflow import completion_recovery as _cr
from rebar.llm.workflow.completion_recovery import CompletionAgentStep
from rebar.llm.workflow.executor import StepContext

pytestmark = pytest.mark.unit

_HAIKU = "claude-haiku-5-5"
_SONNET = "claude-sonnet-5-5"
_UNKNOWN = "openai:gpt-4o"

# The live c9f7 shape that the flat 100,000 bound refuses forever.
_C9F7_CONTEXT_CHARS = 121_147
_C9F7_CRITERIA = 22


# --------------------------------------------------------------------------- #
# The own-window accessor: own window, NOT the escalation max.
# --------------------------------------------------------------------------- #


def test_own_window_accessor_returns_the_matched_models_own_window() -> None:
    """Use the matched model's own window instead of the plan-review ladder maximum."""
    from rebar.llm.model_classes import own_window_tokens
    from rebar.llm.plan_review.sizing import largest_window_tokens

    # Every 5.5 rung has a 1M window, so a ladder model's own window and its escalation
    # maximum coincide; the unlocated floor is where the two accessors' inputs still differ.
    assert own_window_tokens(_HAIKU) == 1_000_000
    assert largest_window_tokens(_HAIKU) == 1_000_000
    assert own_window_tokens(_SONNET) == 1_000_000


def test_own_window_accessor_falls_back_to_the_unlocated_floor_for_unknown_models() -> None:
    """Unknown models get the conservative 200K floor, not the 1M every 5.5 rung declares."""
    from rebar.llm.model_classes import UNLOCATED_MODEL_WINDOW_TOKENS, own_window_tokens

    assert UNLOCATED_MODEL_WINDOW_TOKENS == 200_000
    assert own_window_tokens(_UNKNOWN) == UNLOCATED_MODEL_WINDOW_TOKENS
    assert own_window_tokens("") == UNLOCATED_MODEL_WINDOW_TOKENS


# --------------------------------------------------------------------------- #
# The physical ceiling: window-derived, 2 chars/token.
# --------------------------------------------------------------------------- #


def test_physical_ceiling_is_window_derived_two_chars_per_token() -> None:
    """AC: sonnet and haiku 5.5 → 2,000,000 chars; unknown → the unlocated floor × 2."""
    from rebar.llm.model_classes import UNLOCATED_MODEL_WINDOW_TOKENS

    assert _cr.physical_context_ceiling(_SONNET) == 2_000_000
    assert _cr.physical_context_ceiling(_HAIKU) == 2_000_000
    assert _cr.physical_context_ceiling(_UNKNOWN) == UNLOCATED_MODEL_WINDOW_TOKENS * 2


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
    _cc._validate_recovery_inputs(criteria, context, VERIFIER_DEFAULT_MODEL)


def test_physical_ceiling_refuses_an_oversized_context_on_a_200k_window() -> None:
    """AC: with a 200K-window model (an unlocated model gets the 200K floor; Haiku 4.5 held that
    rung before 5.5) a context over 400,000 chars is refused with a CompletionRecoveryError
    carrying {context_chars, context_char_limit}; the default sonnet model admits a
    400,000-char context (its ceiling is 2,000,000)."""
    # One criterion so the economic product (400,001) stays far under 3,200,000 —
    # isolating the PHYSICAL ceiling as the sole cause of refusal.
    over = "x" * (400_000 + 1)
    with pytest.raises(CompletionRecoveryError) as caught:
        _cc._validate_recovery_inputs(["c"], over, _UNKNOWN)
    diag = caught.value.diagnostic
    assert diag["context_chars"] == 400_001
    assert diag["context_char_limit"] == 400_000

    # The same 400,001-char context is admitted under the default sonnet model.
    _cc._validate_recovery_inputs(["c"], "x" * 400_000, _SONNET)


def test_a_shape_under_both_ceilings_is_admitted() -> None:
    """Negative control: a context that fits the physical ceiling must be admitted (no
    false refusal)."""
    context = "x" * 100_000
    criteria = [f"criterion {i}" for i in range(10)]
    _cc._validate_recovery_inputs(criteria, context, _SONNET)


# --------------------------------------------------------------------------- #
# End-to-end: the c9f7 shape reaches a verdict through the full recovery step.
# --------------------------------------------------------------------------- #


class _RecoverableRunner:
    """Primary call truncates (the door into recovery); banked successor + finalizer succeed."""

    name = "recoverable"

    def __init__(self) -> None:
        self.requests: list = []

    def preflight(self) -> None:
        return None

    def run(self, req):
        self.requests.append(req)
        if len(self.requests) == 1:
            raise UnretryableOutputError("finish_reason=length")
        if req.execution_mode == "single_turn":
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
        # Batched successor (agentic, structured): bank each criterion via the record tool.
        record = req.extra_tools[0] if req.extra_tools else None
        payload = json.loads(req.instructions) if req.instructions.startswith("{") else {}
        for cid in _criterion_ids_in(req.instructions):
            if record is not None:
                record(cid, True, "Observed implementation evidence at src/example.py:10.")
        return {"verdict": "PASS", "criteria": [], "_usage": {"requests": 0}}


def _criterion_ids_in(instructions: str) -> list[str]:
    """The criterion ids the successor was asked to verify (they appear in its instructions)."""
    return re.findall(r"c\d{2}-[0-9a-f]{8}", instructions)


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
