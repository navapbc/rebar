"""Neutral-core rich-text port contracts.

``InboundMapper.normalize_rich_text`` converts remote payloads to text for
inbound application and outbound comparison. ``FieldSanitizer.fit_comment``
applies the Jira comment limit. The tests pin Jira backend wiring for valid
inputs.

Held-out oracles merged from test_richtext_port_heldout: Held-out rich-text port edge contracts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar.reconciler.adapters.jira import adf, comment_limits
from rebar.reconciler.adapters.jira.backend import JiraBackend
from rebar.reconciler.adapters.jira.rich_text_codec import AdfCodec
from rebar.reconciler.adapters.jira_family.rich_text import cutover_clients
from rebar.reconciler.outbound_comments import (
    _decorate_outbound_comment,
    fit_preserving_marker,
)

pytestmark = pytest.mark.unit


_ADF_HELLO = {
    "type": "doc",
    "version": 1,
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
}


def _backend() -> JiraBackend:
    return JiraBackend(transport=object())


def test_normalize_rich_text_decodes_adf_dict() -> None:
    r = _backend().inbound.normalize_rich_text(_ADF_HELLO)
    assert r == adf.adf_to_text(_ADF_HELLO)
    assert "hello" in r


def test_normalize_rich_text_passes_string_through() -> None:
    assert _backend().inbound.normalize_rich_text("plain body") == "plain body"


def test_fit_comment_leaves_short_body_unchanged() -> None:
    assert _backend().sanitizer.fit_comment("a short comment") == "a short comment"


# ── held-out oracles (merged from test_richtext_port_heldout) ──

_REC = Path(__file__).resolve().parents[4] / "src" / "rebar" / "reconciler"


def test_fit_comment_truncates_over_limit_byte_identical() -> None:
    """The port fits comments exactly as the send path does.

    Expected bytes come from the marker-preserving ADF composition. A separate
    transport test captures the sent body and owns send-path parity.
    """
    huge = "x" * 40_000  # well over Jira's comment limit on either measure
    fitted = _backend().sanitizer.fit_comment(huge)

    codec = AdfCodec(rich="cloud" in cutover_clients())
    landed = fit_preserving_marker(_decorate_outbound_comment(huge), codec.fit_outbound)
    decoration_len = len(_decorate_outbound_comment(""))
    expected = landed[: len(landed) - decoration_len]

    assert fitted == expected  # byte-identical to what the send path lands
    assert len(fitted) < len(huge)  # actually truncated
    assert fitted != comment_limits.truncate_comment_body(huge)


def test_normalize_rich_text_never_returns_a_dict() -> None:
    """Bug 1bb2-5da5 defense: a raw ADF dict is decoded to a str, NEVER surfaced as a
    dict (which would corrupt an EDIT event's ``description`` slot)."""
    out = _backend().inbound.normalize_rich_text(_ADF_HELLO)
    assert isinstance(out, str)
    assert out == adf.adf_to_text(_ADF_HELLO)


def test_normalize_rich_text_handles_empty() -> None:
    b = _backend()
    assert b.inbound.normalize_rich_text("") == ""
    assert isinstance(b.inbound.normalize_rich_text({"type": "doc", "content": []}), str)
