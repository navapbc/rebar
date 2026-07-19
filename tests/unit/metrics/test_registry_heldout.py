"""Held-out edge/segregation oracle for the metric registry (ticket c085).

WITHHELD from the implementation subagent. These assert the contracts that
separate a real registry from one that only satisfies the happy path:
- the ``source``/``confidence`` vocabularies are closed sets,
- ``is_authoritative`` segregates authoritative structural signals from
  ``backfill_classified`` (and from unknown sources),
- ``Unavailable.accruing_since`` is a real ISO-8601 timestamp,
- filtering a mixed list of values by ``is_authoritative(v.source)`` drops the
  classified/backfilled value — the segregation the epic AC requires.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from rebar.metrics.registry import (
    REGISTRY,
    MetricSpec,
    MetricValue,
    Unavailable,
    evaluate,
    is_authoritative,
)

pytestmark = pytest.mark.unit

# The closed vocabularies the ticket pins.
_SOURCES = {"structural", "git", "sidecar", "snapshot", "backfill_classified"}
_CONFIDENCES = {"high", "classified"}
_AUTHORITATIVE = {"structural", "git", "sidecar", "snapshot"}


def _spec(**overrides: object) -> MetricSpec:
    base: dict[str, object] = {
        "id": "sample_metric",
        "lens": "code_health",
        "source": "structural",
        "confidence": "high",
        "compute": lambda ctx: 1,
        "accruing_since": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return MetricSpec(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize("source", sorted(_AUTHORITATIVE))
def test_authoritative_sources_are_authoritative(source: str) -> None:
    assert is_authoritative(source) is True


def test_backfill_classified_is_not_authoritative() -> None:
    assert is_authoritative("backfill_classified") is False


@pytest.mark.parametrize("bogus", ["", "backfill_structural", "nonsense", "STRUCTURAL"])
def test_unknown_source_is_not_authoritative(bogus: str) -> None:
    # Defensive: anything outside the closed authoritative set is non-authoritative,
    # so an unclassified/typo'd source can never leak into an authoritative series.
    assert is_authoritative(bogus) is False


def test_registry_sources_and_confidence_in_vocabulary() -> None:
    for spec in REGISTRY:
        assert spec.source in _SOURCES, f"{spec.id}: source {spec.source!r} not in {_SOURCES}"
        assert spec.confidence in _CONFIDENCES, f"{spec.id}: confidence {spec.confidence!r}"


def test_backfill_specs_are_classified_confidence() -> None:
    # Any backfilled source must ride at 'classified' confidence and be non-authoritative
    # — the segregation guarantee stays internally consistent.
    for spec in REGISTRY:
        if spec.source == "backfill_classified":
            assert spec.confidence == "classified", f"{spec.id}: backfill must be classified"
            assert is_authoritative(spec.source) is False


def test_unavailable_accruing_since_is_iso8601() -> None:
    spec = _spec(
        id="iso_metric", compute=lambda ctx: None, accruing_since="2026-03-04T05:06:07+00:00"
    )
    result = evaluate(spec)
    assert isinstance(result, Unavailable)
    # Must parse as a real ISO-8601 instant, not an arbitrary string.
    parsed = datetime.fromisoformat(result.accruing_since)
    assert parsed.year == 2026 and parsed.month == 3 and parsed.day == 4


def test_classified_value_excluded_from_authoritative_rollup() -> None:
    values = [
        evaluate(_spec(id="a", source="structural", confidence="high", compute=lambda ctx: 1)),
        evaluate(_spec(id="b", source="git", confidence="high", compute=lambda ctx: 2)),
        evaluate(
            _spec(
                id="c",
                source="backfill_classified",
                confidence="classified",
                compute=lambda ctx: 3,
            )
        ),
    ]
    authoritative = [v for v in values if isinstance(v, MetricValue) and is_authoritative(v.source)]
    sources = {v.source for v in authoritative}
    assert "backfill_classified" not in sources
    assert sources == {"structural", "git"}
