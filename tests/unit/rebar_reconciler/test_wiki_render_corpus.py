"""Corpus safety tests for the DC wiki renderer (story 271c, epic 708d).

These run the renderer over the vendored ``tests/fixtures/dc_wiki_corpus/`` snapshot
of real rebar prose — the punctuation-dense material pandoc's jira writer mishandles.
The corpus is committed (see ``scripts/build_dc_wiki_corpus.py``) so the counts below
are hermetic and reproducible without a live ticket store.

The claims are SAFETY claims, not fidelity claims: the DC path is one-way, so what
must hold is that nothing is corrupted and that rendering settles.

**Cost discipline.** Rendering the corpus once costs ~884 pandoc subprocess spawns
(~33s). CI runs the suite under ``-n 3 --timeout=300``, so a test that rendered the
corpus five times exceeded the per-test timeout and crashed its xdist worker. Two
things keep this module cheap without weakening any assertion:

* every test shares ONE cached render of the corpus (:func:`_rendered`), instead of
  re-rendering per test; and
* five-pass identity is proven with TWO renders rather than five — see
  :func:`test_dc_corpus_passes_two_to_five_are_byte_identical`.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import pytest

from rebar_reconciler.adapters.jira_family import wiki_render
from rebar_reconciler.adapters.jira_family.wiki_render import (
    code_fragments,
    render_markdown_to_wiki,
)

_CORPUS = Path(__file__).resolve().parents[2] / "fixtures" / "dc_wiki_corpus"

_PIPE_DELIM_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", re.M)
_BOX_RULE_RE = re.compile(r"^\s*\+[-+=]{2,}\+\s*$", re.M)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

_STRATA = ("code_arrow", "table", "prose")


def _load(name: str) -> list[str]:
    return json.loads((_CORPUS / f"{name}.json").read_text(encoding="utf-8"))


def _all_bodies() -> list[str]:
    return [body for name in _STRATA for body in _load(name)]


@lru_cache(maxsize=1)
def _rendered() -> tuple[tuple[str, str], ...]:
    """Every corpus body paired with its first-pass render, computed ONCE.

    Cached because a full corpus render is ~884 pandoc spawns; re-rendering per test
    is what pushed this module past CI's per-test timeout.
    """
    return tuple((body, render_markdown_to_wiki(body)) for body in _all_bodies())


def test_corpus_cardinality_is_pinned() -> None:
    """The fixture is FROZEN; a silent regeneration must fail here."""
    assert len(_load("code_arrow")) == 29
    assert len(_load("table")) == 29
    assert len(_load("prose")) == 120
    assert len(_all_bodies()) == 178


# NOTE: there is deliberately NO test here asserting the corpus is free of the repo's
# RETIRED vocabulary (the old bridge command spellings, the old force-close flag). The
# generator's scrub maps them, and two repo-wide guards already scan EVERY tracked file
# — including this fixture — for exactly those spellings
# (`test_bridge_vocabulary_stale_heldout` and `test_transition_force_flag_24f7`). A local
# copy would be weaker than those, and it could only be written by spelling the retired
# tokens out, which makes THIS file an offender the guards then flag.
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
    offenders = [
        body[:120]
        for body, out in _rendered()
        if any(fragment not in out for fragment in code_fragments(body))
    ]

    assert offenders == []


def test_dc_corpus_tables_survive_verbatim() -> None:
    """Every ASCII table is still a table, un-eroded, after rendering."""
    tables = [
        (body, out)
        for body, out in _rendered()
        if _PIPE_DELIM_RE.search(body) or _BOX_RULE_RE.search(body)
    ]

    assert len(tables) >= 29

    for _body, out in tables:
        assert _PIPE_DELIM_RE.search(out) or _BOX_RULE_RE.search(out)
        assert "\\-\\-" not in out


def test_dc_corpus_html_comments_survive_exactly() -> None:
    """pandoc DELETES HTML comments; rebar's echo marker is one, so they must survive."""
    for body, out in _rendered():
        for marker in _HTML_COMMENT_RE.findall(body):
            assert marker in out


def test_render_is_deterministic() -> None:
    """The premise the cheap five-pass proof rests on: same input, same output."""
    body = "# T\n\nprose -> arrow with **bold**\n\n- a\n- b\n"

    assert render_markdown_to_wiki(body) == render_markdown_to_wiki(body)


@pytest.mark.parametrize("stratum", _STRATA)
def test_dc_corpus_passes_two_to_five_are_byte_identical(stratum: str) -> None:
    """Rendering settles: no ratchet, no drift, across the whole corpus.

    Proven with TWO renders rather than five. The renderer is a pure deterministic
    function R (pinned by ``test_render_is_deterministic``), so once a body reaches a
    fixed point the rest of the sequence is forced: if ``R(p1) == p1`` then
    ``p3 = R(p2) = R(p1) = p2``, and likewise for p4 and p5. Where a body is NOT an
    immediate fixed point, one further render settles it and the same argument
    applies from there — so checking p2, and p3 only when needed, is equivalent to
    checking all five passes, at a fraction of the subprocess cost.

    Split per stratum so no single test carries the whole corpus past CI's per-test
    timeout.
    """
    for body in _load(stratum):
        first = render_markdown_to_wiki(body)
        second = render_markdown_to_wiki(first)
        if second == first:
            continue  # fixed point: passes 2-5 are all `first` by determinism
        third = render_markdown_to_wiki(second)
        assert third == second, "rendering did not settle by pass 3"


def test_dc_corpus_coverage_ratios() -> None:
    """Richness floors, measured over the committed fixture.

    Floors, not equalities: the renderer may only get richer. A drop below either bar
    means eligible units silently started falling back.
    """
    pairs = _rendered()
    changed = [body for body, out in pairs if out != body]

    body_ratio = len(changed) / len(pairs)
    char_ratio = sum(len(b) for b in changed) / sum(len(b) for b, _ in pairs)

    assert body_ratio >= 0.90  # measured 0.916
    assert char_ratio >= 0.95  # measured 0.969


def test_dc_corpus_has_eligible_units_that_actually_change() -> None:
    """Guard against a vacuous pass: eligible units must really be dispatched.

    Uses one stratum, not the whole corpus — the claim is existential, so paying for
    a second full-corpus render to prove it would be waste.
    """
    pandoc = wiki_render._pandoc_path()
    eligible = 0
    changed = 0
    for body in _load("prose"):
        for kind, text in wiki_render._lock_and_split(body):
            if kind != wiki_render._RENDER:
                continue
            eligible += 1
            if wiki_render._render_unit(text, pandoc or "") != text:
                changed += 1

    assert eligible > 0
    assert changed > 0
