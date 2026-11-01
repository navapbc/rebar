"""Contract tests for metric registry uniqueness and dispatch.

``REGISTRY`` contains unique ``MetricSpec.id`` values. ``evaluate`` wraps computed data in
``MetricValue`` with the spec's source and confidence labels. A ``None`` result becomes
``Unavailable`` with its reason and ``accruing_since`` value.

Held-out oracles merged from test_registry_heldout: Held-out edge/segregation oracle for the
metric registry (ticket c085).
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


def _spec(**overrides: object) -> MetricSpec:
    """Build a MetricSpec with sensible defaults for the field(s) under test."""
    base: dict[str, object] = {
        "id": "sample_metric",
        "lens": "code_health",
        "source": "structural",
        "confidence": "high",
        "compute": lambda ctx: 1,
        "accruing_since": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return MetricSpec(**base)


def test_registry_metric_ids_are_unique() -> None:
    ids = [spec.id for spec in REGISTRY]
    assert ids, "REGISTRY must contain at least one MetricSpec"
    assert len(ids) == len(set(ids)), f"duplicate metric ids in REGISTRY: {ids}"


def test_registry_specs_are_fully_labeled() -> None:
    for spec in REGISTRY:
        assert spec.id, "every spec needs a non-empty id"
        assert spec.lens, f"{spec.id}: missing lens"
        assert spec.source, f"{spec.id}: missing source"
        assert spec.confidence, f"{spec.id}: missing confidence"


def test_no_data_yields_unavailable() -> None:
    spec = _spec(id="no_data_metric", compute=lambda ctx: None)
    result = evaluate(spec)
    assert isinstance(result, Unavailable)
    assert result.reason, "Unavailable must carry a non-empty reason"
    assert result.accruing_since, "Unavailable must carry accruing_since"


def test_data_yields_metric_value_carrying_labels() -> None:
    spec = _spec(id="has_data_metric", source="git", confidence="high", compute=lambda ctx: 42)
    result = evaluate(spec)
    assert isinstance(result, MetricValue)
    assert result.value == 42
    # The source/confidence labels ride with the value for downstream segregation.
    assert result.source == "git"
    assert result.confidence == "high"


# ── held-out oracles (merged from test_registry_heldout) ──

_SOURCES = {"structural", "git", "sidecar", "snapshot", "backfill_classified"}


_CONFIDENCES = {"high", "classified"}


_AUTHORITATIVE = {"structural", "git", "sidecar", "snapshot"}


def _spec_heldout(**overrides: object) -> MetricSpec:
    base: dict[str, object] = {
        "id": "sample_metric",
        "lens": "code_health",
        "source": "structural",
        "confidence": "high",
        "compute": lambda ctx: 1,
        "accruing_since": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return MetricSpec(**base)


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
    spec = _spec_heldout(
        id="iso_metric", compute=lambda ctx: None, accruing_since="2026-03-04T05:06:07+00:00"
    )
    result = evaluate(spec)
    assert isinstance(result, Unavailable)
    # Must parse as a real ISO-8601 instant, not an arbitrary string.
    parsed = datetime.fromisoformat(result.accruing_since)
    assert parsed.year == 2026 and parsed.month == 3 and parsed.day == 4


def test_classified_value_excluded_from_authoritative_rollup() -> None:
    values = [
        evaluate(
            _spec_heldout(id="a", source="structural", confidence="high", compute=lambda ctx: 1)
        ),
        evaluate(_spec_heldout(id="b", source="git", confidence="high", compute=lambda ctx: 2)),
        evaluate(
            _spec_heldout(
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
