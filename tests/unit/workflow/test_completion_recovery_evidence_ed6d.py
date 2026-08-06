"""Partial-evidence recovery survives a single criterion's exhaustion (bug ed6d).

The per-criterion evidence loop treated ONE criterion's budget exhaustion as fatal to the
whole aggregate — a single `LLMBudgetExhaustedError` propagating out of the loop discarded
every criterion's evidence already gathered and failed recovery, offering only the
`--force-close` exhaustion-is-not-a-verdict trap. But the per-criterion bound is a
per-criterion safeguard: partial evidence must be preserved, not discarded.

The fix catches `LLMBudgetExhaustedError`, `RunawayToolLoopError`, and token-exhaustion
`UnretryableOutputError` (guarded by `_is_token_exhaustion`, mirroring the primary path)
around each per-criterion evidence run, substitutes a content-free placeholder that names
the failure class and its numeric usage counters, marks the entry `exhausted=true`, and
continues. Recovery fails outright only if EVERY criterion's evidence run fails. A
non-token `UnretryableOutputError` is NOT placeholder-ized (a genuine unretryable defect).

This is the held-out oracle for the partial-evidence contract.
"""

from __future__ import annotations

import json

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.errors import (
    CompletionRecoveryError,
    LLMBudgetExhaustedError,
    LLMError,
    RunawayToolLoopError,
    UnretryableOutputError,
)
from rebar.llm.workflow.completion_recovery import CompletionAgentStep
from rebar.llm.workflow.executor import StepContext

pytestmark = pytest.mark.unit


def _ticket() -> dict:
    criteria = "\n".join(f"- [ ] criterion {index}" for index in range(1, 4))  # 3 criteria
    return {
        "ticket_id": "T-1",
        "title": "partial evidence recovery",
        "ticket_type": "task",
        "description": f"## Acceptance Criteria\n{criteria}",
    }


def _ctx() -> StepContext:
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
        inputs={"ticket_id": "T-1", "context": "Ticket: T-1\nsmall context"},
        workflow={"name": "completion-verification"},
        target_ticket="T-1",
        repo_root=None,
    )


def _budget_exc(*, requests: int = 20, tool_calls: int = 35) -> LLMBudgetExhaustedError:
    exc = LLMBudgetExhaustedError("PROSE_SENTINEL hit step budget request_limit=20")
    exc.diagnostic = {"requests": requests, "tool_calls": tool_calls}
    return exc


def _runaway_exc() -> RunawayToolLoopError:
    return RunawayToolLoopError(
        "PROSE_SENTINEL runaway", diagnostic={"requests": 55, "distinct_ratio_window": 0.5}
    )


class _EvidenceRunner:
    """Primary truncates (door into recovery); then per-criterion evidence runs, some of
    which raise a configured exception; then the finalizer, whose input is captured."""

    name = "evidence"

    def __init__(self, fail_by_index: dict[int, Exception] | None = None) -> None:
        self.requests: list = []
        self.fail_by_index = dict(fail_by_index or {})
        self._agentic_idx = -1
        self.finalizer_payload: dict | None = None

    def preflight(self) -> None:
        return None

    def run(self, req):
        self.requests.append(req)
        if len(self.requests) == 1:
            raise UnretryableOutputError("finish_reason=length")  # primary -> recovery
        if req.execution_mode == "agentic":
            self._agentic_idx += 1
            exc = self.fail_by_index.get(self._agentic_idx)
            if exc is not None:
                raise exc
            return {"text": f"Observed evidence at src/example.py:{self._agentic_idx}."}
        # finalizer (structured)
        payload = json.loads(req.instructions)
        self.finalizer_payload = payload
        expected = payload["expected_criteria"]
        return {
            "verdict": "PASS",
            "findings": [],
            "criteria": [
                {
                    "criterion": c,
                    "met": True,
                    "citation": {"kind": "source", "description": "src/example.py:1"},
                    "kind": "codebase-verifiable",
                }
                for c in expected
            ],
        }


def _step(runner: _EvidenceRunner) -> CompletionAgentStep:
    return CompletionAgentStep(runner=runner, repo_root=None, config=LLMConfig(runner="fake"))


def _run(runner: _EvidenceRunner, monkeypatch):
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *a, **k: _ticket())
    return _step(runner).run(_ctx())


# --------------------------------------------------------------------------- #
# Happy-path pins (implementer-visible).
# --------------------------------------------------------------------------- #


def test_single_budget_exhaustion_no_longer_aborts_recovery(monkeypatch) -> None:
    """AC: with N criteria and one evidence run budget-exhausting, the finalizer still runs
    and receives N evidence entries, the exhausted one carrying a placeholder marked
    exhausted=true."""
    runner = _EvidenceRunner(fail_by_index={1: _budget_exc()})
    result = _run(runner, monkeypatch).outputs

    assert result.get("verdict") in {"PASS", "FAIL"}
    assert runner.finalizer_payload is not None
    evidence = runner.finalizer_payload["bounded_evidence"]
    assert len(evidence) == 3, f"finalizer must receive all 3 entries, got {len(evidence)}"
    exhausted = [e for e in evidence if e.get("exhausted")]
    assert len(exhausted) == 1, f"exactly one entry must be marked exhausted, got {exhausted}"


