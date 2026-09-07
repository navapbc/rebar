"""Contract tests for metric registry uniqueness and dispatch.

``REGISTRY`` contains unique ``MetricSpec.id`` values. ``evaluate`` wraps computed data in
``MetricValue`` with the spec's source and confidence labels. A ``None`` result becomes
``Unavailable`` with its reason and ``accruing_since`` value.
"""

from __future__ import annotations

import pytest

from rebar.metrics.registry import (
    REGISTRY,
    MetricSpec,
    MetricValue,
    Unavailable,
    evaluate,
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
    return MetricSpec(**base)  # type: ignore[arg-type]


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
