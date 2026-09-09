"""Neutral-core rich-text port contracts.

``InboundMapper.normalize_rich_text`` converts remote payloads to text for
inbound application and outbound comparison. ``FieldSanitizer.fit_comment``
applies the Jira comment limit. The tests pin Jira backend wiring for valid
inputs.
"""

from __future__ import annotations

import pytest

from rebar_reconciler.adapters.jira import adf
from rebar_reconciler.adapters.jira.backend import JiraBackend

pytestmark = pytest.mark.unit

_ADF_HELLO = {
    "type": "doc",
    "version": 1,
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
}


def _backend() -> JiraBackend:
    return JiraBackend(transport=object())


# ── InboundMapper.normalize_rich_text (decode) ───────────────────────────────
def test_normalize_rich_text_decodes_adf_dict() -> None:
    r = _backend().inbound.normalize_rich_text(_ADF_HELLO)
    assert r == adf.adf_to_text(_ADF_HELLO)
    assert "hello" in r


def test_normalize_rich_text_passes_string_through() -> None:
    assert _backend().inbound.normalize_rich_text("plain body") == "plain body"


# ── FieldSanitizer.fit_comment (fit-to-limit) ────────────────────────────────
def test_fit_comment_leaves_short_body_unchanged() -> None:
    assert _backend().sanitizer.fit_comment("a short comment") == "a short comment"
