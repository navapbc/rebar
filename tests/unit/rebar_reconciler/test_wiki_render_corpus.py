"""Corpus safety tests for the DC wiki renderer (story 271c, epic 708d).

These run the renderer over the vendored ``tests/fixtures/dc_wiki_corpus/`` snapshot
of real rebar prose — the punctuation-dense material pandoc's jira writer mishandles.
The corpus is committed (see ``scripts/build_dc_wiki_corpus.py``) so the counts below
are hermetic and reproducible without a live ticket store.

The claims are SAFETY claims, not fidelity claims: the DC path is one-way, so what
must hold is that nothing is corrupted and that rendering settles.

**Cost discipline.** Broad routine assertions drive the production segmenter through
committed exact Pandoc outputs. A missing prepared input fails rather than falling
back. Three representative bodies still traverse the installed real Pandoc in every
Verify run; complete real-binary replay belongs to External Integration Tests.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from rebar_reconciler.adapters.jira_family import wiki_render
from rebar_reconciler.adapters.jira_family.wiki_render import (
    code_fragments,
    render_markdown_to_wiki,
)

_CORPUS = Path(__file__).resolve().parents[2] / "fixtures" / "dc_wiki_corpus"
_REPLAY = Path(__file__).resolve().parents[2] / "fixtures" / "dc_wiki_replay"

_PIPE_DELIM_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", re.M)
_BOX_RULE_RE = re.compile(r"^\s*\+[-+=]{2,}\+\s*$", re.M)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

_STRATA = ("code_arrow", "table", "prose")
_PANDOC = wiki_render._pandoc_path()
_NEEDS_PANDOC = pytest.mark.skipif(_PANDOC is None, reason="the `wiki` extra is not installed")

_GENERATOR_SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "generate_dc_wiki_legacy_outputs.py"
)
_GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "generate_dc_wiki_replay", _GENERATOR_SCRIPT
)
assert _GENERATOR_SPEC is not None and _GENERATOR_SPEC.loader is not None
_GENERATOR = importlib.util.module_from_spec(_GENERATOR_SPEC)
_GENERATOR_SPEC.loader.exec_module(_GENERATOR)
_REPLAY_FIXTURES = _GENERATOR.load_replay_fixtures(_REPLAY)
_REPLAY_BY_STRATUM = {fixture["stratum"]: fixture for fixture in _REPLAY_FIXTURES}


def _load(name: str) -> list[str]:
    return json.loads((_CORPUS / f"{name}.json").read_text(encoding="utf-8"))


def _all_bodies() -> list[str]:
    return [body for name in _STRATA for body in _load(name)]


@pytest.fixture
def static_replay() -> Iterator[Any]:
    """Route product segmentation through committed outputs, never a subprocess."""
    converter = _GENERATOR.StaticReplayConverter(_REPLAY_FIXTURES)
    with (
        mock.patch.object(wiki_render, "_pandoc_path", return_value="committed-static-pandoc"),
        mock.patch.object(wiki_render, "_convert", converter),
    ):
        yield converter


@pytest.fixture
def corpus_pass1(static_replay: Any) -> tuple[tuple[str, str], ...]:
    """Every corpus body paired with its deterministic committed-output render."""
    del static_replay
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


def test_dc_corpus_protected_excerpts_are_retained(
    corpus_pass1: tuple[tuple[str, str], ...],
) -> None:
    """The headline safety claim: code is content and never moves.

    Covers pandoc's escaping of punctuation inside code spans (``{{\\->}}``), which
    the renderer rejects via its post-conversion preservation check.
    """
    offenders = [
        body[:120]
        for body, out in corpus_pass1
        if any(fragment not in out for fragment in code_fragments(body))
    ]

    assert offenders == []


def test_dc_corpus_tables_survive_verbatim(corpus_pass1: tuple[tuple[str, str], ...]) -> None:
    """Every ASCII table is still a table, un-eroded, after rendering."""
    tables = [
        (body, out)
        for body, out in corpus_pass1
        if _PIPE_DELIM_RE.search(body) or _BOX_RULE_RE.search(body)
    ]

    assert len(tables) >= 29

    for _body, out in tables:
        assert _PIPE_DELIM_RE.search(out) or _BOX_RULE_RE.search(out)
        assert "\\-\\-" not in out


def test_dc_corpus_html_comments_survive_exactly(
    corpus_pass1: tuple[tuple[str, str], ...],
) -> None:
    """pandoc DELETES HTML comments; rebar's echo marker is one, so they must survive."""
    for body, out in corpus_pass1:
        for marker in _HTML_COMMENT_RE.findall(body):
            assert marker in out


def test_render_is_deterministic(static_replay: Any) -> None:
    """The premise the cheap five-pass proof rests on: same input, same output."""
    del static_replay
    body = _load("prose")[0]

    assert render_markdown_to_wiki(body) == render_markdown_to_wiki(body)


