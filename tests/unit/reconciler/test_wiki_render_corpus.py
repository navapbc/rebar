"""The committed Data Center wiki corpus checks rendering safety and convergence.

Protected code excerpts remain in place, later passes settle, and richness
floors prevent trivial output. Routine tests compare committed Pandoc replay
bytes. Representative product-path tests still use the installed binary."""

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

from rebar.reconciler.adapters.jira_family import wiki_render
from rebar.reconciler.adapters.jira_family.wiki_render import (
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


# Repository-wide guards already check this fixture for retired vocabulary.
# Repeating those tokens here would itself violate those guards.
def test_corpus_carries_no_unscrubbed_secrets() -> None:
    """Re-assert the generator's scrub held, per the capture-fixture doctrine."""
    blob = "\n".join(_all_bodies())

    assert not re.search(r"[A-Za-z0-9._%+-]+@(?!example\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}", blob)
    assert ".atlassian.net" not in blob
    assert not set(re.findall(r"https?://[^\s)>\]]+", blob)) - {"https://example.invalid/redacted"}


def test_dc_corpus_protected_excerpts_are_retained(
    corpus_pass1: tuple[tuple[str, str], ...],
) -> None:
    """Code excerpts remain content and keep their position in rendered corpus bodies."""
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
    """Corpus render passes two through five remain byte-identical without drift."""
    pass1 = dict(corpus_pass1)
    for body in _load(stratum):
        first = pass1[body]
        second = render_markdown_to_wiki(first)
        if second == first:
            continue  # fixed point: passes 2-5 are all `first` by determinism
        third = render_markdown_to_wiki(second)
        assert third == second, "rendering did not settle by pass 3"


def test_dc_corpus_coverage_ratios(corpus_pass1: tuple[tuple[str, str], ...]) -> None:
    """The committed corpus retains required structure and content coverage floors."""
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
    [("code_arrow", 0), ("table", 4), ("prose", 0)],
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
