"""Cloud stores ADF documents, while Data Center stores wiki text.

RichTextCodec separates fitting, round-trip normalization, wire encoding, and
inbound decoding. Cloud fitting measures serialized ADF. The Cloud send path
uses fitting and normalization, while the description sanitizer fits only."""

from __future__ import annotations

from typing import Any

import pytest

from rebar.reconciler.adapters.jira.rich_text_codec import AdfCodec
from rebar.reconciler.adapters.jira_family.rich_text import (
    WIKI_DESCRIPTION_LIMIT,
    WikiTextCodec,
)

_CODECS = [
    pytest.param(AdfCodec, id="cloud-adf"),
    pytest.param(WikiTextCodec, id="datacenter-wiki"),
]


# ---------------------------------------------------------------------------
# The shared contract — both codecs must satisfy every assertion here
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", _CODECS)
def test_short_text_survives_the_full_outbound_inbound_round_trip(factory: Any) -> None:
    """The interchangeability property: a short, already-normalized string comes
    back out of the round trip unchanged for EITHER deployment."""
    codec = factory()
    text = "Add the widget to the dashboard."

    wire = codec.to_wire(codec.normalize_outbound(codec.fit_outbound(text)))

    assert codec.decode_inbound(wire) == text


@pytest.mark.parametrize("factory", _CODECS)
def test_text_within_the_limit_is_returned_unchanged_and_unmarked(factory: Any) -> None:
    """Contrast case for truncation: nothing is appended to a value that fits."""
    codec = factory()
    text = "short enough"

    assert codec.fit_outbound(text) == text
    assert "truncated" not in codec.fit_outbound(text)


# Both codecs use the same visible truncation marker. Keep the wiki and ADF
# constants equal without importing Cloud code into the shared layer.
_TRUNCATION_MARKER = " … [truncated by reconciler]"


@pytest.mark.parametrize("factory", _CODECS)
def test_over_limit_text_is_fitted_and_marked(factory: Any) -> None:
    """Fitting an over-limit value keeps a terminal marker within the cap."""
    codec = factory()
    fitted = codec.fit_outbound("x" * 200_000)

    assert len(fitted) < 200_000
    assert fitted.endswith(_TRUNCATION_MARKER), (
        f"fitted value must END with the truncation marker; got tail {fitted[-40:]!r}"
    )
    assert codec.decode_inbound(codec.to_wire(fitted)).startswith("x")


def test_wiki_over_limit_value_ends_with_the_marker_and_fits_the_cap() -> None:
    """Wiki fitting keeps the terminal marker within WIKI_DESCRIPTION_LIMIT."""
    fitted = WikiTextCodec().fit_outbound("w" * (WIKI_DESCRIPTION_LIMIT + 10_000))

    assert fitted.endswith(_TRUNCATION_MARKER)
    assert len(fitted) <= WIKI_DESCRIPTION_LIMIT


def test_adf_over_limit_value_ends_with_the_marker_within_the_serialized_cap() -> None:
    """ADF fitting keeps the terminal marker inside the serialized-document cap."""
    import json

    from rebar.reconciler.adapters.jira import adf

    fitted = AdfCodec().fit_outbound("z" * 200_000)

    assert fitted.endswith(_TRUNCATION_MARKER)
    assert len(json.dumps(adf.text_to_adf(fitted))) <= adf._ADF_DESCRIPTION_LIMIT


@pytest.mark.parametrize("factory", _CODECS)
def test_fit_is_idempotent(factory: Any) -> None:
    """Applying the fit twice equals applying it once — load-bearing for
    convergence, since the differ re-applies it to the local value every pass."""
    codec = factory()
    once = codec.fit_outbound("y" * 200_000)

    assert codec.fit_outbound(once) == once


# The comment differ compares decoded Jira wire output with normalized local
# text. Their equality prevents duplicate comments and lost Jira formatting.
# normalize_outbound(t) == decode_inbound(to_wire(t)).