def test_successful_evidence_is_byte_identical(monkeypatch) -> None:
    """AC: a run where no evidence agent fails produces the SAME finalizer input as before —
    each entry is exactly {criterion, evidence}, no extra keys, no exhausted marker."""
    runner = _EvidenceRunner(fail_by_index={})
    _run(runner, monkeypatch)

    evidence = runner.finalizer_payload["bounded_evidence"]
    assert len(evidence) == 3
    for entry in evidence:
        assert set(entry) == {"criterion", "evidence"}, (
            f"a healthy entry must be byte-identical to the pre-ed6d shape, got keys {set(entry)}"
        )


# --------------------------------------------------------------------------- #
# Held-out edge cases.
# --------------------------------------------------------------------------- #


def test_runaway_loop_in_an_evidence_run_is_handled_the_same_way(monkeypatch) -> None:
    """AC: a RunawayToolLoopError from an evidence run is placeholder-ized, not fatal."""
    runner = _EvidenceRunner(fail_by_index={0: _runaway_exc()})
    result = _run(runner, monkeypatch).outputs

    assert result.get("verdict") in {"PASS", "FAIL"}
    evidence = runner.finalizer_payload["bounded_evidence"]
    assert len(evidence) == 3
    assert sum(1 for e in evidence if e.get("exhausted")) == 1


def test_token_exhaustion_unretryable_becomes_a_placeholder(monkeypatch) -> None:
    """AC: a token-exhaustion UnretryableOutputError is converted to the exhausted
    placeholder, mirroring the primary path."""
    runner = _EvidenceRunner(fail_by_index={2: UnretryableOutputError("finish_reason=length")})
    result = _run(runner, monkeypatch).outputs

    assert result.get("verdict") in {"PASS", "FAIL"}
    evidence = runner.finalizer_payload["bounded_evidence"]
    assert len(evidence) == 3
    assert sum(1 for e in evidence if e.get("exhausted")) == 1


def test_non_token_unretryable_still_propagates(monkeypatch) -> None:
    """AC: a NON-token UnretryableOutputError is a genuine unretryable defect — it is NOT
    placeholder-ized; recovery fails rather than fabricating a placeholder for it."""
    runner = _EvidenceRunner(
        fail_by_index={1: UnretryableOutputError("refusal: content policy block")}
    )
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *a, **k: _ticket())
    with pytest.raises(LLMError):
        _step(runner).run(_ctx())
    # It must NOT have reached the finalizer with a fabricated placeholder.
    assert runner.finalizer_payload is None


def test_placeholder_is_content_free_and_never_affirmative(monkeypatch) -> None:
    """AC: the placeholder interpolates NO exception prose beyond the failure class name and
    numeric usage counters, and can never read as affirmative evidence."""
    runner = _EvidenceRunner(fail_by_index={0: _budget_exc(requests=20, tool_calls=35)})
    _run(runner, monkeypatch)

    entry = next(e for e in runner.finalizer_payload["bounded_evidence"] if e.get("exhausted"))
    text = entry["evidence"]

    # Names the failure class and its numeric counters.
    assert "LLMBudgetExhaustedError" in text
    assert "20" in text and "35" in text
    # States evidence could not be gathered — never affirmative.
    assert any(
        phrase in text.lower()
        for phrase in ("could not", "not gather", "within recovery bounds", "unable")
    ), f"placeholder must state evidence was NOT gathered, got: {text!r}"
    for affirmative in ("observed", "confirmed", "verified", "met", "src/example.py"):
        assert affirmative not in text.lower(), (
            f"placeholder must never read as affirmative evidence, found {affirmative!r}: {text!r}"
        )
    # No exception PROSE interpolated (only the class name + counters).
    assert "PROSE_SENTINEL" not in text, (
        f"placeholder must not interpolate the exception message prose, got: {text!r}"
    )


def test_every_criterion_exhausted_raises_with_counts(monkeypatch) -> None:
    """AC: if EVERY criterion's evidence run fails, recovery raises CompletionRecoveryError
    with diagnostic {criteria_total, criteria_exhausted}."""
    runner = _EvidenceRunner(fail_by_index={0: _budget_exc(), 1: _runaway_exc(), 2: _budget_exc()})
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *a, **k: _ticket())
    with pytest.raises(CompletionRecoveryError) as caught:
        _step(runner).run(_ctx())
    diag = caught.value.diagnostic
    assert diag.get("criteria_total") == 3
    assert diag.get("criteria_exhausted") == 3
