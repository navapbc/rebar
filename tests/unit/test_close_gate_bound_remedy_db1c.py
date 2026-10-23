"""A context-bound refusal must give an actionable remedy. Description edits invalidate plan-review
material and require re-attestation. Append-only comments cannot be shortened. The message
retains measured size and limit and points to usable diagnostics or an operator override."""

from __future__ import annotations

import re

import pytest

from rebar._commands import gates as _gates
from rebar._commands import transition_close as _tc
from rebar._commands._seam import CommandError
from rebar._engine_support import field_reads as _fr
from rebar.llm.config import VERIFIER_DEFAULT_MODEL
from rebar.llm.errors import CompletionRecoveryError

pytestmark = pytest.mark.unit

# The real ticket db1c was filed against (2932), at the size that produced the live
# failure. The cap is now larger than this, so the fixture deliberately breaches the
# CURRENT cap rather than hard-coding a number that a future raise would make stale.
_TICKET_2932_CHARS = 41_595


def _context_ceiling() -> int:
    """The physical context ceiling for the default verifier model (bug 8eb3 retired the
    flat `_MAX_CONTEXT_CHARS`; the bound is now window-derived)."""
    from rebar.llm.workflow import completion_recovery as _cr

    return _cr.physical_context_ceiling(VERIFIER_DEFAULT_MODEL)


def _bound_error() -> CompletionRecoveryError:
    """The exact exception `_validate_recovery_inputs` raises on a context breach."""
    limit = _context_ceiling()
    return CompletionRecoveryError(
        "completion recovery context bound exceeded",
        diagnostic={
            "context_chars": limit + 1,
            "context_char_limit": limit,
            "criteria_completed": 0,
        },
    )


def _arm_gate(monkeypatch, exc: Exception) -> None:
    """Enable the close gate and make `verify_completion` raise `exc`."""
    import rebar.llm as _llm

    monkeypatch.setattr(_gates, "gate_enabled", lambda *a, **k: True)
    monkeypatch.setattr(_fr, "file_impact", lambda *a, **k: [])

    def _raise(*_a, **_k):
        raise exc

    monkeypatch.setattr(_llm, "verify_completion", _raise)


def _close_message(monkeypatch, exc: Exception) -> str:
    """Drive the operator-visible close path and return the CLI's error text."""
    _arm_gate(monkeypatch, exc)
    with pytest.raises(CommandError) as caught:
        _tc._completion_precheck("rec-0000", "task", ".", None, reason="", force_close="")
    return str(caught.value)


# "shorten/trim/reduce/edit ... description" with no attestation caveat anywhere.
_DESCRIPTION_ADVICE = re.compile(
    r"(shorten|trim|reduce|shrink|edit|cut)[^.]{0,80}\bdescription\b", re.IGNORECASE
)
_ATTESTATION_CAVEAT = re.compile(r"attestation|plan[- ]review|re-?earn|signature", re.IGNORECASE)
_COMMENT_ADVICE = re.compile(
    r"(shorten|trim|reduce|shrink|delete|remove|prune)[^.]{0,80}\bcomments?\b", re.IGNORECASE
)


def test_advising_a_description_edit_must_name_the_attestation_cost(monkeypatch) -> None:
    """A suggested description edit must state that it invalidates the signed plan review and
    requires re-attestation."""
    message = _close_message(monkeypatch, _bound_error())

    if _DESCRIPTION_ADVICE.search(message):
        assert _ATTESTATION_CAVEAT.search(message), (
            "the message advises editing the ticket description but never says that doing "
            "so invalidates the plan-review attestation (material_fingerprint hashes the "
            "description), so following it breaks require_plan_review_for_close and the "
            f"close still fails. Got: {message}"
        )


def test_the_remedy_never_offers_comment_shortening(monkeypatch) -> None:
    """The append-only tracker has no comment deletion or redaction operation, so a bound remedy
    cannot advise shortening comments."""
    message = _close_message(monkeypatch, _bound_error())

    assert not _COMMENT_ADVICE.search(message), (
        "the message tells the operator to shorten/delete comments, but the event store is "
        "append-only: _lib_writes exposes comment() with no delete or redact counterpart, "
        f"so no such operation exists. Got: {message}"
    )


def test_the_size_diagnosis_survives_the_new_remedy(monkeypatch) -> None:
    """Changing remedy text must retain the measured context size and configured limit."""
    message = _close_message(monkeypatch, _bound_error())

    limit = _context_ceiling()
    assert f"{limit + 1:,}" in message, (
        f"the measured context size must survive the rewrite. Got: {message}"
    )
    assert f"{limit:,}" in message, f"the limit must survive the rewrite. Got: {message}"


def test_the_remedy_points_at_a_lever_the_operator_can_actually_pull(monkeypatch) -> None:
    """A bound refusal must direct the operator to the sidecar diagnostic or sanctioned force
    override instead of reporting size alone."""
    message = _close_message(monkeypatch, _bound_error())

    assert re.search(r"gate_error_v1|sidecar|force-close|diagnostic", message, re.IGNORECASE), (
        "the operator is told the close failed on size but given no next step at all. "
        f"Got: {message}"
    )


def test_a_real_ticket_sized_like_2932_is_no_longer_refused() -> None:
    """A ticket near 41,595 characters remains below the 100,000-character recovery bound."""
    from rebar.llm.workflow import completion_recovery as _cr  # noqa: F401

    ceiling = _context_ceiling()
    assert _TICKET_2932_CHARS <= ceiling, (
        f"ticket 2932 ({_TICKET_2932_CHARS:,} chars) would be refused again by a context "
        f"budget of {ceiling:,}; db1c's original failure has regressed"
    )
