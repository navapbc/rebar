"""Refusal-as-content fail-closed guard (bug 8303, pydantic-ai #5221 / #6167).

Characterization result: rebar's structured-output path is NOT vulnerable on the current
Anthropic stack — pydantic-ai maps Anthropic ``stop_reason='refusal'`` to
``finish_reason='content_filter'``, which ``check_stop_reason`` already treats as
unretryable, so a refusal fails closed (the gate degrades to INDETERMINATE, never a hollow
PASS). These tests PIN that guarantee so a future pydantic-ai/provider change cannot silently
reopen the gap, and exercise the defense-in-depth ``check_response`` guard that catches a
refusal even if it ever arrives with an unset/benign ``finish_reason`` (the #5221 shape).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rebar.llm import structured
from rebar.llm.errors import UnretryableOutputError


def _resp(finish_reason=None, provider_details=None):
    return SimpleNamespace(finish_reason=finish_reason, provider_details=provider_details)


# ── rebar catches BOTH the raw Anthropic and the pydantic-ai-normalized refusal forms ──
@pytest.mark.parametrize("reason", ["refusal", "content_filter"])
def test_check_stop_reason_fails_closed_on_refusal_forms(reason) -> None:
    with pytest.raises(UnretryableOutputError):
        structured.check_stop_reason(reason)


# ── CONTRACT: the pydantic-ai Anthropic refusal mapping lands in rebar's catch set ─────
def test_anthropic_refusal_mapping_stays_in_rebar_catch_set() -> None:
    # The gap stays closed only while pydantic-ai maps a refusal onto a finish_reason that
    # rebar treats as unretryable. Import the actual mapping and assert the contract; if a
    # future pydantic-ai remaps 'refusal' to something rebar does not catch (or moves the
    # symbol), THIS test fails loudly — the silent-reopen guard the ticket asks for.
    from pydantic_ai.models.anthropic import _FINISH_REASON_MAP

    mapped = _FINISH_REASON_MAP.get("refusal")
    assert mapped in structured._UNRETRYABLE_STOP_REASONS, (
        f"pydantic-ai now maps Anthropic 'refusal' -> {mapped!r}, which rebar's "
        "_UNRETRYABLE_STOP_REASONS does not catch — the refusal-as-content gap has reopened; "
        "add the value to _UNRETRYABLE_STOP_REASONS."
    )


# ── check_response: the runner's fail-closed guard ─────────────────────────────────────
def test_check_response_raises_on_mapped_content_filter() -> None:
    # The common Anthropic-refusal path: finish_reason normalized to content_filter.
    with pytest.raises(UnretryableOutputError):
        structured.check_response(_resp(finish_reason="content_filter"))


def test_check_response_defense_in_depth_raw_refusal_without_finish_reason() -> None:
    # The #5221 shape: a refusal that arrives with an unset/benign finish_reason but a raw
    # refusal signal in provider_details. The finish_reason-only check would MISS this; the
    # defense-in-depth provider_details read still fails closed.
    with pytest.raises(UnretryableOutputError):
        structured.check_response(
            _resp(finish_reason=None, provider_details={"finish_reason": "refusal"})
        )


def test_check_response_defense_in_depth_refusal_explanation_key() -> None:
    # Anthropic stashes the refusal explanation under provider_details['refusal']; catch it
    # even if finish_reason is 'stop' (a hypothetical future non-mapping adapter).
    with pytest.raises(UnretryableOutputError):
        structured.check_response(
            _resp(finish_reason="stop", provider_details={"refusal": "I can't help with that."})
        )


def test_check_response_passes_clean_turn() -> None:
    # A normal completed turn must NOT raise (no false-positive that would block real work).
    structured.check_response(
        _resp(finish_reason="stop", provider_details={"finish_reason": "end_turn"})
    )
    structured.check_response(_resp(finish_reason=None, provider_details=None))
