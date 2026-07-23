"""Ticket 21ca: rich-text codec + comment limits behind the port (happy path).

Two neutral-core modules reached Jira rich-text/limit modules by literal lazy-load key:
``outbound_comments.py`` (ADF decode for comment-diff + comment truncation) and
``inbound_translate.py`` (ADF decode as inbound defense-in-depth). 21ca routes them
through port roles:

* ``InboundMapper.normalize_rich_text(body)`` — decode a remote rich-text payload to
  text (Jira: ``adf_to_text`` for dicts, identity for strings). Serves BOTH the inbound
  apply path AND the outbound comment-diff decode.
* ``FieldSanitizer.fit_comment(body)`` — pure fit-to-limit for the comment-diff
  comparison (Jira: ``comment_limits.truncate_comment_body``; no send-side warning).

Happy-path oracle: the two port members exist on JiraBackend and behave for well-formed
input.
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
