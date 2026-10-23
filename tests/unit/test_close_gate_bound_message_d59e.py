"""A completion-recovery size bound reports measured context and limit, not a missing agents extra.
Model unavailability still recommends installing the extra or configuring its key."""

from __future__ import annotations

import pytest

from rebar._commands import gates as _gates
from rebar._commands import transition_close as _tc
from rebar._commands._seam import CommandError
from rebar._engine_support import field_reads as _fr
from rebar.llm.errors import CompletionRecoveryError, LLMError
from rebar.llm.failure import LLMOutcome, ResolutionClass


def _arm_gate(monkeypatch, exc: Exception) -> None:
    """Enable the close gate and make ``verify_completion`` raise ``exc``.

    Mirrors ``tests/unit/test_llm_failure_matrix.py::test_force_close_skips_completion_gate``:
    the deterministic file-impact precheck sits before the LLM call and is neutralized
    so the call is actually reached.
    """
    import rebar.llm as _llm

    monkeypatch.setattr(_gates, "gate_enabled", lambda *a, **k: True)
    monkeypatch.setattr(_fr, "file_impact", lambda *a, **k: [])

    def _raise(*_a, **_k):
        raise exc

    monkeypatch.setattr(_llm, "verify_completion", _raise)


def _close(ticket_id: str = "rec-0000") -> None:
    _tc._completion_precheck(ticket_id, "task", ".", None, reason="", force_close="")


def test_context_bound_failure_reports_the_size_not_a_missing_extra(monkeypatch) -> None:
    """The bug: a size-bound breach must explain itself with its own numbers."""
    exc = CompletionRecoveryError(
        "completion recovery context bound exceeded",
        diagnostic={"context_chars": 25_516, "context_char_limit": 24_000, "criteria_completed": 0},
    )
    _arm_gate(monkeypatch, exc)

    with pytest.raises(CommandError) as caught:
        _close()
    message = str(caught.value)

    # The remedy must not be the false one. The operator's extra and key are fine;
    # sending them to reinstall dependencies wastes the one clue they were given.
    assert "install the 'agents' extra" not in message, (
        "a context-bound breach must NOT claim the agents extra is missing — the extra "
        f"and key are present and working in this case. Got: {message}"
    )
    assert "set a model API key" not in message, (
        f"a context-bound breach must NOT claim the API key is missing. Got: {message}"
    )

    # It must instead surface the measurement it already holds, so the operator can act.
    assert "25516" in message or "25,516" in message, (
        f"the message must report the measured size (25516) from the error's own "
        f"diagnostic. Got: {message}"
    )
    assert "24000" in message or "24,000" in message, (
        f"the message must report the limit (24000) it breached. Got: {message}"
    )


def test_genuine_unavailability_still_recommends_installing_the_extra(monkeypatch) -> None:
    """Missing LLM support keeps the install-extra and API-key remedy. Recovery-bound
    errors lose it."""
    exc = LLMError("no model API key configured")
    exc.outcome = LLMOutcome(  # type: ignore[attr-defined]
        resolution_class=ResolutionClass.CHANGE_SETTINGS,
        diagnostic={"exception_type": "LLMError"},
        retryable=False,
    )
    _arm_gate(monkeypatch, exc)

    with pytest.raises(CommandError) as caught:
        _close()
    message = str(caught.value)

    assert "install the 'agents' extra" in message, (
        "a genuine unavailability must STILL tell the operator to install the extra — "
        f"that guidance is correct here. Got: {message}"
    )


def test_non_bound_recovery_failure_does_not_claim_a_size_breach(monkeypatch) -> None:
    """Recovery failures without context-size diagnostics must not be described as size breaches or
    print None-valued bounds."""
    exc = CompletionRecoveryError(
        "completion recovery exhausted at criterion 7",
        diagnostic={"criteria_completed": 7, "stage": "evidence"},
    )
    _arm_gate(monkeypatch, exc)

    with pytest.raises(CommandError) as caught:
        _close()
    message = str(caught.value)

    assert "None" not in message, (
        f"absent diagnostic keys must not render as 'None'. Got: {message}"
    )
    assert "exceeds" not in message, (
        "a non-bound recovery failure must NOT assert that a size limit was exceeded — "
        f"that is a false diagnosis. Got: {message}"
    )
    # It should still say something true and useful: the underlying error, and where the
    # full diagnostic lives.
    assert "criterion 7" in message, (
        f"the real underlying failure must survive into the message. Got: {message}"
    )


def test_context_bound_failure_is_not_reported_as_retryable(monkeypatch) -> None:
    """Collateral invariant: a bound breach is deterministic, so it must not exit 11.

    ``docs/exit-codes.md`` reserves 11 for a *transient, retryable* LLM degrade that a
    driving agent may auto-retry. Retrying an oversized ticket re-breaches the same
    fixed bound, so advertising it as retryable would send an agent into a loop.
    """
    exc = CompletionRecoveryError(
        "completion recovery context bound exceeded",
        diagnostic={"context_chars": 25_516, "context_char_limit": 24_000},
    )
    _arm_gate(monkeypatch, exc)

    with pytest.raises(CommandError) as caught:
        _close()

    assert caught.value.returncode != 11, (
        "a fixed-bound breach is not transient; exit 11 would invite an auto-retry loop"
    )
