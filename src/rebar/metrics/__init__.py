"""Declarative metric registry for rebar (ticket c085).

This package establishes a standalone, declarative registry of the metrics a
future ``rebar metrics`` command reads once. Each metric is described by a
:class:`~rebar.metrics.registry.MetricSpec` carrying its lens, provenance
labels (``source``/``confidence``), and a ``compute`` callable. Evaluating a
spec yields either a :class:`~rebar.metrics.registry.MetricValue` (real data,
labelled for downstream segregation) or an
:class:`~rebar.metrics.registry.Unavailable` (no data has accrued yet).

The package is additive/standalone — importing it changes no existing behavior.
"""

from __future__ import annotations

from rebar.metrics.registry import (
    REGISTRY,
    MetricSpec,
    MetricValue,
    Unavailable,
    evaluate,
    is_authoritative,
)

__all__ = [
    "REGISTRY",
    "MetricSpec",
    "MetricValue",
    "Unavailable",
    "evaluate",
    "is_authoritative",
]
