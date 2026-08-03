"""Bug-trends metrics lens (ticket b967).

Five dimensions over the bug population, split into flow (respect
--since/--until, filtered on close time) and stock (point-in-time snapshots
that deliberately ignore the CLI range):

FLOW dimensions:
- :func:`close_class_by_month` — close-class mix by UTC month.
- :func:`time_to_close_days` — p50/p90 of days from create to close.

STOCK dimensions (always point-in-time; ``since``/``until`` not accepted):
- :func:`open_bug_age_days` — age distribution of open/in-progress bugs.
- :func:`detected_by_distribution` — detection channel mix.
- :func:`caused_by_fan_in` — which upstream tickets caused the most bugs.

Design notes:
- The MISSING close-class cohort is a labeled key inside the result, never
  dropped or merged into a real class (era-skew guard: pre-convention closes
  stay visible as their own bucket).
- ``detected_by_distribution`` returns ``None`` when NO bug carries the field
  (the field ships in a sibling change; its absence degrades this dimension
  alone). When at least one bug has it, "unset" is included with its count.
- Stock dimensions are registered with adapters that never read ctx.since/
  ctx.until; this deviation from the CLI range is intentional and documented
  in the user guide.
- All five registry adapters share ONE store scan per CLI run via
  ``ctx.analysis_cache`` (:func:`_rows_for_ctx`); the public functions scan
  for themselves so direct library calls stay context-free.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any

import rebar
from rebar.config import tracker_dir
from rebar.metrics.event_metrics import _bounds, _in_range
from rebar.metrics.registry import REGISTRY, MetricSpec

_NS_PER_DAY: float = 86_400 * 1_000_000_000
_ACCRUING_SINCE = "first bug ticket"
_ROWS_CACHE_KEY = "bug_trends_rows"

# ---------------------------------------------------------------------------
# Shared scan: one row per bug ticket
# ---------------------------------------------------------------------------


def _ticket_dirs(repo_root: Any) -> list[str]:
    root = str(tracker_dir(repo_root))
    if not os.path.isdir(root):
        return []
    return [
        os.path.join(root, name)
        for name in os.listdir(root)
        if os.path.isdir(os.path.join(root, name))
    ]


def _last_close_ts(ticket_dir: str) -> int | None:
    """Return the ns timestamp of the last STATUS event with status=closed, or None."""
    names = [n for n in _listdir_safe(ticket_dir) if n.endswith("-STATUS.json")]
    names.sort()  # lexicographic = timestamp order (zero-padded 20-digit prefix)
    last: int | None = None
    for name in names:
        try:
            with open(os.path.join(ticket_dir, name), encoding="utf-8") as fh:
                ev = json.load(fh)
        except Exception:  # noqa: BLE001 - malformed event files never break the scan
            continue
        if (ev.get("data") or {}).get("status") == "closed":
            ts = ev.get("timestamp")
            if isinstance(ts, int):
                last = ts
    return last


def _listdir_safe(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def _bug_rows(repo_root: Any) -> list[dict[str, Any]]:
    """Walk tracker dirs and return one row dict per bug ticket.

    Each row carries: id, status, created_at, close_class, closed_at,
    detected_by, caused_by (list of target ids, unresolved targets included
    verbatim). Malformed ticket dirs are skipped, never raised.
    """
    rows: list[dict[str, Any]] = []
    for ticket_dir in _ticket_dirs(repo_root):
        try:
            state = rebar.reduce_ticket(ticket_dir, include_retired=False)
        except Exception:  # noqa: BLE001 - fault isolation per ticket dir
            continue
        if not state or state.get("ticket_type") != "bug":
            continue
        created_at = state.get("created_at")
        if not isinstance(created_at, int):
            continue
        status = state.get("status", "")
        rows.append(
            {
                "id": os.path.basename(ticket_dir),
                "status": status,
                "created_at": created_at,
                "close_class": state.get("close_class"),
                "closed_at": _last_close_ts(ticket_dir) if status == "closed" else None,
                "detected_by": state.get("detected_by"),
                "caused_by": [
                    d["target_id"]
                    for d in (state.get("deps") or [])
                    if d.get("relation") == "caused_by" and "target_id" in d
                ],
            }
        )
    return rows


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile: idx = ceil(q * n) - 1."""
    idx = max(0, math.ceil(q * len(sorted_vals)) - 1)
    return sorted_vals[idx]


