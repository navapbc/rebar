"""The ``RichTextCodec`` contract (story J3, epic e369).

Rich-text encoding is one of the three real Cloud/Data-Center differences: Jira
Cloud's REST v3 requires Atlassian Document Format (a nested JSON document;
sending a plain string returns 400), while Data Center's REST v2 carries
descriptions as plain text / wiki markup.

The contract has FOUR operations, because Cloud's existing behaviour needs all
four kept distinct:

* ``fit_outbound(text)``      — fit to the deployment's limit (Cloud measures the
                                ADF SERIALIZATION, not the plain text)
* ``normalize_outbound(text)``— render the value as it will read after a round
                                trip (Cloud rejoins soft wraps; DC is identity)
* ``to_wire(text)``           — the wire shape (Cloud: an ADF dict; DC: the str)
* ``decode_inbound(body)``    — wire shape back to plain text

``fit_outbound`` and ``normalize_outbound`` are separate on purpose: the Cloud
send path composes BOTH, while the description sanitizer applies only the fit.
Collapsing them would silently change one of those two callers.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler.adapters.jira.rich_text_codec import AdfCodec
from rebar_reconciler.adapters.jira_family.rich_text import (
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


@pytest.mark.parametrize("factory", _CODECS)
def test_over_limit_text_is_fitted_and_marked(factory: Any) -> None:
    """An over-length value is truncated, carries a visible marker so a Jira
    reader knows, and the marker is counted against the limit rather than pushing
    the value over it."""
    codec = factory()
    fitted = codec.fit_outbound("x" * 200_000)

    assert len(fitted) < 200_000
    assert "truncated" in fitted
    assert codec.decode_inbound(codec.to_wire(fitted)).startswith("x")


@pytest.mark.parametrize("factory", _CODECS)
def test_fit_is_idempotent(factory: Any) -> None:
    """Applying the fit twice equals applying it once — load-bearing for
    convergence, since the differ re-applies it to the local value every pass."""
    codec = factory()
    once = codec.fit_outbound("y" * 200_000)

    assert codec.fit_outbound(once) == once


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
    from rebar_reconciler.adapters.jira import adf

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

    from rebar_reconciler.adapters.jira import adf

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