_CODEC_LAW_BODIES = [
    pytest.param("a single short line", id="short-line"),
    pytest.param("one\ntwo\nthree", id="soft-wrapped"),
    pytest.param("```python\ndef f(x):\n    return x * 2\n```", id="code-fence"),
    pytest.param("- first item\n- second item\n* third (asterisk)", id="bullet-list"),
    pytest.param("this is **bold** and this is *emphasis*", id="bold-and-emphasis"),
    pytest.param("a pipe table cell: `a | b | c`", id="literal-pipe-backtick"),
]


@pytest.mark.parametrize("factory", _CODECS)
@pytest.mark.parametrize("text", _CODEC_LAW_BODIES)
def test_normalize_outbound_equals_decode_of_to_wire(factory: Any, text: str) -> None:
    """Every codec preserves the law normalize_outbound(t) == decode_inbound(to_wire(t))."""
    codec = factory()

    assert codec.normalize_outbound(text) == codec.decode_inbound(codec.to_wire(text))


@pytest.mark.parametrize("factory", _CODECS)
def test_decode_inbound_of_none_is_empty_string(factory: Any) -> None:
    assert factory().decode_inbound(None) == ""


@pytest.mark.parametrize("factory", _CODECS)
def test_non_string_values_pass_through_untouched(factory: Any) -> None:
    """The mapper must never coerce a non-``str`` description; today's behaviour
    passes it through, and both codecs preserve that."""
    codec = factory()
    sentinel = {"already": "shaped"}

    assert codec.fit_outbound(sentinel) is sentinel  # type: ignore[arg-type]
    assert codec.normalize_outbound(sentinel) is sentinel  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cloud-specific — ADF
# ---------------------------------------------------------------------------


def test_adf_codec_is_a_pass_through_to_the_pinned_adf_module() -> None:
    """``AdfCodec`` WRAPS ``adapters/jira/adf.py``; it must not reimplement it."""
    from rebar.reconciler.adapters.jira import adf

    codec = AdfCodec()
    text = "some prose that is well within every limit"

    assert codec.fit_outbound(text) == adf.fit_text_to_adf_limit(text)
    assert codec.normalize_outbound(text) == adf.normalize_description(text)
    assert codec.to_wire(text) == adf.text_to_adf(text)
    assert codec.decode_inbound(adf.text_to_adf(text)) == adf.adf_to_text(adf.text_to_adf(text))


def test_adf_to_wire_produces_an_adf_document_not_a_string() -> None:
    """Cloud REST v3 rejects a plain string for ``description`` with a 400."""
    wire = AdfCodec().to_wire("hello")

    assert isinstance(wire, dict)
    assert wire.get("type") == "doc"


def test_adf_fit_measures_the_serialized_document_not_the_plain_text() -> None:
    """The whole reason Cloud cannot use a plain character cap: ADF structure
    inflates the payload, so the limit applies to the serialized document."""
    import json

    from rebar.reconciler.adapters.jira import adf

    fitted = AdfCodec().fit_outbound("z" * 100_000)

    assert len(json.dumps(adf.text_to_adf(fitted))) <= adf._ADF_DESCRIPTION_LIMIT


# ---------------------------------------------------------------------------
# Data Center specific — wiki / plain text
# ---------------------------------------------------------------------------


def test_wiki_codec_truncates_at_its_character_limit() -> None:
    """DC's REST v2 carries plain text, so a plain CHARACTER cap is correct here —
    unlike Cloud, there is no document serialization to measure."""
    fitted = WikiTextCodec().fit_outbound("w" * (WIKI_DESCRIPTION_LIMIT + 5_000))

    assert len(fitted) <= WIKI_DESCRIPTION_LIMIT


def test_wiki_codec_wire_shape_and_decode_are_the_plain_string() -> None:
    codec = WikiTextCodec()

    assert codec.to_wire("h2. Heading") == "h2. Heading"
    assert codec.decode_inbound("h2. Heading") == "h2. Heading"


def test_wiki_normalize_is_identity() -> None:
    """Wiki markup has no soft-wrap rejoin, so normalization is a no-op — but the
    operation still EXISTS so the two codecs are interchangeable."""
    text = "line one\nline two"

    assert WikiTextCodec().normalize_outbound(text) == text
