"""Deterministic stratified sampling over the plan-review corpus for Tier-1 (ticket
presolar-finable-binturong / 53ab-bdf6-de1c-4bb1).

Strata: verdict, leaf/container (does the row have children), a finding-count bucket,
the store, and ``impact_model_version``. Sampling is a PURE function over a list of
row dicts that already carry the stratification fields (``finding_count``,
``impact_model_version``, ``children``, ``verdict``, ``store``) -- the I/O that builds
that pool lives in :mod:`tier1`, keeping this module trivially unit-testable.
"""

from __future__ import annotations

import random
from typing import Any

_FINDING_COUNT_BUCKETS: tuple[tuple[int, str], ...] = (
    (0, "0"),
    (3, "1-3"),
    (10, "4-10"),
)
_FINDING_COUNT_OVERFLOW = "11+"


def finding_count_bucket(n: int) -> str:
    """The bucket label for a finding count: ``"0"``, ``"1-3"``, ``"4-10"``, or ``"11+"``."""
    for ceiling, label in _FINDING_COUNT_BUCKETS:
        if n <= ceiling:
            return label
    return _FINDING_COUNT_OVERFLOW


def stratum_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """The stratification key for one sampling-pool row: ``(verdict, leaf_or_container,
    finding_count_bucket, store, impact_model_version)``."""
    kind = "container" if row.get("children") else "leaf"
    return (
        row.get("verdict"),
        kind,
        finding_count_bucket(int(row.get("finding_count") or 0)),
        row.get("store"),
        row.get("impact_model_version"),
    )


def _row_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("ticket_id") or ""), str(row.get("review_event_uuid") or ""))


def stratified_sample(rows: list[dict[str, Any]], *, n: int, seed: int) -> list[dict[str, Any]]:
    """A deterministic stratified sample of up to ``n`` rows from ``rows``.

    Rows are grouped by :func:`stratum_key`; within each stratum they are sorted by
    ``(ticket_id, review_event_uuid)`` (so the pre-shuffle order never depends on the
    caller's input order) and then shuffled with a ``random.Random(seed)`` private to
    that stratum. The sample is built by round-robining across strata (ordered by their
    key's ``repr``, also seed-independent) so no single large stratum crowds out the
    others, and the SAME ``seed`` over the SAME ``rows`` always yields the SAME sample in
    the SAME order.
    """
    if n <= 0 or not rows:
        return []

    strata: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        strata.setdefault(stratum_key(row), []).append(row)

    for key, bucket in strata.items():
        bucket.sort(key=_row_sort_key)
        random.Random(f"{seed}:{key!r}").shuffle(bucket)

    ordered_keys = sorted(strata, key=repr)
    cursors = dict.fromkeys(ordered_keys, 0)
    result: list[dict[str, Any]] = []
    while len(result) < n:
        progressed = False
        for key in ordered_keys:
            if len(result) >= n:
                break
            bucket = strata[key]
            cursor = cursors[key]
            if cursor < len(bucket):
                result.append(bucket[cursor])
                cursors[key] = cursor + 1
                progressed = True
        if not progressed:
            break
    return result
