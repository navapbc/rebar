"""Small, dependency-free descriptive + rank statistics for the backtest.

Deliberately stdlib-only: the backtest must run in a checkout with no scientific stack
installed, on any CI provider or none. Nothing here touches the shipped size predicate —
that is imported by the caller and never re-derived.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

QUANTILES = (0.5, 0.75, 0.9, 0.95, 0.99)


def quantile(values: Sequence[float], p: float) -> float:
    """Linearly interpolated quantile over ``values`` (unsorted input is fine)."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = (len(ordered) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo), 1)


def describe(values: Sequence[float]) -> dict[str, float]:
    """n / mean / p50..p99 / max for one distribution."""
    if not values:
        return {"n": 0}
    row: dict[str, float] = {
        "n": len(values),
        "mean": round(statistics.mean(values), 1),
    }
    for p in QUANTILES:
        row[f"p{int(p * 100)}"] = quantile(values, p)
    row["max"] = max(values)
    return row


def format_describe(name: str, row: dict[str, float]) -> str:
    """One aligned line per distribution, matching the report's Distributions block."""
    if not row.get("n"):
        return f"{name:<18} n=0"
    cells = " ".join(f"p{int(p * 100)}={_num(row[f'p{int(p * 100)}']):<7}" for p in QUANTILES)
    return f"{name:<18} n={row['n']:<6} mean={row['mean']:<8} {cells} max={_num(row['max'])}"


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _ranks(values: Sequence[float]) -> list[float]:
    """Average ranks, ties shared — the rank transform Spearman is defined over."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranked = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranked[order[k]] = average
        i = j + 1
    return ranked


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else float("nan")


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    return pearson(_ranks(xs), _ranks(ys))


def two_sided_p(rho: float, n: int) -> float:
    """Two-sided p for a rank correlation, normal approximation to the t statistic."""
    if n < 4 or math.isnan(rho) or abs(rho) >= 1:
        return float("nan")
    t = rho * math.sqrt((n - 2) / (1 - rho * rho))
    return math.erfc(abs(t) / math.sqrt(2))


def correlate(xs: Sequence[float], ys: Sequence[float]) -> dict[str, float]:
    """``{n, rho, p}`` for one pairing; ``rho``/``p`` are NaN when n is too small."""
    rho = spearman(xs, ys) if len(xs) >= 2 else float("nan")
    return {
        "n": len(xs),
        "rho": rho if math.isnan(rho) else round(rho, 3),
        "p": two_sided_p(rho, len(xs)),
    }
