"""Corpus safety tests for the DC wiki renderer (story 271c, epic 708d).

These run the renderer over the vendored ``tests/fixtures/dc_wiki_corpus/`` snapshot
of real rebar prose — the punctuation-dense material pandoc's jira writer mishandles.
The corpus is committed (see ``scripts/build_dc_wiki_corpus.py``) so the counts below
are hermetic and reproducible without a live ticket store.

The claims are SAFETY claims, not fidelity claims: the DC path is one-way, so what
must hold is that nothing is corrupted and that rendering settles.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from rebar_reconciler.adapters.jira_family import wiki_render
from rebar_reconciler.adapters.jira_family.wiki_render import (
    code_fragments,
    render_markdown_to_wiki,
)

_CORPUS = Path(__file__).resolve().parents[2] / "fixtures" / "dc_wiki_corpus"

_PIPE_DELIM_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", re.M)
_BOX_RULE_RE = re.compile(r"^\s*\+[-+=]{2,}\+\s*$", re.M)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _load(name: str) -> list[str]:
    return json.loads((_CORPUS / f"{name}.json").read_text(encoding="utf-8"))


def _all_bodies() -> list[str]:
    return _load("code_arrow") + _load("table") + _load("prose")


def test_corpus_cardinality_is_pinned() -> None:
    """The fixture is FROZEN; a silent regeneration must fail here."""
    assert len(_load("code_arrow")) == 29
    assert len(_load("table")) == 29
    assert len(_load("prose")) == 120
    assert len(_all_bodies()) == 178


def test_corpus_carries_no_unscrubbed_secrets() -> None:
    """Re-assert the generator's scrub held, per the capture-fixture doctrine."""
    blob = "\n".join(_all_bodies())

    assert not re.search(r"[A-Za-z0-9._%+-]+@(?!example\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}", blob)
    assert ".atlassian.net" not in blob
    assert not set(re.findall(r"https?://[^\s)>\]]+", blob)) - {"https://example.invalid/redacted"}


def test_dc_corpus_protected_excerpts_are_retained() -> None:
    """The headline safety claim: code is content and never moves.

    Covers pandoc's escaping of punctuation inside code spans (``{{\\->}}``), which
    the renderer rejects via its post-conversion preservation check.
    """
    offenders: list[str] = []
    for body in _all_bodies():
        rendered = render_markdown_to_wiki(body)
        if any(fragment not in rendered for fragment in code_fragments(body)):
            offenders.append(body[:120])

    assert offenders == []


def test_dc_corpus_tables_keep_exact_bytes_over_five_passes() -> None:
    """Every table survives byte-exact, wrapped once, and does not erode."""
    table_bodies = [b for b in _all_bodies() if _PIPE_DELIM_RE.search(b) or _BOX_RULE_RE.search(b)]

    assert len(table_bodies) >= 29

    for body in table_bodies:
        rendered = render_markdown_to_wiki(body)
        assert _PIPE_DELIM_RE.search(rendered) or _BOX_RULE_RE.search(rendered)
        assert "\\-\\-" not in rendered
        settled = rendered
        for _ in range(4):
            settled = render_markdown_to_wiki(settled)
        assert settled == render_markdown_to_wiki(rendered)


def test_dc_corpus_html_comments_survive_exactly() -> None:
    """pandoc DELETES HTML comments; rebar's echo marker is one, so they must survive."""
    for body in _all_bodies():
        rendered = render_markdown_to_wiki(body)
        for marker in _HTML_COMMENT_RE.findall(body):
            assert marker in rendered


def test_dc_corpus_passes_two_to_five_are_byte_identical() -> None:
    """Rendering settles: no ratchet, no drift, across the whole corpus."""
    for body in _all_bodies():
        passes = [render_markdown_to_wiki(body)]
        for _ in range(4):
            passes.append(render_markdown_to_wiki(passes[-1]))
        assert passes[1] == passes[2] == passes[3] == passes[4]


def test_dc_corpus_coverage_ratios() -> None:
    """Richness floors, measured over the committed fixture.

    Floors, not equalities: the renderer may only get richer. A drop below either
    bar means eligible units silently started falling back.
    """
    bodies = _all_bodies()
    changed = [b for b in bodies if render_markdown_to_wiki(b) != b]

    body_ratio = len(changed) / len(bodies)
    char_ratio = sum(len(b) for b in changed) / sum(len(b) for b in bodies)

    assert body_ratio >= 0.90  # measured 0.916
    assert char_ratio >= 0.95  # measured 0.969


def test_dc_corpus_has_eligible_units_that_actually_change() -> None:
    """Guard against a vacuous pass: some unit must really be dispatched and changed."""
    pandoc = wiki_render._pandoc_path()
    eligible = 0
    changed = 0
    for body in _all_bodies():
        for kind, text in wiki_render._lock_and_split(body):
            if kind != wiki_render._RENDER:
                continue
            eligible += 1
            if wiki_render._render_unit(text, pandoc or "") != text:
                changed += 1

    assert eligible > 0
    assert changed > 0
