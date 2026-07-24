"""Declarative metric registry and value/unavailable dispatch (ticket c085).

A :class:`MetricSpec` declares a single metric: its ``id`` and ``lens``, the
provenance labels (``source``/``confidence``) that will ride with any value it
produces, a ``compute`` callable, and ``accruing_since`` — the ISO-8601
timestamp at which the underlying signal began accruing.

:func:`evaluate` runs a spec's ``compute``. When ``compute`` returns real data
the result is a :class:`MetricValue` carrying the value alongside its ``source``
and ``confidence`` labels, so downstream code can segregate authoritative
signals from classified/backfilled ones. When ``compute`` returns ``None`` — no
data has accrued yet — the result is an :class:`Unavailable` carrying a
human-readable ``reason`` and the ``accruing_since`` timestamp.

:data:`REGISTRY` is the declarative list of specs with unique ids. The current
seed metrics have ``compute`` callables that return ``None`` (they will report
``Unavailable`` until their data sources land in later tickets).

Provenance vocabularies are CLOSED:

- ``source`` ∈ ``{structural, git, sidecar, snapshot, backfill_classified}``
- ``confidence`` ∈ ``{high, classified}``

:func:`is_authoritative` returns ``True`` only for the authoritative structural
sources (``structural``, ``git``, ``sidecar``, ``snapshot``); every other
value — including ``backfill_classified`` and any unknown/garbage/empty
string — is ``False``, so an unclassified source can never leak into an
authoritative series.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Closed provenance vocabularies.
SOURCES: frozenset[str] = frozenset(
    {"structural", "git", "sidecar", "snapshot", "backfill_classified"}
)
CONFIDENCES: frozenset[str] = frozenset({"high", "classified"})

# The authoritative sources: signals we trust into an authoritative series.
# ``backfill_classified`` is deliberately excluded.
AUTHORITATIVE_SOURCES: frozenset[str] = frozenset({"structural", "git", "sidecar", "snapshot"})


@dataclass(frozen=True)
class MetricSpec:
    """Declarative description of a single metric.

    ``compute`` is a ``Callable[[context], value | None]``: it returns the
    metric value, or ``None`` when no data has accrued yet. ``accruing_since``
    is an ISO-8601 timestamp string marking when the signal began accruing.
    """

    id: str
    lens: str
    source: str
    confidence: str
    compute: Callable[[Any], Any]
    accruing_since: str


@dataclass(frozen=True)
class MetricValue:
    """A computed metric value with its provenance labels.

    The ``source`` and ``confidence`` labels ride with the value so downstream
    code can segregate authoritative signals from classified/backfilled ones.
    ``metric_id`` is carried for convenience.
    """

    value: Any
    source: str
    confidence: str
    metric_id: str | None = None


@dataclass(frozen=True)
class Unavailable:
    """Sentinel: a metric has no data accrued yet.

    ``reason`` is a non-empty human-readable explanation; ``accruing_since`` is
    the ISO-8601 timestamp at which the signal began accruing.
    """

    reason: str
    accruing_since: str


def is_authoritative(source: str) -> bool:
    """Return ``True`` iff ``source`` is an authoritative structural source.

    Implemented as membership in :data:`AUTHORITATIVE_SOURCES`, so
    ``backfill_classified`` and any unknown/garbage/empty string are ``False`` —
    an unclassified source never leaks into an authoritative series.
    """

    return source in AUTHORITATIVE_SOURCES


def evaluate(spec: MetricSpec, context: Any = None) -> MetricValue | Unavailable:
    """Run ``spec.compute(context)`` and dispatch to value or unavailable.

    Real data yields a :class:`MetricValue` carrying the spec's ``source`` and
    ``confidence`` labels (and its id); a ``None`` return yields an
    :class:`Unavailable` carrying a reason and the spec's ``accruing_since``.
    """

    result = spec.compute(context)
    if result is None:
        return Unavailable(
            reason=f"no data has accrued for '{spec.id}' yet",
            accruing_since=spec.accruing_since,
        )
    return MetricValue(
        value=result,
        source=spec.source,
        confidence=spec.confidence,
        metric_id=spec.id,
    )


def _no_data_yet(context: Any) -> None:
    """Seed ``compute`` for metrics whose data source has not landed yet."""

    return None


# The declarative registry. Ids MUST be unique. Seed metrics report
# ``Unavailable`` until their data sources land in later tickets.
REGISTRY: list[MetricSpec] = [
    MetricSpec(
        id="module_size_trend",
        lens="code_health",
        source="structural",
        confidence="high",
        compute=_no_data_yet,
        accruing_since="2026-07-18T00:00:00+00:00",
    ),
    MetricSpec(
        id="commit_cadence_trend",
        lens="delivery",
        source="git",
        confidence="high",
        compute=_no_data_yet,
        accruing_since="2026-07-18T00:00:00+00:00",
    ),
]
