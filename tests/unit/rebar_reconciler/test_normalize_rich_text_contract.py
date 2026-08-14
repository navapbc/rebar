"""Cross-repo contract test for ``normalize_rich_text`` (story 3289, epic 708d).

The sibling repo ``agentic-dev-platform`` imports the ``normalize_rich_text`` seam
from this package — as the free function
``rebar_reconciler.inbound_fields.normalize_rich_text`` AND through the Backend port's
``InboundMapper.normalize_rich_text`` (``adapters/jira/backend.py:_JiraInbound`` and the
Data Center twin). Its behavior is therefore a PUBLIC contract, not an internal detail:
a change to the accepted input types, the return type, or the decode behavior would
break that importer silently at their call site.

This test pins that shape so a breaking change fails HERE first, in this repo's own
default suite, instead of surfacing downstream. It is a characterization of the
CURRENT public behavior (ticket 21ca), not a new requirement:

  * ``None`` -> ``""`` (empty string, never ``None``).
  * a plain ``str`` passes through UNCHANGED (identity), so decode is idempotent on
    already-decoded text.
  * a non-``None`` non-``dict`` is coerced with ``str(...)``.
  * an ADF ``dict`` decodes to plain/markdown text via ``adf_to_text``.
  * the return type is ALWAYS ``str``.
  * the callable signature is a single positional ``body`` returning ``str``.
  * the ``InboundMapper`` Protocol declares the method, and both Jira-family backend
    ports delegate to the SAME free function (no divergent per-vendor decode).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from rebar_reconciler import inbound_fields
from rebar_reconciler._backend import InboundMapper
from rebar_reconciler.adapters.jira.backend import JiraBackend
from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend

pytestmark = pytest.mark.unit

normalize_rich_text = inbound_fields.normalize_rich_text

# A representative ADF document exercising a heading, bold (strong), and inline code —
# the three constructs the live DC harness asserts render, so the offline contract and
# the live fidelity check agree on what a rich body decodes to.
_ADF_DOC: dict[str, Any] = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "heading",
            "attrs": {"level": 1},
            "content": [{"type": "text", "text": "Title"}],
        },
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "bold", "marks": [{"type": "strong"}]},
                {"type": "text", "text": " and "},
                {"type": "text", "text": "code", "marks": [{"type": "code"}]},
            ],
        },
    ],
}
_ADF_DECODED = "# Title\nHello **bold** and `code`"


def test_none_decodes_to_empty_string() -> None:
    """``None`` yields ``""`` — never ``None`` — so callers can concatenate the result."""
    result = normalize_rich_text(None)
    assert result == ""
    assert isinstance(result, str)


def test_plain_string_passes_through_unchanged() -> None:
    """A plain ``str`` is the identity, so decoding already-decoded text is a no-op."""
    assert normalize_rich_text("plain text") == "plain text"
    assert normalize_rich_text("") == ""


def test_string_decode_is_idempotent() -> None:
    """Re-normalizing a normalized string returns it unchanged (defense-in-depth relies
    on this: the inbound apply path may normalize a value the differ already decoded)."""
    once = normalize_rich_text("already text")
    assert normalize_rich_text(once) == once


def test_non_string_non_dict_is_str_coerced() -> None:
    """A non-``None`` non-``dict`` payload is coerced with ``str(...)`` rather than raising."""
    assert normalize_rich_text(123) == "123"


def test_adf_dict_decodes_to_text() -> None:
    """An ADF ``dict`` decodes via ``adf_to_text`` to plain/markdown text."""
    result = normalize_rich_text(_ADF_DOC)
    assert result == _ADF_DECODED
    assert isinstance(result, str)


def test_return_type_is_always_str() -> None:
    """Every documented input variant returns a ``str``."""
    for payload in (None, "", "x", 42, _ADF_DOC, {}, {"type": "doc"}):
        assert isinstance(normalize_rich_text(payload), str)


def test_public_signature_is_single_positional_body_returning_str() -> None:
    """The callable takes exactly one required parameter and is annotated ``-> str``.

    An added required parameter or a changed return annotation would break the
    downstream import, so both are pinned.
    """
    sig = inspect.signature(normalize_rich_text)
    params = list(sig.parameters.values())
    assert len(params) == 1
    (body,) = params
    assert body.name == "body"
    assert body.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    assert body.default is inspect.Parameter.empty
    assert sig.return_annotation in ("str", str)


def test_inbound_mapper_protocol_declares_the_method() -> None:
    """The ``InboundMapper`` port declares ``normalize_rich_text`` — the name the
    downstream importer binds against."""
    assert hasattr(InboundMapper, "normalize_rich_text")
    assert callable(InboundMapper.normalize_rich_text)


@pytest.mark.parametrize("backend_cls", [JiraBackend, JiraDataCenterBackend])
def test_backend_ports_delegate_to_the_free_function(backend_cls: Any) -> None:
    """Both Jira-family backend ports expose ``.inbound.normalize_rich_text`` and decode
    IDENTICALLY to the free function — there is no divergent per-vendor rich-text decode
    for a downstream consumer to trip over.
    """
    inbound = backend_cls(transport=None).inbound
    assert inbound.normalize_rich_text(_ADF_DOC) == normalize_rich_text(_ADF_DOC)
    assert inbound.normalize_rich_text(None) == ""
    assert inbound.normalize_rich_text("plain") == "plain"