# ---------------------------------------------------------------------------
# Pure derivations over rows (shared by public functions and adapters)
# ---------------------------------------------------------------------------


def _derive_close_class_by_month(
    rows: list[dict[str, Any]], since: str | None, until: str | None
) -> dict[str, dict[str, int]] | None:
    lo, hi = _bounds(since, until)
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        closed_at = row["closed_at"]
        if not isinstance(closed_at, int) or not _in_range(closed_at, lo, hi):
            continue
        month = datetime.fromtimestamp(closed_at / 1e9, tz=timezone.utc).strftime("%Y-%m")
        cls = row["close_class"] if row["close_class"] is not None else "MISSING"
        bucket = result.setdefault(month, {})
        bucket[cls] = bucket.get(cls, 0) + 1
    return result or None


def _derive_time_to_close_days(
    rows: list[dict[str, Any]], since: str | None, until: str | None
) -> dict[str, float | int] | None:
    lo, hi = _bounds(since, until)
    days = [
        (row["closed_at"] - row["created_at"]) / _NS_PER_DAY
        for row in rows
        if isinstance(row["closed_at"], int) and _in_range(row["closed_at"], lo, hi)
    ]
    if not days:
        return None
    days.sort()
    return {"p50": _percentile(days, 0.5), "p90": _percentile(days, 0.9), "count": len(days)}


def _derive_open_bug_age_days(
    rows: list[dict[str, Any]], now_ns: int | None
) -> dict[str, float | int] | None:
    if now_ns is None:
        now_ns = int(time.time() * 1e9)
    ages = [
        (now_ns - row["created_at"]) / _NS_PER_DAY
        for row in rows
        if row["status"] in ("open", "in_progress")
    ]
    if not ages:
        return None
    ages.sort()
    return {
        "p50": _percentile(ages, 0.5),
        "p90": _percentile(ages, 0.9),
        "max": ages[-1],
        "count": len(ages),
    }


def _derive_detected_by_distribution(rows: list[dict[str, Any]]) -> dict[str, int] | None:
    if not any(row["detected_by"] is not None for row in rows):
        return None
    counts: dict[str, int] = {}
    unset = 0
    for row in rows:
        val = row["detected_by"]
        if val is None:
            unset += 1
        else:
            counts[val] = counts.get(val, 0) + 1
    counts["unset"] = unset
    return counts


