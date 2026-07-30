"""A step-budget exhaustion must not blame a missing `[agents]` extra either (bug d59e).

``test_close_gate_bound_message_d59e.py`` fixed this for the bounded-recovery path
(Gerrit change 1027) by having ``transition_close`` consult
``_failure.recovery_failure_cause(exc)``. That helper returns ``None`` for anything
that is not a :class:`CompletionRecoveryError`, so the ``or``-fallback literal remains
the default for **every other** verifier failure — including one that provably ran the
model for 804 seconds.

Observed live while closing 9fd4-a94c-156e-4a56 with the extra installed and a working
API key::

    Error: cannot close 9fd4-...: completion verification could not run (completion
    verification workflow did not produce a verdict: step 'decide@then/verify' failed:
    agent exceeded its step budget (max_iterations=480; ~1 model request per tool
    call). Raise REBAR_LLM_MAX_STEPS or narrow the task.). The completion-verification
    gate is enabled (verify.require_completion_verification_for_close); install the
    'agents' extra and set a model API key. ...

The extra was installed and the key was set — the verifier had just spent 13 minutes
making real model calls. The remedy is false, and it displaces the true one the
exception already carries.

The sibling file's negative control (a genuine unavailability must KEEP the
install-the-extra remedy) still applies and is deliberately not duplicated here.
"""

from __future__ import annotations

import pytest

from rebar._commands import gates as _gates
from rebar._commands import transition_close as _tc
from rebar._commands._seam import CommandError
from rebar._engine_support import field_reads as _fr
from rebar.llm.errors import LLMError

pytestmark = pytest.mark.unit

_STEP_BUDGET_TEXT = (
    "completion verification workflow did not produce a verdict: step "
    "'decide@then/verify' failed: agent exceeded its step budget "
    "(max_iterations=480; ~1 model request per tool call). Raise "
    "REBAR_LLM_MAX_STEPS or narrow the task."
)


def _arm_gate(monkeypatch, exc: Exception) -> None:
    """Enable the close gate and make ``verify_completion`` raise ``exc``."""
    import rebar.llm as _llm

    monkeypatch.setattr(_gates, "gate_enabled", lambda *a, **k: True)
    monkeypatch.setattr(_fr, "file_impact", lambda *a, **k: [])

    def _raise(*_a, **_k):
        raise exc

    monkeypatch.setattr(_llm, "verify_completion", _raise)


def _close() -> None:
    _tc._completion_precheck("rec-0000", "task", ".", None, reason="", force_close="")


def test_step_budget_exhaustion_does_not_blame_a_missing_extra(monkeypatch) -> None:
    """A step-budget breach ran the model, so the extra/key are demonstrably present."""
    _arm_gate(monkeypatch, LLMError(_STEP_BUDGET_TEXT))

    with pytest.raises(CommandError) as caught:
        _close()
    message = str(caught.value)

    assert "install the 'agents' extra" not in message, (
        "a step-budget exhaustion proves the extra and key are working (the verifier "
        "just spent minutes calling the model), so telling the operator to install the "
        f"extra sends them to fix a dependency that is not broken. Got: {message}"
    )


def test_step_budget_exhaustion_surfaces_the_actionable_remedy(monkeypatch) -> None:
    """Removing the false remedy must not leave the operator with nothing.

    Without this, "stop saying install-the-extra" could be satisfied by emitting a bare
    failure. The exception already names the real lever; it must survive to the CLI.
    """
    _arm_gate(monkeypatch, LLMError(_STEP_BUDGET_TEXT))

    with pytest.raises(CommandError) as caught:
        _close()
    message = str(caught.value)

    assert "REBAR_LLM_MAX_STEPS" in message, (
        "the actionable remedy the exception already carries must reach the operator. "
        f"Got: {message}"
    )
