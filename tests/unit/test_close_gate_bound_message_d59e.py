"""The close gate must not blame a missing `[agents]` extra for a size bound (bug d59e).

When the completion verifier's bounded-recovery path rejects an oversized ticket,
``transition_close`` surfaces a single catch-all message telling the operator to
"install the 'agents' extra and set a model API key". For a
:class:`CompletionRecoveryError` that remedy is **false** — the extra and key are
present and working (the same close, retried, returns a real verdict) — and it
sends the operator to fix a dependency for what is actually "this ticket's text
is larger than the recovery bound".

The error already carries the numbers that would explain it:
``diagnostic={"context_chars": …, "context_char_limit": …}``. ``docs/llm-framework.md``
(§"Bounded completion recovery") names that diagnostic as the actionable surface —
"Inspect that diagnostic first" — yet the CLI handler interpolates only ``str(exc)``
and never reads it.

What this pins:

* the bound case reports the MEASURED SIZE and the LIMIT, and does not assert a
  missing extra/key;
* the genuine-unavailability case still DOES get the install-the-extra remedy.

That second test is the reason this file exists in this shape: the remedy text is
correct and useful for the case it was written for, so a fix that simply deletes it
would trade one wrong message for another. Both must hold.
"""

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
    """NEGATIVE CONTROL — the case the remedy text was written for must keep it.

    A missing `[agents]` extra / absent API key surfaces as an ``LLMError`` carrying a
    classifier ``.outcome``. That operator genuinely does need to install the extra and
    set a key, so the fix must narrow the remedy to this case rather than delete it.
    Without this test, "stop saying install-the-extra" could be satisfied by removing
    useful guidance from every failure.
    """
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
    """The trap in fixing this: don't swap one misattribution for another.

    ``CompletionRecoveryError`` is raised at several recovery stages, and only the
    bound checks carry ``context_chars``/``context_char_limit``. An exhaustion at, say,
    criterion 7 carries ``criteria_completed`` instead. A handler that reads those keys
    unconditionally renders "The ticket's context (None chars) exceeds the ... limit
    (None chars)" — which is both nonsense AND a false diagnosis, asserting a size
    breach that did not happen. That is precisely the defect this bug is about, just
    relocated, so it must be pinned rather than left to the next reader to discover.
    """
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
