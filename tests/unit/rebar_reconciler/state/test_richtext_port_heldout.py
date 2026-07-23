"""Ticket 21ca (HELD-OUT edge oracle): rich-text/limit port edges + neutrality sweep.

Withheld from the implementer: the byte-identical over-limit truncation, the decode
edges, the bug 1bb2-5da5 defense (a raw ADF dict never survives as a dict), and the
package-root literal-key sweep with its single recorded inbound_fields.py exemption.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar_reconciler.adapters.jira import adf, comment_limits
from rebar_reconciler.adapters.jira.backend import JiraBackend

pytestmark = pytest.mark.unit

_REC = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine" / "rebar_reconciler"

_ADF_HELLO = {
    "type": "doc",
    "version": 1,
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
}


def _backend() -> JiraBackend:
    return JiraBackend(transport=object())


# ── fit_comment truncates over-limit bodies byte-identically to the vendor rule ─
def test_fit_comment_truncates_over_limit_byte_identical() -> None:
    huge = "x" * 40_000  # well over Jira's 32,767-char comment limit
    fitted = _backend().sanitizer.fit_comment(huge)
    expected = comment_limits.truncate_comment_body(huge)
    assert fitted == expected  # byte-identical to the vendor fit rule
    assert len(fitted) < len(huge)  # actually truncated


# ── normalize_rich_text edges + bug 1bb2-5da5 defense ────────────────────────
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
