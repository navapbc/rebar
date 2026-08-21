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
from rebar_reconciler.adapters.jira.rich_text_codec import AdfCodec
from rebar_reconciler.adapters.jira_family.rich_text import cutover_clients
from rebar_reconciler.outbound_comments import (
    _decorate_outbound_comment,
    fit_preserving_marker,
)

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
    """The vendor fit rule is what the SEND PATH lands, not a standalone cap.

    Retargeted under bug e339-9709-15fe-419a. This oracle originally compared
    ``fit_comment`` against ``comment_limits.truncate_comment_body`` — correct
    when ``acli_cli_ops.add_comment`` still fitted with that same helper, stale
    since commit ``27b868ba55`` moved the send path onto
    ``fit_preserving_marker(body, AdfCodec.fit_outbound)``, which measures the
    SERIALIZED ADF and reserves budget for ``RECONCILER_MARKER``. Pinning the
    superseded helper pinned the defect: the differ's dedup key could never equal
    the landed body, so every over-length comment re-posted on every pass.

    The expectation is therefore rebuilt from the send path itself rather than
    from any one fitter, so it cannot go stale the same way again.
    """
    huge = "x" * 40_000  # well over Jira's comment limit on either measure
    fitted = _backend().sanitizer.fit_comment(huge)

    codec = AdfCodec(rich="cloud" in cutover_clients())
    landed = fit_preserving_marker(_decorate_outbound_comment(huge), codec.fit_outbound)
    decoration_len = len(_decorate_outbound_comment(""))
    expected = landed[: len(landed) - decoration_len]

    assert fitted == expected  # byte-identical to what the send path lands
    assert len(fitted) < len(huge)  # actually truncated
    assert fitted != comment_limits.truncate_comment_body(huge)  # and not the old cap


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