@pytest.mark.parametrize("stratum", _STRATA)
def test_dc_corpus_passes_two_to_five_are_byte_identical(
    stratum: str,
    corpus_pass1: tuple[tuple[str, str], ...],
) -> None:
    """Rendering settles: no ratchet, no drift, across the whole corpus.

    Proven with TWO renders rather than five. The renderer is a pure deterministic
    function R (pinned by ``test_render_is_deterministic``), so once a body reaches a
    fixed point the rest of the sequence is forced: if ``R(p1) == p1`` then
    ``p3 = R(p2) = R(p1) = p2``, and likewise for p4 and p5. Where a body is NOT an
    immediate fixed point, one further render settles it and the same argument
    applies from there — so checking p2, and p3 only when needed, is equivalent to
    checking all five passes, at a fraction of the subprocess cost.

    Split per stratum so no single test carries the whole corpus past CI's per-test
    timeout. Pass 1 comes from the shared ``corpus_pass1`` artifact — it is the same
    ``render_markdown_to_wiki(body)`` value this test used to recompute — so only the
    passes this test actually adds are paid for here.
    """
    pass1 = dict(corpus_pass1)
    for body in _load(stratum):
        first = pass1[body]
        second = render_markdown_to_wiki(first)
        if second == first:
            continue  # fixed point: passes 2-5 are all `first` by determinism
        third = render_markdown_to_wiki(second)
        assert third == second, "rendering did not settle by pass 3"


def test_dc_corpus_coverage_ratios(corpus_pass1: tuple[tuple[str, str], ...]) -> None:
    """Richness floors, measured over the committed fixture.

    Floors, not equalities: the renderer may only get richer. A drop below either bar
    means eligible units silently started falling back.
    """
    pairs = corpus_pass1
    changed = [body for body, out in pairs if out != body]

    body_ratio = len(changed) / len(pairs)
    char_ratio = sum(len(b) for b in changed) / sum(len(b) for b, _ in pairs)

    assert body_ratio >= 0.90  # measured 0.916
    assert char_ratio >= 0.95  # measured 0.969


def _required_passes(body: str) -> list[str]:
    first = render_markdown_to_wiki(body)
    second = render_markdown_to_wiki(first)
    outputs = [first, second]
    if second != first:
        third = render_markdown_to_wiki(second)
        assert third == second
        outputs.append(third)
    return outputs


@pytest.mark.parametrize("stratum", _STRATA)
def test_static_replay_covers_every_required_conversion_and_exact_body_output(
    stratum: str,
    static_replay: Any,
) -> None:
    fixture = _REPLAY_BY_STRATUM[stratum]
    bodies = _load(stratum)
    expected_bodies = fixture["bodies"]
    assert len(expected_bodies) == len(bodies)

    for body, expected in zip(bodies, expected_bodies, strict=True):
        assert expected["source_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
        outputs = _required_passes(body)
        assert expected["pass_output_sha256"] == [
            hashlib.sha256(output.encode("utf-8")).hexdigest() for output in outputs
        ]

    assert static_replay.calls == fixture["conversion_trace"]


@_NEEDS_PANDOC
@pytest.mark.parametrize(
    ("stratum", "body_index"),
    [("code_arrow", 0), ("table", 2), ("prose", 0)],
)
def test_live_pandoc_representative_body_matches_committed_passes(
    stratum: str,
    body_index: int,
) -> None:
    """Three evidence-selected bodies keep the real product boundary in Verify."""
    body = _load(stratum)[body_index]
    expected = _REPLAY_BY_STRATUM[stratum]["bodies"][body_index]
    real_convert = wiki_render._convert
    preservation_fallbacks = 0
    conversion_calls = 0

    def spy(markdown: str, pandoc: str, timeout: float | None = None) -> str | None:
        nonlocal conversion_calls, preservation_fallbacks
        conversion_calls += 1
        converted = real_convert(markdown, pandoc, timeout)
        if converted is not None:
            cursor = 0
            for fragment in code_fragments(markdown):
                position = converted.find(fragment, cursor)
                if position < 0:
                    preservation_fallbacks += 1
                    break
                cursor = position + len(fragment)
        return converted

    with mock.patch.object(wiki_render, "_convert", spy):
        outputs = _required_passes(body)

    assert conversion_calls > 0
    assert expected["source_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert expected["pass_output_sha256"] == [
        hashlib.sha256(output.encode("utf-8")).hexdigest() for output in outputs
    ]
    if stratum == "code_arrow":
        assert preservation_fallbacks > 0
    elif stratum == "table":
        assert "{noformat}" in outputs[0]
        assert len(outputs) == 3
    else:
        assert outputs[0] != body
