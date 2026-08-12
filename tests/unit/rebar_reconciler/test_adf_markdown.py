"""Markdown-aware Cloud ADF conversion tests (story e59d, epic 708d).

These cover the three new pure functions in ``adapters/jira/adf.py`` plus the
whole-codec plain fallback helper. The functions are NOT wired into ``AdfCodec`` or
any live send path in this story — that cutover is story 3388 — so the existing
``AdfCodec`` pass-through pin must stay green untouched.

The corpus assertions run over the committed, scrubbed
``tests/fixtures/cloud_adf_corpus/`` snapshot (see
``scripts/build_cloud_adf_corpus.py``), so they are hermetic and reproducible
without a live ticket store.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rebar_reconciler.adapters.jira import adf

_CORPUS = Path(__file__).resolve().parents[2] / "fixtures" / "cloud_adf_corpus"

# A body counts as structurally rich iff its ADF holds a non-paragraph BLOCK node.
# Inline marks alone do NOT qualify — see the story's explicit predicate.
_BLOCK_NODES = {
    "heading",
    "bulletList",
    "orderedList",
    "codeBlock",
    "taskList",
    "blockquote",
    "rule",
    "table",
    "panel",
}

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

pytestmark = pytest.mark.skipif(
    adf._marklas() is None,
    reason="the `adf` extra is not installed; the functions degrade to plain text "
    "(that degradation is covered by test_cloud_functions_degrade_without_marklas)",
)


def _corpus_bodies() -> list[str]:
    bodies: list[str] = []
    for shard in sorted(_CORPUS.glob("bodies_*.json")):
        bodies.extend(json.loads(shard.read_text(encoding="utf-8")))
    return bodies


def _roundtrip(md: str) -> str:
    return adf.adf_to_markdown(adf.markdown_to_adf(md))


# ---------------------------------------------------------------------------
# Unit
# ---------------------------------------------------------------------------


def test_cloud_adf_roundtrip() -> None:
    """Markdown structure encodes to ADF NODES, not literal ``#``/``-``/``**``."""
    md = "# Title\n\n- [ ] task\n- item **bold** and `code`\n\n```py\nx = 1\n```\n"

    doc = adf.markdown_to_adf(md)
    kinds = [node["type"] for node in doc["content"]]

    assert doc["type"] == "doc"
    assert "heading" in kinds
    assert "codeBlock" in kinds
    assert kinds != ["paragraph"] * len(kinds)  # the plain encode's shape
    # And the decode returns the source Markdown's content.
    decoded = adf.adf_to_markdown(doc)
    assert "# Title" in decoded
    assert "**bold**" in decoded
    assert "x = 1" in decoded


def test_cloud_mark_order_converges() -> None:
    """The measured combined-mark 2-cycle must be gone.

    Without a canonical mark order, ``A **~~both~~** end`` and ``A ~~**both**~~ end``
    alternate forever — an exact 2-cycle that never reaches a fixed point.
    """
    body = "A **~~both~~** end\n"

    first = _roundtrip(body)
    for _ in range(4):
        assert _roundtrip(first) == first


def test_cloud_html_comment_survives() -> None:
    """The engine drops HTML comments; rebar's echo marker is one, so it must survive."""
    marker = "<!-- rebar:reconciler-echo -->"

    assert marker in _roundtrip(f"text {marker} more\n")
    assert marker in _roundtrip(f"{marker}\n")


def test_adf_fit_measures_the_serialized_document_not_the_plain_text() -> None:
    """The Markdown-aware wire inflates differently, so the fit must measure IT."""
    fitted = adf.fit_markdown_to_adf_limit("z" * 100_000)

    assert len(json.dumps(adf.markdown_to_adf(fitted))) <= adf._ADF_DESCRIPTION_LIMIT
    # Idempotent: an already-fitting value is returned unchanged.
    assert adf.fit_markdown_to_adf_limit("short body") == "short body"


def test_cloud_functions_degrade_without_marklas(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the extra absent the functions return PLAIN results and never raise."""
    monkeypatch.setattr(adf, "_marklas", lambda: None)
    md = "# Title\n\n- item\n"

    assert adf.markdown_to_adf(md) == adf.text_to_adf(md)
    assert adf.adf_to_markdown(adf.text_to_adf(md)) == adf.adf_to_text(adf.text_to_adf(md))
    assert adf.fit_markdown_to_adf_limit(md) == md


def test_cloud_plain_fallback_helper_returns_plain_functions() -> None:
    """Story 3388 installs this set atomically, so it must be the PLAIN functions."""
    functions = adf.plain_text_adf_functions()

    assert functions["to_adf"] is adf.text_to_adf
    assert functions["to_text"] is adf.adf_to_text
    assert functions["fit"] is adf.fit_text_to_adf_limit
    assert functions["normalize"] is adf.normalize_description


def test_adf_codec_pass_through_is_untouched_by_this_story() -> None:
    """This story wires nothing: ``AdfCodec`` must still be a plain pass-through."""
    from rebar_reconciler.adapters.jira.rich_text_codec import AdfCodec

    text = "# Heading\n\n- item\n"

    assert AdfCodec().to_wire(text) == adf.text_to_adf(text)


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def test_cloud_corpus_cardinality_is_pinned() -> None:
    """The fixture is FROZEN; a silent regeneration must fail here."""
    assert len(_corpus_bodies()) == 360


def test_cloud_corpus_carries_no_unscrubbed_secrets() -> None:
    """Re-assert the generator's scrub held, per the capture-fixture doctrine."""
    blob = "\n".join(_corpus_bodies())

    assert not re.search(r"[A-Za-z0-9._%+-]+@(?!example\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}", blob)
    assert ".atlassian.net" not in blob
    assert not set(re.findall(r"https?://[^\s)>\]]+", blob)) - {"https://example.invalid/redacted"}


def test_cloud_corpus_idempotence() -> None:
    """Re-encoding a body must not churn the wire: a fixed point after one pass."""
    bodies = _corpus_bodies()
    stable = 0
    for body in bodies:
        once = _roundtrip(body)
        if _roundtrip(once) == once:
            stable += 1

    assert stable / len(bodies) >= 0.996  # measured 100.00%


def test_cloud_corpus_richness() -> None:
    """A floor, not an equality: the encode may only get richer."""
    bodies = _corpus_bodies()
    rich = [
        body
        for body in bodies
        if {node.get("type") for node in adf.markdown_to_adf(body).get("content", [])}
        & _BLOCK_NODES
    ]

    assert len(rich) / len(bodies) >= 0.15  # measured 15.6%


def test_cloud_corpus_html_comments_survive() -> None:
    """Every HTML comment in the committed corpus survives the round-trip."""
    for body in _corpus_bodies():
        for marker in _HTML_COMMENT_RE.findall(body):
            assert marker in _roundtrip(body)