def _derive_caused_by_fan_in(rows: list[dict[str, Any]]) -> dict[str, int] | None:
    totals: dict[str, int] = {}
    for row in rows:
        for target in row["caused_by"]:
            totals[target] = totals.get(target, 0) + 1
    if not totals:
        return None
    return dict(sorted(totals.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------------------
# Public functions (context-free: each scans the store for itself)
# ---------------------------------------------------------------------------


def close_class_by_month(
    repo_root: Any, since: str | None = None, until: str | None = None
) -> dict[str, dict[str, int]] | None:
    """FLOW: bugs closed in [since, until] bucketed by UTC month then close_class.

    Closed bugs without a close_class count under the literal key "MISSING".
    Returns None when no closed bugs fall in range.
    """
    return _derive_close_class_by_month(_bug_rows(repo_root), since, until)


def time_to_close_days(
    repo_root: Any, since: str | None = None, until: str | None = None
) -> dict[str, float | int] | None:
    """FLOW: p50/p90/count of days from create to close over bugs closed in range."""
    return _derive_time_to_close_days(_bug_rows(repo_root), since, until)


def open_bug_age_days(repo_root: Any, now_ns: int | None = None) -> dict[str, float | int] | None:
    """STOCK: age distribution (p50/p90/max/count) of open/in_progress bugs.

    ``now_ns`` is an explicit clock seam; defaults to current time. Deliberately
    ignores since/until (point-in-time snapshot).
    """
    return _derive_open_bug_age_days(_bug_rows(repo_root), now_ns)


def detected_by_distribution(repo_root: Any) -> dict[str, int] | None:
    """STOCK: bug detection channel mix, with an "unset" labeled cohort.

    Returns None when NO bug carries detected_by (sibling-landing degradation).
    """
    return _derive_detected_by_distribution(_bug_rows(repo_root))


def caused_by_fan_in(repo_root: Any) -> dict[str, int] | None:
    """STOCK: per-target count of caused_by links across all bugs, descending.

    Targets are counted verbatim (they need not resolve to existing tickets).
    Returns None when no caused_by links exist.
    """
    return _derive_caused_by_fan_in(_bug_rows(repo_root))


# ---------------------------------------------------------------------------
# Registry integration — idempotent registration at import time
# ---------------------------------------------------------------------------


def _rows_for_ctx(ctx: Any) -> list[dict[str, Any]] | None:
    """Rows for a CLI evaluation context, scanned once per run.

    Uses ``ctx.analysis_cache`` (when the context carries the dict) so the five
    bug_trends specs share one store scan. Returns None when the context has no
    repo_root — evaluate() then renders the metric unavailable.
    """
    repo_root = getattr(ctx, "repo_root", None)
    if repo_root is None:
        return None
    cache = getattr(ctx, "analysis_cache", None)
    if not isinstance(cache, dict):
        return _bug_rows(repo_root)
    rows = cache.get(_ROWS_CACHE_KEY)
    if rows is None:
        rows = _bug_rows(repo_root)
        cache[_ROWS_CACHE_KEY] = rows
    return rows


def _compute_close_class(ctx: Any) -> dict[str, dict[str, int]] | None:
    rows = _rows_for_ctx(ctx)
    if rows is None:
        return None
    return _derive_close_class_by_month(
        rows, getattr(ctx, "since", None), getattr(ctx, "until", None)
    )


def _compute_time_to_close(ctx: Any) -> dict[str, float | int] | None:
    rows = _rows_for_ctx(ctx)
    if rows is None:
        return None
    return _derive_time_to_close_days(
        rows, getattr(ctx, "since", None), getattr(ctx, "until", None)
    )


def _compute_open_age(ctx: Any) -> dict[str, float | int] | None:
    rows = _rows_for_ctx(ctx)
    if rows is None:
        return None
    return _derive_open_bug_age_days(rows, None)


def _compute_detected_by(ctx: Any) -> dict[str, int] | None:
    rows = _rows_for_ctx(ctx)
    if rows is None:
        return None
    return _derive_detected_by_distribution(rows)


def _compute_caused_by(ctx: Any) -> dict[str, int] | None:
    rows = _rows_for_ctx(ctx)
    if rows is None:
        return None
    return _derive_caused_by_fan_in(rows)


def register() -> None:
    """Append the five bug_trends specs to REGISTRY (idempotent by id)."""
    computes = {
        "bug_close_class_by_month": _compute_close_class,
        "bug_time_to_close_days": _compute_time_to_close,
        "bug_open_age_days": _compute_open_age,
        "bug_detected_by_distribution": _compute_detected_by,
        "bug_caused_by_fan_in": _compute_caused_by,
    }
    existing = {spec.id for spec in REGISTRY}
    for metric_id, compute in computes.items():
        if metric_id in existing:
            continue
        REGISTRY.append(
            MetricSpec(
                id=metric_id,
                lens="bug_trends",
                source="structural",
                confidence="high",
                compute=compute,
                accruing_since=_ACCRUING_SINCE,
            )
        )


register()
